# -*- coding: utf-8 -*-
r"""persona_tray.py — 托盘状态灯：看得见「谁在跑」和「水位到哪」

设计（2026-11-10 重写）：
- 图标是一圈进度环：外圈颜色=水位等级，环长=占用百分比，中间不用字（16px 看不清）。
- 悬停提示（title）一行给出：水位 + 哪个客户端在跑 + 守护是否活着。
- 菜单 →「状态」：列出所有客户端与各任务最近一次结果。
- 只读不代办：托盘不派活、不改状态，只显示。

启动：
  D:\ProgramData\SysPersona-Runtime\python\pythonw.exe persona_tray.py

自检：加上 --probe 只打印一帧状态然后退出（不进托盘）。
"""
import json
import os
import subprocess
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw

# ── 路径：以记忆库为准（托盘在代码仓库里，ops\ 在记忆库） ──────────────
def _resolve_root():
    env = os.environ.get("MEM") or os.environ.get("SYSPERSONA_MEM")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "ops")) and os.path.isdir(os.path.join(cur, "sessions")):
            return cur
        cur = os.path.dirname(cur)
    return r"D:\4590-PC-文件\AI会话检索记录"


ROOT = _resolve_root()
OPS = os.path.join(ROOT, "ops")
STATUS = os.path.join(OPS, "persona-status.json")
HCTX = os.path.join(OPS, "hctx.json")
LOG = os.path.join(OPS, "persona.log")
PY = os.path.join(os.path.dirname(sys.executable), "python.exe")
GAUGE = os.path.join(ROOT, "persona_gauge.py")

STALE = 180          # 秒：状态文件多久没更新算守护挂了
GAUGE_EVERY = 60     # 秒：DSH 水位多久测一次（gauge 一次约 1.7 秒，别太勤）
TICK = 5             # 秒：托盘刷新间隔

# ok=False 但「设计如此」的任务：agent 是保活项，配置为只报不拉，永远报 false。
BENIGN_FAIL = {"agent"}

# pythonw 是 GUI 进程，直接 spawn 控制台程序会弹黑窗——必须一律带上这个标志。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 客户端识别：进程名 → 显示名
CLIENTS = [
    ("DeepSeek Harness.exe", "DSH"),
    ("hermes.exe", "Hermes"),
    ("Hermes.exe", "Hermes"),
    ("cherry-studio.exe", "Cherry"),
    ("Cursor.exe", "Cursor"),
]
_CLI_CACHE = {"at": 0.0, "rows": []}
_DSH = {"at": 0.0, "ctx": None, "max": None, "pct": None}
_LOCK = threading.Lock()


# ── 小工具 ───────────────────────────────────────────────────────────
def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def file_age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 1e9


def human(n):
    if n is None:
        return "?"
    if n < 1000:
        return str(n)
    return "%.0fK" % (n / 1000.0)


# ── 客户端：谁在跑 ───────────────────────────────────────────────────
def running_clients():
    """tasklist 一次 100ms，缓存 20 秒（越少 spawn 越安静）。"""
    now = time.time()
    with _LOCK:
        if now - _CLI_CACHE["at"] < 20 and _CLI_CACHE["rows"]:
            return _CLI_CACHE["rows"]
    rows = []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             encoding="mbcs", errors="replace",
                             creationflags=NO_WINDOW).stdout
        names = {}
        for line in out.splitlines():
            parts = line.strip().strip('"').split('","')
            if not parts or not parts[0]:
                continue
            names[parts[0]] = names.get(parts[0], 0) + 1
        for img, label in CLIENTS:
            n = names.get(img, 0)
            if n:
                rows.append((label, True, "%d 进程" % n))
    except Exception as e:
        rows.append(("进程表读取失败", False, str(e)[:40]))
    with _LOCK:
        _CLI_CACHE["at"], _CLI_CACHE["rows"] = now, rows
    return rows


# ── 水位：DSH 走 gauge，Hermes 走 hctx.json ──────────────────────────
def dsh_watermark(force=False):
    now = time.time()
    with _LOCK:
        if not force and now - _DSH["at"] < GAUGE_EVERY and _DSH["ctx"] is not None:
            return dict(_DSH)
    ctx = mx = pct = None
    try:
        out = subprocess.run([PY, GAUGE], capture_output=True, text=True,
                             timeout=60, encoding="utf-8", errors="replace",
                             creationflags=NO_WINDOW).stdout or ""
        for line in out.splitlines():
            if "[上下文]" in line:
                # [上下文] 123K · OK（12.3% / 上限 1.00M）· 调用 245 次
                seg = line.split("·")
                ctx = _parse_k(seg[0].split("]")[-1].strip())
                for s in seg[1:]:
                    if "%" in s:
                        try:
                            pct = float(s.split("（")[1].split("%")[0])
                        except Exception:
                            pass
                    if "上限" in s:
                        mx = _parse_k(s.split("上限")[-1].split("）")[0].strip())
                break
    except Exception:
        pass
    with _LOCK:
        _DSH.update({"at": now, "ctx": ctx, "max": mx, "pct": pct})
        return dict(_DSH)


