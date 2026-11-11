# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_claim.py — 任务认领登记（防重复劳动）
r"""persona_claim.py — 任务认领登记（防重复劳动）

问题（2026-11-09）：多端记忆汇总后，A 端已经做过的事，B 端读到后当成新任务又做一遍。
约定：动手前先查这里；做完标记；别的分身读到「已被认领/已完成」就跳过。

用法：
  python persona_claim.py list                     # 全部登记
  python persona_claim.py check "关键词"            # 已认领/已完成则 exit 1 并打印是谁
  python persona_claim.py claim <id> "标题" [--by X]
  python persona_claim.py done <id> [--by X] [--note "证据"]
  python persona_claim.py drop <id>

存储：ops/task-claims.jsonl（追加写，最后一条同 id 记录为准；不覆盖历史）
"""
import argparse
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(ROOT, "ops", "task-claims.jsonl")


def load():
    if not os.path.exists(STORE):
        return []
    out = []
    for line in io.open(STORE, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def latest():
    state = {}
    for r in load():
        state[r.get("id")] = r
    return state


def append(rec):
    rec["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with io.open(STORE, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def cmd_list(_a):
    st = latest()
    if not st:
        print("（空）")
        return 0
    for k, r in sorted(st.items(), key=lambda kv: kv[1].get("at", "")):
        print("%-8s %-10s %-12s %s" % (k, r.get("status", "?"), r.get("by", "?"), r.get("title", "")))
    return 0


def cmd_check(a):
    kw = a.keyword.lower()
    have = {c.strip().lower() for c in (a.has or "").split(",") if c.strip()}
    hits = [r for r in latest().values()
            if kw in (r.get("title", "") + " " + r.get("id", "")).lower()]
    if not hits:
        print("未认领：可以开始")
        return 0
    rc = 1
    for r in hits:
        need = {c.strip().lower() for c in (r.get("caps") or "").split(",") if c.strip()}
        miss = need - have if have else set()
        status = r.get("status")
        if status == "done":
            print("已完成：%s  [by %s @%s]" % (r.get("title"), r.get("by"), r.get("at")))
            continue
        if status == "dropped":
            continue
        if miss:
            print("能力不足：%s  [claimed by %s] 需要=%s 你有=%s → 别认领" % (
                r.get("title"), r.get("by"), ",".join(sorted(miss)), ",".join(sorted(have))))
            rc = 2
        else:
            print("已被认领：%s  [claimed by %s @%s]" % (r.get("title"), r.get("by"), r.get("at")))
    return rc


def _caps(s):
    return ",".join(sorted({c.strip().lower() for c in (s or "").split(",") if c.strip()}))


def cmd_claim(a):
    cur = latest().get(a.id)
    if cur and cur.get("status") in ("claimed", "done"):
        print("已存在：%s [%s by %s]" % (cur.get("title"), cur.get("status"), cur.get("by")))
        return 1
    append({"id": a.id, "title": a.title, "status": "claimed", "by": a.by,
            "caps": _caps(getattr(a, "cap", ""))})
    print("已认领：%s -> %s" % (a.id, a.title))
    return 0


def cmd_done(a):
    cur = latest().get(a.id)
    title = cur.get("title") if cur else a.id
    append({"id": a.id, "title": title, "status": "done", "by": a.by, "note": a.note or ""})
    print("已完成：%s" % a.id)
    return 0


def cmd_drop(a):
    cur = latest().get(a.id)
    append({"id": a.id, "title": cur.get("title") if cur else a.id,
            "status": "dropped", "by": a.by})
    print("已撤销：%s" % a.id)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", default=None)
    sub = ap.add_subparsers(dest="cmd")

    def add_by(p):
        p.add_argument("--by", default=argparse.SUPPRESS)  # 允许写在子命令后面

    p = sub.add_parser("list"); add_by(p)
    c = sub.add_parser("check"); c.add_argument("keyword"); c.add_argument("--has", default=""); add_by(c)
    c = sub.add_parser("claim"); c.add_argument("id"); c.add_argument("title"); c.add_argument("--cap", default=""); add_by(c)
    c = sub.add_parser("done"); c.add_argument("id"); c.add_argument("--note", default=""); add_by(c)
    c = sub.add_parser("drop"); c.add_argument("id"); add_by(c)
    a = ap.parse_args()
    a.by = getattr(a, "by", None) or os.environ.get("PERSONA_AVATAR", "unknown")
    fn = {"list": cmd_list, "check": cmd_check, "claim": cmd_claim,
          "done": cmd_done, "drop": cmd_drop}.get(a.cmd)
    if not fn:
        ap.print_help()
        return 0
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
