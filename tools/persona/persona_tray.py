# -*- coding: utf-8 -*-
r"""persona_tray.py — 托盘状态灯（只显示与退出，不代办任何活）

设计（2026-11-09 定）：后台服务为主，人不操作；但必须看得见「它在不在跑」。
- 绿：全部正常；黄：有任务在忙；红：有任务失败 / 状态文件超过 3 分钟没更新；
- 双击：弹一个小窗，列出各任务最近一次的状态；
- 菜单：打开日志 / 打开记忆库 / 退出托盘。

启动：快捷方式或开机项，直接跑：
  <runtime>\python\pythonw.exe persona_tray.py      # 或 $env:PERSONA_PY

环境变量：不需要（ROOT 取本脚本所在目录，ops\ 就在旁边）；若单独拷走，
用 PERSONA_ROOT 指定记忆库根即可。不设任何变量也能跑。
"""
import io
import json
import os
import subprocess
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
STATUS = os.path.join(OPS, "persona-status.json")
LOG = os.path.join(OPS, "persona.log")
STALE = 180  # 秒：状态文件超过这个时间没更新，视为守护挂了

_state = {"color": (120, 120, 120), "text": "SysPersona · 读取中"}


def read_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def evaluate(st):
    if not st:
        return (150, 150, 150), "SysPersona · 状态文件缺失", []
    tasks = st.get("tasks") or {}
    lines = []
    busy = failed = False
    now = time.time()
    for name, t in sorted(tasks.items()):
        ok = t.get("ok", True)
        msg = (t.get("msg") or "").strip()
        age = now - (t.get("last") or 0)
        lines.append("%-10s %s  %s" % (name, "OK " if ok else "FAIL", msg[:60]))
        if not ok:
            failed = True
        if t.get("busy"):
            busy = True
    age = now - (st.get("at_ts") or 0)
    if failed:
        return (220, 60, 60), "SysPersona · 有任务失败", lines
    if not st.get("at_ts"):
        # 老格式没有时间戳，用文件的 mtime 兜底
        try:
            age = now - os.path.getmtime(STATUS)
        except OSError:
            age = 0
    if age > STALE:
        return (220, 60, 60), "SysPersona · 守护可能已停（%d 分钟未更新）" % int(age // 60), lines
    if busy:
        return (230, 170, 40), "SysPersona · 忙碌中", lines
    return (60, 170, 90), "SysPersona · 正常", lines


def make_icon(rgb):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=rgb + (255,), outline=(20, 20, 20, 255), width=3)
    return img


def show_status(icon=None, item=None):
    st = read_status()
    rgb, text, lines = evaluate(st)
    body = text + "\n\n" + ("\n".join(lines) if lines else "（无任务）")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo("SysPersona", body)
        root.destroy()
    except Exception:
        print(body)


def open_log(icon=None, item=None):
    """看日志：优先用 看日志.cmd（自己解对编码，乱码治本），没有才退回记事本。

    2026-11-10 他反馈「后台程序的日志打开时乱码」——文件是 UTF-8，
    记事本在没有 BOM 时按 GBK 解；现在既补了 BOM，也给了一个不依赖编辑器猜的入口。
    """
    try:
        viewer = os.path.join(ROOT, "看日志.cmd")
        if os.path.exists(viewer):
            subprocess.Popen(["cmd.exe", "/c", viewer],
                             creationflags=0x00000010)   # CREATE_NEW_CONSOLE
            return
    except Exception:
        pass
    try:
        subprocess.Popen(["notepad.exe", LOG])
    except Exception:
        pass


def open_mem(icon=None, item=None):
    try:
        os.startfile(ROOT)
    except Exception:
        pass


def quit_app(icon, item=None):
    icon.stop()


def refresh(icon):
    while True:
        rgb, text, _ = evaluate(read_status())
        if (rgb, text) != (_state["color"], _state["text"]):
            _state["color"], _state["text"] = rgb, text
            icon.icon = make_icon(rgb)
            icon.title = text
        time.sleep(5)


def pool_summary():
    """任务池只读摘要（v5.5）：只显示，不决策。"""
    p = os.path.join(OPS, "tasks.jsonl")
    if not os.path.exists(p):
        return "任务池：空"
    st = {}
    for line in io.open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        st[r.get("id")] = r
    rows = [r for r in st.values() if r.get("status") in ("pending", "claimed", "running")]
    if not rows:
        return "任务池：空"
    out = ["任务池 %d 条：" % len(rows)]
    for r in sorted(rows, key=lambda x: x.get("prio", 5))[:5]:
        out.append("  p%s %s %s" % (r.get("prio", 5), r.get("status", ""), (r.get("title") or "")[:18]))
    return "\n".join(out)


def show_pool(icon=None, item=None):
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, pool_summary(), "任务池（只读）", 0x40)


def main():
    rgb, text, _ = evaluate(read_status())
    icon = pystray.Icon(
        "SysPersona",
        icon=make_icon(rgb),
        title=text,
        menu=pystray.Menu(
            pystray.MenuItem("查看状态", show_status, default=True),
            pystray.MenuItem("任务池", show_pool),
            pystray.MenuItem("打开日志", open_log),
            pystray.MenuItem("打开记忆库", open_mem),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出托盘", quit_app),
        ),
    )
    threading.Thread(target=refresh, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
