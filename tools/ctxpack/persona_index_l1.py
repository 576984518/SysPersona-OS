# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_index_l1.py — 一级索引（每个来源一行，给"交接时只读一次"用）
"""persona_index_l1.py — 一级索引（每个来源一行，给"交接时只读一次"用）

为什么要它：交接包如果每次都从 150 个文件里现抽摘要，要么慢、要么越抽越长。
一级索引把"有哪些记忆、各多大、第一句说什么"压成一张几十行的表；
交接只读这张表，要看细节再去二级索引（`persona_search.py` / `HANDOFF.md`）。

2026-11-10 补：memory 目录下的**常驻文件**（设定 / 备忘 / 清单式摘要）也编进来，
source 标「记忆设定 / 记忆备忘 / 记忆常驻」，avatar 记 `memory`——它们不属于某个分身。
这样"壳记忆里的指针"和"检索能查到的内容"是同一批文件，不会写了却搜不到。

用法：
  python persona_index_l1.py            # 全量重建 index/L1.jsonl 与 index/L1.md
  python persona_index_l1.py --quiet    # 只写文件，不打印
"""
import argparse
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）
import io

try:
    import persona_vault_io as _VIO

    def _vio_text(p):
        return _VIO.text_of(p)
except Exception:  # 没有保险箱也能跑
    def _vio_text(p):
        return io.open(p, encoding="utf-8", errors="replace").read()

import json
import os
import re
import sys
import time

import persona_avatar as av

ROOT = os.environ.get("SYSPERSONA_ROOT") or _loc.vault()
ARCHIVE = os.path.join(ROOT, "archive")
INDEX = os.path.join(ROOT, "index")

# memory\ 下的常驻目录与文件（不进 archive，但必须能被索引到）
EXTRA_DIRS = [
    (os.path.join(ROOT, "memory", "设定"), "记忆设定"),
    (os.path.join(ROOT, "memory", "备忘"), "记忆备忘"),
    # 文档\ 是说明与结论的正式归处（人 2026-11-12 定的目录规矩）。
    # 这些页不进 archive，但同样必须能被检索到——写完搜不到等于没写。
    (os.path.join(ROOT, "文档"), "文档"),
]
EXTRA_FILES = [
    os.path.join(ROOT, "memory", n) for n in (
        "MEMORY.md", "CONTEXT.md", "USER.md", "INDEX.md", "REENTRY.md",
        "SOP-壳运维.md", "身份与分工.md", "运行环境.md",
    )
]

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
                text = _vio_text(p)
                st = os.path.getmtime(p)
            except OSError:
                continue
            n = len(MSG.findall(text))
            agent, model, avatar = av.from_head(text[:4096], src)
            fam, ver = av.family(model), av.version_of(model)
            pkey = av.persona_key(agent, model)
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
                "family": fam,
                "model_version": ver,
                "persona_key": pkey,
                "mtime": int(st),
            })
    rows.sort(key=lambda r: (r["day"], r["mtime"]), reverse=True)
    return rows


def _extra_row(p, src):
    """常驻文件的一行：没有消息计数，身份归 memory（不参与分身判定）。"""
    try:
        text = _vio_text(p)
        st = os.path.getmtime(p)
    except OSError:
        return None
    rel = os.path.relpath(p, ROOT).replace("\\", "/")
    return {
        "day": time.strftime("%Y-%m-%d", time.localtime(st)),
        "source": src,
        "file": rel,
        "msgs": 0,
        "chars": len(text),
        "head": first_line(text),
        "agent": "memory",
        "model": "",
        "avatar": "memory",
        "family": "",
        "model_version": "",
        "persona_key": "memory",
        "mtime": int(st),
    }


def scan_extra():
    rows = []
    for d, src in EXTRA_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md"):
                r = _extra_row(os.path.join(d, fn), src)
                if r:
                    rows.append(r)
    for p in EXTRA_FILES:
        if os.path.isfile(p):
            r = _extra_row(p, "记忆常驻")
            if r:
                rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    os.makedirs(INDEX, exist_ok=True)
    rows = scan() + scan_extra()
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
        print("一级索引：%d 条（会话 %d + 常驻 %d）→ %s"
              % (len(rows), len(rows) - len([r for r in rows if r["avatar"] == "memory"]),
                 len([r for r in rows if r["avatar"] == "memory"]), out))
        print("人读版：%s" % md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
