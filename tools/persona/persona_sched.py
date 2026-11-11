# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_sched.py — 服务端重派与排队的最小实现（v5.5 §四）
r"""persona_sched.py — 服务端重派与排队的最小实现（v5.5 §四）

一句话：**节点有拒绝权，服务端就只剩两条路——换一个，或者先记着。**
任务不会因为没人接就消失，它只会变旧；变旧必须能被看见（24 小时熔断）。

这一版只做骨架：不联网、不打 HTTP、不建库。`ask()` 是唯一的外部依赖，
换成本机的 `persona_report.dispatch` 就是本机派发器，换成 HTTP 就是服务端——
**同一套代码**（SCHEDULER_SPEC §五）。

用法：
  python persona_sched.py --selftest     # 四条判据自证，不碰真实节点
"""
import argparse
import json
import os
import sys
import time

IDLE, BUSY, OFFLINE = "idle", "busy", "offline"
PENDING, QUEUED, ACCEPTED, FAILED = "pending", "queued", "accepted", "failed"
QUEUE_TIMEOUT = 24 * 3600     # 排队超过 24 小时 → failed + 告警


def now():
    return time.time()


def online(nodes):
    return [n for n in nodes if n.get("state") != OFFLINE]


def dispatch(task, nodes, ask):
    """派一条任务。返回 'accepted' 或 'queued'——**没有第三个返回值，因为绝不丢弃**。"""
    for node in online(nodes):
        if node.get("state") != IDLE:
            continue
        r = ask(node, task)
        if r == "accept":
            task["state"] = ACCEPTED
            node["state"] = BUSY
            return ACCEPTED
        if r == "reject":
            # ★ 这一行是整套逻辑的命门：不标 busy，下一轮又会选中同一个节点，
            #   变成「问一遍→被拒→再问它」，看着在派，其实一步没走。
            node["state"] = BUSY
            continue
        if r in ("timeout", "offline"):
            node["state"] = OFFLINE
            continue
    task["state"] = QUEUED
    # queued_at 只在**第一次**入队时打：每轮重派都刷成"刚刚"的话，
    # 24 小时熔断永远不会触发——池子天天被拒，任务永远"刚排上"。
    if not task.get("queued_at"):
        task["queued_at"] = now()
    task["last_try"] = now()
    return QUEUED


def queued(tasks):
    return sorted([t for t in tasks if t.get("state") == QUEUED],
                  key=lambda t: t.get("queued_at") or 0)


def tick(tasks, nodes, ask, alerts=None):
    """每 5 分钟一次：先熔断过期的，再把等待区按排队的先后派一遍。

    顺序不能反：先派再熔断，会把"刚被重派"的任务当成刚排队（`queued_at` 虽不刷新，
    但读起来容易让人以为顺序无所谓）；而先熔断，过期的任务根本不会再去打扰节点。
    先排队的先派：等待区不是栈——后到的先派会让最老的永远排在最后，
    而最老的恰恰最可能已经过期。
    """
    res = {"accepted": [], "queued": [], "failed": []}
    for task in queued(tasks):
        if now() - (task.get("queued_at") or now()) > QUEUE_TIMEOUT:
            task["state"] = FAILED
            res["failed"].append(task.get("id"))
            if alerts is not None:
                alerts.append({"level": "error", "kind": "task-expired",
                               "id": task.get("id"),
                               "note": "排队超过 24 小时无人可派"})
    for task in queued(tasks):
        r = dispatch(task, nodes, ask)
        res["accepted" if r == ACCEPTED else "queued"].append(task.get("id"))
    return res


# ---------------- 自证：四条判据，用假节点跑，不碰真服务器 ----------------

def _selftest():
    ok = 0
    bad = 0

    def check(name, cond, extra=""):
        nonlocal ok, bad
        if cond:
            ok += 1
            print("  PASS %-28s %s" % (name, extra))
        else:
            bad += 1
            print("  FAIL %-28s %s" % (name, extra))

    # ① 正常派发：只问一次
    asked = []

    def ask_ok(node, task):
        asked.append(node["name"])
        return "accept"

    nodes = [{"name": "a", "state": IDLE}, {"name": "b", "state": IDLE}]
    task = {"id": "T1", "state": PENDING}
    r = dispatch(task, nodes, ask_ok)
    check("① 正常派发", r == ACCEPTED and asked == ["a"] and nodes[0]["state"] == BUSY,
          "问了 %s → %s" % (asked, r))

    # ② 被拒换人：第一个拒，同一次调用落到第二个
    asked = []

    def ask_reject_first(node, task):
        asked.append(node["name"])
        return "reject" if node["name"] == "a" else "accept"

    nodes = [{"name": "a", "state": IDLE}, {"name": "b", "state": IDLE}]
    task = {"id": "T2", "state": PENDING}
    r = dispatch(task, nodes, ask_reject_first)
    check("② 被拒换人", r == ACCEPTED and asked == ["a", "b"] and nodes[0]["state"] == BUSY,
          "问了 %s，a=%s" % (asked, nodes[0]["state"]))

    # ③ 全拒排队：一条都不丢
    asked = []

    def ask_all_reject(node, task):
        asked.append(node["name"])
        return "reject"

    nodes = [{"name": "a", "state": IDLE}, {"name": "b", "state": IDLE}]
    task = {"id": "T3", "state": PENDING}
    r = dispatch(task, nodes, ask_all_reject)
    check("③ 全拒 → queued", r == QUEUED and task["state"] == QUEUED
          and bool(task.get("queued_at")) and len(asked) == 2,
          "问了 %s → %s（没丢）" % (asked, r))

    # ④ 熔断：把 queued_at 拨到 25 小时前
    tasks = [{"id": "T4", "state": QUEUED, "queued_at": now() - 25 * 3600}]
    alerts = []
    res = tick(tasks, [], ask_all_reject, alerts)
    check("④ 24 小时熔断", tasks[0]["state"] == FAILED and res["failed"] == ["T4"]
          and alerts and alerts[0]["kind"] == "task-expired",
          "state=%s 告警=%d" % (tasks[0]["state"], len(alerts)))

    # ⑤ 反例：排队中来了 idle 节点 → 下一轮被派出去
    tasks = [{"id": "T5", "state": QUEUED, "queued_at": now() - 60}]
    nodes = [{"name": "c", "state": IDLE}]
    res = tick(tasks, nodes, ask_ok, [])
    check("⑤ 有节点空出来就派", tasks[0]["state"] == ACCEPTED and res["accepted"] == ["T5"],
          "→ %s" % tasks[0]["state"])

    print("\n== 结果：PASS %d / FAIL %d ==" % (ok, bad))
    return 0 if bad == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="服务端重派与排队（v5.5 §四 参考实现）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    ap.print_help()
    print("\n这一版只有骨架：真实派发时把 ask() 换成 HTTP 调用或 persona_report.dispatch。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