def _parse_k(s):
    s = s.strip().replace(",", "")
    try:
        if s.upper().endswith("K"):
            return int(float(s[:-1]) * 1000)
        if s.upper().endswith("M"):
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except Exception:
        return None


def hermes_watermark():
    d = read_json(HCTX)
    if not d:
        return None
    if file_age(HCTX) > 600:
        return None
    return {"ctx": d.get("ctx"), "max": d.get("max"), "pct": d.get("pct"),
            "model": d.get("model")}


# ── 守护健康 ─────────────────────────────────────────────────────────
def daemon_health():
    st = read_json(STATUS)
    if not st:
        return False, "状态文件缺失", {}
    tasks = st.get("tasks") or {}
    # 有些任务 ok=False 是「设计如此」，不是故障：agent 是「只报不拉」的保活项。
    failed = [k for k, v in tasks.items()
              if v.get("ok") is False and k not in BENIGN_FAIL]
    busy = [k for k, v in tasks.items() if v.get("busy")]
    age = file_age(STATUS)
    if age > STALE:
        return False, "守护可能已停（%d 分钟未更新）" % int(age // 60), tasks
    if failed:
        return False, "任务失败：%s" % ",".join(failed[:3]), tasks
    if busy:
        return True, "忙碌中：%s" % ",".join(busy[:3]), tasks
    return True, "正常", tasks


# ── 汇总一帧 ─────────────────────────────────────────────────────────
def snapshot():
    clients = running_clients()
    dsh = dsh_watermark()
    hm = hermes_watermark()
    ok, msg, tasks = daemon_health()

    wm = dsh if dsh.get("ctx") else hm
    pct = (wm or {}).get("pct")
    ctx = (wm or {}).get("ctx")
    mx = (wm or {}).get("max")
    who = "DSH" if dsh.get("ctx") else ("Hermes" if hm else "—")

    if not ok:
        color = (220, 60, 60)
    elif pct is not None and pct >= 80:
        color = (220, 60, 60)
    elif pct is not None and pct >= 35:
        color = (230, 170, 40)
    else:
        color = (60, 170, 90)

    title = "SysPersona · 水位 %s/%s (%s) · %s" % (
        human(ctx), human(mx), ("%.0f%%" % pct) if pct is not None else "?", who)
    if not ok:
        title += " · 守护异常"
    return {"color": color, "title": title, "pct": pct, "clients": clients,
            "dsh": dsh, "hermes": hm, "daemon": (ok, msg, tasks)}


# ── 画图标：进度环 ───────────────────────────────────────────────────
def make_icon(color, pct):
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, S - 6, S - 6), outline=(70, 70, 70, 255), width=7)
    frac = 0.0 if pct is None else max(0.0, min(1.0, pct / 100.0))
    if frac > 0:
        d.arc((6, 6, S - 6, S - 6), start=-90, end=-90 + int(360 * frac),
              fill=color + (255,), width=7)
    d.ellipse((24, 24, S - 24, S - 24), fill=color + (255,))
    return img


# ── 弹窗：详细状态 ───────────────────────────────────────────────────
def detail_text(s):
    out = [s["title"], ""]
    out.append("【客户端】")
    if s["clients"]:
        for name, alive, note in s["clients"]:
            out.append("  %s %s" % ("●" if alive else "○", name) + ("  " + note if note else ""))
    else:
        out.append("  （没检测到已知客户端）")
    out.append("")
    out.append("【水位】")
    d, h = s["dsh"], s["hermes"]
    out.append("  DSH    %s" % ("%s / %s" % (human(d.get("ctx")), human(d.get("max")))
                                if d.get("ctx") else "未测到"))
    out.append("  Hermes %s" % ("%s / %s" % (human(h.get("ctx")), human(h.get("max")))
                                if h else "未在跑"))
    out.append("")
    ok, msg, tasks = s["daemon"]
    out.append("【守护】%s %s" % ("●" if ok else "●", msg))
    for name in ("hctx", "export", "index", "backup2", "todo", "proxy"):
        t = tasks.get(name)
        if not t:
            continue
        out.append("  %-8s %s  %s" % (name, "OK " if t.get("ok") else "FAIL",
                                      (t.get("msg") or "")[:50]))
    return "\n".join(out)


def show_status(icon=None, item=None):
    text = detail_text(snapshot())
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo("SysPersona 状态", text)
        root.destroy()
    except Exception:
        print(text)


def open_log(icon=None, item=None):
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


# ── 主循环 ───────────────────────────────────────────────────────────
def refresh(icon):
    while True:
        try:
            s = snapshot()
            icon.icon = make_icon(s["color"], s["pct"])
            icon.title = s["title"][:120]
        except Exception:
            pass
        time.sleep(TICK)


def main():
    if "--probe" in sys.argv:
        print(detail_text(snapshot()))
        return
    s = snapshot()
    icon = pystray.Icon(
        "SysPersona",
        icon=make_icon(s["color"], s["pct"]),
        title=s["title"][:120],
        menu=pystray.Menu(
            pystray.MenuItem("状态", show_status, default=True),
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
