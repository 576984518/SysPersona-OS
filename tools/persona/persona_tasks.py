# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：任务池（v5.5 B81）：outbox + 合并器 + 三个本地接口。
"""任务池（v5.5 B81）：outbox + 合并器 + 三个本地接口。

规范：`docs/SCHEDULER_SPEC.md` §4.1（任务 JSON）/ §4.2（三接口）/ §3.1（合并规则）。
设计口径（不另立一份）：
  - 存储 `ops/tasks.jsonl` 只追加，同 id 以最后一行为准（和 `dispatch.jsonl` 同构）；
  - 离线时先落 `ops/outbox.jsonl`，`flush` 补交（判据：不丢）；
  - 认领用**文件锁 + 追加**，抢不到的拿 exit 1（判据：不重复）；
  - 超时改派交给 `persona_report.py reap`，本模块只在池子里标 expired（判据：不卡）；
  - 每条任务留 `source_ids` / `created_by` / `created_at`（判据：可回溯）。

用法：
  python persona_tasks.py submit --title "…" [--prio 3] [--cap python,text]
                                [--dep <id>] [--source-id <总结id>] [--by <avatar>] [--reject]
  python persona_tasks.py pool [--agent <avatar>] [--caps python,text]
  python persona_tasks.py show <id>
  python persona_tasks.py ack <id> --by <avatar>
  python persona_tasks.py done <id> [--note …]
  python persona_tasks.py flush
  python persona_tasks.py merge
  python persona_tasks.py serve [--port 8787]
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import threading
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.environ.get("PERSONA_OPS") or os.path.join(ROOT, "ops")
STORE = os.path.join(OPS, "tasks.jsonl")
OUTBOX = os.path.join(OPS, "outbox.jsonl")
ALERTS = os.path.join(OPS, "alerts.jsonl")

MAX_PRIO, MIN_PRIO = 10, 1
DEFAULT_PRIO = 5
JACCARD = 0.6          # 判"同一件事"的阈值（规范 §3.1 第 2 条，写死便于复核）


# ── 存储 ────────────────────────────────────────────────────────────
def _read(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in io.open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def latest():
    st = {}
    for r in _read(STORE):
        st[r.get("id")] = r
    return st


def _append(rec, path=STORE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = dict(rec)
    rec.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
    with io.open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _alert(level, kind, rec, note):
    _append({"kind": kind, "level": level, "id": rec.get("id", ""),
             "title": rec.get("title", ""), "note": note}, ALERTS)


# ── 任务记录 ────────────────────────────────────────────────────────
def _norm_title(s):
    return re.sub(r"[^\u4e00-\u9fffa-z0-9]+", "", (s or "").lower())


def _grams(s):
    s = _norm_title(s)
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def _jaccard(a, b):
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga | gb))


def make_id(title):
    day = time.strftime("%Y%m%d")
    h = hashlib.sha1(_norm_title(title).encode("utf-8")).hexdigest()[:6]
    return "t-%s-%s" % (day, h)


def normalize(rec):
    """字段归一：prio 夹到 1–10，cap/deps/source_ids 统一成 list。"""
    out = dict(rec)
    try:
        p = int(out.get("prio", DEFAULT_PRIO))
    except (TypeError, ValueError):
        p = DEFAULT_PRIO
    out["prio"] = max(MIN_PRIO, min(MAX_PRIO, p))
    for k in ("cap", "deps", "source_ids"):
        v = out.get(k) or []
        if isinstance(v, str):
            v = [x for x in re.split(r"[,，;；\s]+", v) if x]
        out[k] = list(v)
    out.setdefault("status", "pending")
    out.setdefault("assignee", "")
    out.setdefault("note", "")
    out.setdefault("reject", False)
    return out


def submit(title, prio=DEFAULT_PRIO, cap=None, deps=None, source_ids=None,
           by="", reject=False, offline=False, tid=None):
    """上交一条任务。同一件事（标题归一相同或词袋重叠 ≥0.6）不新建，只补来源。"""
    rec = normalize({"id": tid or make_id(title), "title": title, "prio": prio,
                     "cap": cap or [], "deps": deps or [], "source_ids": source_ids or [],
                     "source": "summary", "created_by": by,
                     "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "reject": bool(reject), "status": "pending"})

    if rec["reject"]:
        rec["status"] = "rejected"
        return _append(rec, OUTBOX if offline else STORE), "rejected"

    for old in latest().values():
        if old.get("status") in ("done", "rejected"):
            continue
        same = _norm_title(old.get("title")) == _norm_title(title) or \
            _jaccard(old.get("title"), title) >= JACCARD
        if same:
            merged = dict(old)
            merged["source_ids"] = sorted(set(old.get("source_ids", [])) | set(rec["source_ids"]))
            merged["note"] = (old.get("note", "") + " | 合并自 %s" % rec["id"]).strip(" |")
            merged["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return _append(merged, OUTBOX if offline else STORE), "merged"

    return _append(rec, OUTBOX if offline else STORE), "created"


def flush():
    """离线补交：outbox → 池子。返回补交条数。"""
    rows = _read(OUTBOX)
    if not rows:
        return 0
    for r in rows:
        _append(r, STORE)
    os.remove(OUTBOX)
    return len(rows)


# ── 排序与匹配 ──────────────────────────────────────────────────────
def sort_key(rec, done_ids=None):
    done_ids = done_ids or set()
    blocked = 1 if any(d not in done_ids for d in rec.get("deps", [])) else 0
    return (blocked, rec.get("prio", DEFAULT_PRIO), rec.get("created_at", ""))


def pool(agent="", caps=None):
    st = latest()
    done_ids = {k for k, r in st.items() if r.get("status") == "done"}
    rows = [r for r in st.values() if r.get("status") in ("pending", "claimed", "running")]
    if agent:
        have = set(c.strip().lower() for c in (caps or []))
        rows = [r for r in rows
                if (not agent or r.get("assignee", "") in ("", agent))
                and (not have or set(c.lower() for c in r.get("cap", [])) <= have)]
    return sorted(rows, key=lambda r: sort_key(r, done_ids))


# ── 认领（带锁，抢不到 exit 1）──────────────────────────────────────
class _Lock(object):
    def __init__(self, path, timeout=5.0):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        end = time.time() + self.timeout
        while True:
            try:
                self.fh = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except OSError:
                if time.time() > end:
                    raise SystemExit("任务池被占用超过 %.0fs" % self.timeout)
                time.sleep(0.02)

    def __exit__(self, *a):
        try:
            os.close(self.fh)
        except OSError:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass


def ack(tid, by, caps=None):
    """接手。返回 (ok, msg)：已被别人拿走 → (False, ...)，调用方用 exit 1。"""
    os.makedirs(OPS, exist_ok=True)
    with _Lock(os.path.join(OPS, "pool.lock")):
        rec = latest().get(tid)
        if not rec:
            return False, "没有这条任务：%s" % tid
        if rec.get("status") in ("done", "rejected"):
            return False, "任务已%s：%s" % (rec["status"], tid)
        if rec.get("assignee") and rec.get("assignee") != by:
            return False, "已被 %s 认领：%s" % (rec["assignee"], tid)
        have = set(c.strip().lower() for c in (caps or []))
        need = set(c.lower() for c in rec.get("cap", []))
        if have and not need <= have:
            return False, "能力不足：需要 %s，你有 %s" % (",".join(sorted(need)), ",".join(sorted(have)))
        upd = dict(rec)
        upd.update({"status": "claimed", "assignee": by,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        _append(upd)
        return True, "已认领：%s → %s" % (tid, by)


def done(tid, note=""):
    rec = latest().get(tid)
    if not rec:
        return False, "没有这条任务：%s" % tid
    upd = dict(rec)
    upd.update({"status": "done", "note": (rec.get("note", "") + " | " + note).strip(" |"),
                "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _append(upd)
    return True, "已完成：%s" % tid


# ── HTTP（三接口，语义与本地一致）───────────────────────────────────
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    """三个接口的处理器。定义只有这一份：`serve()` 和自检都从这里起。"""

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/tasks":
            b = self._body()
            if not b.get("title"):
                return self._send(400, {"error": "title 必填"})
            tid = b.get("id") or make_id(b["title"])
            existed = tid in latest()
            rec, how = submit(b["title"], b.get("prio", DEFAULT_PRIO), b.get("cap"),
                              b.get("deps"), b.get("source_ids"), b.get("created_by", ""),
                              bool(b.get("reject")), tid=tid)
            return self._send(200 if existed else 201, {"id": rec["id"], "how": how})
        m = re.match(r"^/tasks/([^/]+)/ack$", path)
        if m:
            b = self._body()
            ok, msg = ack(m.group(1), b.get("agent", ""), b.get("caps"))
            if ok:
                return self._send(200, {"ok": True, "msg": msg})
            return self._send(409 if "已被" in msg else 403, {"ok": False, "msg": msg})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        path, _, q = self.path.partition("?")
        if path != "/tasks":
            return self._send(404, {"error": "not found"})
        agent, caps = "", []
        for kv in q.split("&"):
            k, _, v = kv.partition("=")
            if k == "agent":
                agent = v
            elif k == "caps":
                caps = [c for c in v.split(",") if c]
        rows = pool(agent, caps)
        return self._send(200, {"count": len(rows), "tasks": rows})


def make_server(port=8787):
    """起一个只绑 127.0.0.1 的池子服务。port=0 时由系统分配（自检用）。"""
    return ThreadingHTTPServer(("127.0.0.1", port), _Handler)


def serve(port=8787):
    srv = make_server(port)
    print("persona_tasks serve 127.0.0.1:%d（POST /tasks · GET /tasks?agent= · POST /tasks/<id>/ack）"
          % srv.server_address[1])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


# ── CLI ─────────────────────────────────────────────────────────────
def merge_pool():
    """把同一件事的多种说法并成一条（保留全部 source_ids）。返回合并条数。"""
    st = latest()
    hits, seen = 0, []
    for r in sorted(st.values(), key=lambda x: x.get("created_at", "")):
        if r.get("status") in ("done", "rejected"):
            continue
        for o in seen:
            if _jaccard(o.get("title"), r.get("title")) >= JACCARD:
                upd = dict(o)
                upd["source_ids"] = sorted(set(o.get("source_ids", [])) | set(r.get("source_ids", [])))
                upd["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _append(upd)
                hits += 1
                break
        else:
            seen.append(r)
    return hits


def pool_size():
    return len(pool())


def _caps(s):
    return [c.strip() for c in re.split(r"[,，;；\s]+", s or "") if c.strip()]


def main():
    ap = argparse.ArgumentParser(description="任务池（v5.5）：上交 / 合并 / 派发视图")
    ap.add_argument("--by", default=os.environ.get("PERSONA_AVATAR", ""))
    sub = ap.add_subparsers(dest="cmd")

    def add_by(sp):            # 允许把 --by 写在子命令后面（和 persona_claim 一致）
        sp.add_argument("--by", default=argparse.SUPPRESS)

    c = sub.add_parser("submit")
    c.add_argument("--title", required=True)
    c.add_argument("--prio", type=int, default=DEFAULT_PRIO)
    c.add_argument("--cap", default="")
    c.add_argument("--dep", action="append", default=[])
    c.add_argument("--source-id", action="append", default=[], dest="source_ids")
    c.add_argument("--reject", action="store_true")
    c.add_argument("--offline", action="store_true", help="强制走 outbox（模拟服务端不可用）")
    add_by(c)

    c = sub.add_parser("pool")
    c.add_argument("--agent", default="")
    c.add_argument("--caps", default="")

    c = sub.add_parser("show")
    c.add_argument("id")

    c = sub.add_parser("ack")
    c.add_argument("id")
    c.add_argument("--caps", default="")
    add_by(c)

    c = sub.add_parser("done")
    c.add_argument("id")
    c.add_argument("--note", default="")
    add_by(c)

    sub.add_parser("flush")
    sub.add_parser("merge")

    c = sub.add_parser("serve")
    c.add_argument("--port", type=int, default=8787)

    a = ap.parse_args()

    if a.cmd == "submit":
        rec, how = submit(a.title, a.prio, _caps(a.cap), a.dep, a.source_ids,
                          a.by, a.reject, a.offline)
        print("%s %s  prio=%d  status=%s" % (how, rec["id"], rec["prio"], rec["status"]))
        return 0

    if a.cmd == "pool":
        rows = pool(a.agent, _caps(a.caps))
        if not rows:
            print("（池子空）")
            return 0
        for r in rows:
            print("%-16s p%-2d %-9s %-22s cap=%s deps=%s" % (
                r.get("id", ""), r.get("prio", 5), r.get("status", ""),
                (r.get("title") or "")[:22], ",".join(r.get("cap", [])),
                ",".join(r.get("deps", []))))
        print("—— %d 条" % len(rows))
        return 0

    if a.cmd == "show":
        r = latest().get(a.id)
        if not r:
            print("没有这条任务：%s" % a.id)
            return 1
        print(json.dumps(r, ensure_ascii=False, indent=2))
        srcs = r.get("source_ids") or []
        print("\n来源 %d 条：%s" % (len(srcs), ", ".join(srcs) or "（无）"))
        print("提出者：%s @ %s" % (r.get("created_by") or "?", r.get("created_at") or "?"))
        return 0

    if a.cmd == "ack":
        ok, msg = ack(a.id, a.by, _caps(a.caps))
        print(msg)
        return 0 if ok else 1

    if a.cmd == "done":
        ok, msg = done(a.id, a.note)
        print(msg)
        return 0 if ok else 1

    if a.cmd == "flush":
        print("补交 %d 条（outbox → 池子）" % flush())
        return 0

    if a.cmd == "merge":
        st = latest()
        hits = 0
        seen = []
        for r in sorted(st.values(), key=lambda x: x.get("created_at", "")):
            if r.get("status") in ("done", "rejected"):
                continue
            for o in seen:
                if _jaccard(o.get("title"), r.get("title")) >= JACCARD:
                    upd = dict(o)
                    upd["source_ids"] = sorted(set(o.get("source_ids", [])) | set(r.get("source_ids", [])))
                    upd["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    _append(upd)
                    hits += 1
                    break
            else:
                seen.append(r)
        print("合并 %d 条" % hits)
        return 0

    if a.cmd == "serve":
        return serve(a.port)

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
