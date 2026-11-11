#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：SysPersona Launcher — L2 最小实现（PoC）
"""
SysPersona Launcher — L2 最小实现（PoC）

证明 docs/LAUNCHER_SPEC.md 的 L2 闭环可行：
  加载人格 → 预算内检索记忆 → 尾部注入 → 转发模型 → 异步落库 → 睡眠固化

零第三方依赖（标准库 + SQLite FTS5），单进程、单 worker、单写入者。
不包含（属 L3/L4）：加密容器、唯一在场、多设备租约、局域网鉴权。

用法：
  python launcher.py --home ./data --port 8000 --provider mock
  python launcher.py --home ./data --port 8000 --provider ollama --model qwen2.5:1.5b
"""

import argparse
import hashlib
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SAFETY_TOKENS = 128
MEMORY_RATIO = 0.22
TIER_CTX = {"low": 4096, "mid": 8192, "high": 16384}
STOPWORDS = {"就是", "这个", "那个", "我们", "你们", "他们", "什么", "怎么", "可以", "还是",
             "因为", "所以", "但是", "然后", "现在", "今天", "昨天", "一下", "一个"}


# ---------------------------------------------------------------- 基础设施

def now_ts():
    return time.time()


def iso_date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def estimate_tokens(text):
    """CJK 1.0 / ASCII 0.25 / 空白 0.15 —— 不引 tokenizer，靠真实用量自校准。"""
    cjk = ascii_chars = spaces = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
        elif ch.isspace():
            spaces += 1
        elif ch.isascii():
            ascii_chars += 1
    return int(cjk + ascii_chars * 0.25 + spaces * 0.15) + 1


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def keywords(text, k=5):
    toks = re.findall(r"[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z0-9_.+-]{2,}", text)
    cnt = Counter(t for t in toks if t not in STOPWORDS)
    return [w for w, _ in cnt.most_common(k)]


def importance_of(text):
    score = 0.2 + min(len(text), 400) / 1000.0
    if "?" in text or "？" in text:
        score += 0.15
    if re.search(r"答应|保证|一定|记得|别忘|明天|今晚|下周", text):
        score += 0.35
    if re.search(r"不要|禁止|不能|必须|绝不", text):
        score += 0.3
    return round(min(score, 1.0), 3)


CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
WORD_RUN = re.compile(r"[A-Za-z0-9_]+")


def seg(text):
    """FTS5 的 unicode61 不切中文，这里把 CJK 转成双字 gram，查询同法，等价于简易分词。"""
    out, pos = [], 0
    for m in CJK_RUN.finditer(text):
        out.extend(w.lower() for w in WORD_RUN.findall(text[pos:m.start()]))
        run = m.group()
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
        pos = m.end()
    out.extend(w.lower() for w in WORD_RUN.findall(text[pos:]))
    return " ".join(out)


# ---------------------------------------------------------------- 人格

DEFAULT_KERNEL = {
    "id": "lin",
    "name": "林",
    "version": "1.0.0",
    "identity": {"name": "林", "self_ref": "我", "user_ref": "你", "relation": "长期协作的伙伴"},
    "values": ["诚实 > 顺从", "先给结论，再给依据"],
    "boundaries": ["不替用户做不可逆的决定", "不伪造记忆，不假装在场"],
}
DEFAULT_TRAITS = {
    "preferences": [
        {"target": "机械键盘", "polarity": 1, "weight": 80, "evidence": 2},
        {"target": "无缩进代码", "polarity": -1, "weight": 95, "evidence": 3},
    ],
    "reflexes": [{"trigger": "被否定", "reaction": "先解释再让步", "strength": 60}],
    "voice": {"sentence": "短句", "avoid": ["堆叠感叹号"]},
    "evolution": {"max_step": 2, "weekly_cap": 5, "min_evidence": 3},
}
DEFAULT_STATE = {"emotion": {"label": "平静", "value": 0}, "focus": "", "updated": 0}


