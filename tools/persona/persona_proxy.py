# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_proxy.py — 交互代理（v5.3）：智能体里的 AI 通过它调用本机
r"""persona_proxy.py — 交互代理（v5.3）：智能体里的 AI 通过它调用本机

为什么要有它：智能体直接拿 shell，等于每次都在「什么都可能被执行」的前提下工作，
既没有白名单也没有台账。代理把「AI 能对本机做什么」收敛成一张**动词表**：
每个动词有固定参数、固定上限、逐次审计；默认只读，写与执行要显式开。

边界（写死，不做开关）：
  * 只绑 127.0.0.1，不监听外网；
  * 每个请求必须带 `X-Persona-Token`（首次启动自动生成 `ops/proxy-token.txt`）；
  * `mem.read` 只能读 ROOT 之内（外部一律 403），单次上限 8000 字；
  * `sys.run` 默认全部拒绝：只有 `ops/proxy-allow.json` 里逐条列出的命令前缀才放行；
  * 每次调用追加 `ops/proxy-audit.jsonl`：动词、参数摘要、耗时、结果码（**不记 token**）。

用法：
  python persona_proxy.py serve [--port 8787]     # 常驻（守护进程会替你拉起）
  python persona_proxy.py token                   # 看 token 文件路径
  python persona_proxy.py verbs                   # 列白名单动词
  python persona_proxy.py call health             # 本机自测
  python persona_proxy.py call index.top '{"k":5}'

协议见 `docs/INTERACT_SPEC.md`。
"""
import argparse
import hmac
import io
import json
import os
import socket
import subprocess

try:
    import persona_win as _pw  # 无窗口：GUI 进程里 spawn 控制台程序不弹黑窗
except Exception:
    pass

import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
TOKEN_FILE = os.path.join(OPS, "proxy-token.txt")
ALLOW_FILE = os.path.join(OPS, "proxy-allow.json")
AUDIT = os.path.join(OPS, "proxy-audit.jsonl")
VERSION = "5.3.0"
DEFAULT_PORT = 8787
MAX_BODY = 256 * 1024
MAX_READ = 8000


