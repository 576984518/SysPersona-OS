# -*- coding: utf-8 -*-
r"""persona_tray.py — 托盘状态灯：多客户端并列显示 + 实时水位

设计（2026-11-10 重写）：
- 有几个 agent 客户端在跑，就并列显示几个，各自带自己的水位，互不覆盖。
  DSH 的水位**只认壳的 `pressureTokens`**（`persona_gauge.dsh_pressure()`，壳判压缩
  用的就是那个数）；Hermes 的来自守护写的 `ops\hctx.json`；没有水位源的客户端只报「在跑」。
  `ops\dsh-ctx.json`（账本增量）只留痕/审计，**不再参与显示**——同一个数只能有一个来源。
- 图标是同心进度环：最外圈第一个客户端、往里第二圈……环长=各自占用百分比。
- 悬停提示一行并列：`DSH 123K/1.0M 12% · Hermes 125K/1.0M 12%`。
- 菜单「状态」：逐个客户端 + 守护各任务最近一次结果。
- 只读不代办：托盘不派活、不改状态，只显示。

启动：
  <runtime>\python\pythonw.exe persona_tray.py      # 或 $env:PERSONA_PY
自检：
  同上 --probe 只打印一帧状态然后退出（不进托盘）。

环境变量：SYSPERSONA_ROOT（旧名 PERSONA_ROOT / MEM / SYSPERSONA_MEM 也认）指记忆库根；
不设就按脚本自身目录向上找含 ops\ 与 sessions\ 的目录，再找不到直接报错——不再写死盘符。
"""
import json
import os
import subprocess
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw

try:
    from persona_root import root as _persona_root
except ImportError:   # 仓库布局：tools\tray\ → tools\ctxpack\
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ctxpack"))
    from persona_root import root as _persona_root


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
    found = _persona_root(strict=False)
    if found:
        return found
    raise SystemExit("找不到记忆库根：请设 SYSPERSONA_ROOT（或 MEM）指向记忆库目录；"
                     "托盘不再退回某台机器的路径。")


ROOT = _resolve_root()
OPS = os.path.join(ROOT, "ops")
STATUS = os.path.join(OPS, "persona-status.json")
HCTX = os.path.join(OPS, "hctx.json")
LOG = os.path.join(OPS, "persona.log")
PY = os.path.join(os.path.dirname(sys.executable), "python.exe")
GAUGE = os.path.join(ROOT, "persona_gauge.py")