class Persona:
    def __init__(self, home):
        self.dir = os.path.join(home, "persona")
        os.makedirs(self.dir, exist_ok=True)
        self.kernel = self._load("kernel", DEFAULT_KERNEL)
        self.traits = self._load("traits", DEFAULT_TRAITS)
        self.state = self._load("state", DEFAULT_STATE)
        self.system_text = self._render()
        self.version = str(self.kernel.get("version", "0"))
        self.hash = sha(self.system_text)

    def _load(self, name, default):
        path = os.path.join(self.dir, name + ".json")
        if not os.path.exists(path):
            self._save(name, default)
            return dict(default)
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, name, obj):
        tmp = os.path.join(self.dir, name + ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, sort_keys=True, indent=2)
        os.replace(tmp, os.path.join(self.dir, name + ".json"))

    def _render(self):
        """字节稳定：排序键 + 紧凑分隔符 + 无时间戳。这是缓存命中的前提。"""
        return ("# 人格内核\n" + canonical(self.kernel) +
                "\n# 性格结构\n" + canonical(self.traits))

    def state_block(self, resume):
        emo = self.state.get("emotion", {})
        parts = ["<state>", "情绪：%s %+d" % (emo.get("label", "平静"), int(emo.get("value", 0)))]
        if self.state.get("focus"):
            parts.append("当前焦点：%s" % self.state["focus"])
        if resume:
            parts.append("上次在聊：%s" % resume.get("last_user", "")[:60])
            if resume.get("open_items"):
                parts.append("未完成：%s" % "；".join(resume["open_items"])[:80])
        parts.append("</state>")
        return "\n".join(parts)


