# -*- coding: utf-8 -*-
"""表达观察（原 drift，语义已改）：底座自带性格 → 没有零点，也就没有"漂移"。

他 2026-11-09 的判断：「漂移的前提是，模型底座本身没有性格，但是这根本都不成立。」

于是本工具只做两件事：
  1. **留痕**：按 `persona_key`（模型+版本）每天记一行表达快照（平均句长 / 问句率 / 决定词密度）；
  2. **对照**：`diff` 把最近两次快照并排给人看——**只给数，不判"正常/跑偏"**。

没有基线、没有阈值、没有告警。要改人格只有两条路：改记忆、改内核（都要人点头）。
"""
import argparse
import io
import json
import os
import re
import sys
import time
from collections import defaultdict

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
OPS = os.path.join(ROOT, "ops")
STORE = os.path.join(OPS, "observe.jsonl")

DECIDE = ("决定", "必须", "不要", "以后都", "记住", "禁止", "优先", "定下来")
BLOCK = re.compile(r"(?ms)^## 助手\s*$\s*(.*?)(?=^## |\Z)")


def _key(head, src=""):
    try:
        import persona_avatar as av
        agent, model, _ = av.from_head(head, src)
        return av.persona_key(agent, model)
    except Exception:
        return "unknown-x"


def _metrics(texts):
    if not texts:
        return None
    n = len(texts)
    return {"n": n,
            "chars": round(sum(len(t) for t in texts) / float(n), 1),
            "q": round(sum(1 for t in texts if "？" in t or "?" in t) / float(n), 3),
            "decide": round(sum(1 for t in texts if any(w in t for w in DECIDE)) / float(n), 3)}


def collect(days=3):
    """最近 N 天，按 persona_key 收集助手发言并算指标。"""
    since = time.time() - days * 86400
    buckets = defaultdict(list)
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in fns:
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dp, fn)
            try:
                if os.path.getmtime(p) < since:
                    continue
                text = io.open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"(?m)^\d{4}-\d{2}-\d{2}$", os.path.abspath(p))
            src = ""
            if m:
                parts = os.path.abspath(p).split(os.sep)
                i = parts.index(m.group(0))
                src = parts[i + 1] if len(parts) > i + 1 else ""
            buckets[_key(text.partition("\n---\n")[0], src)].extend(
                b.group(1).strip()[:2000] for b in BLOCK.finditer(text))
    return {k: _metrics(v) for k, v in buckets.items() if _metrics(v)}


def snapshot(days=3):
    """记今天的一行留痕（同一天同 persona_key 覆盖，不重复堆）。"""
    today = time.strftime("%Y-%m-%d")
    rows = []
    for key, m in sorted(collect(days).items()):
        rows.append({"day": today, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "persona_key": key, "window": "%dd" % days,
                     "verdict": "留痕（无基线：底座自带性格）", **m})
    old = []
    if os.path.exists(STORE):
        old = [json.loads(l) for l in io.open(STORE, encoding="utf-8") if l.strip()]
    keep = [r for r in old if not (r.get("day") == today)]
    os.makedirs(OPS, exist_ok=True)
    with io.open(STORE, "w", encoding="utf-8", newline="\n") as f:
        for r in keep + rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


def diff(show_n=5):
    if not os.path.exists(STORE):
        print("（还没有留痕）")
        return 0
    rows = [json.loads(l) for l in io.open(STORE, encoding="utf-8") if l.strip()]
    by_key = defaultdict(list)
    for r in rows:
        by_key[r["persona_key"]].append(r)
    for key, lst in sorted(by_key.items()):
        lst.sort(key=lambda r: r["day"])
        a, b = lst[0], lst[-1]
        print("%-14s %s→%s  句长 %6.1f → %6.1f   问句 %.3f → %.3f   条数 %d → %d"
              % (key, a["day"], b["day"], a["chars"], b["chars"], a["q"], b["q"], a["n"], b["n"]))
    print("—— 只给数，不判正常/跑偏（底座自带性格，没有零点）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="表达观察（只留痕，不判定）")
    ap.add_argument("cmd", nargs="?", default="snapshot", choices=["snapshot", "diff"])
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    if a.cmd == "diff":
        return diff()
    rows = snapshot(a.days)
    print("留痕 %d 个 persona_key（%s）" % (len(rows), os.path.relpath(STORE, ROOT)))
    for r in rows[:6]:
        print("  %-14s 条数 %5d 句长 %6.1f 问句 %.3f" % (r["persona_key"], r["n"], r["chars"], r["q"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
