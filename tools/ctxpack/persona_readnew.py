# -*- coding: utf-8 -*-
"""每次任务前读会话：一条命令，问"有没有新内容"。

规矩（2026-11-09 定）：**开工前先读，不等他说"1"**——他有时候没讲有新内容。

做法：
  - 记一个水位 `ops/last-read.json`（上次读到的最新归档 mtime + 文件名）；
  - 比归档里最新的 .md；**更新的**就列出来，并把最新那份的尾部打出来（默认 60 行）；
  - 没有更新就 exit 2（脚本可判），有就 exit 0。

用法：
  python persona_readnew.py            # 检查 + 有新内容就打尾部
  python persona_readnew.py --lines 120
  python persona_readnew.py --mark     # 只记水位，不打印（读完以后收尾用）
"""
import argparse
import io
import json
import os
import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
OPS = os.path.join(ROOT, "ops")
MARK = os.path.join(OPS, "last-read.json")
LATEST = os.path.join(ROOT, "LATEST.md")


def newest():
    """返回 [(mtime, path)] 按时间倒序（只取 .md）。"""
    out = []
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in fns:
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dp, fn)
            try:
                out.append((os.path.getmtime(p), p))
            except OSError:
                continue
    out.sort(reverse=True)
    return out


def load_mark():
    try:
        return json.load(io.open(MARK, encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": 0.0, "file": ""}


def save_mark(rec):
    os.makedirs(OPS, exist_ok=True)
    tmp = MARK + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(rec, ensure_ascii=False, indent=1))
    os.replace(tmp, MARK)


def page_time():
    try:
        txt = io.open(LATEST, encoding="utf-8", errors="replace").read(600)
        for line in txt.splitlines():
            if "更新时间" in line:
                return line.split("：")[-1].strip()
    except OSError:
        pass
    return "?"


def tail(path, n):
    lines = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
    return "\n".join(lines[-n:])


def main():
    ap = argparse.ArgumentParser(description="读会话：有没有新内容")
    ap.add_argument("--lines", type=int, default=60)
    ap.add_argument("--mark", action="store_true")
    a = ap.parse_args()

    files = newest()
    if not files:
        print("归档是空的：%s" % ARCHIVE)
        return 1
    mark = load_mark()
    m0 = float(mark.get("at") or 0)
    if m0 <= 0:
        save_mark({"at": files[0][0], "file": os.path.relpath(files[0][1], ROOT),
                   "when": time.strftime("%H:%M:%S")})
        print("首次运行：以当前最新为水位（%s）；下次起只报比它更新的。"
              % os.path.relpath(files[0][1], ROOT))
        return 2

    fresh = [(t, p) for t, p in files if t > m0 + 1]

    if a.mark:
        save_mark({"at": files[0][0], "file": os.path.relpath(files[0][1], ROOT), "when": time.strftime("%H:%M:%S")})
        print("水位已记：%s" % os.path.relpath(files[0][1], ROOT))
        return 0

    if not fresh:
        print("无新内容。最新归档：%s（页面更新于 %s）"
              % (os.path.relpath(files[0][1], ROOT), page_time()))
        return 2

    print("有新内容 %d 份（页面更新于 %s）：" % (len(fresh), page_time()))
    for t, p in fresh[:5]:
        print("  - %s  %s" % (time.strftime("%H:%M:%S", time.localtime(t)),
                              os.path.relpath(p, ROOT)))
    print("\n—— 最新那份的尾部 ——\n")
    print(tail(fresh[0][1], a.lines))
    save_mark({"at": fresh[0][0], "file": os.path.relpath(fresh[0][1], ROOT),
               "when": time.strftime("%H:%M:%S")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