# 注入告警（2026-11-10）：状态/三档判定/提示音都在记忆库的 persona_inject.py 里，
# 托盘只读它，阈值不在这里复制一份。
# 不能直接 `import persona_inject`：仓库 tools\ctxpack 下也有一份旧版，会抢先命中。
def _load_inject():
    path = os.path.join(ROOT, "persona_inject.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("persona_inject_live", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


INJ = _load_inject()
if INJ is not None and not hasattr(INJ, "evaluate_state"):
    INJ = None          # 旧版 persona_inject（没有告警函数）：托盘照常跑，只是不显示注入档

STALE = 180          # 秒：状态文件多久没更新算守护挂了
GAUGE_EVERY = 60     # 秒：DSH 水位多久读一次（直接读壳的缓存文件，很轻；60 秒够用）
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
_DSH = {"at": 0.0, "ctx": None, "max": None, "pct": None, "note": ""}
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


def human_win(n):
    """窗口大小：先按两位小数取整再显示一位（1.048576M → 1.1M）。

    壳的 contextWindow 真值是 1,048,576；旧版是把 gauge 打印的「上限 1.05M」再
    四舍五入成 1.1M。这里保持同一显示，**不因为换了数据源就改视觉**。
    """
    if n is None:
        return "?"
    if n >= 1000000:
        return "%.1fM" % round(n / 1000000.0, 2)
    return human(n)


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
# 2026-11-10：**显示只认壳的 pressureTokens**（persona_gauge.dsh_pressure()）。
# `ops\dsh-ctx.json` 是账本增量（两次快照相减/调用数），和壳判压缩用的那个数实测
# 差一百多 K；它现在只留痕/审计，**不再参与显示**——同一个数只能有一个来源。
WIN_FALLBACK = 1048576      # 壳的 contextWindow 真值（1 MiB）：窗口读不到时按它显示


def _load_gauge():
    """从记忆库直接加载 persona_gauge，用它的 `dsh_pressure()` 取数。

    不能 `import persona_gauge`：仓库 tools\\ctxpack 下也有一份旧版会抢先命中。
    也不能去解析 gauge 的 stdout：`[上下文]` 那行走的是兜底链
    （pressureTokens → 账本增量 → Hermes → 账本平均），pressureTokens 缺失时它会
    悄悄换成别处的数——那就又变成「两个入口两个水位」了。
    """
    path = os.path.join(ROOT, "persona_gauge.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("persona_gauge_live", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod if hasattr(mod, "dsh_pressure") else None
    except Exception:
        return None


GAUGE_MOD = _load_gauge()
# 阈值只在 persona_limits.py 定义；gauge 已经把它 import 进来了，这里借它的。
_LIMIT = getattr(GAUGE_MOD, "LIMIT", None) or 1000000


def _legacy_dsh():
    """**旧读法**（只在 import 失败时降级用）：跑 persona_gauge.py 解析 stdout。

    返回 (ctx, max, pct)，一行都解析不出来就全 None。注意这一行可能是兜底链给的数
    （含 ops\\dsh-ctx.json 的 last），所以调用方必须在 tooltip 上明标 `(旧读法)`，
    不许静默冒充壳口径。
    """
    ctx = mx = pct = None
    try:
        out = subprocess.run([PY, GAUGE], capture_output=True, text=True,
                             timeout=60, encoding="utf-8", errors="replace",
                             creationflags=NO_WINDOW).stdout or ""
        for line in out.splitlines():
            if "[上下文]" in line:
                seg = line.split("·")
                ctx = _parse_k(seg[0].split("]")[-1].strip().lstrip("~"))
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
    return ctx, mx, pct


def dsh_watermark(force=False):
    """DSH 水位：只认壳的 pressureTokens；读不到就 ctx=None，**不许拿旧数冒充**。"""
    now = time.time()
    with _LOCK:
        if not force and _DSH["at"] and now - _DSH["at"] < GAUGE_EVERY:
            return dict(_DSH)
    press = mx = pct = None
    note = ""
    if GAUGE_MOD is not None:
        try:
            press, _surface, win = GAUGE_MOD.dsh_pressure()
        except Exception:
            press, win = 0, 0
        mx = win or WIN_FALLBACK
        if press and press > 0:
            pct = 100.0 * press / (win or _LIMIT)
        else:
            press, pct = None, None      # 读数不可用：显示 ?，不冒充
            note = " 读数不可用"
    else:
        press, mx, pct = _legacy_dsh()   # 降级：旧读法，明标
        note = " (旧读法)"
        if press is None:
            note += " 读数不可用"
        elif mx is None:
            mx = WIN_FALLBACK
    with _LOCK:
        _DSH.update({"at": now, "ctx": press, "max": mx, "pct": pct, "note": note})
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
        rows.append({"name": "DSH", "alive": True, "procs": n, "wm": True,
                     "ctx": d.get("ctx"), "max": d.get("max"), "pct": d.get("pct"),
                     "note": d.get("note") or ""})

    h = hermes_watermark()
    hn = counts.get("hermes.exe", 0) + counts.get("Hermes.exe", 0)
    if hn:
        rows.append({"name": "Hermes", "alive": True, "procs": hn, "wm": bool(h),
                     "ctx": (h or {}).get("ctx"), "max": (h or {}).get("max"),
                     "pct": (h or {}).get("pct"), "note": ""})

    for img, label in CLIENTS:
        if label in ("DSH", "Hermes"):
            continue
        c = counts.get(img, 0)
        if c:
            rows.append({"name": label, "alive": True, "procs": c, "wm": False,
                         "ctx": None, "max": None, "pct": None, "note": ""})
    return rows


def wm_part(r):
    """一个客户端在 tooltip 里的水位片段。

    正常：`DSH 392K/1.1M 37%`（格式与旧版一字不差）。
    读数不可用：`DSH ?/1.1M 读数不可用`——**宁可显示 ?，也不拿旧数冒充**。
    """
    if not r.get("wm"):
        return "%s 在跑" % r["name"]
    core = "%s %s/%s" % (r["name"],
                         human(r["ctx"]) if r["ctx"] is not None else "?",
                         human_win(r["max"]))
    if r["ctx"] is not None and r["pct"] is not None:
        core += " %.0f%%" % r["pct"]
    return core + (r.get("note") or "")


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
    inj = INJ.evaluate_state() if INJ else None

    # 竖条顺序 = 客户端顺序（位置固定，便于辨认：左=DSH，右=下一个）
    bars = [(color_of(r["pct"]), r["pct"], r["ctx"] is not None) for r in rows]

    parts = [wm_part(r) for r in rows]
    title = "SysPersona · " + (" · ".join(parts) if parts else "没有客户端在跑")
    if not ok:
        title += " · 守护异常"
    if inj and inj["level"] != "ok":
        title += " · " + inj["text"]
    return {"bars": bars, "title": title, "rows": rows, "daemon": (ok, msg, tasks),
            "inject": inj}


# ── 画图标：一个客户端一个正方形，几个就并排几个 ─────────────────────
def make_icon(bars, alarm="ok"):
    """bars = [(color, pct, 有无水位)]；只放「正在运行」的客户端。

    n 个客户端 → 画布横向切成 n 个正方形（边长 = 64/n），从左到右排开：
    1 个 = 一个满尺寸正方形，2 个 = 两个并排，4 个 = 四个；正方形里从底部
    往上填充水位高度，颜色按各自水位（绿 / 黄 / 红）。

    alarm == "warn"/"error" 时整块底色变黄/红（注入发不出去），条块压成白色，
    保证一眼能看出「告警」而不是普通水位。
    """
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    n = len(bars)
    alarm_bg = {"warn": (230, 170, 40), "error": (220, 60, 60)}.get(alarm)
    if alarm_bg:
        d.rectangle((0, 0, S - 1, S - 1), fill=alarm_bg + (255,))
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
            d.rectangle((x, y0 + side - h, x + side - 1, y0 + side - 1),
                        fill=((255, 255, 255) if alarm_bg else color) + (255,))
        d.rectangle((x, y0, x + side - 1, y0 + side - 1),
                    outline=((30, 30, 30) if alarm_bg else color) + (255,), width=1)
    return img


# ── 弹窗：详细状态 ───────────────────────────────────────────────────
def detail_text(s):
    out = [s["title"], ""]
    inj = s.get("inject")
    if inj:
        out.append("【注入】%s" % inj.get("text", ""))
        out.append("")
    out.append("【客户端】")
    if s["rows"]:
        for r in s["rows"]:
            if r["ctx"] is not None:
                wm = "%s / %s%s" % (human(r["ctx"]), human_win(r["max"]),
                                    r.get("note") or "")
            elif r.get("wm"):
                wm = "? / %s%s" % (human_win(r["max"]),
                                   r.get("note") or " 读数不可用")
            else:
                wm = "无水位源"
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
            icon.icon = make_icon(s["bars"], (s.get("inject") or {}).get("level", "ok"))
            icon.title = s["title"][:120]
            if INJ:
                INJ.check_alert()      # 上升沿才响；marker 落盘，不会每轮都响
        except Exception:
            pass
        time.sleep(TICK)


def retry_inject(icon=None, item=None):
    """菜单「立即重试一次注入」：只跑探针（选窗+抢焦点），不真发内容。"""
    script = os.path.join(ROOT, "persona_inject.py")
    try:
        r = subprocess.run([PY, script, "--probe"], cwd=ROOT, capture_output=True,
                           text=True, timeout=40, encoding="utf-8", errors="replace",
                           creationflags=NO_WINDOW)
        msg = ((r.stdout or "") + (r.stderr or "")).strip() or ("exit %d" % r.returncode)
    except Exception as e:
        msg = "探针失败：%s" % e
    _dialog(msg[:800], "立即重试一次注入（只探测，不发内容）")



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
        s = snapshot()
        ev = INJ.check_alert() if INJ else None
        if ev:
            print("[托盘] 注入档位=%s → 图标底色=%s" % (
                ev.get("level"),
                {"error": "红", "warn": "黄"}.get(ev.get("level"), "无告警（按水位上色）")))
        print(detail_text(s))
        if ev:
            if ev.get("beeped"):
                print("[提示音] 响（上升沿 %s → %s；MessageBeep %s）" % (
                    ev.get("prev"), ev.get("level"),
                    "MB_ICONHAND" if ev.get("level") == "error" else "MB_ICONEXCLAMATION"))
            elif ev.get("level") == "ok":
                print("[提示音] 不响（正常/已恢复；上次报过 %s）" % ev.get("prev"))
            else:
                print("[提示音] 不响（当前 %s 与上次已报的 %s 同档，只响一次）" % (
                    ev.get("level"), ev.get("prev")))
        return
    s = snapshot()
    icon = pystray.Icon(
        "SysPersona",
        icon=make_icon(s["bars"], (s.get("inject") or {}).get("level", "ok")),
        title=s["title"][:120],
        menu=pystray.Menu(
            pystray.MenuItem("状态", show_status, default=True),
            pystray.MenuItem("立即重试一次注入", retry_inject),
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