# ---------------------------------------------------------------- 记忆

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT UNIQUE, persona_id TEXT, session TEXT, ts REAL,
  role TEXT, text TEXT, summary TEXT, tags TEXT,
  importance REAL DEFAULT 0.2, consolidated INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_ep_ts ON episodes(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(text, tags, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id TEXT, k TEXT, v TEXT, ts REAL,
  confidence REAL, source INTEGER);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Memory:
    def __init__(self, home, persona_id):
        self.path = os.path.join(home, "memory.db")
        self.persona_id = persona_id
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def write(self, ep):
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO episodes(uuid,persona_id,session,ts,role,text,summary,tags,importance,consolidated,deleted)"
                " VALUES(?,?,?,?,?,?,?,?,?,0,0)",
                (ep["uuid"], self.persona_id, ep["session"], ep["ts"], ep["role"],
                 ep["text"], "", ep["tags"], ep["importance"]))
            if cur.lastrowid:
                self.db.execute("INSERT INTO mem_fts(rowid,text,tags) VALUES(?,?,?)",
                                (cur.lastrowid, seg(ep["text"]), seg(ep["tags"])))
            self.db.commit()
            return cur.lastrowid

    def recent(self, n=6):
        with self.lock:
            rows = self.db.execute(
                "SELECT role,text,ts FROM episodes WHERE deleted=0 ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [{"role": r, "text": t, "ts": ts} for r, t, ts in reversed(rows)]

    def search(self, query, limit=200):
        toks, seen = [], set()
        for t in seg(query).split():
            if t in STOPWORDS or t in seen:
                continue
            seen.add(t)
            toks.append(t)
        if not toks:
            return []
        match = " OR ".join('"%s"' % t for t in toks[:24])
        with self.lock:
            try:
                rows = self.db.execute(
                    "SELECT e.id,e.ts,e.role,e.text,e.importance "
                    "FROM mem_fts JOIN episodes e ON e.id=mem_fts.rowid "
                    "WHERE mem_fts MATCH ? AND e.deleted=0 LIMIT ?",
                    (match, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        out = []
        now = now_ts()
        prefs = [p.get("target", "") for p in self._traits_targets()]
        for eid, ts, role, text, imp in rows:
            hits = sum(1 for t in toks if t in text)
            if not hits:
                continue
            rel = hits / (hits + 2.0)
            age_days = max(0.0, (now - ts) / 86400.0)
            attention = math.exp(-age_days / 2.0)
            profile = imp
            persona = 0.0
            if prefs:
                hit = sum(1 for p in prefs if p and p in text)
                persona = min(1.0, hit / max(1, len(prefs)))
            score = rel * (1 + 0.25 * profile + 0.15 * attention + 0.15 * persona)
            out.append({"id": eid, "ts": ts, "role": role, "text": text,
                        "importance": imp, "score": score,
                        "type": "episode", "attention": attention})
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    def _traits_targets(self):
        try:
            with open(os.path.join(os.path.dirname(self.path), "persona", "traits.json"), encoding="utf-8") as f:
                return json.load(f).get("preferences", [])
        except Exception:
            return []

    def pending(self):
        with self.lock:
            return self.db.execute(
                "SELECT id,uuid,ts,role,text,importance FROM episodes WHERE consolidated=0 AND deleted=0 ORDER BY id").fetchall()

    def consolidate(self, eid, summary, tags):
        with self.lock:
            self.db.execute("UPDATE episodes SET consolidated=1, summary=?, tags=? WHERE id=?",
                            (summary, tags, eid))
            self.db.commit()

    def count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM episodes WHERE deleted=0").fetchone()[0]

    def snapshot(self, dest):
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if os.path.exists(dest):
                os.remove(dest)
            self.db.execute("VACUUM INTO ?", (dest,))
            return os.path.getsize(dest)

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


class Writer(threading.Thread):
    """单写入者：热路径只入队，批量提交。"""

    def __init__(self, mem, batch=20, interval=0.2):
        super().__init__(daemon=True)
        self.mem = mem
        self.q = queue.Queue(maxsize=256)
        self.batch, self.interval = batch, interval
        self.stop_flag = threading.Event()

    def run(self):
        buf = []
        while not self.stop_flag.is_set():
            try:
                buf.append(self.q.get(timeout=self.interval))
            except queue.Empty:
                pass
            while len(buf) < self.batch:
                try:
                    buf.append(self.q.get_nowait())
                except queue.Empty:
                    break
            for ep in buf:
                try:
                    self.mem.write(ep)
                except Exception as e:
                    sys.stderr.write("[writer] %s\n" % e)
            buf = []

    def put(self, ep):
        try:
            self.q.put_nowait(ep)
        except queue.Full:
            self.mem.write(ep)

    def flush(self, timeout=5.0):
        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            time.sleep(0.02)


# ---------------------------------------------------------------- 模型

class MockProvider:
    """确定性 mock：把注入结果回显，用来断言"记忆确实进了 prompt"。"""

    def __init__(self, model="mock-a"):
        self.model = model
        self.calls = 0
        self._last_prefix = ""
        self.lock = threading.Lock()

    def chat(self, messages, max_tokens, ctx):
        with self.lock:
            self.calls += 1
            prefix = messages[0]["content"] + "\n" + "\n".join(
                m["content"] for m in messages[1:-1])
            cached = estimate_tokens(self._last_prefix) if prefix.startswith(self._last_prefix) and self._last_prefix else 0
            self._last_prefix = prefix
        last = messages[-1]["content"]
        mem = ctx.get("first_memory") or "（无）"
        kw = "、".join(ctx.get("keywords") or []) or "无"
        text = ("【mock:%s】人格=%s；记忆命中=%d；关键词=%s；我看到的记忆：%s"
                % (self.model, ctx.get("persona_id"), ctx.get("memory_hits", 0), kw, mem))
        return {"text": text, "cached_tokens": cached, "eval_count": estimate_tokens(text)}


class OllamaProvider:
    def __init__(self, model="qwen2.5:1.5b", host="http://127.0.0.1:11434"):
        self.model, self.host, self.calls = model, host, 0

    def chat(self, messages, max_tokens, ctx):
        self.calls += 1
        body = json.dumps({"model": self.model, "messages": messages, "stream": False,
                           "keep_alive": "30m",
                           "options": {"num_ctx": ctx["num_ctx"], "num_predict": max_tokens}}).encode()
        req = urllib.request.Request(self.host + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        return {"text": data.get("message", {}).get("content", ""),
                "cached_tokens": data.get("prompt_eval_cached_count", 0),
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "eval_count": data.get("eval_count", 0)}


# ---------------------------------------------------------------- 主体

class Launcher:
    def __init__(self, home, provider="mock", model=None, num_ctx=None, tier="low", verbose=False):
        self.home = os.path.abspath(home)
        self.verbose = verbose
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(os.path.join(self.home, "snapshots"), exist_ok=True)
        self.persona = Persona(self.home)
        self._persona_tokens = estimate_tokens(self.persona.system_text)
        self.mem = Memory(self.home, self.persona.kernel.get("id", "lin"))
        self.writer = Writer(self.mem)
        self.writer.start()
        self.tier = tier
        self.num_ctx = num_ctx or TIER_CTX.get(tier, 4096)
        if provider == "mock":
            self.provider = MockProvider(model or "mock-a")
        else:
            self.provider = OllamaProvider(model or "qwen2.5:1.5b")
        self.lock = threading.Lock()
        self.model_calls = 0
        self.last_ratio = 0.0
        self.last_timing = {}
        self.recovered = False
        self.ready = True
        self.started = now_ts()
        self.resume = self._load_resume()
        self._recover()
        if self.verbose:
            sys.stderr.write("[launcher] home=%s persona=%s hash=%s ctx=%d\n"
                             % (self.home, self.persona.kernel.get("id"), self.persona.hash, self.num_ctx))

    # -- 生命周期 -------------------------------------------------
    def _journal_path(self):
        return os.path.join(self.home, "journal.state")

    def _journal(self, state=None):
        p = self._journal_path()
        if state is None:
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read().strip()
            except FileNotFoundError:
                return "clean"
        with open(p, "w", encoding="utf-8") as f:
            f.write(state)

    def _load_resume(self):
        p = os.path.join(self.home, "resume.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _recover(self):
        if self._journal() == "sleeping":
            self.recovered = True
            with self.mem.lock:
                self.mem.db.execute("UPDATE episodes SET consolidated=0")
                self.mem.db.commit()
            self._journal("clean")
            if self.verbose:
                sys.stderr.write("[launcher] recovered from dirty sleep; episodes replayed\n")

    def sleep(self):
        """固化：指针 → 抽取 → 快照。顺序不可换（指针必须最先写）。"""
        t0 = time.time()
        self._journal("sleeping")
        recent = self.mem.recent(6)
        last_user = next((r["text"] for r in reversed(recent) if r["role"] == "user"), "")
        open_items = [kw for kw in keywords(last_user, 3)]
        self.resume = {"ts": now_ts(), "last_user": last_user[:200],
                       "open_items": open_items, "session": "default"}
        tmp = os.path.join(self.home, "resume.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.resume, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(self.home, "resume.json"))

        stats = {"episodes": 0, "tags": 0}
        for eid, _uuid, ts, role, text, imp in self.mem.pending():
            tags = ",".join(keywords(text))
            summary = text[:60].replace("\n", " ")
            self.mem.consolidate(eid, summary, tags)
            stats["episodes"] += 1
            stats["tags"] += len(tags.split(",")) if tags else 0

        snap = os.path.join(self.home, "snapshots", "mem-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        size = self.mem.snapshot(snap)
        self._journal("clean")
        snaps = sorted(os.listdir(os.path.join(self.home, "snapshots")))
        for old in snaps[:-5]:
            try:
                os.remove(os.path.join(self.home, "snapshots", old))
            except OSError:
                pass
        return {"ok": True, "consolidated": stats["episodes"], "snapshot": os.path.basename(snap),
                "snapshot_bytes": size, "elapsed_ms": round((time.time() - t0) * 1000, 1)}

    # -- 请求 -----------------------------------------------------
    def _budget(self, max_tokens):
        budget_in = self.num_ctx - max_tokens - SAFETY_TOKENS
        persona_tokens = self._persona_tokens
        state_tokens = estimate_tokens(self.persona.state_block(self.resume))
        memory_budget = int(budget_in * MEMORY_RATIO)
        history_budget = budget_in - persona_tokens - state_tokens - memory_budget
        return {"budget_in": budget_in, "memory": max(0, memory_budget),
                "history": max(0, history_budget), "persona": persona_tokens}

    def _memory_block(self, query, budget, counter):
        items = self.mem.search(query, limit=100)[:32]
        lines, used = [], 0
        for it in items:
            line = "[M%d|%s|%s] %s" % (it["id"], iso_date(it["ts"]), it["type"],
                                       it["text"][:120].replace("\n", " "))
            t = estimate_tokens(line)
            if used + t > budget:
                counter["truncated"] = True
                break
            lines.append(line)
            used += t
        block = ""
        if lines:
            block = "<memory>\n" + "\n".join(lines) + "\n</memory>"
        return block, used, items[:len(lines)]

    def build(self, messages, max_tokens, bypass=False):
        t0 = time.time()
        b = self._budget(max_tokens)
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]["content"] if user_msgs else ""
        if estimate_tokens(last_user) > self.num_ctx * 0.4:
            raise OverBudget("单条输入超过 num_ctx 的 40%%（%d tokens）" % estimate_tokens(last_user))

        if bypass:
            return list(messages), {"tokens": estimate_tokens("\n".join(m.get("content", "") for m in messages)),
                                    "memory_hits": 0, "memory_tokens": 0, "truncated": "",
                                    "timing": {"retrieve_ms": 0.0}, "first_memory": "",
                                    "keywords": [], "budget": b}

        counter = {"truncated": False}
        mem_block, mem_tokens, items = self._memory_block(last_user, b["memory"], counter)
        state_block = self.persona.state_block(self.resume)

        history = messages[:-1] if messages and messages[-1].get("role") == "user" else messages
        kept, used = [], 0
        for m in reversed(history):
            t = estimate_tokens(m.get("content", ""))
            if used + t > b["history"]:
                counter["truncated"] = True
                break
            kept.append(m)
            used += t
        kept.reverse()

        tail = "\n\n" + state_block
        if mem_block:
            tail += "\n" + mem_block + "\n以上为背景记忆，仅在相关时使用；不要复述编号。"
        final = [{"role": "system", "content": self.persona.system_text}] + kept + \
                [{"role": "user", "content": last_user + tail}]

        # 硬预算：先丢最老的历史，再丢记忆尾块；仍超则拒绝，绝不静默溢出。
        while estimate_tokens("\n".join(m["content"] for m in final)) > b["budget_in"] and kept:
            kept.pop(0)
            counter["truncated"] = True
            final = [{"role": "system", "content": self.persona.system_text}] + kept + \
                    [{"role": "user", "content": last_user + tail}]
        if estimate_tokens("\n".join(m["content"] for m in final)) > b["budget_in"]:
            mem_block, mem_tokens, items = "", 0, []
            tail = "\n\n" + state_block
            final = [{"role": "system", "content": self.persona.system_text}] + kept + \
                    [{"role": "user", "content": last_user + tail}]
            counter["truncated"] = True
        if estimate_tokens("\n".join(m["content"] for m in final)) > b["budget_in"]:
            raise OverBudget("prompt 超出预算（persona+turns 已占满 num_ctx）")

        prompt_tokens = estimate_tokens("\n".join(m["content"] for m in final))
        timing = {"retrieve_ms": round((time.time() - t0) * 1000, 1)}
        first_line = ""
        if mem_block:
            parts = mem_block.split("\n")
            first_line = parts[1] if len(parts) > 1 else ""
        meta = {"tokens": prompt_tokens, "memory_hits": len(items), "memory_tokens": mem_tokens,
                "truncated": "memory" if counter["truncated"] else "", "timing": timing,
                "first_memory": first_line[:80],
                "keywords": keywords(last_user, 4), "budget": b}
        return final, meta

    def chat(self, payload, bypass=False):
        model = payload.get("model") or getattr(self.provider, "model", "mock-a")
        messages = payload.get("messages") or []
        if not messages:
            raise BadRequest("messages 不能为空")
        max_tokens = int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 512)
        max_tokens = max(32, min(max_tokens, 1024))
        t0 = time.time()
        final, meta = self.build(messages, max_tokens, bypass=bypass)
        ctx = {"persona_id": self.persona.kernel.get("id"), "num_ctx": self.num_ctx,
               "memory_hits": meta["memory_hits"], "first_memory": meta["first_memory"],
               "keywords": meta["keywords"]}
        try:
            out = self.provider.chat(final, max_tokens, ctx)
        except Exception as e:
            raise ModelUnavailable(str(e))
        self.model_calls += 1
        prompt_tokens = out.get("prompt_tokens") or meta["tokens"]
        cached = out.get("cached_tokens", 0)
        self.last_ratio = round(cached / max(1, prompt_tokens), 3)
        self.last_timing = dict(meta["timing"], upstream_ms=round((time.time() - t0) * 1000, 1))
        self._record(messages[-1].get("content", ""), out["text"], meta)
        return {
            "id": "chatcmpl-" + uuid_hex(),
            "object": "chat.completion",
            "created": int(now_ts()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": out["text"]},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens,
                      "completion_tokens": out.get("eval_count", estimate_tokens(out["text"])),
                      "total_tokens": prompt_tokens + out.get("eval_count", 0)},
            "_meta": {"memory_hits": meta["memory_hits"], "injected_tokens": meta["memory_tokens"],
                      "truncated": meta["truncated"], "ratio": self.last_ratio,
                      "timing": self.last_timing, "model_calls": self.model_calls},
        }

    def _record(self, user_text, reply, meta):
        ts = now_ts()
        self.writer.put({"uuid": uuid_hex(), "session": "default", "ts": ts, "role": "user",
                         "text": user_text, "tags": ",".join(keywords(user_text)),
                         "importance": importance_of(user_text)})
        self.writer.put({"uuid": uuid_hex(), "session": "default", "ts": ts + 0.001, "role": "assistant",
                         "text": reply, "tags": ",".join(keywords(reply)),
                         "importance": max(0.1, importance_of(reply) - 0.1)})
        self.persona.state["focus"] = "、".join(meta["keywords"][:2]) or self.persona.state.get("focus", "")
        self.persona.state["updated"] = ts

    def status(self):
        return {
            "ready": self.ready,
            "persona": {"id": self.persona.kernel.get("id"), "version": self.persona.version,
                        "hash": self.persona.hash},
            "tier": self.tier, "num_ctx": self.num_ctx,
            "model": getattr(self.provider, "model", "?"),
            "model_calls": self.model_calls,
            "episodes": self.mem.count(),
            "journal": self._journal(),
            "recovered": self.recovered,
            "cache_hit_ratio": self.last_ratio,
            "memory_ratio": MEMORY_RATIO,
            "timing": self.last_timing,
            "uptime_s": round(now_ts() - self.started, 1),
        }

    def search(self, q, k=10):
        return [{"id": it["id"], "date": iso_date(it["ts"]), "text": it["text"][:200],
                 "score": round(it["score"], 3), "importance": it["importance"]} for it in self.mem.search(q)[:k]]


# ---------------------------------------------------------------- 错误类型

class BadRequest(Exception):
    code, http = "bad_request", 400


class OverBudget(Exception):
    code, http = "budget_exceeded", 413


class ModelUnavailable(Exception):
    code, http = "model_unavailable", 503


def uuid_hex():
    import uuid
    return uuid.uuid4().hex[:24]


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SysPersonaLauncher/0.1"

    @property
    def L(self):
        return self.server.launcher

    def log_message(self, fmt, *args):
        if self.L.verbose:
            sys.stderr.write("[http] %s\n" % (fmt % args))

    def _json(self, code, obj, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, exc):
        self._json(getattr(exc, "http", 500),
                   {"error": {"message": str(exc), "type": "invalid_request_error",
                              "code": getattr(exc, "code", "internal_error")}})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/healthz":
            return self._json(200, {"ok": True})
        if u.path == "/readyz":
            return self._json(200 if self.L.ready else 503, {"ready": self.L.ready})
        if u.path == "/v1/models":
            return self._json(200, {"object": "list", "data": [
                {"id": "syspersona/" + getattr(self.L.provider, "model", "mock-a"),
                 "object": "model", "owned_by": "syspersona"}]})
        if u.path == "/syspersona/status":
            return self._json(200, self.L.status())
        if u.path == "/syspersona/persona":
            p = self.L.persona
            return self._json(200, {"kernel": p.kernel, "traits": p.traits, "state": p.state,
                                    "hash": p.hash, "version": p.version})
        if u.path == "/syspersona/memory/search":
            return self._json(200, {"query": q.get("q", [""])[0],
                                    "results": self.L.search(q.get("q", [""])[0], int(q.get("k", ["10"])[0]))})
        if u.path.startswith("/syspersona/presence"):
            return self._json(501, {"error": {"message": "唯一在场属 L4，本实现未启用",
                                              "type": "not_implemented", "code": "not_implemented"}})
        return self._json(404, {"error": {"message": "not found", "type": "invalid_request_error",
                                          "code": "not_found"}})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/v1/chat/completions":
                return self._chat()
            if u.path == "/syspersona/wake":
                return self._json(200, {"ok": True, "already": True, **self.L.status()})
            if u.path == "/syspersona/sleep":
                with self.L.lock:
                    res = self.L.sleep()
                return self._json(202, res)
            if u.path == "/syspersona/seal":
                return self._json(501, {"error": {"message": "seal 属 L4（vault），本实现未启用",
                                                  "type": "not_implemented", "code": "not_implemented"}})
            return self._json(404, {"error": {"message": "not found", "code": "not_found"}})
        except (BadRequest, OverBudget, ModelUnavailable) as e:
            return self._err(e)
        except json.JSONDecodeError as e:
            return self._json(400, {"error": {"message": "invalid json: %s" % e, "code": "bad_request"}})

    def _chat(self):
        payload = self._body()
        stream = bool(payload.get("stream"))
        bypass = (self.headers.get("X-SP-Bypass") or "") in ("1", "true", "yes")
        if not self.L.lock.acquire(timeout=30):
            return self._json(429, {"error": {"message": "busy", "type": "rate_limit",
                                              "code": "busy"}}, {"Retry-After": "5"})
        try:
            res = self.L.chat(payload, bypass=bypass)
        finally:
            self.L.lock.release()
        meta = res.pop("_meta")
        headers = {
            "X-SP-Persona": str(self.L.persona.kernel.get("id", "lin")),
            "X-SP-Persona-Version": self.L.persona.version,
            "X-SP-Memory-Hits": meta["memory_hits"],
            "X-SP-Injected-Tokens": meta["injected_tokens"],
            "X-SP-Cache-Hit-Ratio": meta["ratio"],
            "X-SP-Tier": self.L.tier,
            "X-SP-Truncated": meta["truncated"],
            "X-SP-Timing": json.dumps(meta["timing"], separators=(",", ":")),
        }
        if not stream:
            return self._json(200, res, headers)
        # SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        for k, v in headers.items():
            self.send_header(k, str(v))
        self.end_headers()
        text = res["choices"][0]["message"]["content"]
        base = {"id": res["id"], "object": "chat.completion.chunk", "created": res["created"],
                "model": res["model"]}
        chunks = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}])]
        step = max(1, len(text) // 6)
        for i in range(0, len(text), step):
            chunks.append(dict(base, choices=[{"index": 0, "delta": {"content": text[i:i + step]},
                                               "finish_reason": None}]))
        chunks.append(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                           usage=res["usage"]))
        try:
            for ev in chunks:
                data = ("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8")
                self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
                self.wfile.flush()
            end = b"data: [DONE]\n\n"
            self.wfile.write(("%X\r\n" % len(end)).encode() + end + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(home, host="127.0.0.1", port=8000, provider="mock", model=None,
          num_ctx=None, tier="low", verbose=False):
    L = Launcher(home, provider=provider, model=model, num_ctx=num_ctx, tier=tier, verbose=verbose)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.launcher = L
    return httpd, L


def main():
    ap = argparse.ArgumentParser(description="SysPersona Launcher (L2 PoC)")
    ap.add_argument("--home", default="./data")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--provider", choices=["mock", "ollama"], default="mock")
    ap.add_argument("--model", default=None)
    ap.add_argument("--num-ctx", type=int, default=None)
    ap.add_argument("--tier", choices=list(TIER_CTX), default="low")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--require-grant", action="store_true",
                    help="启动前校验物理设备授权（v2.2.0）")
    ap.add_argument("--persona-id", default="lin")
    a = ap.parse_args()

    if a.require_grant:
        from device_gate import check_grant, ensure_device
        ensure_device(a.home)
        ok, reason = check_grant(a.home, a.persona_id)
        if not ok:
            sys.stderr.write("E_PHYSICAL_UNAUTHORIZED: %s\n" % reason)
            sys.exit(3)
    httpd, L = serve(a.home, a.host, a.port, a.provider, a.model, a.num_ctx, a.tier, a.verbose)
    print("launcher ready: http://%s:%d  (persona=%s ctx=%d provider=%s)"
          % (a.host, a.port, L.persona.kernel.get("id"), L.num_ctx, a.provider))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down: flushing queue + sleep")
        L.writer.stop_flag.set()
        L.writer.flush()
        L.sleep()
        httpd.shutdown()


if __name__ == "__main__":
    main()
