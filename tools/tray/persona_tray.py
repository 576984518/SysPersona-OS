# -*- coding: utf-8 -*-
r"""persona_tray.py — 托盘状态灯：多客户端并列显示 + 实时水位

设计（2026-11-10 重写）：
- 有几个 agent 客户端在跑，就并列显示几个，各自带自己的水位，互不覆盖。
  DSH 的水位来自 `persona_gauge.py`，Hermes 的来自守护写的 `ops\hctx.json`；
  没有水位源的客户端只报「在跑」。
- 图标是同心进度环：最外圈第一个客户端、往里第二圈……环长=各自占用百分比。
- 悬停提示一行并列：`DSH 123K/1.0M 12% · Hermes 125K/1.0M 12%`。
- 菜单「状态」：逐个客户端 + 守护各任务最近一次结果。
- 只读不代办：托盘不派活、不改状态，只显示。

启动：
  D:\ProgramData\SysPersona-Runtime\python\pythonw.exe persona_tray.py
自检：
  同上 --probe 只打印一帧状态然后退出（不进托盘）。
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
PROC_EVERY = 20      # 秒：进程表缓存
TICK = 5             # 秒：托盘刷新间隔

# ok=False 但「设计如此」的任务：agent 是保活项，配置为只报不拉，永远报 false。
BENIGN_FAIL = {"agent"}

# pythonw 是 GUI 进程，直接 spawn 控制台程序会弹黑窗——必须一律带上这个标志。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 客户端识别：进程名 → 显示名（顺序即图标里环的顺序）
CLIENTS = [
    ("DeepSeek Harness.exe", "DSH"),
    ("hermes.exe", "Hermes"),
    ("Hermes.exe", "Hermes"),
    ("cherry-studio.exe", "Cherry"),
    ("Cursor.exe", "Cursor"),
]

_PROC = {"at": 0.0, "counts": {}}
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
    if n < 1000000:
        return "%.0fK" % (n / 1000.0)
    return "%.1fM" % (n / 1000000.0)


def color_of(pct):
    if pct is None:
        return (60, 170, 90)
    if pct >= 80:
        return (220, 60, 60)
    if pct >= 35:
        return (230, 170, 40)
    return (60, 170, 90)


# ── 进程表 ───────────────────────────────────────────────────────────
def proc_counts():
    now = time.time()
    with _LOCK:
        if now - _PROC["at"] < PROC_EVERY and _PROC["counts"]:
            return _PROC["counts"]
    counts = {}
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             encoding="mbcs", errors="replace",
                             creationflags=NO_WINDOW).stdout
        for line in out.splitlines():
            parts = line.strip().strip('"').split('","')
            if not parts or not parts[0]:
                continue
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    except Exception:
        pass
    with _LOCK:
        _PROC["at"], _PROC["counts"] = now, counts
    return counts


# ── 水位源 ───────────────────────────────────────────────────────────
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
    if not d or file_age(HCTX) > 600:
        return None
    return {"ctx": d.get("ctx"), "max": d.get("max"), "pct": d.get("pct")}


# ── 客户端列表：谁在跑 + 各自水位 ────────────────────────────────────
def client_rows():
    counts = proc_counts()
    rows = []

    d = dsh_watermark()
    n = counts.get("DeepSeek Harness.exe", 0)
    # 只显示「正在运行」的客户端：没运行的不进图标、不进提示。
    if n:
        rows.append({"name": "DSH", "alive": True, "procs": n,
                     "ctx": d.get("ctx"), "max": d.get("max"), "pct": d.get("pct")})

    h = hermes_watermark()
    hn = counts.get("hermes.exe", 0) + counts.get("Hermes.exe", 0)
    if hn:
        rows.append({"name": "Hermes", "alive": True, "procs": hn,
                     "ctx": (h or {}).get("ctx"), "max": (h or {}).get("max"),
                     "pct": (h or {}).get("pct")})

    for img, label in CLIENTS:
        if label in ("DSH", "Hermes"):
            continue
        c = counts.get(img, 0)
        if c:
            rows.append({"name": label, "alive": True, "procs": c,
                         "ctx": None, "max": None, "pct": None})
    return rows


# ── 守护健康 ─────────────────────────────────────────────────────────
def daemon_health():
    st = read_json(STATUS)
    if not st:
        return False, "状态文件缺失", {}
    tasks = st.get("tasks") or {}
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
    rows = client_rows()
    ok, msg, tasks = daemon_health()

    # 竖条顺序 = 客户端顺序（位置固定，便于辨认：左=DSH，右=下一个）
    bars = [(color_of(r["pct"]), r["pct"], r["ctx"] is not None) for r in rows]

    parts = []
    for r in rows:
        if r["ctx"] is not None:
            parts.append("%s %s/%s %s" % (r["name"], human(r["ctx"]), human(r["max"]),
                                          ("%.0f%%" % r["pct"]) if r["pct"] is not None else "?"))
        else:
            parts.append("%s 在跑" % r["name"])
    title = "SysPersona · " + (" · ".join(parts) if parts else "没有客户端在跑")
    if not ok:
        title += " · 守护异常"
    return {"bars": bars, "title": title, "rows": rows, "daemon": (ok, msg, tasks)}


# ── 画图标：一个客户端一个正方形，几个就并排几个 ─────────────────────
def make_icon(bars):
    """bars = [(color, pct, 有无水位)]；只放「正在运行」的客户端。

    n 个客户端 → 画布横向切成 n 个正方形（边长 = 64/n），从左到右排开：
    1 个 = 一个满尺寸正方形，2 个 = 两个并排，4 个 = 四个；正方形里从底部
    往上填充水位高度，颜色按各自水位（绿 / 黄 / 红）。
    """
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    n = len(bars)
    if n == 0:
        d.rectangle((22, 22, S - 22, S - 22), outline=(120, 120, 120, 255), width=3)
        return img
    gap = 2 if n > 1 else 0
    side = max(4, (S - gap * (n - 1)) // n)
    total = side * n + gap * (n - 1)
    x0 = (S - total) // 2
    y0 = (S - side) // 2
    for i, (color, pct, has) in enumerate(bars[:8]):
        x = x0 + i * (side + gap)
        d.rectangle((x, y0, x + side - 1, y0 + side - 1), fill=(38, 38, 38, 255))
        if has and pct is not None:
            frac = max(0.0, min(1.0, pct / 100.0))
            h = max(1, int(round(frac * side)))
            d.rectangle((x, y0 + side - h, x + side - 1, y0 + side - 1), fill=color + (255,))
        d.rectangle((x, y0, x + side - 1, y0 + side - 1), outline=color + (255,), width=1)
    return img


# ── 弹窗：详细状态 ───────────────────────────────────────────────────
def detail_text(s):
    out = [s["title"], "", "【客户端】"]
    if s["rows"]:
        for r in s["rows"]:
            wm = ("%s / %s" % (human(r["ctx"]), human(r["max"]))) if r["ctx"] is not None else "无水位源"
            out.append("  %s %-8s %-18s %s" % ("●" if r["alive"] else "○", r["name"], wm,
                                               ("%d 进程" % r["procs"]) if r["procs"] else ""))
    else:
        out.append("  （没检测到已知客户端）")
    ok, msg, tasks = s["daemon"]
    out.append("")
    out.append("【守护】%s" % msg)
    for name in ("hctx", "export", "index", "backup2", "todo", "proxy"):
        t = tasks.get(name)
        if not t:
            continue
        out.append("  %-8s %s  %s" % (name, "OK " if t.get("ok") else "FAIL",
                                      (t.get("msg") or "")[:46]))
    return "\n".join(out)



def _dialog(text, title):
    """独立线程弹框：托盘线程的消息泵不能被占住。"""
    import threading
    import ctypes

    def run():
        try:
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40 | 0x40000)
        except Exception:
            print(text)

    threading.Thread(target=run, daemon=True).start()


def show_status(icon=None, item=None):
    """原生 MessageBox。

    2026-11-10 踩坑：tkinter 的 messagebox 在 pythonw 里会**卡死**——窗口出来但
    点「确定」和「×」都没反应（Tk 根窗口被 withdraw 后模态循环收不到消息）。
    改用 Win32 的 MessageBoxW：MB_ICONINFORMATION | MB_TOPMOST，一定能关。
    """
    text = detail_text(snapshot())
    _dialog(text, "SysPersona 状态")


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
            icon.icon = make_icon(s["bars"])
            icon.title = s["title"][:120]
        except Exception:
            pass
        time.sleep(TICK)



# ── 任务池：托盘 ↔ 分身 的唯一通道（只走 ops/tasks.jsonl）────────────
TASKS = os.path.join(OPS, "tasks.jsonl")
TODO_PY = os.path.join(ROOT, "persona_tasks.py")


def _task_rows():
    """读任务池，按 id 去重，只留没结束的。"""
    if not os.path.exists(TASKS):
        return []
    latest = {}
    try:
        with open(TASKS, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                tid = r.get("id") or r.get("task_id")
                if tid:
                    latest[tid] = r
    except OSError:
        return []
    rows = [r for r in latest.values()
            if r.get("status") in ("pending", "claimed", "running")]
    rows.sort(key=lambda r: (r.get("prio", 5), r.get("at", "")))
    return rows


def pool_text(limit=8):
    rows = _task_rows()
    if not rows:
        return "任务池空"
    out = ["任务池 %d 条：" % len(rows)]
    for r in rows[:limit]:
        out.append("  p%s %-8s %s" % (r.get("prio", 5), r.get("status", ""),
                                      (r.get("title") or "")[:34]))
    if len(rows) > limit:
        out.append("  …还有 %d 条" % (len(rows) - limit))
    out.append("")
    out.append("（只读；派活由我 submit，认领由我 claim）")
    return "\n".join(out)


def show_pool(icon=None, item=None):
    _dialog(pool_text(), "任务池（只读）")


def _ask(prompt, title="SysPersona"):
    """原生输入框：用 WSH 的 InputBox（tkinter 在 pythonw 下会卡死）。"""
    import tempfile
    vbs = ("Set a = WScript.Arguments\n"
           "x = InputBox(\"\" \" & a(0), \"\" \" & a(1))\n"
           "If Len(x) > 0 Then WScript.Echo x\n")
    fd, path = tempfile.mkstemp(suffix=".vbs")
    os.close(fd)
    try:
        with open(path, "w", encoding="mbcs", errors="replace") as f:
            f.write(vbs)
        r = subprocess.run(["cscript.exe", "//nologo", path, prompt, title],
                           capture_output=True, text=True, timeout=180,
                           encoding="mbcs", errors="replace",
                           creationflags=NO_WINDOW)
        return (r.stdout or "").strip()
    except Exception:
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def submit_task(icon=None, item=None):
    """在托盘里派一条活：写进任务池，下一轮我 pool 就能看到。"""
    title = _ask("派一条任务给分身（会写进任务池，我下一轮认领）：", "派活")
    if not title:
        return
    try:
        r = subprocess.run([PY, TODO_PY, "submit", "--title", title],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace",
                           cwd=ROOT, creationflags=NO_WINDOW)
        msg = (r.stdout or r.stderr or "").strip()[:300] or "已提交"
    except Exception as e:
        msg = "提交失败：%s" % e
    _dialog(msg, "派活")


def main():
    if "--probe" in sys.argv:
        print(detail_text(snapshot()))
        return
    s = snapshot()
    icon = pystray.Icon(
        "SysPersona",
        icon=make_icon(s["bars"]),
        title=s["title"][:120],
        menu=pystray.Menu(
            pystray.MenuItem("状态", show_status, default=True),
            pystray.MenuItem("任务池（只读）", show_pool),
            pystray.MenuItem("派一条…", submit_task),
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
