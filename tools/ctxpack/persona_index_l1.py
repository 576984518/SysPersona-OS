# -*- coding: utf-8 -*-
"""persona_index_l1.py — 一级索引（每个来源一行，给"交接时只读一次"用）

为什么要它：交接包如果每次都从 150 个文件里现抽摘要，要么慢、要么越抽越长。
一级索引把"有哪些记忆、各多大、第一句说什么"压成一张几十行的表；
交接只读这张表，要看细节再去二级索引（`persona_search.py` / `HANDOFF.md`）。

用法：
  python persona_index_l1.py            # 全量重建 index/L1.jsonl 与 index/L1.md
  python persona_index_l1.py --quiet    # 只写文件，不打印
"""
import argparse
import io
import json
import os
import re
import sys
import time

import persona_avatar as av

ROOT = os.environ.get("SYSPERSONA_ROOT") or r"D:\4590-PC-文件\AI会话检索记录"
ARCHIVE = os.path.join(ROOT, "archive")
INDEX = os.path.join(ROOT, "index")

MSG = re.compile(r"(?m)^## (用户|助手)\s*$")


def first_line(text):
    """取正文第一段人话：优先第一条用户消息，跳过标题/元信息/引用行。"""
    m = re.search(r"(?m)^## 用户\s*$", text)
    body = text[m.end():] if m else text
    for line in body.splitlines():
        s = line.strip().lstrip("#>-* ").strip()
        if len(s) < 6:
            continue
        if s.startswith(("-", "`", "|", "http")):
            continue
        return s[:42]
    return ""


def scan():
    rows = []
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in sorted(fns):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            parts = rel.split("/")
            day = parts[1] if len(parts) > 2 else ""
            src = parts[2] if len(parts) > 3 else ""
            try:
                text = io.open(p, encoding="utf-8", errors="replace").read()
                st = os.path.getmtime(p)
            except OSError:
                continue
            n = len(MSG.findall(text))
            agent, model, avatar = av.from_head(text[:4096], src)
            rows.append({
                "day": day,
                "source": src,
                "file": rel,
                "msgs": n,
                "chars": len(text),
                "head": first_line(text),
                "agent": agent,
                "model": model,
                "avatar": avatar,
                "mtime": int(st),
            })
    rows.sort(key=lambda r: (r["day"], r["mtime"]), reverse=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    os.makedirs(INDEX, exist_ok=True)
    rows = scan()
    out = os.path.join(INDEX, "L1.jsonl")
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    md = os.path.join(INDEX, "L1.md")
    lines = ["# 一级索引（每个来源一行）", "",
             "| 日期 | 来源 | 分身 | 文件 | 消息 | 首句 |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %d | %s |"
                     % (r["day"], r["source"], r["avatar"], os.path.basename(r["file"]),
                        r["msgs"], r["head"]))
    io.open(md, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

    if not a.quiet:
        print("一级索引：%d 个来源 → %s" % (len(rows), out))
        print("人读版：%s" % md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