class ProxyError(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code
        self.msg = msg


# ── 动词表 ────────────────────────────────────────────────────────────

def _master():
    import persona_master
    return persona_master


def v_health(a):
    return {"ok": True, "name": "persona-proxy", "version": VERSION,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "root": ROOT}


def v_index_top(a):
    k = min(int(a.get("k") or 8), 50)
    snap = _master().snapshot(k)
    return {"total": snap["total"], "recent": snap["sources"], "pending": snap["pending"]}


def v_mem_grep(a):
    import re
    q = (a.get("q") or "").strip()
    if not q:
        raise ProxyError(400, "缺少 q")
    k = min(int(a.get("k") or 10), 50)
    hits = []
    for base in ("sessions", "archive", "memory"):
        d = os.path.join(ROOT, base)
        for dirpath, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith(".md"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    t = io.open(p, encoding="utf-8-sig", errors="replace").read()
                except OSError:
                    continue
                n = t.count(q)
                if not n:
                    continue
                pos = max(0, t.find(q) - 60)
                snip = re.sub(r"\s+", " ", t[pos:pos + 200])
                hits.append({"path": os.path.relpath(p, ROOT).replace("\\", "/"),
                             "hits": n, "snippet": snip})
    hits.sort(key=lambda x: -x["hits"])
    return {"q": q, "total": len(hits), "hits": hits[:k]}


def v_mem_read(a):
    rel = (a.get("path") or "").strip().replace("\\", "/")
    if not rel:
        raise ProxyError(400, "缺少 path")
    p = os.path.realpath(os.path.join(ROOT, rel))
    root = os.path.realpath(ROOT)
    if p != root and not p.startswith(root + os.sep):
        raise ProxyError(403, "只允许读 ROOT 之内的文件")
    if not os.path.isfile(p):
        raise ProxyError(404, "没有这个文件")
    limit = min(int(a.get("max_chars") or 4000), MAX_READ)
    try:
        t = io.open(p, encoding="utf-8-sig", errors="replace").read()
    except OSError as e:
        raise ProxyError(500, "读失败：%s" % e)
    return {"path": rel, "chars": len(t), "truncated": len(t) > limit, "text": t[:limit]}


def v_wake_list(_a):
    import persona_wake
    items = persona_wake.load()
    out = []
    for r in items:
        row = {k: r.get(k) for k in ("id", "purpose", "agent", "every", "enabled")}
        row["due"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("due") or 0))
        out.append(row)
    return {"alarms": out}


def v_wake_add(a):
    import persona_wake
    purpose = (a.get("purpose") or "").strip()
    if not purpose:
        raise ProxyError(400, "闹钟必须带 purpose")
    now = time.time()
    due = now + int(a.get("in_seconds") or 60)
    if a.get("at"):
        due = persona_wake.parse_at(a["at"])
    wid = "w%d" % (int(now * 1000) % 100000000)
    rec = {"id": wid, "due": due, "every": int(a.get("every") or 0), "purpose": purpose,
           "agent": a.get("agent") or "", "run": a.get("run") or "", "enabled": True,
           "created": time.strftime("%Y-%m-%d %H:%M:%S"), "last_run": 0,
           "by": "proxy"}
    items = persona_wake.load()
    items.append(rec)
    persona_wake.save(items)
    return {"id": wid, "due": time.strftime("%Y-%m-%d %H:%M", time.localtime(due))}


def v_report(a):
    import persona_report as R
    agent = (a.get("agent") or "").strip()
    if not agent:
        raise ProxyError(400, "缺少 agent")
    d = R.load()
    now = time.time()
    rec = d.get(agent) or {"first": now}
    rec.update({"agent": agent, "last": now, "status": a.get("status") or "idle",
                "at": time.strftime("%Y-%m-%d %H:%M:%S"), "note": a.get("note") or "",
                "read_index": bool(a.get("read_index"))})
    rec["count"] = rec.get("count", 0) + 1
    d[agent] = rec
    R.save(d)
    return {"agent": agent, "status": rec["status"], "read_index": rec["read_index"]}


def v_who(a):
    import persona_report as R
    d = R.load()
    now = time.time()
    within = int(a.get("within") or 600)
    out = [{"agent": k, "status": v.get("status", ""),
            "age_s": int(now - (v.get("last") or 0)),
            "read_index": bool(v.get("read_index")), "note": v.get("note", "")}
           for k, v in d.items() if now - (v.get("last") or 0) <= within]
    out.sort(key=lambda x: x["age_s"])
    return {"within": within, "agents": out}


def v_dispatch_add(a):
    import persona_report as R
    import persona_dispatch as D
    agent = (a.get("agent") or "").strip()
    task = (a.get("task") or "").strip()
    if not agent or not task:
        raise ProxyError(400, "缺少 agent 或 task")
    d = R.load()
    rec = d.get(agent)
    now = time.time()
    if not rec or now - (rec.get("last") or 0) > 600:
        raise ProxyError(409, "报到门未过：%s 最近 600 秒内没报到" % agent)
    if not rec.get("read_index"):
        raise ProxyError(409, "索引门未过：%s 报到时没读总索引" % agent)
    r = D.create(agent, task)
    return {"id": r["id"], "agent": agent, "task": task}


def v_dispatch_reap(a):
    import persona_dispatch as D
    return D.reap(ttl=int(a.get("ttl") or 600))


def v_sys_proc(a):
    name = (a.get("name") or "").strip()
    if not name or any(c in name for c in '\\/:*?"<>|'):
        raise ProxyError(400, "name 只能是进程名（不许带路径）")
    # 进程查询只有一份实现：和保活共用，免得多处判定口径走偏
    try:
        import persona_keepalive as K
        state = K.running(name)
    except Exception:
        state = None
    return {"name": name, "running": state, "query_ok": state is not None,
            "note": "" if state is not None else "查不了进程状态（权限被拒）"}


def _allow():
    try:
        d = json.load(io.open(ALLOW_FILE, encoding="utf-8-sig"))
        return [c.strip() for c in (d.get("allow") or []) if c.strip()]
    except (OSError, ValueError):
        return []


def v_sys_run(a):
    cmd = (a.get("cmd") or "").strip()
    if not cmd:
        raise ProxyError(400, "缺少 cmd")
    allowed = _allow()
    ok = any(cmd == c or cmd.startswith(c + " ") for c in allowed)
    if not ok:
        raise ProxyError(403, "未授权命令（在 ops/proxy-allow.json 的 allow 里逐条加前缀）")
    timeout = min(int(a.get("timeout") or 60), 120)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout, cwd=ROOT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ProxyError(504, "命令超时（%ds）" % timeout)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    return {"rc": r.returncode, "out": out[-4000:], "err": err[-2000:]}


VERBS = {
    "health": (v_health, "无参数"),
    "index.top": (v_index_top, "k ≤ 50"),
    "mem.grep": (v_mem_grep, "q, k ≤ 50"),
    "mem.read": (v_mem_read, "path（ROOT 内）, max_chars ≤ 8000"),
    "wake.list": (v_wake_list, "无参数"),
    "wake.add": (v_wake_add, "purpose 必填；in_seconds / at / every / agent / run"),
    "report.report": (v_report, "agent 必填；status / note / read_index"),
    "report.who": (v_who, "within ≤ ..."),
    "dispatch.add": (v_dispatch_add, "agent, task（两道门都要过）"),
    "dispatch.reap": (v_dispatch_reap, "ttl"),
    "sys.proc": (v_sys_proc, "name（进程名）"),
    "sys.run": (v_sys_run, "cmd（必须在 proxy-allow.json 白名单里）, timeout ≤ 120"),
}


# ── token / 审计 ──────────────────────────────────────────────────────

def token(create=True):
    try:
        t = io.open(TOKEN_FILE, encoding="utf-8-sig").read().strip()
        if t:
            return t
    except OSError:
        pass
    if not create:
        return ""
    import secrets
    t = secrets.token_hex(24)
    os.makedirs(OPS, exist_ok=True)
    with io.open(TOKEN_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(t + "\n")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


def audit(rec):
    rec["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(AUDIT, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _short(v, n=120):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def call_verb(verb, args, peer="local"):
    t0 = time.time()
    code, res = 200, None
    try:
        fn = VERBS.get(verb)
        if not fn:
            raise ProxyError(404, "没有这个动词：%s（可用：%s）" % (verb, ", ".join(sorted(VERBS))))
        res = fn[0](args or {})
    except ProxyError as e:
        code, res = e.code, {"error": e.msg}
    except Exception as e:
        code, res = 500, {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    ms = int((time.time() - t0) * 1000)
    audit({"verb": verb, "args": _short(args), "code": code, "ms": ms, "peer": peer})
    return code, {"ok": code == 200, "verb": verb, "ms": ms,
                  "result": res if code == 200 else None,
                  "error": None if code == 200 else res.get("error")}


# ── HTTP ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "persona-proxy/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def _peer_ok(self):
        try:
            ip = self.client_address[0]
        except (IndexError, TypeError):
            return False
        return ip in ("127.0.0.1", "::1")

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/v1/health"):
            self._send(200, {"ok": True, "name": "persona-proxy", "version": VERSION})
        else:
            self._send(404, {"ok": False, "error": "只支持 GET /health 与 POST /v1/call"})

    def do_POST(self):
        if self.path.split("?")[0] not in ("/v1/call", "/call"):
            return self._send(404, {"ok": False, "error": "未知路径"})
        if not self._peer_ok():
            return self._send(403, {"ok": False, "error": "只接受本机请求"})
        want = token()
        got = self.headers.get("X-Persona-Token") or ""
        if not want or not hmac.compare_digest(want, got):
            audit({"verb": "-", "args": "", "code": 401, "ms": 0, "peer": "unauth"})
            return self._send(401, {"ok": False, "error": "token 不对（见 ops/proxy-token.txt）"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return self._send(413, {"ok": False, "error": "请求体过大"})
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"ok": False, "error": "body 不是 JSON"})
        try:
            code, obj = call_verb(body.get("verb", ""), body.get("args") or {}, "127.0.0.1")
        except Exception as e:            # 兜底：任何异常都要回一句，不能把连接直接断掉
            code, obj = 500, {"ok": False, "error": str(e)[:200]}
        self._send(code, obj)


def is_up(port=DEFAULT_PORT, host="127.0.0.1", timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def serve(port=DEFAULT_PORT):
    if is_up(port):
        print("代理已在 127.0.0.1:%d 上跑着，不重复启动" % port)
        return 3
    token()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print("persona-proxy %s 监听 127.0.0.1:%d（token: %s）" % (VERSION, port, TOKEN_FILE))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def client_call(verb, args_json="{}", port=DEFAULT_PORT):
    """本机自测用：直接走 HTTP，和外部调用者同一条路径。"""
    import urllib.request
    if not is_up(port):
        print("代理没在跑（127.0.0.1:%d）" % port)
        return 2
    try:
        args = json.loads(args_json or "{}")
    except ValueError:
        print("args 不是 JSON")
        return 2
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/call" % port,
        data=json.dumps({"verb": verb, "args": args}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Persona-Token": token(create=True)},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(json.dumps(json.loads(r.read().decode("utf-8")), ensure_ascii=False, indent=1))
            return 0
    except urllib.error.HTTPError as e:
        print(e.read().decode("utf-8", "replace"))
        return 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("serve"); c.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("token")
    sub.add_parser("verbs")
    c = sub.add_parser("call")
    c.add_argument("verb")
    c.add_argument("args", nargs="?", default="{}")
    c.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    if a.cmd == "serve":
        return serve(a.port)
    if a.cmd == "verbs":
        for k in sorted(VERBS):
            print("%-16s %s" % (k, VERBS[k][1]))
        return 0
    if a.cmd == "token":
        print(TOKEN_FILE)
        print(token())
        return 0
    if a.cmd == "call":
        return client_call(a.verb, a.args, a.port)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
