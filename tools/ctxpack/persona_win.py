# -*- coding: utf-8 -*-
"""persona_win.py — 让本进程 spawn 的所有子进程都不弹控制台窗口（Windows）

背景（2026-11-10）：常驻进程是 `pythonw`（GUI 子系统，没有控制台）。它一旦 spawn
控制台程序（`node` / `cmd` / `powershell` / `clip` / `tasklist`），Windows 会**新建一个
控制台窗口**给子进程——屏幕上就是一闪一闪的黑窗。

`persona_daemon.py` 自己在每处 `run()` 上带了 `creationflags=CREATE_NO_WINDOW`，
但**被它调用的子脚本内部**再 spawn（比如 `persona_gauge.py` 起 node 导出、起 `clip`
写剪贴板）就漏了，于是照样闪。

用法：在脚本顶部，`import subprocess` 之后加一行

    import persona_win; persona_win.hide_child_windows()

它把 `subprocess.Popen` 换成带默认 `CREATE_NO_WINDOW` 的版本；`subprocess.run/call/
check_output` 内部都是走 Popen，所以一处生效、全体生效。幂等，可重复调用。
"""
import os
import subprocess

_CREATE_NO_WINDOW = 0x08000000


def hide_child_windows():
    """幂等：把所有子进程的 creationflags 默认补上 CREATE_NO_WINDOW。"""
    if os.name != "nt":
        return False
    popen = subprocess.Popen
    if getattr(popen, "_persona_no_window", False):
        return True

    def _popen(*args, **kwargs):
        kwargs.setdefault("creationflags", _CREATE_NO_WINDOW)
        return popen(*args, **kwargs)

    _popen._persona_no_window = True
    _popen._persona_original = popen
    subprocess.Popen = _popen
    return True


# 被 import 就自动生效——省得每个脚本忘了调
hide_child_windows()
