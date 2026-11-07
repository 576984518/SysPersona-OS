#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ctxpack.py — 上下文提取与归档（MVP）

三个动作，全部离线、零依赖：

  archive  把一段对话原样归档（可读 .md + .md.gz + 索引），防止平台清理后找不回
  digest   从一段对话里抽出「状态 / 定下的 / 否决的 / 下一步」，几百字，喂给新会话
  search   在归档里检索，只取命中片段，不把整包东西塞进上下文

用法：
  python ctxpack.py archive chat.md --title "v2.4 讨论" --tags 规范,分身
  python ctxpack.py digest chat.md -o HANDOFF-session.md
  python ctxpack.py search 分身 --limit 5
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
INDEX = os.path.join(ARCHIVE, "index.jsonl")

DECIDE = ("必须", "决定", "采用", "规则", "不能", "MUST", "应当")
REJECT = ("否决", "不要", "放弃", "不再", "错了", "删掉", "驳回", "反例")
NEXT = ("下一步", "待办", "TODO", "还要", "接下来", "未做", "计划")
STATUS = ("版本", "当前", "状态", "已完成", "发布")


def _slug(s):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", s).strip("-")
    return (s[:40] or "note")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _lines(text):
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(line)
    return out


def _pick(lines, keys, limit):
    seen, picked = set(), []
    for line in lines:
        if any(k in line for k in keys):
            key = line[:60]
            if key in seen:
                continue
            seen.add(key)
            picked.append(line if len(line) <= 200 else line[:200] + "…")
            if len(picked) >= limit:
                break
    return picked


def digest(path, out_path=None, budget=2400):
    text = _read(path)
    lines = _lines(text)
    sections = [
        ("状态", _pick(lines, STATUS, 6)),
        ("定下的", _pick(lines, DECIDE, 10)),
        ("否决的", _pick(lines, REJECT, 8)),
        ("下一步", _pick(lines, NEXT, 8)),
    ]
    stamp = time.strftime("%Y-%m-%d %H:%M")
    out = ["# 交接摘要", "", "> 由 `ctxpack.py digest` 生成 · 源文件 `%s` · %s" % (os.path.basename(path), stamp), ""]
    for title, items in sections:
        if not items:
            continue
        out.append("## " + title)
        out += ["- " + it.lstrip("-* ").strip() for it in items]
        out.append("")
    body = "\n".join(out)
    if len(body) > budget:
        body = body[:budget] + "\n\n…（截断：原文件仍在归档中）\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            f.write(body)
        return out_path, len(body)
    return body, len(body)


def archive(path, title=None, tags=None):
    os.makedirs(ARCHIVE, exist_ok=True)
    text = _read(path)
    day = time.strftime("%Y-%m-%d")
    d = os.path.join(ARCHIVE, day)
    os.makedirs(d, exist_ok=True)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    name = "%s-%s.md" % (_slug(title or os.path.splitext(os.path.basename(path))[0]), h)
    full = os.path.join(d, name)
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    with gzip.open(full + ".gz", "wb") as f:
        f.write(text.encode("utf-8"))
    rec = {
        "file": os.path.relpath(full, ROOT).replace("\\", "/"),
        "day": day,
        "title": title or os.path.basename(path),
        "tags": tags or [],
        "chars": len(text),
        "sha256": h,
        "saved_at": time.time(),
    }
    with open(INDEX, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return full


def search(q, limit=5, ctx=1):
    if not os.path.isdir(ARCHIVE):
        return []
    hits = []
    for day in sorted(os.listdir(ARCHIVE), reverse=True):
        d = os.path.join(ARCHIVE, day)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(d, fn)
            try:
                lines = _read(p).splitlines()
            except OSError:
                continue
            idxs = [i for i, l in enumerate(lines) if q.lower() in l.lower()]
            if not idxs:
                continue
            best = idxs[0]
            lo, hi = max(0, best - ctx), min(len(lines), best + ctx + 1)
            snippet = "\n".join(lines[lo:hi]).strip()
            hits.append({"file": os.path.relpath(p, ROOT).replace("\\", "/"),
                         "score": len(idxs), "snippet": snippet})
    hits.sort(key=lambda x: (-x["score"], x["file"]))
    return hits[:limit]


def main():
    ap = argparse.ArgumentParser(description="上下文提取与归档")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("archive")
    a.add_argument("path")
    a.add_argument("--title", default=None)
    a.add_argument("--tags", default="")

    d = sub.add_parser("digest")
    d.add_argument("path")
    d.add_argument("-o", "--out", default=None)
    d.add_argument("--budget", type=int, default=2400)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)

    args = ap.parse_args()
    if args.cmd == "archive":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        full = archive(args.path, args.title, tags)
        print("archived:", full)
        print("gz:      ", full + ".gz")
    elif args.cmd == "digest":
        res, size = digest(args.path, args.out, args.budget)
        print(res if args.out is None else "wrote %s (%d chars)" % (args.out, size))
    else:
        hits = search(args.query, args.limit)
        if not hits:
            print("no hits")
            return 1
        for h in hits:
            print("== %s  (hits=%d)" % (h["file"], h["score"]))
            print(h["snippet"])
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
