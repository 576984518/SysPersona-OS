# -*- coding: utf-8 -*-
"""记忆库根目录的唯一实现（2026-11-09 收口：原来 index / search 各写了一份）。

优先级：环境变量 `SYSPERSONA_ROOT` → 当前文件所在目录 → 其父目录（谁含 archive/ 就是谁）。
"""
import os


def find_root():
    env = os.environ.get("SYSPERSONA_ROOT")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(cand, "archive")):
            return cand
    return here


ROOT = find_root()
