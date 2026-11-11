# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：maint.py — 记忆库与本地缓存的维护：扫描 / 压缩 / 清理
r"""
maint.py — 记忆库与本地缓存的维护：扫描 / 压缩 / 清理

设计原则：
  1. 默认只报告（dry-run），加 --apply 才真动手；
  2. 只碰"可再生"或"已归档"的东西，绝不碰唯一副本；
  3. 每一步都打印依据（年龄、大小、为什么可回收）。

用法：
  python maint.py             # 只看能回收多少
  python maint.py --apply     # 真执行

环境变量：SYSPERSONA_ROOT 指记忆库根（旧名 PERSONA_ROOT 也认）；不设就按脚本所在
目录推导，推不出来直接抛错。缓存类路径一律在 `~\.dsh` / `~\.cache` 下，跟着用户走。
"""

import argparse
import glob
import gzip
import io
import os
import shutil
import time

from persona_root import root as _persona_root

HOME = os.path.expanduser("~")
ROOT = _persona_root()
NOW = time.time()
DAY = 86400

# 备份保留份数（原来 8，压到 4）
KEEP_BACKUPS = 4
# archive 里的 .md 超过这个天数就删（.gz 副本已存在，不丢内容）
ARCHIVE_MD_DAYS = 30
# 日志保留天数
LOG_DAYS = 7


def age_days(p):
    try:
        return (NOW - os.path.getmtime(p)) / DAY
    except OSError:
        return 0


def size_of(paths):
    return sum(os.path.getsize(p) for p in paths if os.path.isfile(p))


def report(label, paths, note=""):
    if not paths:
        return 0
    total = size_of(paths)
    print("  %-46s %7.1f MB  %3d 个  %s" % (label, total / 1048576, len(paths), note))
    for p in paths[:3]:
        print("      - %s (%.0f 天前)" % (os.path.basename(p), age_days(p)))
    return total


def plan():
    total = 0
    print("=== 1. 备份轮转（保留 %d 份）===" % KEEP_BACKUPS)
    zips = sorted(glob.glob(os.path.join(ROOT, "备份", "*.zip")), key=os.path.getmtime, reverse=True)
    old = zips[KEEP_BACKUPS:]
    total += report("可删的旧备份", old)

    print("\n=== 2. 归档 .md 只留 .gz（超 %d 天）===" % ARCHIVE_MD_DAYS)
    mds = [p for p in glob.glob(os.path.join(ROOT, "archive", "**", "*.md"), recursive=True)
           if os.path.exists(p + ".gz") and age_days(p) > ARCHIVE_MD_DAYS]
    total += report("有 .gz 副本的旧 .md", mds, "（内容仍在 .gz 里）")

    print("\n=== 3. Herdsman 日志（超 %d 天）===" % LOG_DAYS)
    logs = [p for p in glob.glob(os.path.join(HOME, ".cache", "herdsman", "logs", "**", "*.log"), recursive=True)
            if age_days(p) > LOG_DAYS]
    total += report("旧日志", logs)

    print("\n=== 4. WebView2 缓存（可再生）===")
    wv = []
    for pat in ("Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache"):
        wv += glob.glob(os.path.join(HOME, ".herdsman", "webview2", "EBWebView", "Default", pat, "**", "*"), recursive=True)
        wv += glob.glob(os.path.join(HOME, ".herdsman", "webview2", "EBWebView", pat, "**", "*"), recursive=True)
    wv = [p for p in wv if os.path.isfile(p)]
    total += report("缓存文件", wv, "（会重新生成）")

    print("\n=== 5. Herdsman 工具链安装包（解压后的运行时已存在）===")
    cache_tc = os.path.join(HOME, ".cache", "herdsman", "work", "toolchains")
    run_tc = os.path.join(HOME, ".herdsman", "work", "toolchains", "runtimes")
    zips = []
    if os.path.isdir(cache_tc) and os.path.isdir(run_tc):
        for f in os.listdir(cache_tc):
            p = os.path.join(cache_tc, f)
            if os.path.isfile(p) and f.lower().endswith((".zip", ".exe", ".msi", ".7z")):
                zips.append(p)
    total += report("安装包（可重新下载）", zips, "（运行时已解压在 .herdsman）")

    print("\n=== 6. DSH 旧会话（超 60 天，先导出再归档）===")
    olds = []
    for f in glob.glob(os.path.join(HOME, ".dsh", "sessions", "*", "*", "session.v4.jsonl.zstd")):
        if age_days(f) > 60:
            olds.append(f)
    total += report("旧会话（需先导出）", olds)

    print("\n=== 6. 只报告、不建议删 ===")
    for p, why in (
        (os.path.join(HOME, ".dsh", "profiles"), "插件依赖，删了插件坏"),
        (os.path.join(HOME, ".cache", "herdsman", "work"), "Agent 工作区数据"),
        (os.path.join(HOME, ".herdsman", "work"), "会话/记忆数据库"),
        (os.path.join(ROOT, "archive"), "唯一归档"),
        (os.path.join(ROOT, "sessions"), "唯一正文"),
    ):
        if os.path.isdir(p):
            m = 0
            for base, _, fs in os.walk(p):
                for f in fs:
                    try:
                        m += os.path.getsize(os.path.join(base, f))
                    except OSError:
                        pass
            print("  %-46s %7.1f MB  %s" % (os.path.basename(p), m / 1048576, why))

    print("\n可回收合计: %.1f MB" % (total / 1048576))
    return {"backups": old, "mds": mds, "logs": logs, "webview": wv, "oldsessions": olds, "toolchain": zips}


def apply(p):
    freed = 0
    for path in p["backups"] + p["mds"] + p["logs"] + p["webview"] + p["toolchain"]:
        try:
            freed += os.path.getsize(path)
            os.remove(path)
        except OSError:
            pass
    # DSH 空目录清理
    for base, dirs, files in os.walk(os.path.join(HOME, ".dsh", "sessions"), topdown=False):
        if not dirs and not files:
            try:
                os.rmdir(base)
            except OSError:
                pass
    print("已回收 %.1f MB" % (freed / 1048576))
    return freed


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    p = plan()
    if a.apply:
        print()
        apply(p)
