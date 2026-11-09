# -*- coding: utf-8 -*-
r"""persona_wake.py — 闹钟登记与唤醒简讯（v5.3）

规则（2026-11-09 定，v5.3 成文）：
  * 闹钟必须带 purpose（醒来做什么），没有 purpose 一律拒绝登记——防「醒了不知道干什么」；
  * 到点不是「拉起来就算完」：先摘一份**总索引简讯**（最近的来源 + 派发中的任务），
    再执行 purpose、写触发记录、替 agent 报到（已读索引），然后才交给调用方；
  * 三种来源：定时（人设）/ 任务（智能体自设）/ 事件（调度器直接写记录）。

用法：
  python persona_wake.py add --in 90 "检查抓取结果" [--every 3600] [--agent hermes-v4]
  python persona_wake.py add --at 18:30 "跑一次索引"
  python persona_wake.py list
  python persona_wake.py rm <id>
  python persona_wake.py fire            # 手动触发一次（守护进程每 30 秒自动跑）
  python persona_wake.py brief [--k 8]   # 只打印「醒来该先看的那段」

存储：ops/wake.json（闹钟表）；ops/wake-fires.jsonl（触发记录，只追加）；
      ops/wake-brief.json + ops/wake-brief.jsonl（唤醒简讯，最新一份 + 历史）。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
REG = os.path.join(OPS, "wake.json")
FIRES = os.path.join(OPS, "wake-fires.jsonl")
BRIEF = os.path.join(OPS, "wake-brief.json")
BRIEF_HIST = os.path.join(OPS, "wake-brief.jsonl")


def load():
    try:
        with io.open(REG, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(items):
    os.makedirs(OPS, exist_ok=True)
    with io.open(REG, "w", encoding="utf-8", newline="\n") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def parse_at(s):
    """'18:30' -> 今天的那个时刻；已过则顺延到明天。"""
    hh, mm = s.split(":")
    now = time.localtime()
    t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, int(hh), int(mm), 0, 0, 0, -1))
    if t <= time.time():
        t += 86400
    return t


def cmd_add(a):
    if not (a.purpose or "").strip():
        print("拒绝登记：必须写「醒来做什么」(purpose)，没有 purpose 的闹钟是空转")
        return 2
    now = time.time()
    due = parse_at(a.at) if a.at else now + (a.in_ or 60)
    wid = "w%d" % (int(now * 1000) % 100000000)
    rec = {"id": wid, "due": due, "every": a.every or 0, "purpose": a.purpose.strip(),
           "agent": a.agent or "", "run": a.run or "", "enabled": True,
           "created": time.strftime("%Y-%m-%d %H:%M:%S"), "last_run": 0}
    items = load()
    items.append(rec)
    save(items)
    print("已登记 %s：%s（%s）" % (wid, rec["purpose"],
                                time.strftime("%m-%d %H:%M", time.localtime(due))))
    return 0


def cmd_list(_a):
    items = load()
    if not items:
        print("（无闹钟）")
        return 0
    for r in items:
        print("%-9s %s  %-6s %-14s %s" % (
            r["id"], time.strftime("%m-%d %H:%M", time.localtime(r["due"])),
            "每%ds" % r["every"] if r["every"] else "一次",
            ("→ " + r["agent"]) if r.get("agent") else "",
            ("（已停用）" if not r.get("enabled", True) else "") + r["purpose"]))
    return 0


def cmd_rm(a):
    items = [r for r in load() if r["id"] != a.id]
    save(items)
    print("已删 %s" % a.id)
    return 0


def index_brief(k=8):
    """唤醒简讯的索引段：所有来源里最近的 k 条 + 派发中的任务。"""
    try:
        import persona_master
        snap = persona_master.snapshot(k)
        return {"index_total": snap["total"], "recent": snap["sources"],
                "pending": snap["pending"]}
    except SystemExit:
        return {"index_total": 0, "recent": [], "pending": [], "note": "没有总索引"}
    except Exception as e:
        return {"index_total": 0, "recent": [], "pending": [], "note": "索引读不出来：%s" % str(e)[:60]}


def write_brief(rec, k=8):
    """写一份唤醒简讯：醒来第一眼看的就是它。"""
    body = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wake_id": rec.get("id", ""), "purpose": rec.get("purpose", ""),
            "agent": rec.get("agent", ""), "source": rec.get("source", "alarm")}
    body.update(index_brief(k))
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(BRIEF, "w", encoding="utf-8", newline="\n") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
        with io.open(BRIEF_HIST, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return body


def render_brief(b):
    lines = ["唤醒简讯 %s（%s）" % (b.get("at", ""), b.get("purpose", "")),
             "总索引 %d 条来源，最近：" % b.get("index_total", 0)]
    for s in b.get("recent") or []:
        lines.append("  %s  %-6s %4d条  %s" % (s["time"], s["source"], s["msgs"], s["head"]))
    if b.get("pending"):
        lines.append("派发中的任务：")
        for r in b["pending"][-5:]:
            lines.append("  %s → %-14s %s" % (r.get("at", ""), r.get("agent", ""), r.get("task", "")))
    if b.get("note"):
        lines.append("（%s）" % b["note"])
    return "\n".join(lines)


def cmd_brief(a):
    if os.path.exists(BRIEF) and not a.fresh:
        try:
            b = json.load(io.open(BRIEF, encoding="utf-8-sig"))
        except (OSError, ValueError):
            b = {}
    else:
        b = write_brief({"id": "", "purpose": "手动查看", "agent": a.agent}, a.k)
    print(render_brief(b))
    return 0


def fire_due(limit=2, k=8):
    """守护进程每 30 秒调一次：到点就摘简讯、执行 purpose、写触发记录、推进下次时间。"""
    items = load()
    now = time.time()
    fired = 0
    for r in items:
        if not r.get("enabled", True) or r["due"] > now:
            continue
        brief = write_brief(r, k)
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "id": r["id"],
               "purpose": r["purpose"], "agent": r.get("agent", ""), "run": r.get("run", ""),
               "index_total": brief.get("index_total", 0),
               "brief": os.path.relpath(BRIEF, ROOT).replace("\\", "/")}
        run_cmd = (r.get("run") or "").strip()
        if run_cmd:
            env = dict(os.environ, PERSONA_PURPOSE=r["purpose"], PERSONA_WAKE_ID=r["id"],
                       PERSONA_BRIEF=BRIEF, PERSONA_ROOT=ROOT)
            try:
                pr = subprocess.run(run_cmd, shell=True, capture_output=True, text=True,
                                    timeout=120, env=env, cwd=ROOT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                rec["rc"] = pr.returncode
                rec["out"] = ((pr.stdout or "") + (pr.stderr or ""))[-400:]
            except Exception as exc:
                rec["rc"] = -1
                rec["out"] = str(exc)[:200]
        try:
            os.makedirs(OPS, exist_ok=True)
            with io.open(FIRES, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if r.get("agent"):
            _auto_report(r, brief, now)
        r["last_run"] = now
        if r.get("every"):
            while r["due"] <= now:
                r["due"] += r["every"]
        else:
            r["enabled"] = False
        fired += 1
        if fired >= limit:
            break
    if fired:
        save(items)
    return fired


def _auto_report(r, brief, now):
    """替 agent 报到：醒来这件事本身就是一次心跳，且此时总索引已读过。"""
    try:
        import persona_report as _PR
        d = _PR.load()
        rec = d.get(r["agent"]) or {"first": now}
        rec.update({"agent": r["agent"], "last": now, "status": "woken",
                    "note": "闹钟唤醒：%s（简讯含 %d 条来源）" % (r["purpose"],
                                                          brief.get("index_total", 0)),
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"), "read_index": True})
        rec["count"] = rec.get("count", 0) + 1
        d[r["agent"]] = rec
        _PR.save(d)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("add")
    c.add_argument("purpose")
    c.add_argument("--in", dest="in_", type=int, default=0, help="多少秒后（默认 60）")
    c.add_argument("--at", default="", help="HH:MM，晚于当前则今天，否则明天")
    c.add_argument("--every", type=int, default=0, help="重复周期（秒）")
    c.add_argument("--agent", default="")
    c.add_argument("--run", default="", dest="run", help="触发时要执行的命令（可选）")
    sub.add_parser("list")
    c = sub.add_parser("rm"); c.add_argument("id")
    sub.add_parser("fire")
    c = sub.add_parser("brief")
    c.add_argument("--k", type=int, default=8)
    c.add_argument("--agent", default="")
    c.add_argument("--fresh", action="store_true", help="忽略已有简讯，现摘一份")
    a = ap.parse_args()
    fn = {"add": cmd_add, "list": cmd_list, "rm": cmd_rm, "brief": cmd_brief,
          "fire": lambda _a: print("触发 %d 个" % fire_due())}.get(a.cmd)
    if not fn:
        ap.print_help()
        return 0
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
