# -*- coding: utf-8 -*-
r"""镜像（v5.4 B80）：把会话全文原样落盘，再谈压缩。

规范：`docs/BACKGROUND_SPEC.md` §四（镜像文件规范）。要点：
  - 路径 `archive\<日期>\<来源>\<会话id>-mirror.md`；
  - 文件头逐行齐全（`设备/底座/镜像于/触发/sha256/原文`），`blocks()` 靠它认设备与底座；
  - **幂等**：同名且 sha256 相同 → 跳过；不同 → 换名加时间戳，**绝不覆盖**；
  - 只写字，不删字：镜像完还要能逐字读回原文。

用法：
  python persona_mirror.py <会话文件> [--trigger "hctx=812345"]
  python persona_mirror.py --check           # 列出已镜像的条目
"""
import argparse
import hashlib
import io
import os
import re
import sys
import time

import persona_avatar as av

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.environ.get("PERSONA_ARCHIVE") or os.path.join(ROOT, "archive")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _head_field(head, *names):
    for n in names:
        m = re.search(r"(?m)^-\s*%s[：:]\s*(.+?)\s*$" % n, head)
        if m:
            return m.group(1)
    return ""


def target_path(src_path, day=None, source=None):
    day = day or time.strftime("%Y-%m-%d")
    name = os.path.basename(src_path)
    sid = os.path.splitext(name)[0]
    return os.path.join(ARCHIVE, day, source or "电脑会话", "%s-mirror.md" % sid)


def mirror(src_path, trigger="", dest=None):
    """把会话原文镜像到 archive。返回 (路径, 状态)，状态 ∈ created / skipped / renamed。"""
    text = io.open(src_path, encoding="utf-8", errors="replace").read()
    digest = sha256(text)
    head = text.partition("\n---\n")[0]

    # 从路径取 日期/来源，取不到就用今天/电脑会话（事实优先，推不出来才兜底）
    parts = os.path.abspath(src_path).split(os.sep)
    day = next((p for p in parts if re.match(r"^\d{4}-\d{2}-\d{2}$", p)), "") or time.strftime("%Y-%m-%d")
    src_bucket = parts[parts.index(day) + 1] if day in parts and len(parts) > parts.index(day) + 1 else "电脑会话"
    dst = dest or target_path(src_path, day, src_bucket)
    try:
        rel_src = os.path.relpath(src_path, ROOT).replace("\\", "/")
    except ValueError:            # 跨盘（C: 的临时文件镜像进 D:）不许炸
        rel_src = os.path.abspath(src_path).replace("\\", "/")

    # 头字段解析不在这里再写一份：直接用 persona_avatar（唯一出处）
    dev, mod, _ = av.from_head(text[:4096], src_bucket)
    header = (
        "- 会话：%s\n"
        "- 设备：%s\n"
        "- 底座：%s\n"
        "- 镜像于：%s\n"
        "- 触发：%s\n"
        "- sha256：%s\n"
        "- 原文：%s\n"
        "\n---\n\n" % (
            os.path.splitext(os.path.basename(src_path))[0], dev or "unknown",
            mod or "unknown", time.strftime("%Y-%m-%d %H:%M:%S"), trigger or "-",
            digest, rel_src))

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    state = "created"
    if os.path.exists(dst):
        old = io.open(dst, encoding="utf-8", errors="replace").read()
        if digest in old:
            return dst, "skipped"                      # 同名同内容：幂等
        stem, ext = os.path.splitext(dst)
        dst = "%s-%s%s" % (stem, time.strftime("%H%M%S"), ext)   # 不同内容：另存，不覆盖
        state = "renamed"

    with io.open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 镜像 %s\n\n" % os.path.basename(src_path))
        f.write(header)
        f.write(text)
    return dst, state


def verify(path):
    """自证：镜像里记的 sha256 与它自己的正文一致。返回 (ok, 说明)。"""
    text = io.open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"(?m)^-\s*sha256[：:]\s*([0-9a-f]{64})\s*$", text)
    if not m:
        return False, "没有 sha256 行"
    if "\n---\n\n" not in text:
        return False, "没有头部结束线"
    body = text.split("\n---\n\n", 1)[1]
    got = sha256(body)
    return (got == m.group(1)), ("一致" if got == m.group(1) else
                                 "不一致：头部 %s / 正文 %s" % (m.group(1)[:8], got[:8]))


def newest_session():
    """最近改动的一个会话文件（镜像入口用）。没有就返回空串。"""
    base = os.path.join(ROOT, "sessions")
    best, best_t = "", 0.0
    for dp, _, fns in os.walk(base):
        for fn in fns:
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dp, fn)
            try:
                t = os.path.getmtime(p)
            except OSError:
                continue
            if t > best_t:
                best, best_t = p, t
    return best


def main():
    ap = argparse.ArgumentParser(description="会话镜像（v5.4）：先落盘，再压缩")
    ap.add_argument("src", nargs="?")
    ap.add_argument("--trigger", default="")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.check:
        n = 0
        for dp, _, fns in os.walk(ARCHIVE):
            for fn in fns:
                if fn.endswith("-mirror.md"):
                    p = os.path.join(dp, fn)
                    ok, msg = verify(p)
                    print("%-5s %s" % ("OK" if ok else "BAD", p))
                    n += 1
        print("—— %d 个镜像" % n)
        return 0

    if not a.src:
        ap.error("要么给会话文件，要么 --check")
    if not os.path.exists(a.src):
        print("没有这个文件：%s" % a.src)
        return 1
    dst, state = mirror(a.src, a.trigger)
    ok, msg = verify(dst) if state != "skipped" else (True, "已存在（幂等跳过）")
    print("%s %s  %s" % (state, dst, msg))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
