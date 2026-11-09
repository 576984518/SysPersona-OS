# -*- coding: utf-8 -*-
"""日度高光摘要（v5.6）：每天一份，只写当天定下的事，方便按天回溯。

他网页端定的第 4 条：「每天生成一份 DAILY_HIGHLIGHT_YYYYMMDD.md，只写当天讨论的核心与突破」。

规则：
  - 素材只来自 `archive\\<日期>\\` 的原文，不二次总结、不引用没出现过的结论；
  - 每条摘句都带来源文件名，能顺藤摸到原文（续接包的「可溯源」同一条纪律）；
  - 只挑「决定 / 定下 / 否决 / 以后都 / 改成 / 不要」这类句子，宁缺勿滥。
"""
import argparse
import io
import os
import re
import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
OUT = os.path.join(ROOT, "memory", "日度高光")
KEY = ("决定", "定下", "否决", "以后都", "改成", "改为", "不要", "必须", "规则", "口径")
MSG = re.compile(r"(?ms)^## (用户|助手)\s*$\s*(.*?)(?=^## |\Z)")


def collect(day):
    base = os.path.join(ARCHIVE, day)
    hits, files = [], []
    if not os.path.isdir(base):
        return hits, files
    for dp, _, fns in os.walk(base):
        for fn in sorted(fns):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dp, fn)
            try:
                text = io.open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            files.append(os.path.relpath(p, ROOT).replace("\\", "/"))
            for m in MSG.finditer(text):
                body = re.sub(r"(?m)^>.*$\n?", "", m.group(2)).strip()
                if len(body) < 20:
                    continue
                first = body.splitlines()[0][:110]
                if any(k in body[:200] for k in KEY):
                    hits.append((first, os.path.basename(p)))
    return hits, files


def build(day):
    hits, files = collect(day)
    lines = ["# %s 高光" % day, "",
             "> 只写当天**定下的事**；每句带来源文件，可回原文核对。", "",
             "## 素材", "", "- 会话文件 **%d** 个" % len(files), "",
             "## 定下的", ""]
    seen = set()
    n = 0
    for text, src in hits:
        key = text[:26]
        if key in seen:
            continue
        seen.add(key)
        lines.append("- %s  `（%s）`" % (text, src))
        n += 1
        if n >= 18:
            break
    if n == 0:
        lines.append("- （今天没有可引用的决定句）")
    lines += ["", "## 指针", "", "- 原文目录：`archive/%s/`" % day, ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="日度高光摘要")
    ap.add_argument("--date", default="")
    ap.add_argument("--print", action="store_true", dest="show")
    a = ap.parse_args()
    day = a.date or time.strftime("%Y-%m-%d")
    text = build(day)
    if a.show:
        print(text)
        return 0
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "DAILY_HIGHLIGHT_%s.md" % day.replace("-", ""))
    io.open(p, "w", encoding="utf-8", newline="\n").write(text)
    print("已生成 %s（%d 字）" % (os.path.relpath(p, ROOT), len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
