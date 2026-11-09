# -*- coding: utf-8 -*-
r"""persona_master.py — 总索引速览（唤醒后的第一件事）

规矩（v5.3 硬规则）：**唤醒 ≠ 拉起**。醒来先读总索引（所有来源汇总），
知道后面发生了什么，再报到、再认领；不读索引就认领 = 盲干。

总索引 = `index/L1.jsonl`：每个来源一行（时间 / 来源 / 条数 / 首句）。
它回答「全局发生过什么」；要问「这条具体来自哪」，再去读分索引和原文。

用法：
  python persona_master.py                  # 最近 10 条来源 + 派发中的任务
  python persona_master.py --k 20
  python persona_master.py --day 2026-11-09
  python persona_master.py --source 网页会话
  python persona_master.py --pending        # 只看派发中
  python persona_master.py --json           # 机器读：唤醒简讯 / 代理用

退出码：0 正常；2 没有总索引（先跑 persona_index_l1.py）。
"""
import argparse
import io
import json
import os
import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
L1 = os.path.join(ROOT, "index", "L1.jsonl")


def rows(day="", source=""):
    """按 mtime 倒序返回总索引行。"""
    out = []
    if not os.path.exists(L1):
        raise SystemExit("没有总索引：%s（先跑 persona_index_l1.py）" % L1)
    for line in io.open(L1, encoding="utf-8-sig", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if day and r.get("day") != day:
            continue
        if source and source not in (r.get("source") or ""):
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
    return out


def _avatars(rs):
    """分身分布：谁记了多少来源。只统计，不判断谁更重要。"""
    out = {}
    for r in rs:
        k = r.get("avatar") or "unknown"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _pending():
    """派发中的任务。没有 persona_dispatch 就退回读 dispatch.jsonl 的旧口径。"""
    try:
        import persona_dispatch
        return persona_dispatch.pending()
    except Exception:
        p = os.path.join(ROOT, "ops", "dispatch.jsonl")
        if not os.path.exists(p):
            return []
        out = []
        for line in io.open(p, encoding="utf-8-sig", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") == "pending":
                out.append(r)
        return out


def snapshot(k=10, day="", source=""):
    rs = rows(day, source)
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "index": os.path.relpath(L1, ROOT).replace("\\", "/"),
        "total": len(rs),
        "k": k,
        "sources": [{"time": time.strftime("%m-%d %H:%M", time.localtime(r.get("mtime") or 0)),
                     "mtime": r.get("mtime") or 0,
                     "day": r.get("day", ""),
                     "source": r.get("source", ""),
                     "avatar": r.get("avatar", ""),
                     "msgs": r.get("msgs") or 0,
                     "head": (r.get("head") or "").replace("\n", " ")[:80]} for r in rs[:k]],
        "avatars": _avatars(rs),
        "pending": _pending(),
    }


def render(snap):
    lines = []
    for s in snap["sources"]:
        lines.append("%s  %-6s %-26s %4d条  %s" % (s["time"], s["source"],
                                                   (s.get("avatar") or "unknown")[:26],
                                                   s["msgs"], s["head"]))
    lines.append("—— 共 %d 条来源（按时间倒序取前 %d）" % (snap["total"], snap["k"]))
    if snap.get("avatars"):
        lines.append("分身： " + " · ".join("%s %d" % (k, v) for k, v in
                                          list(snap["avatars"].items())[:6]))
    if snap["pending"]:
        lines.append("")
        lines.append("== 派发中的任务 ==")
        for r in snap["pending"][-5:]:
            lines.append("%s  → %-14s %s" % (r.get("at", ""), r.get("agent", ""),
                                            r.get("task", "")))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--day", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--pending", action="store_true", help="只看派发中的任务")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    snap = snapshot(a.k, a.day, a.source)
    if a.pending:
        snap["sources"] = []
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
    else:
        text = render(snap) if snap["sources"] else ""
        if snap["pending"]:
            text = (text + "\n\n" if text else "") + "== 派发中的任务 ==\n" + "\n".join(
                "%s  → %-14s %s" % (r.get("at", ""), r.get("agent", ""), r.get("task", ""))
                for r in snap["pending"][-5:])
        print(text or "（总索引为空）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
