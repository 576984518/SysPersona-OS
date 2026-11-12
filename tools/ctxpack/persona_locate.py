# -*- coding: utf-8 -*-
r"""persona_locate.py — 本机位置的就地解析（v6.11.0，收 B89 的尾）

为什么单独一个：repo 里还有几处把"某台机器的路径"写成字面量（宿主名、盘符、用户名）。
它们不一定跑错，但只要换机器就一定错，而且**公开仓库里带着别人机器的路径**也不合适。

这里的每个函数都按同一顺序回答"它在哪"：
    1) 环境变量（人显式设的）
    2) persona_root（唯一出口，它已经处理过变量/本机配置/多盘探测）
    3) 由标准环境变量推出来的默认位置（`%LOCALAPPDATA%` / `%PROGRAMDATA%`）
    4) 都没有 → 返回空串，让调用处报一句清楚的话，**不猜一个能跑的值**

用法：
    import persona_locate as _loc
    exe = os.environ.get("PERSONA_NODE") or _loc.node()
"""

import os
import sys

try:
    import persona_root as _R
except Exception:                                  # noqa: BLE001  独立运行时里可能不在 sys.path
    _R = None

DSH_EXE = "DeepSeek Harness.exe"
HERMES_DIR = "Hermes Agent CN Desktop"
HERMES_EXE = "hermes-agent-cn-desktop.exe"
RUNTIME_DIR = "SysPersona-Runtime"


def _root(fn):
    if not _R:
        return ""
    try:
        return fn() or ""
    except Exception:                              # noqa: BLE001
        return ""


def vault():
    r"""记忆库根：变量 → persona_root → 空串（不猜）。"""
    v = os.environ.get("SYSPERSONA_ROOT") or ""
    if v:
        return v
    return _root(lambda: _R.root())


def node():
    v = os.environ.get("PERSONA_NODE") or ""
    if v:
        return v
    v = _root(lambda: _R.node())
    if v:
        return v
    rt = os.environ.get("SYSPERSONA_RUNTIME") or _root(lambda: _R.runtime_dir()) or ""
    return os.path.join(rt, "node", "bin", "node.exe") if rt else ""


def runtime():
    v = os.environ.get("SYSPERSONA_RUNTIME") or ""
    if v:
        return v
    v = _root(lambda: _R.runtime_dir())
    if v:
        return v
    pd = os.environ.get("PROGRAMDATA") or os.environ.get("ProgramData") or ""
    return os.path.join(pd, RUNTIME_DIR) if pd else ""


def dsh_exe():
    r"""壳（DSH）的 exe：变量 → 常见安装位置（用户目录/公共 ProgramData），不写死用户名。"""
    v = os.environ.get("SYSPERSONA_DSH_EXE") or ""
    if v:
        return v
    cands = []
    la = os.environ.get("LOCALAPPDATA") or ""
    if la:
        cands.append(os.path.join(la, "Programs", "DeepSeek Harness", DSH_EXE))
    pd = os.environ.get("PROGRAMDATA") or os.environ.get("ProgramData") or ""
    if pd:
        cands.append(os.path.join(pd, "DeepSeek Harness", DSH_EXE))
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0] if cands else ""


def hermes_home():
    v = os.environ.get("PERSONA_HERMES_HOME") or os.environ.get("SYSPERSONA_HERMES_HOME") or ""
    if v:
        return v
    v = _root(lambda: _R.hermes_home())
    if v:
        return v
    pd = os.environ.get("PROGRAMDATA") or os.environ.get("ProgramData") or ""
    return os.path.join(pd, HERMES_DIR, "data", "hermes-home") if pd else ""


def hermes_exe():
    v = os.environ.get("PERSONA_HERMES_EXE") or ""
    if v:
        return v
    h = hermes_home()
    if h:
        cand = os.path.join(os.path.dirname(os.path.dirname(h)), HERMES_EXE)
        if os.path.isfile(cand):
            return cand
        cand = os.path.join(os.path.dirname(os.path.dirname(h)), HERMES_EXE)
        return cand
    pd = os.environ.get("PROGRAMDATA") or os.environ.get("ProgramData") or ""
    return os.path.join(pd, HERMES_DIR, HERMES_EXE) if pd else ""


def mirror():
    r"""同步盘/镜像上的库副本（可选）。不设就是空串——**不替某台机器做主**。"""
    return os.environ.get("SYSPERSONA_MIRROR") or ""


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for name in ("runtime", "node", "dsh_exe", "hermes_home", "hermes_exe", "mirror"):
        v = globals()[name]()
        print("%-12s %s" % (name, v or "（没有：不猜）"))
