# -*- coding: utf-8 -*-
r"""persona_inject.py — 把一段文字「注入」到正在运行的壳（DSH）里，当作他发的一句话

为什么这么做（2026-11-10 定，B71）：
- 壳没有对外的「发消息」接口：`dsh headless --session-id` 虽然能 resume 会话，但它要
  独立凭据（`MISSING_CREDENTIAL` / `ACCOUNT_SIGN_IN_REQUIRED`），而且会和开着的界面抢写；
- 所以走**界面层注入**：找到壳的主窗口 → 把文字放进剪贴板 → 聚焦窗口 → Ctrl+V → 回车。
  对壳来说，和用户自己打字发出去完全一样，不碰模型、不碰凭据、不改会话文件。

规矩：
- 只找**主窗口**（`Chrome_WidgetWin_1` + 进程 `DeepSeek Harness.exe`），不猜、不点坐标；
- 注入前保存剪贴板，注入后还原——不弄丢他复制的东西；
- `--dry-run` 只报告会做什么，不动手；
- 每次注入都记一行 `ops/inject.jsonl`（时间 / 目标会话标题 / 字数 / 结果）。

用法：
  python persona_inject.py --text "【唤醒简讯】……"
  python persona_inject.py --file brief.md
  python persona_inject.py --text "..." --dry-run
  python persona_inject.py --list          # 列出可见的壳窗口
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
LOG = os.path.join(OPS, "inject.jsonl")

u = ctypes.windll.user32
k = ctypes.windll.kernel32

# 64 位下必须声明返回类型，否则句柄被截断成 0（2026-11-10 实踩：写剪贴板直接崩）
k.GlobalAlloc.restype = ctypes.c_void_p
k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k.GlobalLock.restype = ctypes.c_void_p
k.GlobalLock.argtypes = [ctypes.c_void_p]
k.GlobalUnlock.argtypes = [ctypes.c_void_p]
u.GetClipboardData.restype = ctypes.c_void_p
u.GetClipboardData.argtypes = [ctypes.c_uint]
u.SetClipboardData.restype = ctypes.c_void_p
u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

WM_SETTEXT = 0x000C
SW_RESTORE = 9
CF_UNICODETEXT = 13
VK_CONTROL, VK_V, VK_RETURN = 0x11, 0x56, 0x0D
KEYEVENTF_KEYUP = 0x0002


def _proc_name(pid):
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(300)
    size = wt.DWORD(300)
    ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k.CloseHandle(h)
    return buf.value.split("\\")[-1] if ok else ""


def list_windows():
    """所有可见的壳窗口：(hwnd, 标题, 进程名, 类名)"""
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(hwnd, cls, 128)
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = _proc_name(pid.value)
        if "DeepSeek Harness" not in name:
            return True
        t = ctypes.create_unicode_buffer(n + 2)
        u.GetWindowTextW(hwnd, t, n + 2)
        out.append((hwnd, t.value, name, cls.value))
        return True

    u.EnumWindows(cb, 0)
    return out


def pick_window():
    wins = [w for w in list_windows() if "Chrome_WidgetWin_1" == w[3]]
    return wins[0] if wins else None


def get_clipboard():
    if not u.OpenClipboard(None):
        return ""
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def set_clipboard(text):
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    if not u.OpenClipboard(None):
        return False
    try:
        h = k.GlobalAlloc(0x0042, size)      # GMEM_MOVEABLE | GMEM_ZEROINIT
        if not h:
            return False
        p = k.GlobalLock(h)
        if not p:
            k.GlobalFree(h) if hasattr(k, "GlobalFree") else None
            return False
        try:
            ctypes.memmove(p, ctypes.addressof(data), size)
        finally:
            k.GlobalUnlock(h)
        u.EmptyClipboard()
        u.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        u.CloseClipboard()


def paste_and_enter():
    u.keybd_event(VK_CONTROL, 0, 0, 0)
    u.keybd_event(VK_V, 0, 0, 0)
    u.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    u.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.4)
    u.keybd_event(VK_RETURN, 0, 0, 0)
    u.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)


def last_input_age():
    """他上一次动鼠标键盘是多少秒前（防打断用）。"""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not u.GetLastInputInfo(ctypes.byref(li)):
        return 1e9
    return max(0.0, (k.GetTickCount() - li.dwTime) / 1000.0)


def focus_window(hwnd):
    """把窗口拿到前台，但**不改他的窗口状态**（最大化就保持最大化）。

    2026-11-10 踩坑：原来无条件 ShowWindow(SW_RESTORE)，把用户最大化的窗口还原成小窗——
    消息没注入成功，倒先把人的窗口弄乱了。现在只有最小化时才还原。
    """
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
    if u.GetForegroundWindow() == hwnd:
        return True
    fg = u.GetForegroundWindow()
    t_fg = u.GetWindowThreadProcessId(fg, None)
    t_me = k.GetCurrentThreadId()
    attached = False
    try:
        attached = bool(u.AttachThreadInput(t_fg, t_me, True))
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.SetFocus(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            u.AttachThreadInput(t_fg, t_me, False)
    time.sleep(0.4)
    return u.GetForegroundWindow() == hwnd


def inject(text, dry_run=False, settle=1.2, wait_idle=5):
    win = pick_window()
    if not win:
        return False, "没找到壳的主窗口（DeepSeek Harness 是否在跑？）"
    hwnd, title, name, _cls = win
    brief = text.strip()
    if not brief:
        return False, "正文为空"
    if dry_run:
        return True, "干跑：将注入 %d 字到窗口「%s」（窗口句柄 %s）" % (len(brief), title[:40], hwnd)
    # 防打断：他正在打字就先等，别把人输入框里的字冲掉
    if wait_idle:
        age = last_input_age()
        if age < wait_idle:
            time.sleep(min(60.0, wait_idle - age + 0.5))
    old = get_clipboard()
    try:
        if not set_clipboard(brief):
            return False, "写剪贴板失败"
        got = focus_window(hwnd)
        time.sleep(settle)
        paste_and_enter()
        time.sleep(0.8)
        return True, "已注入 %d 字到「%s」%s" % (len(brief), title[:40],
                                                "" if got else "（注意：没能抢到前台焦点）")
    finally:
        time.sleep(0.3)
        set_clipboard(old)                   # 还原他的剪贴板


def log(rec):
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="把文字注入到壳里（当作用户发的一句话）")
    ap.add_argument("--text", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--wait-idle", type=int, default=5, help="他最近 N 秒在动就先等")
    a = ap.parse_args()

    if a.list:
        ws = list_windows()
        if not ws:
            print("没有可见的壳窗口")
        for hwnd, title, name, cls in ws:
            print("hwnd=%s 类=%s 进程=%s 标题=%s" % (hwnd, cls, name, title[:50]))
        return

    text = a.text
    if a.file and os.path.exists(a.file):
        text = io.open(a.file, encoding="utf-8").read()
    ok, msg = inject(text, dry_run=a.dry_run, wait_idle=a.wait_idle)
    log({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "len": len((text or "").strip()),
         "dry": bool(a.dry_run), "ok": ok, "msg": msg})
    print(("OK   " if ok else "FAIL ") + msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
