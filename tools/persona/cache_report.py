# -*- coding: utf-8 -*-
"""cache_report.py — 报本会话的 token / 缓存 / 成本（与 DSH 状态栏同源）

用法：
  python cache_report.py            # 自动取最近活跃会话
  python cache_report.py <sessionId 片段>
  python cache_report.py --json     # 机器可读
"""
import io
import json
import os
import sys

LEDGER = os.path.join(os.path.expanduser("~"), ".dsh", "storages", "cost-meter", "ledger.json")


def human(n):
    n = float(n)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fK" % (n / 1e3)
    return str(int(n))


def load():
    data = json.loads(io.open(LEDGER, encoding="utf-8", errors="replace").read())
    days = data.get("days") or {}
    if not days:
        return None, None
    date = sorted(days)[-1]
    return date, days[date]


def main():
    want = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    date, day = load()
    if not day:
        print("账本里没有数据")
        return
    sessions = day.get("sessions") or []
    if not sessions:
        print("今天还没有会话记录")
        return
    rec = None
    if want:
        rec = next((s for s in sessions if want[0] in s.get("id", "")), None)
    if rec is None:
        rec = max(sessions, key=lambda s: s.get("costCny", 0))

    inp = rec.get("input", 0)
    cache = rec.get("cacheRead", 0)
    out = rec.get("output", 0)
    hit = cache / (cache + inp) * 100 if (cache + inp) else 0

    if as_json:
        print(json.dumps({"date": date, "session": rec.get("id"), "costCny": rec.get("costCny"),
                          "input": inp, "cacheRead": cache, "output": out,
                          "calls": rec.get("calls"), "hitPct": round(hit, 1)}, ensure_ascii=False))
        return

    print("本会话 %s" % (rec.get("id", "?")[:22]))
    print("  花费 ￥%.3f   调用 %d 次" % (rec.get("costCny", 0), rec.get("calls", 0)))
    print("  输入 %s   缓存 %s（命中 %.1f%%）   输出 %s"
          % (human(inp), human(cache), hit, human(out)))


if __name__ == "__main__":
    main()
