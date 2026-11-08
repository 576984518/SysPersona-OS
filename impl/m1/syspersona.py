#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SysPersona M1 — 最小闭环参考实现。

只依赖 Python 标准库（sqlite3 / json / argparse），用于验证四件事：
  1. 人格文件能在启动时被加载并注入提示；
  2. 对话能落库为情景记忆；
  3. 「睡眠」能把情景压缩成事实与画像；
  4. 换一个模型后重新加载，它还记得之前的事。

模型默认是 mock（确定性回复，用于验证链路），可选 ollama。

用法：
  python syspersona.py --home ./data init --persona ../../examples/persona.m1.json
  python syspersona.py --home ./data chat --say "我叫黄，我在做一个 NAS 调优项目"
  python syspersona.py --home ./data sleep
  python syspersona.py --home ./data switch-model --model mock-b
  python syspersona.py --home ./data chat --say "我叫什么？我在做什么？"
  python syspersona.py --home ./data status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_NAME = "memory.db"
PERSONA_NAME = "persona.json"
CONFIG_NAME = "config.json"
PERSONA_DIR = "persona"
KERNEL_FILE = "kernel.json"
TRAITS_FILE = "traits.json"
STATE_FILE = "state.json"

# 硬件档位 → 阈值预设：所有阈值都是硬件参数的函数
PROFILES = {
    "low": {"context_budget": 8, "hot_days": 2, "warm_days": 60, "prune_days": 180},
    "mid": {"context_budget": 12, "hot_days": 3, "warm_days": 90, "prune_days": 365},
    "high": {"context_budget": 24, "hot_days": 7, "warm_days": 180, "prune_days": 730},
}


def apply_profile(persona: dict, name: str) -> None:
    preset = PROFILES[name]
    memory = persona.setdefault("memory", {})
    memory["context_budget"] = preset["context_budget"]
    retention = memory.setdefault("retention", {})
    retention.update(
        {
            "hot_days": preset["hot_days"],
            "warm_days": preset["warm_days"],
            "prune_days": preset["prune_days"],
        }
    )
    memory["profile"] = name

SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_versions (
    id INTEGER PRIMARY KEY,
    persona_uuid TEXT NOT NULL,
    version TEXT NOT NULL,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    occurred_at TEXT NOT NULL,
    consolidated INTEGER NOT NULL DEFAULT 0,
    memory_type TEXT NOT NULL DEFAULT 'episode'
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.8,
    source_episode INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(subject, predicate, object)
);

