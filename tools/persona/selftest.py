# -*- coding: utf-8 -*-
r"""selftest.py — v5.3 自检（不碰真库）

在临时目录造一个小记忆库（`index/L1.jsonl` + 两条会话），把 `PERSONA_ROOT` 指过去，
再逐条验 v5.3 的四块：

  T1 总索引速览          3 条来源能否按时间倒序读出
  T2 闹钟                无 purpose 拒绝；到点触发要摘简讯、注入环境变量、替 agent 报到
  T3 派发的两道门        没报到不派；报到但没读索引不派；都过了才派
  T4 超时回收            过期标 expired，有醒着的人就改派一条新的，原记录留痕
  T5 交互代理            动词表 / 越界 403 / 未授权 403 / 未知动词 404 / token 401 / 审计有记录

为什么要有它：v5.3 的判据是「行为」不是「文档」——这些数字只有在真跑一遍之后才算证据。

用法：
  python selftest.py            # 跑完自动清理临时库
  python selftest.py --keep     # 留着临时库看现场
退出码：0 全过；1 有失败。
"""
import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def make_root():
    """临时库：优先系统临时目录，建不了就落到脚本旁边（沙箱里常见）。

    也可以显式指定：PERSONA_SELFTEST_ROOT=<可写目录>。
    """
    base = os.environ.get("PERSONA_SELFTEST_ROOT") or tempfile.gettempdir()
    # 不用 mkdtemp：它建成只有「属主」权限的目录，受限令牌下连子目录都进不去
    root = os.path.join(base, "persona-selftest-%d" % int(time.time() * 1000))
    try:
        os.makedirs(root)
    except OSError:
        root = os.path.join(HERE, "persona-selftest-%d" % int(time.time() * 1000))
        os.makedirs(root)
    os.makedirs(os.path.join(root, "ops"), exist_ok=True)
    os.makedirs(os.path.join(root, "index"), exist_ok=True)
    day = os.path.join(root, "sessions", "2026-11-09", "电脑会话")
    os.makedirs(day, exist_ok=True)
    rows = [
        {"day": "2026-11-09", "source": "电脑会话", "msgs": 2, "mtime": 1794216000,
         "head": "第一条：电脑会话在说派发超时"},
        {"day": "2026-11-09", "source": "网页会话", "msgs": 4, "mtime": 1794215000,
         "head": "第二条：网页会话在说总索引"},
        {"day": "2026-11-08", "source": "手机端", "msgs": 1, "mtime": 1794200000,
         "head": "第三条：手机端补一条"},
    ]
    with io.open(os.path.join(root, "index", "L1.jsonl"), "w", encoding="utf-8",
                 newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with io.open(os.path.join(day, "a.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# a\n\n派发超时回收要能改派，而且原记录不许被覆盖。\n")
    return root


class Runner(object):
    def __init__(self, root):
        self.root = root
        self.env = dict(os.environ, PERSONA_ROOT=root, PYTHONIOENCODING="utf-8")
        self.passed = 0
        self.failed = 0

    def run(self, script, *args):
        r = subprocess.run([PY, os.path.join(HERE, script)] + list(args),
                           env=self.env, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=180)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            print("  PASS  %-30s %s" % (name, detail))
        else:
            self.failed += 1
            print("  FAIL  %-30s %s" % (name, detail))
        return ok

    def path(self, *p):
        return os.path.join(self.root, *p)

    def jsonl(self, *p):
        fp = self.path(*p)
        if not os.path.exists(fp):
            return []
        out = []
        for line in io.open(fp, encoding="utf-8-sig", errors="replace"):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out


# ── T1 总索引 ─────────────────────────────────────────────────────────

def t1(r):
    rc, out = r.run("persona_master.py", "--json", "--k", "5")
    try:
        snap = json.loads(out)
    except ValueError:
        return r.check("T1 总索引 JSON", False, "输出不是 JSON：%s" % out[:120])
    r.check("T1 来源条数", snap.get("total") == 3, "total=%s" % snap.get("total"))
    r.check("T1 时间倒序", [s["source"] for s in snap["sources"]] == ["电脑会话", "网页会话", "手机端"],
            "顺序=%s" % [s["source"] for s in snap["sources"]])
    rc, out = r.run("persona_master.py", "--k", "2")
    r.check("T1 人读输出", rc == 0 and "共 3 条来源" in out, out.splitlines()[-1][:40] if out else "")


# ── T2 闹钟 ──────────────────────────────────────────────────────────

def t2(r):
    rc, out = r.run("persona_wake.py", "add", "--in", "1", "   ")
    r.check("T2 无 purpose 拒绝", rc == 2 and "拒绝登记" in out, "rc=%s" % rc)
    rc, out = r.run("persona_wake.py", "add", "--in", "1", "自检触发", "--agent", "t-agent",
                    "--run", "cmd /c echo purpose=%PERSONA_PURPOSE%")
    r.check("T2 登记", rc == 0 and "已登记" in out, out.strip()[:40])
    time.sleep(2.2)
    rc, out = r.run("persona_wake.py", "fire")
    r.check("T2 到点触发", rc == 0 and "触发 1 个" in out, out.strip()[:40])
    fires = r.jsonl("ops", "wake-fires.jsonl")
    f = fires[-1] if fires else {}
    r.check("T2 触发记录带简讯", bool(f) and f.get("index_total") == 3
            and f.get("brief") == "ops/wake-brief.json", "index_total=%s" % f.get("index_total"))
    r.check("T2 purpose 注入环境变量", "purpose=自检触发" in (f.get("out") or ""),
            (f.get("out") or "").strip()[:50])
    rc, out = r.run("persona_wake.py", "brief")
    r.check("T2 简讯正文", "总索引 3 条来源" in out, out.splitlines()[1][:40] if out else "")
    try:
        reps = json.load(io.open(r.path("ops", "reports.json"), encoding="utf-8-sig"))
    except (OSError, ValueError):
        reps = {}
    r.check("T2 自动报到并标已读索引", bool(reps.get("t-agent", {}).get("read_index")),
            "status=%s" % reps.get("t-agent", {}).get("status"))


# ── T3 派发两道门 ─────────────────────────────────────────────────────

def t3(r):
    rc, _ = r.run("persona_report.py", "report", "--agent", "t2", "--note", "刚连上")
    rc, out = r.run("persona_report.py", "dispatch", "t2", "跑一次索引")
    r.check("T3 没读索引不派", rc == 3 and "没读总索引" in out, "rc=%s" % rc)
    rc, _ = r.run("persona_report.py", "report", "--agent", "t2", "--read-index")
    rc, out = r.run("persona_report.py", "dispatch", "t2", "跑一次索引")
    r.check("T3 读过索引才派", rc == 0 and "已派发" in out, out.strip()[:44])
    rc, out = r.run("persona_master.py", "--pending")
    r.check("T3 总索引里看得到派发", "t2" in out and "跑一次索引" in out, "")
    rc, out = r.run("persona_report.py", "dispatch", "ghost", "没人报到的活")
    r.check("T3 没报到不派", rc == 2 and "没有报到" in out, "rc=%s" % rc)


# ── T4 超时回收 ───────────────────────────────────────────────────────

def t4(r):
    old = {"at": "2026-11-09 09:00:00", "agent": "old-agent", "task": "昨天的活",
           "status": "pending"}
    with io.open(r.path("ops", "dispatch.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(old, ensure_ascii=False) + "\n")
    r.run("persona_report.py", "report", "--agent", "rg2", "--read-index", "--note", "闲着")
    rc, out = r.run("persona_report.py", "reap", "--ttl", "600", "--json")
    try:
        res = json.loads(out)
    except ValueError:
        return r.check("T4 回收输出", False, out[:120])
    exp = res.get("expired") or []
    r.check("T4 过期一条", len(exp) == 1 and exp[0]["agent"] == "old-agent",
            "expired=%d" % len(exp))
    r.check("T4 改派给醒着的人", bool(exp) and exp[0]["to"] == "rg2",
            "to=%s" % (exp[0]["to"] if exp else ""))
    rc, out = r.run("persona_master.py", "--pending")
    r.check("T4 新派发进总索引", "rg2" in out and "old-agent" not in out, "")
    dt = r.jsonl("ops", "dispatch.jsonl")
    has_update = any(x.get("status") == "expired" and "改派" in (x.get("note") or "") for x in dt)
    r.check("T4 原记录留痕", has_update, "append-only 有 expired 更新行")
    new_id = exp[0]["next"] if exp else ""
    rc, out = r.run("persona_report.py", "ack", new_id, "--status", "done", "--note", "验收通过")
    rc2, out2 = r.run("persona_master.py", "--pending")
    r.check("T4 ack 后不再挂起", rc == 0 and "rg2" not in out2, out.strip()[:40])


def t4b(r):
    """改派次数用完 → failed + 告警，不再继续转手。"""
    dead = {"at": "2026-11-09 09:00:00", "agent": "dead-end", "task": "转手三次没人接的活",
            "status": "pending", "retry_count": 3, "max_retry": 3}
    with io.open(r.path("ops", "dispatch.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(dead, ensure_ascii=False) + "\n")
    rc, out = r.run("persona_report.py", "reap", "--ttl", "600", "--json")
    try:
        res = json.loads(out)
    except ValueError:
        return r.check("T4b 回收输出", False, out[:120])
    failed = res.get("failed") or []
    r.check("T4b 超上限标 failed", len(failed) == 1 and failed[0]["agent"] == "dead-end",
            "failed=%d" % len(failed))
    r.check("T4b 不再无限改派", not res.get("redispatched"), "redispatched=%d" % len(res.get("redispatched") or []))
    alerts = r.jsonl("ops", "alerts.jsonl")
    r.check("T4b 写告警", any(a.get("kind") == "dispatch_failed" for a in alerts),
            "alerts=%d 行" % len(alerts))
    rc, out = r.run("persona_master.py", "--pending")
    r.check("T4b 不再挂起", "dead-end" not in out, "")


# ── T5 交互代理 ───────────────────────────────────────────────────────

def t5(r):
    import persona_proxy as PX          # 导入时 PERSONA_ROOT 已指向临时库
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), PX.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever)
    th.daemon = True
    th.start()

    def post(verb, args=None, tok=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/call" % port,
            data=json.dumps({"verb": verb, "args": args or {}}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Persona-Token": tok if tok is not None else PX.token()},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=10) as resp:
            h = json.loads(resp.read().decode("utf-8"))
        r.check("T5 health", resp.status == 200 and h.get("version") == PX.VERSION,
                "version=%s" % h.get("version"))

        code, o = post("index.top", {"k": 2})
        r.check("T5 index.top", code == 200 and o["result"]["total"] == 3, "total=%s" % o.get("result", {}).get("total"))

        code, o = post("mem.grep", {"q": "派发超时"})
        r.check("T5 mem.grep", code == 200 and o["result"]["total"] >= 1,
                "hits=%s" % o.get("result", {}).get("total"))

        code, o = post("mem.read", {"path": "sessions/2026-11-09/电脑会话/a.md"})
        r.check("T5 mem.read", code == 200 and "改派" in o["result"]["text"], "")

        code, o = post("mem.read", {"path": "../../etc/passwd"})
        r.check("T5 mem.read 越界 403", code == 403, o.get("error", "")[:30])

        code, o = post("nosuch.verb", {})
        r.check("T5 未知动词 404", code == 404, "")

        code, o = post("sys.run", {"cmd": "cmd /c echo hi"})
        r.check("T5 sys.run 默认拒 403", code == 403, o.get("error", "")[:30])

        with io.open(r.path("ops", "proxy-allow.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump({"allow": ["cmd /c echo selftest-ok"]}, f, ensure_ascii=False)
        code, o = post("sys.run", {"cmd": "cmd /c echo selftest-ok"})
        r.check("T5 白名单内放行", code == 200 and "selftest-ok" in (o["result"]["out"] or ""),
                (o.get("result") or {}).get("out", "").strip()[:40])

        code, o = post("health", {}, tok="wrong-token")
        r.check("T5 错 token 401", code == 401, "")

        code, o = post("sys.proc", {"name": "explorer"})
        res = o["result"]
        r.check("T5 sys.proc 三态", code == 200 and "query_ok" in res,
                "running=%s query_ok=%s" % (res.get("running"), res.get("query_ok")))
        r.check("T5 查不了要明说",
                res.get("query_ok") is not False or bool(res.get("note")),
                res.get("note") or "本机能查")

        audit = r.jsonl("ops", "proxy-audit.jsonl")
        r.check("T5 逐次审计", len(audit) >= 9 and all("at" in x for x in audit),
                "audit=%d 行" % len(audit))
        r.check("T5 审计不含 token", all("X-Persona-Token" not in json.dumps(x, ensure_ascii=False)
                                     for x in audit), "")
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    root = make_root()
    os.environ["PERSONA_ROOT"] = root
    r = Runner(root)
    print("== v5.3 自检（临时库 %s）==" % root)
    try:
        for fn in (t1, t2, t3, t4, t4b, t5):
            try:
                fn(r)
            except Exception as e:
                r.check(fn.__name__.upper() + " 未捕获异常", False, "%s: %s" % (type(e).__name__, e))
    finally:
        print("\n== 结果：PASS %d / FAIL %d ==" % (r.passed, r.failed))
        if a.keep:
            print("现场保留：%s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
