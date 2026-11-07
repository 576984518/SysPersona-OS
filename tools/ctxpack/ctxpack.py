#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ctxpack.py — 上下文提取与归档（MVP，v2.4.1）

只处理"变过的文件"：按 mtime + size 记录水位，提取过的自动跳过；
提取完先归档打包，再记水位，下次不会重复处理。

  archive  归档一段对话（可读 .md + .gz + index）
  digest   提取「状态 / 定下的 / 否决的 / 下一步」；可传目录，只处理变更文件
  search   检索归档片段，可按时间或频次排序
  status   看哪些文件还没被处理

用法：
  python ctxpack.py digest ./notes -o HANDOFF.md --dry-run
  python ctxpack.py digest ./notes -o HANDOFF.md
  python ctxpack.py search 分身 --sort time
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
STATE = os.path.join(ROOT, "state.json")

DECIDE = ("必须", "决定", "采用", "规则", "不能", "MUST", "应当")
REJECT = ("否决", "不要", "放弃", "不再", "错了", "删掉", "驳回", "反例")
NEXT = ("下一步", "待办", "TODO", "还要", "接下来", "未做", "计划")
STATUS = ("版本", "当前", "状态", "已完成", "发布")
EXTS = (".md", ".txt", ".markdown")


def _slug(s):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", s).strip("-")
    return (s[:40] or "note")


def _read(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def _lines(text):
    return [l.strip() for l in text.splitlines() if l.strip()]


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


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"processed": {}}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def sig(path):
    st = os.stat(path)
    return {"mtime": int(st.st_mtime), "size": st.st_size}


def scan(root, st):
    """返回 (changed, skipped)。跳过归档目录与状态文件。"""
    changed, skipped = [], []
    files = []
    if os.path.isfile(root):
        files = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("archive", ".git", "__pycache__")]
            for fn in filenames:
                if fn.lower().endswith(EXTS):
                    files.append(os.path.join(dirpath, fn))
    for p in sorted(files):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        cur = sig(p)
        old = st["processed"].get(rel)
        if old and old.get("mtime") == cur["mtime"] and old.get("size") == cur["size"]:
            skipped.append(rel)
        else:
            changed.append((p, rel))
    return changed, skipped


def digest_text(path, budget=2400):
    lines = _lines(_read(path))
    out = []
    for title, keys, limit in (
        ("状态", STATUS, 4),
        ("定下的", DECIDE, 8),
        ("否决的", REJECT, 6),
        ("下一步", NEXT, 6),
    ):
        items = _pick(lines, keys, limit)
        if items:
            out.append("### " + title)
            out += ["- " + it.lstrip("-* ").strip() for it in items]
            out.append("")
    body = "\n".join(out)
    return body if len(body) <= budget else body[:budget] + "\n…（截断）\n"


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
    with open(INDEX, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "file": os.path.relpath(full, ROOT).replace("\\", "/"),
            "day": day,
            "title": title or os.path.basename(path),
            "tags": tags or [],
            "chars": len(text),
            "sha256": h,
            "saved_at": time.time(),
        }, ensure_ascii=False) + "\n")
    return full


def cmd_digest(root, out_path, dry_run, st):
    changed, skipped = scan(root, st)
    print("待处理 %d 个，已跳过 %d 个（未变更）" % (len(changed), len(skipped)))
    if not changed:
        print("没有新内容，不生成摘要。")
        return 1
    for p, rel in changed:
        print("  + %s" % rel)
    if dry_run:
        return 0

    parts = ["# 交接摘要", "", "> 由 `ctxpack.py digest` 生成 · %s" % time.strftime("%Y-%m-%d %H:%M"), ""]
    for p, rel in changed:
        parts.append("## 来源：`%s`" % rel)
        parts.append("")
        parts.append(digest_text(p) or "（无可抽取内容）")
        parts.append("")

    text = "\n".join(parts)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    for p, rel in changed:
        full = archive(p, title=os.path.basename(p))
        st["processed"][rel] = sig(p)
        st["processed"][rel]["archived_as"] = os.path.relpath(full, ROOT).replace("\\", "/")
        st["processed"][rel]["at"] = time.time()
    save_state(st)
    print("已写 %s（%d 字），并归档 %d 个文件；水位已更新。" % (out_path, len(text), len(changed)))
    return 0


def cmd_status(root, st):
    changed, skipped = scan(root, st)
    print("未处理：%d" % len(changed))
    for _, rel in changed:
        print("  + " + rel)
    print("已处理：%d" % len(skipped))
    for rel in skipped[:10]:
        print("  - " + rel)
    return 0


def search(q, limit=5, ctx=1, sort="freq"):
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
            i = idxs[0]
            snippet = "\n".join(lines[max(0, i - ctx):i + ctx + 1]).strip()
            hits.append({"file": os.path.relpath(p, ROOT).replace("\\", "/"),
                         "day": day, "score": len(idxs), "snippet": snippet})
    if sort == "time":
        hits.sort(key=lambda x: (x["day"], x["file"]), reverse=True)
    else:
        hits.sort(key=lambda x: (-x["score"], x["day"]), reverse=False)
    return hits[:limit]


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="上下文提取与归档")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("archive")
    a.add_argument("path")
    a.add_argument("--title", default=None)
    a.add_argument("--tags", default="")

    d = sub.add_parser("digest")
    d.add_argument("path")
    d.add_argument("-o", "--out", default="HANDOFF-session.md")
    d.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--sort", choices=["freq", "time"], default="freq")

    st = sub.add_parser("status")
    st.add_argument("path", default=".")

    args = ap.parse_args()
    state = load_state()

    if args.cmd == "archive":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        full = archive(args.path, args.title, tags)
        print("archived:", full)
        print("gz:      ", full + ".gz")
    elif args.cmd == "digest":
        return cmd_digest(args.path, args.out, args.dry_run, state)
    elif args.cmd == "status":
        return cmd_status(args.path, state)
    else:
        hits = search(args.query, args.limit, sort=args.sort)
        if not hits:
            print("no hits")
            return 1
        for h in hits:
            print("== %s  (hits=%d, %s)" % (h["file"], h["score"], h["day"]))
            print(h["snippet"])
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