CREATE TABLE IF NOT EXISTS persona_traits (
    id INTEGER PRIMARY KEY,
    trait_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    polarity TEXT,
    weight REAL,
    baseline REAL,
    evidence INTEGER NOT NULL DEFAULT 0,
    is_locked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_drift_log (
    id INTEGER PRIMARY KEY,
    trait_id TEXT NOT NULL,
    old_weight REAL,
    new_weight REAL,
    delta REAL,
    evidence_count INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flip_requests (
    id INTEGER PRIMARY KEY,
    trait_id TEXT NOT NULL,
    from_polarity TEXT NOT NULL,
    to_polarity TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS runtime_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mood TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    topics TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    attention REAL NOT NULL DEFAULT 0,
    attention_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(name, kind)
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 1,
    priority REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(src, dst, relation)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    tag TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    summary TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return current_dt().isoformat(timespec="seconds")


_VIRTUAL_NOW = None


def current_dt():
    """虚拟时钟：模拟时由 simulate 推进，正常运行时等于真实时间。"""
    if _VIRTUAL_NOW is not None:
        return _VIRTUAL_NOW
    return datetime.now(timezone.utc).astimezone()


def advance(delta) -> None:
    global _VIRTUAL_NOW
    _VIRTUAL_NOW = current_dt() + delta


def _layers_dir(home: Path) -> Path:
    return home / PERSONA_DIR


def split_persona_layers(persona: dict, home: Path) -> None:
    """把单文件人格拆成三层：kernel / traits / state。"""
    directory = _layers_dir(home)
    directory.mkdir(parents=True, exist_ok=True)

    kernel = {key: persona[key] for key in ("schema", "id", "version") if key in persona}
    kernel["kernel"] = persona.get("kernel", {})
    traits = {key: persona[key] for key in ("traits", "memory", "evolution", "meta") if key in persona}
    state = {
        "mood": {"愉悦": 0.0, "烦躁": 0.0, "专注": 0.0, "疲惫": 0.0},
        "updated_at": now(),
    }

    (directory / KERNEL_FILE).write_text(
        json.dumps(kernel, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / TRAITS_FILE).write_text(
        json.dumps(traits, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_persona_layers(home: Path) -> dict:
    """合并 kernel ← traits；没有目录时回退到单文件。"""
    directory = _layers_dir(home)
    if not directory.is_dir():
        return json.loads((home / PERSONA_NAME).read_text(encoding="utf-8-sig"))

    kernel = json.loads((directory / KERNEL_FILE).read_text(encoding="utf-8-sig"))
    traits = json.loads((directory / TRAITS_FILE).read_text(encoding="utf-8-sig"))
    merged = dict(traits)
    for key in ("schema", "id", "version"):
        if key in kernel:
            merged[key] = kernel[key]
    merged["kernel"] = kernel.get("kernel", {})
    return merged


_IN_SLEEP = False


def persist_traits_file(store: "Store", home: Path) -> None:
    """睡眠结算后把权重写回 traits.json —— 文件是真相，且只有睡眠能写。"""
    if not _IN_SLEEP:
        print("  [权限拒绝] traits 只能在睡眠结算时写入")
        return
    directory = _layers_dir(home)
    if not directory.is_dir():
        return
    path = directory / TRAITS_FILE
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    weights = {row["trait_id"]: round(float(row["weight"]), 1) for row in store.traits()}
    for pref in data.get("traits", {}).get("preferences", []):
        if pref.get("id") in weights:
            pref["weight"] = weights[pref["id"]]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Store:
    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(home / DB_NAME)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(persona_traits)")}
        if "stress_offset" not in cols:
            self.db.execute(
                "ALTER TABLE persona_traits ADD COLUMN stress_offset REAL NOT NULL DEFAULT 0"
            )
        if "stress_at" not in cols:
            self.db.execute("ALTER TABLE persona_traits ADD COLUMN stress_at TEXT")
        if "last_support_at" not in cols:
            self.db.execute("ALTER TABLE persona_traits ADD COLUMN last_support_at TEXT")
        if "last_discount_at" not in cols:
            self.db.execute("ALTER TABLE persona_traits ADD COLUMN last_discount_at TEXT")
        if "first_seen" not in cols:
            self.db.execute("ALTER TABLE persona_traits ADD COLUMN first_seen TEXT")
        ep_cols = {row[1] for row in self.db.execute("PRAGMA table_info(episodes)")}
        if "archived" not in ep_cols:
            self.db.execute("ALTER TABLE episodes ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "access_count" not in ep_cols:
            self.db.execute("ALTER TABLE episodes ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
        if "last_access" not in ep_cols:
            self.db.execute("ALTER TABLE episodes ADD COLUMN last_access TEXT")
        if "memory_type" not in ep_cols:
            self.db.execute(
                "ALTER TABLE episodes ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episode'"
            )
        self.db.commit()

    # ---------- persona ----------
    def load_persona(self) -> dict:
        return load_persona_layers(self.home)

    def save_persona(self, persona: dict) -> None:
        directory = _layers_dir(self.home)
        if directory.is_dir():
            traits = {
                key: persona[key]
                for key in ("traits", "memory", "evolution", "meta")
                if key in persona
            }
            (directory / TRAITS_FILE).write_text(
                json.dumps(traits, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return
        (self.home / PERSONA_NAME).write_text(
            json.dumps(persona, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record_persona(self, persona: dict) -> None:
        import hashlib

        body = json.dumps(persona, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        self.db.execute(
            "INSERT INTO persona_versions (persona_uuid, version, body, content_hash, is_active, created_at)"
            " VALUES (?,?,?,?,1,?)",
            (persona["id"], persona["version"], body, digest, now()),
        )
        self.db.commit()

    # ---------- config ----------
    def load_config(self) -> dict:
        p = self.home / CONFIG_NAME
        if not p.exists():
            return {"model": "mock-a", "session_id": str(uuid.uuid4())}
        return json.loads(p.read_text(encoding="utf-8-sig"))

    def save_config(self, cfg: dict) -> None:
        (self.home / CONFIG_NAME).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- episodes ----------
    def add_episode(self, session_id: str, role: str, content: str,
                    importance: float = 0.5, memory_type: str = "episode") -> int:
        cur = self.db.execute(
            "INSERT INTO episodes (session_id, role, content, importance, occurred_at, memory_type)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, role, content, importance, now(), memory_type),
        )
        self.db.commit()
        return int(cur.lastrowid)

    # ---------- 能力面与拒绝（PERSONA_SPEC §2.1 / §5）----------
    def permissions(self) -> dict:
        k = (self.load_persona().get("kernel") or {})
        p = k.get("permissions") or {}
        return {
            "default": p.get("default", "deny"),
            "allow": list(p.get("allow") or []),
            "ask": list(p.get("ask") or []),
            "deny": list(p.get("deny") or []),
        }

    def decide_capability(self, capability: str) -> str:
        """返回 allow / ask / deny。deny 优先，其次 ask，再次 allow，都没有走 default。"""
        p = self.permissions()

        def hit(items: list) -> bool:
            for c in items:
                if c == "*" or capability == c or capability.startswith(c + ":"):
                    return True
            return False

        if hit(p["deny"]):
            return "deny"
        if hit(p["ask"]):
            return "ask"
        if hit(p["allow"]):
            return "allow"
        return p["default"] if p["default"] in ("allow", "ask", "deny") else "deny"

    def record_refusal(self, clause: str, instruction: str,
                       session_id: str = "runtime") -> dict:
        """拒绝时留痕：追加 persona_conflict 记忆 + audit；绝不覆盖旧记忆。"""
        at = now()
        payload = {
            "refused": True,
            "clause": clause,
            "instruction": instruction[:200],
            "decided": "refused",
            "at": at,
            "summary": "因 %s 拒绝了 %s" % (clause, instruction[:40].replace("\n", " ")),
        }
        content = json.dumps(payload, ensure_ascii=False)
        eid = self.add_episode(session_id, "system", content, importance=0.9,
                               memory_type="persona_conflict")
        self.audit("refuse", content)
        return dict(payload, episode_id=eid)

    def recent_episodes(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def all_episodes(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM episodes WHERE archived = 0 ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def unconsolidated(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM episodes WHERE consolidated = 0 ORDER BY id"
        ).fetchall()

    # ---------- facts ----------
    def add_fact(self, subject: str, predicate: str, obj: str, source: int | None, confidence: float = 0.8) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO facts (subject, predicate, object, confidence, source_episode, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (subject, predicate, obj, confidence, source, now()),
        )
        self.db.commit()

    def search_facts(self, keywords: list[str], predicates: list[str] | None = None, limit: int = 8) -> list[sqlite3.Row]:
        if predicates:
            marks = ",".join("?" * len(predicates))
            return self.db.execute(
                f"SELECT * FROM facts WHERE predicate IN ({marks})"
                " ORDER BY confidence DESC, id DESC LIMIT ?",
                (*predicates, limit),
            ).fetchall()
        if not keywords:
            return self.db.execute(
                "SELECT * FROM facts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        where = " OR ".join(["subject LIKE ? OR predicate LIKE ? OR object LIKE ?"] * len(keywords))
        params: list[str] = []
        for k in keywords:
            like = f"%{k}%"
            params.extend([like, like, like])
        return self.db.execute(
            f"SELECT * FROM facts WHERE {where} ORDER BY confidence DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()

    # ---------- traits ----------
    def sync_traits(self, persona: dict) -> None:
        for p in persona.get("traits", {}).get("preferences", []):
            weight = float(p.get("weight", 50))
            self.db.execute(
                "INSERT OR IGNORE INTO persona_traits"
                " (trait_id, kind, target, polarity, weight, baseline, evidence, is_locked, updated_at, first_seen)"
                " VALUES (?,?,?,?,?,?,0,?,?,?)",
                (
                    p["id"],
                    "preference",
                    p["target"],
                    p.get("polarity", "like"),
                    weight,
                    float(p.get("baseline", weight)),
                    int(bool(p.get("locked", False))),
                    now(),
                    now(),
                ),
            )
        self.db.commit()

    def traits(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM persona_traits ORDER BY id"
        ).fetchall()

    def update_trait(self, trait_id: str, new_weight: float, delta: float, count: int, reason: str) -> None:
        self.db.execute(
            "UPDATE persona_traits SET weight=?, evidence=evidence+?, updated_at=?,"
            " last_support_at=CASE WHEN ?>0 THEN ? ELSE last_support_at END"
            " WHERE trait_id=?",
            (new_weight, count, now(), count, now(), trait_id),
        )
        self.db.execute(
            "INSERT INTO persona_drift_log (trait_id, old_weight, new_weight, delta, evidence_count, reason, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (trait_id, new_weight - delta, new_weight, delta, count, reason, now()),
        )
        self.db.commit()

    def weekly_drift(self, trait_id: str, days: int = 7) -> tuple[float, float]:
        """返回 (上行累计, 下行累计)，两个方向分别计算额度。"""
        rows = self.db.execute(
            "SELECT delta, created_at FROM persona_drift_log"
            " WHERE trait_id=? AND reason NOT LIKE '旧标签再验证%'"
            " ORDER BY id DESC",
            (trait_id,),
        ).fetchall()
        cutoff = current_dt().timestamp() - days * 86400
        up = 0.0
        down = 0.0
        for row in rows:
            try:
                ts = datetime.fromisoformat(row["created_at"]).timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                continue
            delta = float(row["delta"])
            if delta > 0:
                up += delta
            else:
                down += -delta
        return up, down

    # ---------- flip requests ----------
    def has_pending_flip(self, trait_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM flip_requests WHERE trait_id=? AND status='pending' LIMIT 1",
            (trait_id,),
        ).fetchone()
        return row is not None

    def create_flip_request(self, trait_id: str, from_p: str, to_p: str, count: int) -> int:
        cur = self.db.execute(
            "INSERT INTO flip_requests (trait_id, from_polarity, to_polarity, evidence_count, status, created_at)"
            " VALUES (?,?,?,?,'pending',?)",
            (trait_id, from_p, to_p, count, now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def pending_flips(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM flip_requests WHERE status='pending' ORDER BY id"
        ).fetchall()

    def get_flip(self, flip_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM flip_requests WHERE id=?", (flip_id,)).fetchone()

    def decide_flip(self, flip_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE flip_requests SET status=?, decided_at=? WHERE id=?",
            (status, now(), flip_id),
        )
        self.db.commit()

    def set_polarity(self, trait_id: str, polarity: str, baseline: float) -> None:
        self.db.execute(
            "UPDATE persona_traits SET polarity=?, baseline=?, updated_at=? WHERE trait_id=?",
            (polarity, baseline, now(), trait_id),
        )
        self.db.commit()

    # ---------- runtime state ----------
    def load_state(self) -> tuple[dict, str]:
        state_path = _layers_dir(self.home) / STATE_FILE
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8-sig"))
            return data.get("mood", {}), data.get("updated_at", now())
        row = self.db.execute(
            "SELECT mood, updated_at FROM runtime_state WHERE id=1"
        ).fetchone()
        if not row:
            return {"愉悦": 0.0, "烦躁": 0.0, "专注": 0.0, "疲惫": 0.0}, now()
        return json.loads(row["mood"]), row["updated_at"]

    def save_state(self, mood: dict) -> None:
        directory = _layers_dir(self.home)
        if directory.is_dir():
            (directory / STATE_FILE).write_text(
                json.dumps({"mood": mood, "updated_at": now()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        self.db.execute(
            "INSERT INTO runtime_state (id, mood, updated_at) VALUES (1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET mood=excluded.mood, updated_at=excluded.updated_at",
            (json.dumps(mood, ensure_ascii=False), now()),
        )
        self.db.commit()

    # ---------- stress offset ----------
    def bump_stress(self, trait_id: str, delta: float) -> None:
        row = self.db.execute(
            "SELECT stress_offset FROM persona_traits WHERE trait_id=?", (trait_id,)
        ).fetchone()
        current = float(row["stress_offset"] or 0.0) if row else 0.0
        new = max(-2.0, min(2.0, current + delta))
        self.db.execute(
            "UPDATE persona_traits SET stress_offset=?, stress_at=? WHERE trait_id=?",
            (new, now(), trait_id),
        )
        self.db.commit()

    def clear_stress(self, trait_id: str) -> None:
        self.db.execute(
            "UPDATE persona_traits SET stress_offset=0, stress_at=NULL WHERE trait_id=?",
            (trait_id,),
        )
        self.db.commit()

    # ---------- resume ----------
    def save_resume(self, topics: list[str]) -> None:
        self.db.execute(
            "INSERT INTO resume_state (id, topics, updated_at) VALUES (1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET topics=excluded.topics, updated_at=excluded.updated_at",
            (json.dumps(topics, ensure_ascii=False), now()),
        )
        self.db.commit()

    def load_resume(self) -> tuple[list[str], str | None]:
        row = self.db.execute(
            "SELECT topics, updated_at FROM resume_state WHERE id=1"
        ).fetchone()
        if not row:
            return [], None
        return json.loads(row["topics"]), row["updated_at"]

    def archived_count(self) -> int:
        return int(
            self.db.execute("SELECT COUNT(*) FROM episodes WHERE archived=1").fetchone()[0]
        )

    # ---------- profile ----------
    def set_profile(self, summary: str) -> None:
        self.db.execute(
            "INSERT INTO profile (id, summary, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
            (summary, now()),
        )
        self.db.commit()

    def get_profile(self) -> str:
        row = self.db.execute("SELECT summary FROM profile WHERE id = 1").fetchone()
        return row["summary"] if row else ""

    # ---------- misc ----------
    def audit(self, action: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO audit_log (action, detail, created_at) VALUES (?,?,?)",
            (action, detail, now()),
        )
        self.db.commit()

    def mark_consolidated(self, ids: list[int]) -> None:
        self.db.executemany("UPDATE episodes SET consolidated = 1 WHERE id = ?", [(i,) for i in ids])
        self.db.commit()

    def counts(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "episodes": q("SELECT COUNT(*) FROM episodes"),
            "facts": q("SELECT COUNT(*) FROM facts"),
            "unconsolidated": q("SELECT COUNT(*) FROM episodes WHERE consolidated = 0"),
        }


# ---------------- models ----------------

class MockModel:
    """确定性模型：用于验证链路，不追求智能。"""

    def __init__(self, name: str = "mock-a"):
        self.name = name

    def reply(self, persona: dict, facts: list[str], memories: list[str], message: str, mood: dict) -> str:
        who = persona["kernel"]["identity"]["name"]
        head = f"（{self.name} · {who}）" + mood_prefix(mood)
        parts: list[str] = []
        if facts:
            parts.append("我记得：" + "；".join(facts))
        if memories:
            parts.append("你提过：「" + "」「".join(memories) + "」")
        if parts:
            return head + " ".join(parts)
        return head + "我还不了解这件事，先记下来。"


class OllamaModel:
    def __init__(self, name: str = "qwen2.5:7b", host: str = "http://127.0.0.1:11434"):
        self.name = name
        self.host = host.rstrip("/")

    def reply(self, persona: dict, facts: list[str], memories: list[str], message: str, mood: dict) -> str:
        k = persona["kernel"]
        context = [f"当前情绪：{mood_text(mood)}"]
        if facts:
            context.append("已知事实：" + "；".join(facts))
        if memories:
            context.append("相关记忆：" + "；".join(memories))
        system = (
            f"你是{k['identity']['name']}。语气：{k['voice_baseline']['tone']}。"
            f"你的价值观：{'、'.join(k['values'])}。"
            + ("交互：先回一句「收到」再开工，后台任务不要阻塞对话。"
               if (k.get("interaction") or {}).get("ack_first") else "")
            + ("边界：删记忆、改内核、换底座必须先问人；与内核冲突时要拒绝并记下冲突。"
               if (k.get("constitution") or {}).get("right_to_refuse") else "")
            + ("\n".join(context) if context else "（暂无记忆）")
            + "\n回答简短，先给结论。"
        )
        payload = json.dumps(
            {"model": self.name, "prompt": f"{system}\n\n用户：{message}\n你：", "stream": False}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()
        except (urllib.error.URLError, TimeoutError) as exc:
            return f"[ollama 不可用: {exc}] 先确认本地已启动 ollama。"


def make_model(spec: str):
    if spec.startswith("ollama:"):
        return OllamaModel(spec.split(":", 1)[1])
    return MockModel(spec)


# ---------------- runtime state ----------------

CUES = [
    (re.compile(r"厉害|不错|谢谢|很好|棒|辛苦"), "愉悦", 6.0),
    (re.compile(r"不对|错了|又错|怎么搞|搞错|失望|差劲|乱来"), "烦躁", 8.0),
    (re.compile(r"报错|失败|崩溃|卡住|bug"), "专注", 6.0),
    (re.compile(r"累|困|睡了|休息"), "疲惫", 6.0),
]


def tick_mood(mood: dict, updated_at: str, text: str, tau_min: float = 30.0) -> dict:
    """情绪按分钟衰减，再吸收本轮刺激。只影响当次会话，不写 traits。"""
    import math

    try:
        elapsed = (
            current_dt() - datetime.fromisoformat(updated_at)
        ).total_seconds() / 60.0
    except ValueError:
        elapsed = 0.0
    decay = math.exp(-max(0.0, elapsed) / tau_min)
    for key in list(mood):
        mood[key] = round(float(mood[key]) * decay, 1)
    for pattern, key, delta in CUES:
        if pattern.search(text):
            mood[key] = min(30.0, round(mood.get(key, 0.0) + delta, 1))
    return mood


def mood_text(mood: dict) -> str:
    items = [f"{k} +{v:g}" for k, v in mood.items() if v >= 1]
    return "、".join(items) if items else "平稳"


def mood_prefix(mood: dict) -> str:
    angry = mood.get("烦躁", 0)
    glad = mood.get("愉悦", 0)
    if angry >= 10 and angry > glad:
        return "（有点烦躁，先说结论）"
    if glad >= 10 and glad > angry:
        return "（心情不错）"
    return ""


def stress_value(trait, tau_hours: float = 6.0) -> float:
    """应激偏移的当前残值：offset × exp(-小时 / 6)。"""
    import math

    offset = float(trait["stress_offset"] or 0.0)
    at = trait["stress_at"]
    if not at or offset == 0.0:
        return 0.0
    try:
        hours = (
            current_dt() - datetime.fromisoformat(at)
        ).total_seconds() / 3600.0
    except ValueError:
        hours = 0.0
    return offset * math.exp(-max(0.0, hours) / tau_hours)


def settle_stress(store: "Store", persona: dict, skip: set[str] | None = None) -> list[str]:
    """睡眠时结算：残值够大就并入权重，已经衰减到无意义就丢弃。

    同一轮里已经被证据调整过的偏好，跳过应激结算——避免一天内双重计入。
    """
    ev = persona.get("evolution", {}) or {}
    max_step = float(ev.get("max_step", 2))
    weekly_cap = float(ev.get("weekly_cap", 5))
    skip = skip or set()

    changes: list[str] = []
    for trait in store.traits():
        if trait["is_locked"]:
            continue
        if trait["trait_id"] in skip:
            had_offset = float(trait["stress_offset"] or 0.0) != 0.0
            store.clear_stress(trait["trait_id"])
            if had_offset:
                changes.append(f"{trait['trait_id']} 已按证据结算，本轮应激丢弃")
            continue
        value = stress_value(trait)
        if abs(value) < 0.3:
            if float(trait["stress_offset"] or 0.0) != 0.0:
                store.clear_stress(trait["trait_id"])
                changes.append(f"{trait['trait_id']} 应激偏移已衰减，丢弃")
            continue
        up_used, down_used = store.weekly_drift(trait["trait_id"])
        remaining = weekly_cap - (up_used if value > 0 else down_used)
        if remaining <= 0:
            store.clear_stress(trait["trait_id"])
            changes.append(f"{trait['trait_id']} 额度不足，应激偏移丢弃")
            continue
        delta = max(-remaining, min(remaining, value, max_step))
        weight = float(trait["weight"])
        new_weight = max(0.0, min(100.0, weight + delta))
        store.update_trait(
            trait["trait_id"], new_weight, new_weight - weight, 0, f"应激偏移并入（{value:+.1f}）"
        )
        store.clear_stress(trait["trait_id"])
        changes.append(
            f"{trait['trait_id']} {weight:.1f} → {new_weight:.1f}（应激并入 {value:+.1f}）"
        )
    return changes


# ---------------- consolidation ----------------

PATTERNS = [
    (re.compile(r"我叫([\w\u4e00-\u9fa5]{1,20})"), "称呼"),
    (re.compile(r"我是([\w\u4e00-\u9fa5]{1,20})"), "身份"),
    (re.compile(r"我在做([^，。！？；\n]{2,40})"), "在做"),
    (re.compile(r"我喜欢([^，。！？；\n]{1,30})"), "喜欢"),
    (re.compile(r"我讨厌([^，。！？；\n]{1,30})"), "讨厌"),
]

INTENTS = [
    (re.compile(r"叫什么|名字|我是谁|称呼"), ["称呼", "身份"]),
    (re.compile(r"做什么|在做什么|项目|忙什么"), ["在做"]),
    (re.compile(r"喜欢什么|喜欢"), ["喜欢"]),
    (re.compile(r"讨厌什么|讨厌"), ["讨厌"]),
]

NEGATION = re.compile(r"不|别|讨厌|烦|腻|受不了|再也不|没意思|无聊")


def intent_predicates(text: str) -> list[str]:
    out: list[str] = []
    for pattern, predicates in INTENTS:
        if pattern.search(text):
            out.extend(predicates)
    return list(dict.fromkeys(out))


def _clean(value: str) -> str:
    value = value.strip().strip("，。！？；、 ")
    value = re.sub(r"^(一个|一件|一项|一款|一些|一名|个|件|项|款)", "", value).strip()
    return value


def extract_facts(text: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for pattern, predicate in PATTERNS:
        for m in pattern.finditer(text):
            value = _clean(m.group(1))
            if value:
                out.append(("user", predicate, value))
    return out


def keywords_of(text: str) -> list[str]:
    stop = {"我", "你", "他", "的", "了", "吗", "呢", "什么", "怎么", "在", "是", "和", "与", "还", "有"}
    tokens = re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{2,}", text)
    return [t for t in tokens if t not in stop][:6]


def _overlap(target: str, text: str) -> bool:
    chars = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", target)
    if len(chars) <= 2:
        return chars in text
    grams = {chars[i : i + 2] for i in range(len(chars) - 1)}
    return any(g in text for g in grams)


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fa5]+", text):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    for word in re.findall(r"[A-Za-z0-9_]{2,}", text.lower()):
        out.add(word)
    return out


# ---------------- 语义检索（M2，可选） ----------------

_M2_STATE = {"ready": False, "provider": None, "warned": False}


def _embedding_provider(cfg: dict):
    """懒加载 M2；任何失败都只降级，不影响 M1。"""
    if _M2_STATE["ready"]:
        return _M2_STATE["provider"]
    _M2_STATE["ready"] = True
    try:
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from m2.embed.providers import detect

        _M2_STATE["provider"] = detect((cfg or {}).get("retrieval") or {})
    except Exception as exc:  # noqa: BLE001
        _M2_STATE["provider"] = None
        if not _M2_STATE["warned"]:
            print(f"  [向量检索] provider 不可用：{exc}（回退词面检索）")
            _M2_STATE["warned"] = True
    return _M2_STATE["provider"]


def _retrieval_mode(cfg: dict, override: str | None = None) -> str:
    retr = (cfg or {}).get("retrieval") or {}
    mode = (override or retr.get("mode") or "auto").lower()
    if mode == "auto":
        provider = _embedding_provider(cfg)
        return "hybrid" if provider is not None and getattr(provider, "is_semantic", False) else "lexical"
    return mode if mode in ("lexical", "hybrid") else "lexical"


def _index_episode(store: "Store", episode_id: int, text: str) -> None:
    cfg = store.load_config()
    retr = cfg.get("retrieval") or {}
    if retr.get("index_on_write", True) is False:
        return
    provider = _embedding_provider(cfg)
    if provider is None:
        return
    try:
        from m2.embed import index as embed_index

        embed_index.index_episodes(store.db, [episode_id], provider, provider.model)
    except Exception as exc:  # noqa: BLE001
        if not _M2_STATE["warned"]:
            print(f"  [向量索引] 失败：{exc}（继续词面检索）")
            _M2_STATE["warned"] = True
        _M2_STATE["provider"] = None


def _vector_candidates(store: "Store", query: str, cfg: dict) -> list[tuple[int, float]]:
    provider = _embedding_provider(cfg)
    if provider is None:
        return []
    retr = cfg.get("retrieval") or {}
    try:
        from m2.embed import search as embed_search

        vector = provider.embed([query])[0]
        return embed_search.cosine_candidates(
            store.db,
            vector,
            provider.model,
            floor=float(retr.get("floor", 0.25)),
            limit=int(retr.get("candidates", 50)),
        )
    except Exception as exc:  # noqa: BLE001
        if not _M2_STATE["warned"]:
            print(f"  [向量检索] 查询失败：{exc}（回退词面检索）")
            _M2_STATE["warned"] = True
        _M2_STATE["provider"] = None
        return []


def _reindex_missing(store: "Store", limit: int = 200) -> int:
    cfg = store.load_config()
    provider = _embedding_provider(cfg)
    if provider is None:
        return 0
    try:
        from m2.embed import index as embed_index

        return embed_index.reindex_missing(store.db, provider, provider.model, limit=limit)
    except Exception:  # noqa: BLE001
        return 0



def _recency(ts: str, tau_days: float = 7.0) -> float:
    import math

    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return 0.5
    now_dt = current_dt()
    age_days = max(0.0, (now_dt - then).total_seconds() / 86400.0)
    return math.exp(-age_days / tau_days)


def persona_salience(traits: list, text: str) -> float:
    """它自己在意什么：命中偏好权重之和 + 边界类关键词。"""
    score = 0.0
    for trait in traits:
        if _overlap(trait["target"], text):
            score += float(trait["weight"]) / 100.0
    for keyword in ("承诺", "答应", "边界", "隐私", "约定"):
        if keyword in text:
            score += 0.3
    return min(1.0, score)


def retrieve(
    store: "Store",
    query: str,
    k: int = 3,
    scores: dict[str, float] | None = None,
    persona: dict | None = None,
    mode: str | None = None,
) -> list[tuple[float, object]]:
    """混合打分：relevance（词面/向量融合）+ 时间近因 + 重要性 + 画像 + 人格。

    向量只替换 relevance，主观调制（画像/关注/人格）仍然是加分项，不是门槛。
    """
    q = _tokens(query)
    if not q:
        return []
    cfg = store.load_config()
    active_mode = _retrieval_mode(cfg, mode)
    traits = store.traits() if persona else []

    rows_by_id: dict[int, object] = {}
    lexical: dict[int, float] = {}
    for row in store.all_episodes():
        rows_by_id[row["id"]] = row
        overlap = len(q & _tokens(row["content"])) / len(q)
        if overlap > 0:
            lexical[row["id"]] = overlap

    relevance: dict[int, float] = dict(lexical)
    if active_mode == "hybrid":
        lexical_rank = {rid: index + 1 for index, rid in enumerate(
            sorted(lexical, key=lambda rid: -lexical[rid]))}
        vector_rank: dict[int, int] = {}
        for rank, (rid, _sim) in enumerate(_vector_candidates(store, query, cfg), 1):
            vector_rank[rid] = rank
            if rid not in rows_by_id:
                row = store.db.execute("SELECT * FROM episodes WHERE id=?", (rid,)).fetchone()
                if row is not None:
                    rows_by_id[rid] = row
        if vector_rank:
            try:
                from m2.embed.search import fuse

                relevance = fuse(lexical_rank, vector_rank)
            except Exception:  # noqa: BLE001
                relevance = dict(lexical)

    scored: list[tuple[float, object]] = []
    for rid, rel in relevance.items():
        row = rows_by_id.get(rid)
        if row is None:
            continue
        score = 0.6 * rel + 0.25 * _recency(row["occurred_at"]) + 0.15 * row["importance"]
        if row["role"] != "user":
            score *= 0.7          # 助手回复常复述用户内容，降权避免自我强化
        if scores:
            boost = max(
                (value for name, value in scores.items() if name in row["content"]),
                default=0.0,
            )
            score += 0.25 * boost  # 画像优先级是加分项，不是门槛
        if traits:
            score += 0.15 * persona_salience(traits, row["content"])
        scored.append((score, row))

    # 归档条目靠标签召回：内容被压缩了，但锚点还在
    if q:
        marks = ",".join("?" * len(q))
        archived_rows = store.db.execute(
            f"SELECT DISTINCT e.id, e.content, e.occurred_at, e.importance, e.role"
            f" FROM episodes e JOIN tags t ON t.target_id = e.id AND t.target_kind = 'episode'"
            f" WHERE e.archived = 1 AND t.tag IN ({marks})",
            tuple(q),
        ).fetchall()
        seen_ids = {row["id"] for _, row in scored}
        for row in archived_rows:
            if row["id"] in seen_ids:
                continue
            scored.append((0.45 + 0.1 * _recency(row["occurred_at"]), row))

    scored.sort(key=lambda x: -x[0])
    top = scored[:k]
    for _, row in top:
        store.db.execute(
            "UPDATE episodes SET access_count = access_count + 1, last_access = ? WHERE id = ?",
            (now(), row["id"]),
        )
    if top:
        store.db.commit()
    return top


def snippet(text: str, n: int = 36) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n] + "…"


def _is_question(text: str) -> bool:
    return bool(re.search(r"[？?]", text)) or bool(
        re.search(r"什么|怎么|如何|为什么|哪(个|里|些)|吗\s*[。！!]?$", text)
    )


def apply_drift(store: "Store", persona: dict, episodes: list) -> tuple[list[str], set[str]]:
    """按证据微调偏好权重，返回 (日志行, 本轮变过的 trait_id)。"""
    ev = persona.get("evolution", {}) or {}
    min_evidence = int(ev.get("min_evidence", 3))
    max_step = float(ev.get("max_step", 2))
    weekly_cap = float(ev.get("weekly_cap", 5))
    pf = ev.get("polarity_flip", {}) or {}
    dead_zone = float(pf.get("dead_zone", 20))
    min_reverse = int(pf.get("min_reverse_evidence", 5))
    requires_human = bool(pf.get("requires_human", True))
    user_msgs = [r["content"] for r in episodes if r["role"] == "user"]

    changes: list[str] = []
    touched: set[str] = set()
    for trait in store.traits():
        if trait["is_locked"]:
            continue
        weight = float(trait["weight"])
        baseline = float(trait["baseline"])
        polarity = trait["polarity"]
        mentions = [m for m in user_msgs if _overlap(trait["target"], m)]
        negated = [m for m in mentions if NEGATION.search(m)]
        if polarity == "like":
            support, reverse = len(mentions) - len(negated), len(negated)
        else:
            support, reverse = len(negated), len(mentions) - len(negated)
        up_used, down_used = store.weekly_drift(trait["trait_id"])

        if support >= min_evidence and reverse < min_evidence:
            remaining_up = max(0.0, weekly_cap - up_used)
            if remaining_up <= 0:
                changes.append(
                    f"{trait['trait_id']} 上行已达 7 天上限（{up_used:.1f}/{weekly_cap:.0f}）"
                )
                continue
            delta = min(max_step, round(0.5 * support, 1), remaining_up)
            new_weight = min(100.0, weight + delta)
            if new_weight <= weight:
                continue
            reason = f"{support} 条记忆支持「{trait['target']}」"
            store.update_trait(trait["trait_id"], new_weight, new_weight - weight, support, reason)
            touched.add(trait["trait_id"])
            changes.append(
                f"{trait['trait_id']} {weight:.1f} → {new_weight:.1f}（支持 {support} 条）"
            )
            continue

        if reverse >= min_evidence and support < min_evidence:
            remaining_down = max(0.0, weekly_cap - down_used)
            if remaining_down <= 0:
                changes.append(
                    f"{trait['trait_id']} 下行已达 7 天上限（{down_used:.1f}/{weekly_cap:.0f}）"
                )
                continue
            delta = min(max_step, round(0.5 * reverse, 1), remaining_down, weight)
            new_weight = max(0.0, weight - delta)
            if new_weight >= weight:
                continue
            reason = f"{reverse} 条反向证据指向「{trait['target']}」"
            store.update_trait(trait["trait_id"], new_weight, new_weight - weight, reverse, reason)
            touched.add(trait["trait_id"])
            changes.append(
                f"{trait['trait_id']} {weight:.1f} → {new_weight:.1f}（反向 {reverse} 条）"
            )
            # 极性翻转闸门：死区 + 反向证据数 + 人工确认，三者缺一不可
            if (
                new_weight < dead_zone
                and reverse >= min_reverse
                and requires_human
                and not store.has_pending_flip(trait["trait_id"])
            ):
                target_polarity = "dislike" if polarity == "like" else "like"
                fid = store.create_flip_request(
                    trait["trait_id"], polarity, target_polarity, reverse
                )
                changes.append(f"已生成极性翻转待确认 #{fid}")
            continue

        # 本轮没有决定性证据：向基线缓慢回弹，下行额度单独计算
        if store.has_pending_flip(trait["trait_id"]):
            # 翻转待人工确认期间冻结权重，避免回弹把它拉回旧极性
            changes.append(f"{trait['trait_id']} 有翻转待确认，权重冻结")
            continue

        gap = weight - baseline
        if abs(gap) >= 0.05:
            remaining_down = max(0.0, weekly_cap - down_used)
            if remaining_down <= 0:
                changes.append(
                    f"{trait['trait_id']} 下行已达 7 天上限（{down_used:.1f}/{weekly_cap:.0f}）"
                )
                continue
            step = min(0.5, abs(gap), remaining_down)
            new_weight = weight - step if gap > 0 else weight + step
            store.update_trait(
                trait["trait_id"], new_weight, new_weight - weight, 0, "无新证据，回归基线"
            )
            changes.append(f"{trait['trait_id']} {weight:.1f} → {new_weight:.1f}（回归基线）")

    return changes, touched


# ---------------- commands ----------------

def cmd_init(args) -> None:
    home = Path(args.home)
    store = Store(home)
    src = Path(args.persona)
    if src.is_dir():
        directory = _layers_dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        for name in (KERNEL_FILE, TRAITS_FILE, STATE_FILE):
            source = src / name
            if source.exists():
                shutil.copyfile(source, directory / name)
        persona = load_persona_layers(home)
    else:
        persona = json.loads(src.read_text(encoding="utf-8-sig"))
        if args.profile:
            apply_profile(persona, args.profile)
        split_persona_layers(persona, home)
    if args.profile and src.is_dir():
        apply_profile(persona, args.profile)
        traits = {
            key: persona[key]
            for key in ("traits", "memory", "evolution", "meta")
            if key in persona
        }
        (_layers_dir(home) / TRAITS_FILE).write_text(
            json.dumps(traits, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    store.record_persona(persona)
    store.sync_traits(persona)
    cfg = store.load_config()
    cfg.setdefault("model", "mock-a")
    cfg.setdefault("session_id", str(uuid.uuid4()))
    store.save_config(cfg)
    store.audit("init", f"persona={persona['id']} version={persona['version']}")
    print(f"初始化完成：{home}")
    print(f"  人格：{persona['kernel']['identity']['name']} v{persona['version']}")
    print(f"  模型：{cfg['model']}")


def cmd_chat(args) -> None:
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    cfg = store.load_config()
    model = make_model(cfg.get("model", "mock-a"))

    message = args.say
    if message is None:
        message = input("你：").strip()
        if not message:
            return

    print(respond(store, persona, model, cfg, message, retrieval=args.retrieval))


def respond(store: "Store", persona: dict, model, cfg: dict, message: str, retrieval: str | None = None) -> str:
    level = maintenance_level(store, persona)
    if level == "critical":
        print("  [维护优先] 上下文临界，前台让路")
        for line in run_maintenance(store, persona, "临界水位"):
            print(line)
    else:
        evict_by_pressure(store, persona)

    scores = entity_scores(store)
    hits = retrieve(store, message, k=3, scores=scores, persona=persona, mode=retrieval)
    memory_lines = [
        snippet(row["content"])
        for _, row in hits
        if row["role"] == "user" and not _is_question(row["content"])
    ]
    memory_lines = list(dict.fromkeys(memory_lines))[:2]

    if not memory_lines:
        topics, resumed_at = store.load_resume()
        if topics and resumed_at and _days_since(resumed_at) <= 3:
            memory_lines = ["上次在聊：" + "、".join(topics)]

    facts_rows = store.search_facts(keywords_of(message), intent_predicates(message))
    fact_lines = [f"{r['predicate']}：{r['object']}" for r in facts_rows]

    mood, updated_at = store.load_state()
    mood = tick_mood(mood, updated_at, message)
    store.save_state(mood)

    for trait in store.traits():
        if trait["is_locked"] or not _overlap(trait["target"], message):
            continue
        negated = bool(NEGATION.search(message))
        supportive = (negated and trait["polarity"] == "dislike") or (
            not negated and trait["polarity"] == "like"
        )
        store.bump_stress(trait["trait_id"], 1.0 if supportive else -1.0)

    user_id = store.add_episode(cfg["session_id"], "user", message)
    _index_episode(store, user_id, message)
    reply = model.reply(persona, fact_lines, memory_lines, message, mood)
    reply_id = store.add_episode(cfg["session_id"], "assistant", reply)
    _index_episode(store, reply_id, reply)
    return reply


def cmd_sleep(args) -> None:
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    for line in run_sleep(store, persona, home):
        print(line)


def _days_since(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return 0.0
    return max(0.0, (current_dt() - then).total_seconds() / 86400.0)


def revalidate_traits(store: "Store", persona: dict) -> list[str]:
    """5.5 旧标签再验证：长期没被新记忆支撑的偏好，每年打折 10%。"""
    ev = persona.get("evolution", {}) or {}
    ar = ev.get("anti_rigidity", {}) or {}
    stale_days = float(ar.get("stale_after_days", 90))
    discount = float(ar.get("unverified_discount_per_year", 0.10))

    lines: list[str] = []
    for trait in store.traits():
        if trait["is_locked"]:
            continue
        anchor = trait["last_support_at"] or trait["first_seen"] or trait["updated_at"]
        age = _days_since(anchor)
        if age < stale_days:
            continue
        last_discount = trait["last_discount_at"]
        if last_discount and _days_since(last_discount) < stale_days:
            continue
        weight = float(trait["weight"])
        new_weight = round(max(0.0, weight * (1.0 - discount)), 1)
        if new_weight == weight:
            continue
        store.update_trait(
            trait["trait_id"],
            new_weight,
            new_weight - weight,
            0,
            f"旧标签再验证：{int(age)} 天未更新，-{discount:.0%}",
        )
        store.db.execute(
            "UPDATE persona_traits SET last_discount_at=? WHERE trait_id=?",
            (now(), trait["trait_id"]),
        )
        store.db.commit()
        lines.append(f"{trait['trait_id']} {weight:.1f} → {new_weight:.1f}（{int(age)} 天未更新）")
    return lines


def rigidity_index(store: "Store") -> tuple[int, dict]:
    """僵化指数：旧偏好占比 0.6 + 旧事实占比 0.4。"""
    traits = store.traits()
    facts = store.db.execute("SELECT created_at FROM facts").fetchall()
    old_traits = sum(
        1
        for t in traits
        if _days_since(t["last_support_at"] or t["first_seen"] or t["updated_at"]) >= 90
    )
    old_facts = sum(1 for f in facts if _days_since(f["created_at"]) >= 90)
    trait_ratio = old_traits / len(traits) if traits else 0.0
    fact_ratio = old_facts / len(facts) if facts else 0.0
    score = round(100 * (0.6 * trait_ratio + 0.4 * fact_ratio))
    return score, {"旧偏好占比": round(trait_ratio, 2), "旧事实占比": round(fact_ratio, 2)}


def exploration_candidates(store: "Store", episodes: list) -> list[str]:
    """5.5 探索预算：找反复出现、但没被任何偏好或事实覆盖的主题。"""
    covered = [t["target"] for t in store.traits()]
    covered += [r["object"] for r in store.db.execute("SELECT object FROM facts")]
    counts: dict[str, int] = {}
    for row in episodes:
        if row["role"] != "user":
            continue
        for token in re.findall(r"[\u4e00-\u9fa5]{2,4}", row["content"]):
            if any(token in item for item in covered):
                continue
            counts[token] = counts.get(token, 0) + 1
    hot = [(token, count) for token, count in counts.items() if count >= 2]
    hot.sort(key=lambda item: -item[1])
    return [f"探索候选：「{token}」出现 {count} 次，未被覆盖（1% 预算）" for token, count in hot[:1]]


def tag_episode(store: "Store", episode_id: int, content: str) -> None:
    """归档前打标签：压缩后靠标签仍能召回。"""
    names = [row["name"] for row in store.db.execute("SELECT name FROM entities").fetchall()]
    tokens = set(_tokens(content)) | {name for name in names if name in content}
    for token in tokens:
        store.db.execute(
            "INSERT INTO tags (tag, target_kind, target_id) VALUES (?, 'episode', ?)",
            (token, episode_id),
        )
    store.db.commit()


def cleanup_episodes(store: "Store", persona: dict) -> list[str]:
    """分级清理：温数据压摘要，冷数据删除原文；热窗口不动。"""
    retention = ((persona.get("memory") or {}).get("retention") or {})
    warm_days = float(retention.get("warm_days", 90))
    prune_days = float(retention.get("prune_days", 365))

    archived = 0
    pruned = 0
    rows = store.db.execute(
        "SELECT id, content, occurred_at, archived FROM episodes"
    ).fetchall()
    for row in rows:
        age = _days_since(row["occurred_at"])
        if age >= prune_days:
            store.db.execute("DELETE FROM episodes WHERE id=?", (row["id"],))
            pruned += 1
        elif age >= warm_days and not row["archived"]:
            tag_episode(store, row["id"], row["content"])
            short = snippet(row["content"], 60) + "（已归档）"
            store.db.execute(
                "UPDATE episodes SET content=?, archived=1 WHERE id=?", (short, row["id"])
            )
            _index_episode(store, int(row["id"]), short)
            archived += 1
    store.db.commit()
    if pruned:
        try:
            from m2.embed import store as embed_store

            embed_store.prune(store.db)
        except Exception:  # noqa: BLE001
            pass

    lines: list[str] = []
    if archived:
        lines.append(f"清理：{archived} 条原文压成摘要（超过 {warm_days:.0f} 天）")
    if pruned:
        lines.append(f"清理：{pruned} 条冷数据删除（超过 {prune_days:.0f} 天）")
    return lines


def window_size(store: "Store") -> int:
    return int(
        store.db.execute("SELECT COUNT(*) FROM episodes WHERE archived=0").fetchone()[0]
    )


def maintenance_level(store: "Store", persona: dict) -> str:
    """窗口 / 预算：<1 正常，≥1 软水位，≥1.5 临界（前台让路）。"""
    budget = int((persona.get("memory") or {}).get("context_budget", 12))
    ratio = window_size(store) / max(1, budget)
    if ratio >= 1.5:
        return "critical"
    if ratio >= 1.0:
        return "soft"
    return "none"


def keep_score(store: "Store", traits: list, scores: dict[str, float], row) -> float:
    """这条记忆「值不值得留」：用户在意 + 它在不在意 + 新旧 + 重要性。"""
    content = row["content"]
    boost = max((value for name, value in scores.items() if name in content), default=0.0)
    salience = persona_salience(traits, content) if traits else 0.0
    return (
        0.35 * boost
        + 0.35 * salience
        + 0.2 * _recency(row["occurred_at"])
        + 0.1 * float(row["importance"])
    )


def evict_by_pressure(store: "Store", persona: dict) -> int:
    """上下文压力驱逐：按保留权重从低到高归档，水位由硬件预算决定。"""
    memory_cfg = persona.get("memory") or {}
    budget = int(memory_cfg.get("context_budget", 12))
    low_ratio = float(memory_cfg.get("low_watermark", 0.7))

    rows = store.db.execute(
        "SELECT id, content, occurred_at, importance FROM episodes WHERE archived = 0 ORDER BY id ASC"
    ).fetchall()
    if len(rows) <= budget:
        return 0

    keep = max(4, int(budget * low_ratio))
    newest = {row["id"] for row in rows[-2:]}   # 最近 2 条无条件保留，其余按权重竞争
    traits = store.traits()
    scores = entity_scores(store)

    candidates = [row for row in rows if row["id"] not in newest]
    candidates.sort(key=lambda row: keep_score(store, traits, scores, row))
    victims = candidates[: max(0, len(rows) - keep)]
    for row in victims:
        tag_episode(store, row["id"], row["content"])
        store.db.execute(
            "UPDATE episodes SET content=?, archived=1 WHERE id=?",
            (snippet(row["content"], 60) + "（上下文压力归档）", row["id"]),
        )
    store.db.commit()
    store.audit("evict", f"window={len(rows)} budget={budget} evicted={len(victims)}")
    if victims:
        print(f"  [实时归档] {len(victims)} 条（窗口 {len(rows)} > 预算 {budget}，按保留权重升序）")
    return len(victims)


def run_maintenance(store: "Store", persona: dict, reason: str) -> list[str]:
    """维护任务：水位到线就检索一遍、压缩归档。临界时由前台让路。"""
    level = maintenance_level(store, persona)
    if level == "none":
        return []
    evicted = evict_by_pressure(store, persona)
    store.audit("maintenance", f"level={level} reason={reason} evicted={evicted}")
    return [f"维护（{reason}，水位 {level}）：归档 {evicted} 条"]


KIND_BY_PREDICATE = {
    "称呼": "person",
    "身份": "person",
    "喜欢": "interest",
    "讨厌": "interest",
    "习惯": "habit",
    "在做": "project",
}


def _ensure_entity(store: "Store", name: str, kind: str) -> int:
    row = store.db.execute(
        "SELECT id FROM entities WHERE name=? AND kind=?", (name, kind)
    ).fetchone()
    if row:
        return int(row["id"])
    cur = store.db.execute(
        "INSERT INTO entities (name, kind, created_at) VALUES (?,?,?)", (name, kind, now())
    )
    store.db.commit()
    return int(cur.lastrowid)


def rebuild_graph(store: "Store", rows: list | None = None) -> int:
    """画像图：边权按 90 天半衰期衰减，再叠加本轮提及次数。"""
    root = _ensure_entity(store, "user", "person")

    # 1. 先让所有边随时间衰减
    for edge in store.db.execute(
        "SELECT id, weight, confidence, updated_at FROM edges"
    ).fetchall():
        decayed = float(edge["weight"]) * math.exp(
            -max(0.0, _days_since(edge["updated_at"])) / 90.0
        )
        store.db.execute(
            "UPDATE edges SET weight=?, priority=?, updated_at=? WHERE id=?",
            (round(decayed, 3), round(decayed * float(edge["confidence"]), 4), now(), edge["id"]),
        )

    # 2. 确保 facts 对应的边存在（不覆盖已有权重）
    facts = store.db.execute("SELECT predicate, object FROM facts").fetchall()
    for fact in facts:
        kind = KIND_BY_PREDICATE.get(fact["predicate"], "topic")
        dst = _ensure_entity(store, fact["object"], kind)
        store.db.execute(
            "INSERT OR IGNORE INTO edges (src, dst, relation, weight, confidence, priority, updated_at)"
            " VALUES (?,?,?,0.0,1.0,0,?)",
            (root, dst, fact["predicate"], now()),
        )

    # 3. 本轮提及累加：提到一次，长期权重加一
    texts = [row["content"] for row in (rows or []) if row["role"] == "user"]
    if texts:
        for entity in store.db.execute("SELECT id, name FROM entities WHERE name <> 'user'").fetchall():
            mentions = sum(1 for text in texts if entity["name"] in text)
            if not mentions:
                continue
            store.db.execute(
                "UPDATE edges SET weight = weight + ?, updated_at=? WHERE dst=?",
                (mentions, now(), entity["id"]),
            )

    # 4. 重算画像优先级
    for edge in store.db.execute("SELECT id, weight, confidence FROM edges").fetchall():
        store.db.execute(
            "UPDATE edges SET priority=? WHERE id=?",
            (round(float(edge["weight"]) * float(edge["confidence"]), 4), edge["id"]),
        )
    store.db.commit()
    return int(store.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0])


def entity_scores(store: "Store") -> dict[str, float]:
    """两套优先级归一化后相加：长期画像 0.6 + 近期关注 0.4。"""
    rows = store.db.execute(
        "SELECT e.name, ed.weight, ed.confidence, ed.updated_at"
        " FROM entities e JOIN edges ed ON ed.dst = e.id WHERE e.name <> 'user'"
    ).fetchall()
    cutoff = (current_dt() - timedelta(days=7)).isoformat(timespec="seconds")
    recent = [
        row["content"]
        for row in store.db.execute(
            "SELECT content FROM episodes WHERE occurred_at >= ?", (cutoff,)
        ).fetchall()
    ]

    raw: dict[str, float] = {}
    for row in rows:
        profile = row["weight"] * row["confidence"] * math.exp(
            -max(0.0, _days_since(row["updated_at"])) / 90.0
        )
        attention = sum(1 for text in recent if row["name"] in text)
        raw[row["name"]] = raw.get(row["name"], 0.0) + 0.6 * profile + 0.4 * attention

    if raw:
        peak = max(raw.values()) or 1.0
        raw = {name: value / peak for name, value in raw.items()}
    return raw


def resume_topics(rows: list) -> list[str]:
    stop = set("我你他她它的了是在做一个这那有和与就都也还很太要会能可以把被让给对从到吗呢吧啊呀")
    counts: dict[str, int] = {}
    for row in rows:
        if row["role"] != "user":
            continue
        text = row["content"]
        for word in re.findall(r"[A-Za-z0-9_]{2,}", text):
            counts[word] = counts.get(word, 0) + 1
        for run in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
            core = run.strip("".join(stop))
            if len(core) >= 2:
                counts[core] = counts.get(core, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0])))
    return [token for token, _ in ranked[:3]]


def run_sleep(store: "Store", persona: dict, home: Path) -> list[str]:
    global _IN_SLEEP
    _IN_SLEEP = True
    rows = store.unconsolidated()
    lines: list[str] = []

    if not rows:
        lines.append("没有需要归档的对话。")
    else:
        user_text = "\n".join(r["content"] for r in rows if r["role"] == "user")
        facts = extract_facts(user_text)
        for subject, predicate, obj in facts:
            store.add_fact(subject, predicate, obj, rows[-1]["id"])

        summary = "；".join(f"{p}：{o}" for _, p, o in facts)
        if summary:
            store.set_profile(summary)
        store.mark_consolidated([r["id"] for r in rows])

        snap = home / "snapshots"
        snap.mkdir(exist_ok=True)
        name = f"pre_consolidate_{current_dt().strftime('%Y%m%d_%H%M%S')}.json"
        (snap / name).write_text(
            json.dumps(
                {
                    "episodes": [dict(r) for r in rows],
                    "profile": summary,
                    "created_at": now(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        store.db.execute(
            "INSERT INTO snapshots (kind, path, created_at) VALUES ('pre_consolidate', ?, ?)",
            (str(snap / name), now()),
        )
        store.db.commit()
        store.audit("sleep", f"episodes={len(rows)} facts={len(facts)}")
        lines.append(f"归档完成：{len(rows)} 条对话 → {len(facts)} 条事实")
        if facts:
            lines.append("  新事实：" + "；".join(f"{p}={o}" for _, p, o in facts))

        changes, touched = apply_drift(store, persona, rows)
        if changes:
            lines.append("  人格微调：" + "；".join(changes))
        else:
            lines.append("  人格微调：无（证据不足，未达阈值）")

        stress_changes = settle_stress(store, persona, skip=touched)
        if stress_changes:
            lines.append("  应激结算：" + "；".join(stress_changes))

        store.save_state({"愉悦": 0.0, "烦躁": 0.0, "专注": 0.0, "疲惫": 0.0})
        lines.append("  情绪归零（睡眠）")

        topics = resume_topics(rows)
        if topics:
            store.save_resume(topics)
            lines.append("  恢复指针：" + "、".join(topics))

    # 维护任务：即使没有新对话也要跑
    relations = rebuild_graph(store, rows)
    if relations and rows:
        lines.append(f"  画像图：{relations} 条关系")
    lines.extend("  " + item for item in cleanup_episodes(store, persona))
    revalidated = revalidate_traits(store, persona)
    if revalidated:
        lines.append("  旧标签再验证：" + "；".join(revalidated))
    for note in exploration_candidates(store, rows):
        lines.append("  " + note)

    persist_traits_file(store, home)
    indexed = _reindex_missing(store)
    if indexed:
        lines.append(f"  向量索引：补建 {indexed} 条")
    _IN_SLEEP = False
    return lines


def cmd_switch_model(args) -> None:
    home = Path(args.home)
    store = Store(home)
    cfg = store.load_config()
    old = cfg.get("model", "mock-a")
    cfg["model"] = args.model
    cfg["session_id"] = str(uuid.uuid4())
    store.save_config(cfg)
    store.audit("switch-model", f"{old} -> {args.model}")
    print(f"模型已切换：{old} → {args.model}（新会话 {cfg['session_id'][:8]}）")


def cmd_recall(args) -> None:
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    hits = retrieve(
        store, args.query, k=args.top, scores=entity_scores(store), persona=persona,
        mode=getattr(args, "retrieval", None),
    )
    if not hits:
        print("没有匹配的记忆。")
        return
    for score, row in hits:
        print(f"{score:.3f}  [{row['role']:<9}] {snippet(row['content'], 50)}")


def cmd_flip_review(args) -> None:
    store = Store(Path(args.home))
    flip_id = args.approve or args.reject
    if flip_id:
        row = store.get_flip(flip_id)
        if not row or row["status"] != "pending":
            print("没有这条待确认请求。")
            return
        trait = store.db.execute(
            "SELECT * FROM persona_traits WHERE trait_id=?", (row["trait_id"],)
        ).fetchone()
        if not trait:
            print("对应的偏好已不存在。")
            return
        if args.approve:
            weight = float(trait["weight"])
            store.decide_flip(flip_id, "approved")
            store.set_polarity(row["trait_id"], row["to_polarity"], weight)
            store.db.execute(
                "INSERT INTO persona_drift_log (trait_id, old_weight, new_weight, delta, evidence_count, reason, created_at)"
                " VALUES (?,?,?,0,?,?,?)",
                (row["trait_id"], weight, weight, row["evidence_count"], "人工确认极性翻转", now()),
            )
            store.db.commit()
            store.audit("flip-approved", f"{row['trait_id']} {row['from_polarity']}->{row['to_polarity']}")
            print(
                f"已批准：{row['trait_id']} {row['from_polarity']} → {row['to_polarity']}"
                f"（权重保持 {weight:.1f}）"
            )
        else:
            store.decide_flip(flip_id, "rejected")
            store.audit("flip-rejected", row["trait_id"])
            print(f"已拒绝：{row['trait_id']} 保持 {row['from_polarity']}")
        return

    pending = store.pending_flips()
    if not pending:
        print("没有待确认的翻转请求。")
        return
    for row in pending:
        print(
            f"#{row['id']}  {row['trait_id']}  {row['from_polarity']} → {row['to_polarity']}"
            f"  反向证据 {row['evidence_count']} 条  {row['created_at']}"
        )


def cmd_send(args) -> None:
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    with (home / "inbox.txt").open("a", encoding="utf-8") as f:
        f.write(args.say.strip() + "\n")
    print(f"已投递：{args.say}")


def cmd_serve(args) -> None:
    home = Path(args.home)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)   # 常驻时按行刷新，便于观察
    store = Store(home)
    persona = store.load_persona()
    cfg = store.load_config()
    model = make_model(cfg.get("model", "mock-a"))
    inbox = home / "inbox.txt"
    inbox.touch(exist_ok=True)
    pos = inbox.stat().st_size
    last_active = time.time()

    print(f"常驻中：{home}")
    print(f"  模型：{cfg.get('model')}   空闲 {args.idle_seconds}s 自动睡眠   Ctrl+C 停止")
    print(f"  投递：另开窗口 python syspersona.py --home \"{home}\" send --say \"...\"")

    try:
        while True:
            if maintenance_level(store, persona) != "none":
                for line in run_maintenance(store, persona, "空闲维护"):
                    print(line)

            size = inbox.stat().st_size
            if size < pos:
                pos = 0
            if size > pos:
                with inbox.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                pos = size
                for line in chunk.splitlines():
                    message = line.strip()
                    if not message:
                        continue
                    reply = respond(store, persona, model, cfg, message)
                    print(f"你：{message}")
                    print(f"它：{reply}")
                    last_active = time.time()
            if time.time() - last_active >= args.idle_seconds:
                print("—— 空闲，进入睡眠 ——")
                for line in run_sleep(store, persona, home):
                    print(line)
                last_active = time.time()
            time.sleep(args.tick)
    except KeyboardInterrupt:
        print("\n已停止。")


BUILTIN_SCENARIO = [
    {"messages": ["回答简洁一点。", "我喜欢简洁的回答。", "以后都简洁些。"], "expect": {"pref_short": 81.5}},
    {"messages": ["今天天气不错。"], "expect": {"pref_short": 81.0}},
    {"messages": ["继续简洁回答。", "保持简洁。", "简洁最好。"], "expect": {"pref_short": 82.5}},
    {"messages": ["简洁点。", "简洁。", "回答简洁。"], "expect": {"pref_short": 84.0}},
    {"messages": ["简洁些。", "简洁。", "简洁点。"], "expect": {"pref_short": 84.5}},
    {"messages": ["随便聊聊。"], "expect": {"pref_short": 84.0}},
    {"messages": ["我不喜欢长篇大论。"] * 5, "expect": {"pref_short": 83.5, "pref_longpost": 16.0}},
    {"messages": ["周末休息。"], "expect": {"pref_longpost": 16.0}},
    {"messages": ["没什么事。"]},
    {"messages": ["我讨厌简洁的回答。"], "sleep": False},
    {"messages": [], "expect": {"pref_short": 82.0}},
    {"messages": ["在吗。"]},
    {"messages": ["嗯。"]},
    {"messages": [], "expect": {"pref_short": 81.0}},
]


def load_scenario(path: str | None) -> dict:
    if not path:
        return {"days": BUILTIN_SCENARIO, "expect_final": {"pending_flips": 1}}
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def cmd_simulate(args) -> None:
    global _VIRTUAL_NOW
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    cfg = store.load_config()
    model = make_model(cfg.get("model", "mock-a"))
    spec = load_scenario(args.scenario)
    scenario = spec.get("days", [])
    final_expect = spec.get("expect_final") or {}

    failures: list[str] = []
    checked = 0
    _VIRTUAL_NOW = current_dt()
    start = _VIRTUAL_NOW
    print(f"模拟开始：{start:%Y-%m-%d}   共 {len(scenario)} 天（虚拟时钟，1 天 = 1 步）")
    print("—" * 72)
    try:
        for index, day in enumerate(scenario, 1):
            advance(timedelta(days=1))
            messages = day.get("messages", []) or []
            for message in messages:
                respond(store, persona, model, cfg, message, retrieval=args.retrieval)

            lines: list[str] = []
            if day.get("sleep", True):
                lines = run_sleep(store, persona, home)

            weights = {
                t["trait_id"]: float(t["weight"]) + stress_value(t) for t in store.traits()
            }
            print(
                f"第 {index:>2} 天  消息 {len(messages)}  "
                + "  ".join(f"{k}={v:.1f}" for k, v in weights.items())
            )
            for line in lines:
                if any(
                    k in line
                    for k in (
                        "人格微调",
                        "应激结算",
                        "新事实",
                        "旧标签",
                        "探索候选",
                        "清理",
                        "恢复指针",
                    )
                ):
                    print("        " + line.strip())

            if index % 30 == 0:
                score, parts = rigidity_index(store)
                print(
                    f"        [月度维护] 第 {index} 天：僵化指数 {score}/100"
                    f"（旧偏好 {parts['旧偏好占比']:.0%}，旧事实 {parts['旧事实占比']:.0%}）"
                    "  → 该做一次环境重置了"
                )

            for key, want in (day.get("expect") or {}).items():
                checked += 1
                got = weights.get(key)
                if got is None:
                    failures.append(f"第 {index} 天：{key} 不存在")
                elif abs(got - float(want)) > 0.05:
                    failures.append(f"第 {index} 天：{key} 期望 {float(want):.1f}，实际 {got:.1f}")

        pending = len(store.pending_flips())
        for key, want in final_expect.items():
            checked += 1
            if key == "pending_flips" and pending != int(want):
                failures.append(f"结束时 pending_flips 期望 {want}，实际 {pending}")
    finally:
        if store.pending_flips():
            print("—" * 72)
            print(f"待确认的极性翻转：{len(store.pending_flips())} 条（用 flip-review 查看）")
        print(f"模拟结束：{current_dt():%Y-%m-%d}   共 {len(scenario)} 天")
        _VIRTUAL_NOW = None

    if failures:
        print(f"断言失败 {len(failures)}/{checked}：")
        for item in failures:
            print("  - " + item)
        raise SystemExit(1)
    print(f"断言通过：{checked} 条")


def cmd_export(args) -> None:
    home = Path(args.home)
    if not (home / DB_NAME).exists():
        print("没有可导出的数据（先 init 并对话）。")
        raise SystemExit(1)
    persona = load_persona_layers(home)
    db_bytes = (home / DB_NAME).read_bytes()
    digest = hashlib.sha256(db_bytes).hexdigest()
    out = Path(args.out) if args.out else Path(
        f"persona-{persona['id'][:8]}-{current_dt():%Y%m%d}.syspersona"
    )

    files: list[str] = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        directory = _layers_dir(home)
        if directory.is_dir():
            for name in (KERNEL_FILE, TRAITS_FILE, STATE_FILE):
                source = directory / name
                if source.exists():
                    bundle.writestr(f"{PERSONA_DIR}/{name}", source.read_bytes())
                    files.append(f"{PERSONA_DIR}/{name}")
        else:
            bundle.writestr(PERSONA_NAME, (home / PERSONA_NAME).read_bytes())
            files.append(PERSONA_NAME)
        bundle.writestr(DB_NAME, db_bytes)
        files.append(DB_NAME)
        if (home / CONFIG_NAME).exists():
            bundle.writestr(CONFIG_NAME, (home / CONFIG_NAME).read_bytes())
            files.append(CONFIG_NAME)
        manifest = {
            "schema": "syspersona/bundle@1",
            "persona_id": persona["id"],
            "persona_version": persona["version"],
            "exported_at": now(),
            "db_sha256": digest,
            "files": files,
        }
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    print(f"已导出：{out}（{out.stat().st_size / 1024:.1f} KB）")
    print(f"  persona {persona['id'][:8]} v{persona['version']}   sha256 {digest[:16]}…")
    print("  注意：这是明文包，跨机移动前请自行加密或放在可信介质上。")


def _read_bundle(path: Path) -> tuple[dict, dict, str]:
    with zipfile.ZipFile(path) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        data = {name: bundle.read(name) for name in bundle.namelist() if name != "manifest.json"}
    digest = hashlib.sha256(data[DB_NAME]).hexdigest()
    return manifest, data, digest


def cmd_import(args) -> None:
    manifest, data, digest = _read_bundle(Path(args.file))
    if digest != manifest["db_sha256"]:
        print("校验失败：memory.db 与清单不一致，拒绝导入。")
        raise SystemExit(1)

    home = Path(args.home)
    if (home / DB_NAME).exists() and not args.force:
        print(f"{home} 已有数据；确需覆盖请加 --force。")
        raise SystemExit(1)
    home.mkdir(parents=True, exist_ok=True)
    for name, blob in data.items():
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    print(f"已导入到：{home}")
    print(f"  persona {manifest['persona_id'][:8]} v{manifest['persona_version']}   sha256 {digest[:16]}…")


def cmd_verify(args) -> None:
    manifest, _, digest = _read_bundle(Path(args.file))
    ok = digest == manifest["db_sha256"]
    print(("校验通过" if ok else "校验失败") + f"：{args.file}")
    print(
        f"  persona {manifest['persona_id'][:8]} v{manifest['persona_version']}"
        f"   导出于 {manifest['exported_at']}"
    )
    if not ok:
        raise SystemExit(1)


def cmd_prune(args) -> None:
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    evicted = evict_by_pressure(store, persona)
    lines = cleanup_episodes(store, persona)
    if not evicted and not lines:
        print("无需清理。")
        return
    print(f"实时归档：{evicted} 条")
    for line in lines:
        print(line)


def cmd_layers(args) -> None:
    home = Path(args.home)
    directory = _layers_dir(home)
    if not directory.is_dir():
        print("当前是单文件人格（persona.json），没有分层。")
        return
    specs = [
        (KERNEL_FILE, "0 内核", "只有人"),
        (TRAITS_FILE, "1 性格", "睡眠结算"),
        (STATE_FILE, "2 状态", "每轮对话"),
    ]
    print(f"{'文件':<14}{'层级':<10}{'可写':<10}{'大小'}")
    for name, level, writer in specs:
        path = directory / name
        size = f"{path.stat().st_size} B" if path.exists() else "缺失"
        print(f"{name:<14}{level:<10}{writer:<10}{size}")
    persona = load_persona_layers(home)
    print(f"合并后：{persona['kernel']['identity']['name']} v{persona.get('version')}")


def cmd_embed(args) -> None:
    """向量索引维护：status / reindex / prune。"""
    store = Store(Path(args.home))
    cfg = store.load_config()
    provider = _embedding_provider(cfg)
    if provider is None:
        print("没有可用的 embedding provider。")
        raise SystemExit(1)
    from m2.embed import index as embed_index
    from m2.embed import store as embed_store

    embed_store.ensure_schema(store.db)
    if args.action == "status":
        info = embed_store.coverage(store.db, provider.model)
        print(f"provider：{provider.id}  模型：{provider.model}  语义：{'是' if provider.is_semantic else '否（哈希基线）'}")
        print(f"已索引：{info['indexed']}/{info['episodes']}（{info['coverage']:.0%}）")
        for row in info["models"]:
            print(f"  {row['model']}  {row['count']} 条  dim={row['dim']}")
        return
    if args.action == "reindex":
        if args.all:
            count = embed_index.reindex_all(store.db, provider, provider.model)
        else:
            count = embed_index.reindex_missing(store.db, provider, provider.model, limit=args.limit)
        print(f"已重建 {count} 条向量（模型 {provider.model}）")
        return
    removed = embed_store.prune(store.db)
    print(f"已清理 {removed} 条失效向量")


def cmd_eval(args) -> None:
    """检索评测：recall@k 与 MRR，词面 vs 混合。"""
    store = Store(Path(args.home))
    persona = store.load_persona()
    spec = json.loads(Path(args.queries).read_text(encoding="utf-8-sig"))
    queries = spec.get("queries") or []
    if not queries:
        print("评测集为空。")
        raise SystemExit(1)
    scores = entity_scores(store)
    hits = 0
    mrr = 0.0
    latency = 0.0
    rows = []
    for item in queries:
        text = item["q"]
        expect = item.get("expect") or []
        start = time.perf_counter()
        found = retrieve(store, text, k=args.k, scores=scores, persona=persona, mode=args.retrieval)
        latency += (time.perf_counter() - start) * 1000
        rank = None
        for index, (_score, row) in enumerate(found, 1):
            if any(term in row["content"] for term in expect):
                rank = index
                break
        if rank:
            hits += 1
            mrr += 1.0 / rank
        rows.append({"q": text, "rank": rank, "expect": expect})
    stats = {
        "queries": len(queries),
        "k": args.k,
        "recall": hits / len(queries),
        "mrr": mrr / len(queries),
        "avg_ms": round(latency / len(queries), 1),
        "mode": args.retrieval or "auto",
    }
    if args.json:
        print(json.dumps({"stats": stats, "rows": rows}, ensure_ascii=False, indent=2))
        return
    for row in rows:
        print(f"{'✓' if row['rank'] else '×'} rank={row['rank'] or '-'}  {row['q']}")
    print(f"recall@{args.k}={stats['recall']:.2f}  MRR={stats['mrr']:.2f}  "
          f"平均 {stats['avg_ms']} ms  模式={stats['mode']}")


def cmd_status(args) -> None:
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    cfg = store.load_config()
    counts = store.counts()
    mood, _ = store.load_state()
    print(f"人格：{persona['kernel']['identity']['name']}  v{persona['version']}  id={persona['id'][:8]}")
    print(f"模型：{cfg.get('model')}   会话：{cfg.get('session_id', '')[:8]}")
    print(f"情绪：{mood_text(mood)}")
    print(f"记忆：{counts['episodes']} 条对话，{counts['facts']} 条事实，{counts['unconsolidated']} 条待归档")
    topics, resumed_at = store.load_resume()
    if topics:
        print(f"恢复指针：{'、'.join(topics)}（{resumed_at}）")
    if store.archived_count():
        print(f"已归档：{store.archived_count()} 条（原文已压成摘要，仍可弱召回）")
    scores = entity_scores(store)
    if scores:
        top = sorted(scores.items(), key=lambda item: -item[1])[:5]
        print("画像图：" + "、".join(f"{name} {value:.2f}" for name, value in top))
    score, parts = rigidity_index(store)
    print(
        f"僵化：{score}/100（旧偏好 {parts['旧偏好占比']:.0%}，旧事实 {parts['旧事实占比']:.0%}）"
    )
    profile = store.get_profile()
    if profile:
        print(f"画像：{profile}")
    traits = store.traits()
    if traits:
        for t in traits:
            up, down = store.weekly_drift(t["trait_id"])
            effective = float(t["weight"]) + stress_value(t)
            stress = stress_value(t)
            extra = f"，应激 {stress:+.1f}" if abs(stress) >= 0.1 else ""
            print(
                f"偏好：{t['target']} [{t['polarity']}]  {effective:.1f}"
                f"（基线 {t['baseline']:.1f}{extra}，本周 上行 {up:.1f}/5 下行 {down:.1f}/5）"
            )
        pending = store.pending_flips()
        if pending:
            print(f"待确认的翻转请求：{len(pending)} 条（用 flip-review 查看）")
        last = store.db.execute(
            "SELECT * FROM persona_drift_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            print(f"最近漂移：{last['trait_id']} {last['old_weight']:.1f}→{last['new_weight']:.1f}"
                  f"（{last['reason']}）")


def _load_vault_main():
    """懒加载 M4：缺少可选依赖时只影响 vault 命令。"""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from m4.vault.cli import main as vault_main
    except ImportError as exc:
        print(f"E_UNSUPPORTED: 无法加载 m4.vault（{exc}）")
        raise SystemExit(1)
    return vault_main


def cmd_vault(args) -> None:
    raise SystemExit(_load_vault_main()(list(args.vault_args)))


def main() -> None:
    # `syspersona vault ...` 直接转发，避免 REMAINDER 吞掉 `vault --help`
    if len(sys.argv) > 1 and sys.argv[1] == "vault":
        raise SystemExit(_load_vault_main()(sys.argv[2:]))

    parser = argparse.ArgumentParser(prog="syspersona", description="SysPersona M1")
    parser.add_argument("--home", default="./data", help="数据目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化人格与数据库")
    p_init.add_argument("--persona", required=True, help="persona JSON 路径或分层目录")
    p_init.add_argument("--profile", choices=list(PROFILES), help="按硬件档位套用阈值预设")
    p_init.set_defaults(func=cmd_init)

    p_chat = sub.add_parser("chat", help="对话（不带 --say 进入交互）")
    p_chat.add_argument("--say", help="非交互模式，直接给一句输入")
    p_chat.add_argument("--retrieval", choices=["auto", "lexical", "hybrid"], help="检索模式")
    p_chat.set_defaults(func=cmd_chat)

    p_sleep = sub.add_parser("sleep", help="归档：摘要、抽取事实、快照")
    p_sleep.set_defaults(func=cmd_sleep)

    p_switch = sub.add_parser("switch-model", help="切换模型")
    p_switch.add_argument("--model", required=True)
    p_switch.set_defaults(func=cmd_switch_model)

    p_status = sub.add_parser("status", help="查看状态")
    p_status.set_defaults(func=cmd_status)

    p_recall = sub.add_parser("recall", help="检索记忆（调试用）")
    p_recall.add_argument("--query", required=True)
    p_recall.add_argument("--top", type=int, default=5)
    p_recall.add_argument("--retrieval", choices=["auto", "lexical", "hybrid"], help="检索模式")
    p_recall.set_defaults(func=cmd_recall)

    p_flip = sub.add_parser("flip-review", help="查看或确认极性翻转请求")
    g = p_flip.add_mutually_exclusive_group()
    g.add_argument("--approve", type=int, metavar="ID")
    g.add_argument("--reject", type=int, metavar="ID")
    p_flip.set_defaults(func=cmd_flip_review)

    p_send = sub.add_parser("send", help="向常驻进程投递一句话")
    p_send.add_argument("--say", required=True)
    p_send.set_defaults(func=cmd_send)

    p_serve = sub.add_parser("serve", help="常驻：轮询 inbox，空闲自动睡眠")
    p_serve.add_argument("--idle-seconds", type=int, default=300)
    p_serve.add_argument("--tick", type=float, default=1.0)
    p_serve.set_defaults(func=cmd_serve)

    p_sim = sub.add_parser("simulate", help="虚拟时钟：几秒跑完数周")
    p_sim.add_argument("--scenario", help="场景 JSON（默认内置 14 天）")
    p_sim.add_argument("--retrieval", choices=["auto", "lexical", "hybrid"], default="lexical")
    p_sim.set_defaults(func=cmd_simulate)

    p_embed = sub.add_parser("embed", help="向量索引维护（M2）")
    p_embed.add_argument("action", choices=["status", "reindex", "prune"])
    p_embed.add_argument("--all", action="store_true", help="全量重建（默认只补缺失）")
    p_embed.add_argument("--limit", type=int, default=1000)
    p_embed.set_defaults(func=cmd_embed)

    p_eval = sub.add_parser("eval", help="检索评测：recall@k / MRR")
    p_eval.add_argument("--queries", required=True)
    p_eval.add_argument("--k", type=int, default=5)
    p_eval.add_argument("--retrieval", choices=["auto", "lexical", "hybrid"])
    p_eval.add_argument("--json", action="store_true")
    p_eval.set_defaults(func=cmd_eval)

    p_export = sub.add_parser("export", help="把人格与记忆打包成 .syspersona")
    p_export.add_argument("--out", help="输出文件（默认 persona-<id>-<date>.syspersona）")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="从 .syspersona 恢复到 --home")
    p_import.add_argument("--file", required=True)
    p_import.add_argument("--force", action="store_true", help="覆盖已有数据")
    p_import.set_defaults(func=cmd_import)

    p_verify = sub.add_parser("verify", help="校验 .syspersona 的完整性")
    p_verify.add_argument("--file", required=True)
    p_verify.set_defaults(func=cmd_verify)

    p_prune = sub.add_parser("prune", help="立即执行压力驱逐与过期清理")
    p_prune.set_defaults(func=cmd_prune)

    p_layers = sub.add_parser("layers", help="查看人格分层文件与写权限")
    p_layers.set_defaults(func=cmd_layers)

    p_vault = sub.add_parser("vault", help="加密容器（M4；需要 cryptography 可选依赖）", add_help=False)
    p_vault.add_argument("vault_args", nargs=argparse.REMAINDER)
    p_vault.set_defaults(func=cmd_vault)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
