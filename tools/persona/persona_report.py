# -*- coding: utf-8 -*-
r"""persona_report.py — 报到 + 派发 + 超时回收（心跳的另一半）

规则（v5.3）：唤醒 = 心跳。醒来做三件事：
  1. 读总索引（`persona_master.py`）——知道发生了什么；
  2. **向服务端报到一次**（我醒了、我是谁、我读没读索引）；
  3. 进入等待。

派发有两道门：
  * **报到门**：最近 `--within` 秒没报到的不派——派给一个不知道在不在的东西没有意义；
  * **索引门**：报到时没标 `--read-index` 的不派——不读索引就认领 = 盲干。

派发超时回收（v5.3）：`reap` 把超过 ttl 还挂着的 pending 标过期；
有别的分身醒着就自动改派一条新的（原记录留痕，不覆盖）。

用法：
  python persona_report.py report --agent hermes-v4 --read-index [--status idle] [--note "刚醒"]
  python persona_report.py who [--within 600]
  python persona_report.py dispatch <agent> "<任务>"      # 两道门都过才派
  python persona_report.py reap [--ttl 600] [--json]      # 超时回收 + 改派
  python persona_report.py ack <id> [--status done]       # 接手/做完，回写状态
"""
import argparse
import io
import json
import os
import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
REPORT = os.path.join(OPS, "reports.json")


def load():
    try:
        with io.open(REPORT, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(d):
    os.makedirs(OPS, exist_ok=True)
    with io.open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def cmd_report(a):
    d = load()
    now = time.time()
    rec = d.get(a.agent) or {"first": now}
    rec.update({"agent": a.agent, "last": now, "status": a.status, "at": _ts(now),
                "note": a.note, "read_index": bool(a.read_index)})
    rec["count"] = rec.get("count", 0) + 1
    d[a.agent] = rec
    save(d)
    print("已报到：%s（status=%s，读索引=%s）" % (a.agent, a.status,
                                              "是" if a.read_index else "否"))
    return 0


def _ts(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t if t is not None else time.time()))


def cmd_who(a):
    d = load()
    now = time.time()
    alive = [(k, v) for k, v in d.items() if now - (v.get("last") or 0) <= a.within]
    if not alive:
        print("最近 %d 秒内没有任何分身报到" % a.within)
        return 0
    for k, v in sorted(alive, key=lambda kv: -(kv[1].get("last") or 0)):
        print("%-16s %-6s %3ds 前  读索引=%s  %s" % (
            k, v.get("status", ""), int(now - (v.get("last") or 0)),
            "是" if v.get("read_index") else "否", v.get("note", "")))
    return 0


def cmd_dispatch(a):
    d = load()
    rec = d.get(a.agent)
    now = time.time()
    if not rec or now - (rec.get("last") or 0) > a.within:
        print("不派发：%s 最近 %d 秒内没有报到（先让它醒一次）" % (a.agent, a.within))
        return 2
    if not rec.get("read_index") and not a.force:
        print("不派发：%s 报到时没读总索引（先跑 persona_master.py，"
              "再 report --read-index；确需强派加 --force）" % a.agent)
        return 3
    try:
        import persona_dispatch as D
        r = D.create(a.agent, a.task)
    except Exception as e:                     # 退回旧口径：只写文件，不建 id
        r = {"at": _ts(), "agent": a.agent, "task": a.task, "status": "pending"}
        with io.open(os.path.join(OPS, "dispatch.jsonl"), "a", encoding="utf-8",
                     newline="\n") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("（persona_dispatch 不可用：%s）" % str(e)[:60])
    print("已派发 %s → %s：%s" % (r.get("id", "-"), a.agent, a.task))
    return 0


def cmd_ack(a):
    try:
        import persona_dispatch as D
    except Exception as e:
        print("persona_dispatch 不可用：%s" % str(e)[:80])
        return 1
    st = D.state()
    if a.id not in st:
        print("没有这条派发：%s" % a.id)
        return 1
    D.update(a.id, a.status, a.note)
    print("%s → %s%s" % (a.id, a.status, ("（%s）" % a.note) if a.note else ""))
    return 0


def cmd_reap(a):
    try:
        import persona_dispatch as D
    except Exception as e:
        print("persona_dispatch 不可用：%s" % str(e)[:80])
        return 1
    res = D.reap(ttl=a.ttl, max_retry=a.max_retry)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if not (res["expired"] or res.get("failed")):
        print("没有超时的派发（ttl=%ds）" % a.ttl)
        return 0
    for r in res["expired"]:
        tail = ("改派给 %s（新 id %s）" % (r["to"], r["next"])) if r["to"] else "无人可改派"
        print("过期 %s → %-12s %s（挂了 %ds，%s）" % (r["id"], r["agent"], r["task"],
                                                  r["age_s"], tail))
    for r in res.get("failed") or []:
        print("放弃 %s → %-12s %s（已换过 %d 次，写 alerts.jsonl 叫人）"
              % (r["id"], r["agent"], r["task"], r["retry_count"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("report")
    c.add_argument("--agent", required=True)
    c.add_argument("--status", default="idle")
    c.add_argument("--note", default="")
    c.add_argument("--read-index", action="store_true", dest="read_index",
                   help="已读总索引（派发的第二道门）")
    c = sub.add_parser("who")
    c.add_argument("--within", type=int, default=600)
    c = sub.add_parser("dispatch")
    c.add_argument("agent")
    c.add_argument("task")
    c.add_argument("--within", type=int, default=600)
    c.add_argument("--force", action="store_true", help="跳过索引门（人工兜底）")
    c = sub.add_parser("ack")
    c.add_argument("id")
    c.add_argument("--status", default="done")
    c.add_argument("--note", default="")
    c = sub.add_parser("reap")
    c.add_argument("--ttl", type=int, default=600)
    c.add_argument("--max-retry", dest="max_retry", type=int, default=None,
                   help="最多换几个分身（默认 3）；超过标 failed 并写 alerts.jsonl")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args()
    fn = {"report": cmd_report, "who": cmd_who, "dispatch": cmd_dispatch,
          "ack": cmd_ack, "reap": cmd_reap}.get(a.cmd)
    if not fn:
        ap.print_help()
        return 0
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
