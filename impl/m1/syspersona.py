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
import json
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_NAME = "memory.db"
PERSONA_NAME = "persona.json"
CONFIG_NAME = "config.json"

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
    consolidated INTEGER NOT NULL DEFAULT 0
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
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(home / DB_NAME)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---------- persona ----------
    def load_persona(self) -> dict:
        return json.loads((self.home / PERSONA_NAME).read_text(encoding="utf-8"))

    def save_persona(self, persona: dict) -> None:
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
        return json.loads(p.read_text(encoding="utf-8"))

    def save_config(self, cfg: dict) -> None:
        (self.home / CONFIG_NAME).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- episodes ----------
    def add_episode(self, session_id: str, role: str, content: str, importance: float = 0.5) -> int:
        cur = self.db.execute(
            "INSERT INTO episodes (session_id, role, content, importance, occurred_at)"
            " VALUES (?,?,?,?,?)",
            (session_id, role, content, importance, now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def recent_episodes(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def all_episodes(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
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
                " (trait_id, kind, target, polarity, weight, baseline, evidence, is_locked, updated_at)"
                " VALUES (?,?,?,?,?,?,0,?,?)",
                (
                    p["id"],
                    "preference",
                    p["target"],
                    p.get("polarity", "like"),
                    weight,
                    float(p.get("baseline", weight)),
                    int(bool(p.get("locked", False))),
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
            "UPDATE persona_traits SET weight=?, evidence=evidence+?, updated_at=? WHERE trait_id=?",
            (new_weight, count, now(), trait_id),
        )
        self.db.execute(
            "INSERT INTO persona_drift_log (trait_id, old_weight, new_weight, delta, evidence_count, reason, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (trait_id, new_weight - delta, new_weight, delta, count, reason, now()),
        )
        self.db.commit()

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

    def reply(self, persona: dict, facts: list[str], memories: list[str], message: str) -> str:
        who = persona["kernel"]["identity"]["name"]
        head = f"（{self.name} · {who}）"
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

    def reply(self, persona: dict, facts: list[str], memories: list[str], message: str) -> str:
        k = persona["kernel"]
        context = []
        if facts:
            context.append("已知事实：" + "；".join(facts))
        if memories:
            context.append("相关记忆：" + "；".join(memories))
        system = (
            f"你是{k['identity']['name']}。语气：{k['voice_baseline']['tone']}。"
            f"你的价值观：{'、'.join(k['values'])}。"
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


def _recency(ts: str, tau_days: float = 7.0) -> float:
    import math

    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return 0.5
    now_dt = datetime.now(timezone.utc).astimezone()
    age_days = max(0.0, (now_dt - then).total_seconds() / 86400.0)
    return math.exp(-age_days / tau_days)


def retrieve(store: "Store", query: str, k: int = 3) -> list[tuple[float, object]]:
    """混合打分：词面重合 0.6 + 时间近因 0.25 + 重要性 0.15。"""
    q = _tokens(query)
    if not q:
        return []
    scored: list[tuple[float, object]] = []
    for row in store.all_episodes():
        t = _tokens(row["content"])
        overlap = len(q & t) / len(q)
        if overlap == 0:
            continue
        score = 0.6 * overlap + 0.25 * _recency(row["occurred_at"]) + 0.15 * row["importance"]
        if row["role"] != "user":
            score *= 0.7          # 助手回复常复述用户内容，降权避免自我强化
        scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def snippet(text: str, n: int = 36) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n] + "…"


def _is_question(text: str) -> bool:
    return bool(re.search(r"[？?]", text)) or bool(
        re.search(r"什么|怎么|如何|为什么|哪(个|里|些)|吗\s*[。！!]?$", text)
    )


def apply_drift(store: "Store", persona: dict, episodes: list) -> list[str]:
    """按证据微调偏好权重：单次有上限，证据不足不动。"""
    ev = persona.get("evolution", {}) or {}
    min_evidence = int(ev.get("min_evidence", 3))
    max_step = float(ev.get("max_step", 2))
    user_msgs = [r["content"] for r in episodes if r["role"] == "user"]

    changes: list[str] = []
    for trait in store.traits():
        if trait["is_locked"]:
            continue
        hits = sum(1 for msg in user_msgs if _overlap(trait["target"], msg))
        if hits < min_evidence:
            continue
        delta = min(max_step, round(0.5 * hits, 1))
        new_weight = min(100.0, float(trait["weight"]) + delta)
        if new_weight == float(trait["weight"]):
            continue
        reason = f"{hits} 条记忆提及「{trait['target']}」"
        store.update_trait(trait["trait_id"], new_weight, new_weight - trait["weight"], hits, reason)
        changes.append(f"{trait['trait_id']} {trait['weight']:.0f} → {new_weight:.0f}（证据 {hits} 条）")
    return changes


# ---------------- commands ----------------

def cmd_init(args) -> None:
    home = Path(args.home)
    store = Store(home)
    src = Path(args.persona)
    persona = json.loads(src.read_text(encoding="utf-8"))
    store.save_persona(persona)
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

    hits = retrieve(store, message, k=3)
    memory_lines = [
        snippet(row["content"])
        for _, row in hits
        if row["role"] == "user" and not _is_question(row["content"])
    ]
    memory_lines = list(dict.fromkeys(memory_lines))[:2]

    facts_rows = store.search_facts(keywords_of(message), intent_predicates(message))
    fact_lines = [f"{r['predicate']}：{r['object']}" for r in facts_rows]

    store.add_episode(cfg["session_id"], "user", message)
    reply = model.reply(persona, fact_lines, memory_lines, message)
    store.add_episode(cfg["session_id"], "assistant", reply)

    print(reply)


def cmd_sleep(args) -> None:
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    rows = store.unconsolidated()
    if not rows:
        print("没有需要归档的对话。")
        return

    user_text = "\n".join(r["content"] for r in rows if r["role"] == "user")
    facts = extract_facts(user_text)
    for subject, predicate, obj in facts:
        store.add_fact(subject, predicate, obj, rows[-1]["id"])

    summary = "；".join(f"{p}：{o}" for _, p, o in facts) or "（本轮没有可提炼的事实）"
    store.set_profile(summary)
    store.mark_consolidated([r["id"] for r in rows])

    snap = home / "snapshots"
    snap.mkdir(exist_ok=True)
    name = f"pre_consolidate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
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
    print(f"归档完成：{len(rows)} 条对话 → {len(facts)} 条事实")
    if facts:
        print("  新事实：" + "；".join(f"{p}={o}" for _, p, o in facts))

    changes = apply_drift(store, persona, rows)
    if changes:
        print("  人格微调：" + "；".join(changes))
    else:
        print("  人格微调：无（证据不足，未达阈值）")


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
    hits = retrieve(store, args.query, k=args.top)
    if not hits:
        print("没有匹配的记忆。")
        return
    for score, row in hits:
        print(f"{score:.3f}  [{row['role']:<9}] {snippet(row['content'], 50)}")


def cmd_status(args) -> None:
    home = Path(args.home)
    store = Store(home)
    persona = store.load_persona()
    cfg = store.load_config()
    counts = store.counts()
    print(f"人格：{persona['kernel']['identity']['name']}  v{persona['version']}  id={persona['id'][:8]}")
    print(f"模型：{cfg.get('model')}   会话：{cfg.get('session_id', '')[:8]}")
    print(f"记忆：{counts['episodes']} 条对话，{counts['facts']} 条事实，{counts['unconsolidated']} 条待归档")
    profile = store.get_profile()
    if profile:
        print(f"画像：{profile}")
    traits = store.traits()
    if traits:
        print("偏好：" + "；".join(
            f"{t['target']} {t['weight']:.0f}（基线 {t['baseline']:.0f}）" for t in traits
        ))
        last = store.db.execute(
            "SELECT * FROM persona_drift_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            print(f"最近漂移：{last['trait_id']} {last['old_weight']:.0f}→{last['new_weight']:.0f}"
                  f"（{last['reason']}）")


def main() -> None:
    parser = argparse.ArgumentParser(prog="syspersona", description="SysPersona M1")
    parser.add_argument("--home", default="./data", help="数据目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化人格与数据库")
    p_init.add_argument("--persona", required=True, help="persona JSON 路径")
    p_init.set_defaults(func=cmd_init)

    p_chat = sub.add_parser("chat", help="对话（不带 --say 进入交互）")
    p_chat.add_argument("--say", help="非交互模式，直接给一句输入")
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
    p_recall.set_defaults(func=cmd_recall)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
