import os as _os_shim, sys as _sys_shim
_sys_shim.path.insert(0, _os_shim.path.join(_os_shim.path.dirname(_os_shim.path.abspath(__file__)), "脚本"))
# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_selfcheck.py — 心跳自检：14 项，单项失败不拖垮整体，目标 < 8 秒
r"""persona_selfcheck.py — 心跳自检：14 项，单项失败不拖垮整体，目标 < 8 秒

每次心跳（守护 TASK selfcheck，120 秒）跑一遍，逐项判定后写 `ops\selfcheck.json`：
    {at, items: {名: {ok, status, detail}}, summary}
status 三态：`ok` / `skip`（平台不符，非 Windows 上判不了）/ `fail`。

八项：
    tray      托盘进程在不在（命令行含 persona_tray.py）       ← 仅 Windows
    daemon    守护进程 + ops\persona.log 最后一行是否 < 5 分钟  ← 仅 Windows（进程表）
    watermark 壳口径 pressureTokens 与 ops\dsh-ctx.json 最近一次读数是否一致（≤5%）
    cdp       127.0.0.1:9222/json/version 是否有响应；recapture.lock 超 35 分钟只报 lock_stale
    inject    注入闸：连续失败 ≥3 或距上次成功 >60 分钟（阈值只认 persona_inject）← 仅 Windows
    queue     inflight 条数 / tasks 未完成条数（只报数字，不判红）
    cost      今日 ¥ + calls + 余额（ledger 读不到就判红；余额 < ¥5 每天一次 warn）
    capture   最近两天最新会话正文的年龄 < 6 小时（当地 0–8 点放宽到 12 小时）
    index     index\L1.jsonl 的 mtime 同上阈值（索引停建＝检索悄悄变瞎）
    backup    第二备份根里最新的文件 < 48 小时（没配 backup2_root 判红，不猜）
    disk      C: / D: 剩余空间 > 10%
    tasks     SysPersona-* 计划任务数量（首次跑记基线，之后只防丢，新增不算异常）← 仅 Windows
    model     本地档 / 免费档能否 GET /v1/models（1 小时探一次、不花 token，有一档活即 OK）
    manifest  代码/配置与自检基线（ops\manifest.json）是否一致：改了没登记就红
              （登记：`python 脚本/persona_manifest.py --record "说明"`）

平台归类：托盘/守护靠 wmic/PowerShell 枚举进程表，注入闸依赖 ctypes.wintypes + user32，
这三项在非 Windows 上判 `skip`（detail 写「平台不符（非 Windows）：…」），不判 `fail`；
Windows 上照旧判 fail，真失败不许降级成 skip。

用法：
    python persona_selfcheck.py --once     # 一行汇总（心跳用 --quiet）
    python persona_selfcheck.py --json     # 整份 JSON
    python persona_selfcheck.py --quiet    # 只在状态**变化**时追加 ops\persona.log 一行
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WIN = os.name == "nt"

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


# 控制台 / 守护的 run() 都按 utf-8 解；pythonw 下 stdout 可能是 None。
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None:
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── 路径：一律走 persona_root，不写死盘符 ────────────────────────────
def _resolve_root():
    env = (os.environ.get("SYSPERSONA_ROOT") or os.environ.get("PERSONA_ROOT")
           or os.environ.get("MEM") or os.environ.get("SYSPERSONA_MEM"))
    if env and os.path.isdir(env):
        return env
    cur = BASE
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "ops")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    try:
        from persona_root import root as _r
        found = _r(strict=False)
        if found and os.path.isdir(found):
            return found
    except Exception:
        pass
    try:
        from persona_root import find_root as _f
        found = _f()
        if found and os.path.isdir(found):
            return found
    except Exception:
        pass
    return BASE


ROOT = _resolve_root()
OPS = os.path.join(ROOT, "ops")
OUT = os.path.join(OPS, "selfcheck.json")
ALERT_MARK = os.path.join(OPS, "selfcheck-alert.json")
ALERTS = os.path.join(OPS, "alerts.jsonl")
LOG = os.path.join(OPS, "persona.log")
STATUS = os.path.join(OPS, "persona-status.json")
DSH_CTX = os.path.join(OPS, "dsh-ctx.json")
INJECT_STATE = os.path.join(OPS, "inject-state.json")
INFLIGHT = os.path.join(OPS, "inflight.json")
TASKS = os.path.join(OPS, "tasks.jsonl")
RECAPTURE_LOCK = os.path.join(OPS, "recapture.lock")

CDP_URL = "http://127.0.0.1:9222/json/version"
LOG_STALE = 300          # 秒：心跳 30s，5 分钟不动就是死了
LOCK_STALE = 35 * 60     # 秒：抓取锁超过 35 分钟只报，不删
BALANCE_LOW = 5.0        # 元
ORDER = ("tray", "daemon", "paths", "watermark", "cdp", "inject", "queue", "cost",
         "capture", "index", "backup", "disk", "tasks", "model", "manifest")
LABEL = {"tray": "托盘", "daemon": "守护", "watermark": "水位", "cdp": "CDP",
         "inject": "注入", "queue": "队列", "cost": "成本",
         "capture": "抓取", "index": "索引", "backup": "备份", "disk": "磁盘",
         "tasks": "计划任务", "model": "模型", "paths": "路径变量", "manifest": "台账"}

# 平台归类：判据本身只能在 Windows 上取到的项。非 Windows 上判 skip、不判 fail——
# 把「平台没这个能力」记成缺陷，会把真正的失败淹掉。Windows 上这里完全不生效。
WIN_ONLY = {
    "tray": "托盘进程靠 wmic/PowerShell 枚举进程表，非 Windows 上取不到",
    "daemon": "守护进程靠 wmic/PowerShell 枚举进程表，非 Windows 上取不到",
    "inject": "注入闸依赖 ctypes.wintypes / user32，非 Windows 上导入不了",
    "tasks": "计划任务靠 schtasks（Windows 工具），非 Windows 上取不到",
}

STATUS_OK = "ok"
STATUS_SKIP = "skip"
STATUS_FAIL = "fail"


# ── 小工具 ───────────────────────────────────────────────────────────
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        f.write("\n")
    os.replace(tmp, path)


def _human(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1000000:
        return "%.0fK" % (n / 1000.0)
    m = n / 1000000.0
    return ("%dM" % round(m)) if abs(m - round(m)) < 0.05 else ("%.1fM" % m)


def _tail(path, nbytes=8192):
    """读文件尾 nbytes：persona.log 已经几百 K，不整份读。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ── 进程表：wmic（实测 0.13s）→ 没有就 PowerShell Get-CimInstance ──────
_PROCS = None
_PROC_ERR = ""


def _decode(b):
    for enc in ("utf-8", "mbcs"):
        try:
            return (b or b"").decode(enc, "replace")
        except (LookupError, AttributeError):
            pass
    return (b or b"").decode("utf-8", "replace")


def proc_list():
    r"""返回 [(pid, commandline)]；读不到返回 [] 并在 _PROC_ERR 里写原因。

    2026-11-12 修（换机器后自检一直报「进程表读不到」）：
      - Win11 24H2 **没有 wmic.exe**，先判在不在，别再白等 5 秒超时；
      - 全表 `Get-CimInstance Win32_Process` 在慢机器上要 8 秒以上，6 秒超时必被打断——
        而这里只关心 python / pythonw（托盘、守护、注入器都是它），所以**查询时就过滤**。
    """
    global _PROCS, _PROC_ERR
    if _PROCS is not None:
        return _PROCS
    out = []

    if shutil.which("wmic"):
        try:
            r = subprocess.run(["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                               capture_output=True, timeout=8, creationflags=NO_WINDOW)
            text = _decode(r.stdout)
            if r.returncode == 0 and "CommandLine" in text:
                for row in csv.reader(io.StringIO(text)):
                    if len(row) < 3:
                        continue
                    cmd, pid = row[1], row[2].strip()
                    if pid.isdigit():
                        out.append((int(pid), cmd))
        except (OSError, subprocess.SubprocessError) as e:
            _PROC_ERR = "wmic 不可用：%s" % str(e)[:60]
    if out:
        _PROCS, _PROC_ERR = out, ""
        return _PROCS

    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\""
          " | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        text = _decode(r.stdout).strip()
        data = json.loads(text) if text else []
        if isinstance(data, dict):
            data = [data]
        for it in data or []:
            try:
                out.append((int(it.get("ProcessId")), str(it.get("CommandLine") or "")))
            except (TypeError, ValueError):
                continue
        _PROC_ERR = "" if out else "没有 python 进程（PowerShell 查询成功但为空）"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        _PROC_ERR = "进程表读不到：%s" % str(e)[:80]
    _PROCS = out
    return _PROCS

def _py_cmd(cmd, script, flag=""):
    """这条命令行是不是「python(w).exe ...\\<script> [flag]」本身。

    不能只做 `"脚本/persona_daemon.py" in cmd`：守护自己的 PowerShell 启动器 / DSH 的
    subprocess runner 命令行里也会带这几个字，会被误当成进程在跑。
    """
    pat = r'pythonw?\.exe["\s]+[^;|<>"\n]*%s' % re.escape(script)
    if flag:
        pat += r'["\s]+%s' % re.escape(flag)
    return re.search(pat, cmd or "", re.I) is not None


# ── 1 托盘 ───────────────────────────────────────────────────────────
def item_tray():
    procs = proc_list()
    if not procs:
        return False, "进程表读不到（%s）" % (_PROC_ERR or "空")
    hits = [pid for pid, cmd in procs if _py_cmd(cmd, "persona_tray")]   # 单文件版与 multi 版都算
    if hits:
        return True, "PID %s · 命令行含 persona_tray" % ",".join(str(p) for p in hits[:3])
    return False, "进程表里没有 persona_tray（单文件版或 multi 版）"


# ── 2 守护 ───────────────────────────────────────────────────────────
def item_daemon():
    procs = proc_list()
    hits = [pid for pid, cmd in procs
            if _py_cmd(cmd, "persona_daemon.py", "--daemon")]
    if not hits:
        return False, "没有 persona_daemon.py --daemon 进程"
    age, why = None, ""
    text = _tail(LOG)
    lines = [x for x in text.splitlines() if x.strip()]
    if lines:
        try:
            t = time.mktime(time.strptime(lines[-1][:19], "%Y-%m-%d %H:%M:%S"))
            age = max(0.0, time.time() - t)
        except (ValueError, OverflowError):
            why = "最后一行时间戳解析失败，用 mtime 兜底；"
    if age is None:
        try:
            age = max(0.0, time.time() - os.path.getmtime(LOG))
        except OSError:
            return False, "PID %s · 日志读不到" % ",".join(str(p) for p in hits[:3])
    detail = "PID %s · 日志最后一行 %d 秒前" % (",".join(str(p) for p in hits[:3]), age)
    if age > LOG_STALE:
        return False, "%s（>%d 秒，心跳停了）" % (detail, LOG_STALE)
    return True, why + detail


# ── 3 水位一致性（基准 = 托盘实际显示的那个数；同源比对）──────────────
_TRAY_MOD = None


def _load_tray():
    """按文件路径加载库根那份托盘，取 snapshot()["title"]（不启动托盘、不建图标）。"""
    global _TRAY_MOD
    if _TRAY_MOD is not None:
        return _TRAY_MOD
    path = os.path.join(ROOT, "脚本/persona_tray.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("persona_tray_selfcheck", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TRAY_MOD = mod if hasattr(mod, "snapshot") else None
    except Exception:
        _TRAY_MOD = None
    return _TRAY_MOD


def _parse_dsh_k(title):
    """从托盘 title 里取 `DSH 392K/1.1M` 的那个 392K；`?` 或没有数返回 None。"""
    m = re.search(r"DSH\s+(\?|[0-9][0-9.]*[KMG]?)\s*/", title or "")
    if not m or m.group(1) == "?":
        return None
    s = m.group(1).replace(",", "")
    try:
        if s.upper().endswith("K"):
            return int(float(s[:-1]) * 1000)
        if s.upper().endswith("M"):
            return int(float(s[:-1]) * 1000000)
        if s.upper().endswith("G"):
            return int(float(s[:-1]) * 1000000000)
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _water_state(path):
    d = _read_json(path) or {}
    try:
        return int(d.get("pressure") or 0), int(d.get("equal") or 0)
    except (TypeError, ValueError):
        return 0, 0


def item_watermark():
    try:
        import persona_gauge as g
        press, _surface, win = g.dsh_pressure()
    except Exception as e:
        return False, "读数不可用（persona_gauge 出错：%s）" % str(e)[:80]
    press = int(press or 0)

    mod = _load_tray()
    if mod is None:
        return False, "托盘读数不可用（加载 persona_tray.py 失败）"
    try:
        title = str((mod.snapshot() or {}).get("title") or "")
    except Exception as e:
        return False, "托盘读数不可用（snapshot 出错：%s）" % str(e)[:60]
    tray_val = _parse_dsh_k(title)
    if not tray_val:
        return False, "托盘读数不可用（title=%s）" % title[:80]

    # 留痕：dsh-ctx.json 的 last 口径不同，**不参与判定**，只附在 detail 里。
    rec = _read_json(DSH_CTX) or {}
    try:
        trace = int(rec.get("last") or 0)
    except (TypeError, ValueError):
        trace = 0

    diff = abs(press - tray_val)
    rel = diff / float(max(press, tray_val))
    tol_abs = 52000          # 5% 窗口（1M）≈52K
    ok = rel <= 0.05 or diff <= tol_abs

    # 粘滞检查：连续 6 轮完全相等 = 疑似读死（正常一轮工具调用一定会变）。
    prev_p, equal = _water_state(os.path.join(OPS, "selfcheck-state.json"))
    equal = (equal + 1) if (prev_p and prev_p == press) else 0
    try:
        _write_json(os.path.join(OPS, "selfcheck-state.json"),
                    {"at": _now(), "pressure": press, "equal": equal})
    except OSError:
        pass

    detail = "托盘 %s / gauge %d / 留痕 %s（差 %s / %.1f%%，阈值 5%% 或 52K）" % (
        _human(tray_val), press, _human(trace) if trace else "—",
        _human(diff), rel * 100.0)
    if equal >= 6:
        return False, detail + " · 水位疑似读死（连续 %d 轮完全相等）" % equal
    return ok, detail


# ── 4 CDP ────────────────────────────────────────────────────────────
def _capture_off():
    """抓取总闸：`ops\\persona.conf.json` 里 `"capture": 0` = 后台不抓网页。

    2026-11-12 起就是这样（风控之后人定「以后只在前台按 F5 才抓」）。
    这时候 CDP 不通是**正常的**，不能判红——否则托盘永远黄、还天天触发转红唤醒。
    """
    try:
        d = _read_json(os.path.join(OPS, "persona.conf.json")) or {}
        v = d.get("capture")
        return v is not None and float(v) <= 0
    except Exception:                      # noqa: BLE001
        return False


def item_cdp():
    ok, detail = False, ""
    try:
        with urllib.request.urlopen(CDP_URL, timeout=2) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        ok = isinstance(doc, dict) and ("Browser" in doc or "webSocketDebuggerUrl" in doc)
        detail = "9222 有响应（%s）" % (doc.get("Browser") or "JSON OK")
    except Exception as e:
        if _capture_off():
            return True, "抓取已停用（capture=0），浏览器没开是正常的"
        detail = "9222 无响应：%s" % (str(e)[:60] or type(e).__name__)
    if os.path.exists(RECAPTURE_LOCK):
        try:
            age = time.time() - os.path.getmtime(RECAPTURE_LOCK)
            if age > LOCK_STALE:
                detail += " · lock_stale（%.0f 分钟，未删锁只报）" % (age / 60.0)
        except OSError:
            pass
    return ok, detail


# ── 5 注入闸（阈值只认 persona_inject，不自创）────────────────────────
def item_inject():
    try:
        import persona_inject as inj
        ev = inj.evaluate_state()
    except Exception as e:
        return False, "persona_inject 不可用：%s" % str(e)[:80]
    streak = int(ev.get("streak") or 0)
    since = ev.get("since_min")
    detail = "连续失败 %d · 距上次成功 %s · %s" % (
        streak, ("%d 分钟" % since) if since is not None else "?",
        {"ok": "正常", "warn": "warn", "error": "error"}.get(ev.get("level"), ev.get("level")))
    return ev.get("level") == "ok", detail


# ── 6 队列（只报数字，不判红）────────────────────────────────────────
def item_queue():
    infl = _read_json(INFLIGHT) or {}
    n_in = len(infl.get("items") or []) if isinstance(infl.get("items"), list) else 0
    n_un = 0
    try:
        with io.open(TASKS, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if isinstance(t, dict) and str(t.get("status") or "") not in (
                        "done", "cancelled", "canceled", "rejected", "dropped", "expired"):
                    n_un += 1
    except OSError:
        n_un = -1
    return True, "inflight %d · tasks 未完成 %d" % (n_in, n_un)


# ── 7 今日成本（ledger 权威；余额 < ¥5 每天一次 warn）──────────────────
def _alert_once(kind, rec):
    """同一天同类只报一次；marker 落 ops\\selfcheck-alert.json。"""
    day = time.strftime("%Y-%m-%d")
    try:
        mark = _read_json(ALERT_MARK) or {}
    except Exception:
        mark = {}
    marks = mark.get(day) if isinstance(mark.get(day), dict) else {}
    if marks.get(kind):
        return False
    marks[kind] = rec.get("at")
    mark[day] = marks
    try:
        _write_json(ALERT_MARK, mark)
    except OSError:
        pass
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(ALERTS, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return True


def item_cost():
    try:
        import persona_cost as c
        d = c.report_data()
    except Exception as e:
        return False, "persona_cost 不可用：%s" % str(e)[:80]
    if d.get("source") != "ledger":
        return False, "账本读不到：%s" % (d.get("ledger_error") or "未知")
    money = float(d.get("money") or 0.0)
    calls = d.get("calls")
    bal = d.get("balance")
    detail = "今日 ¥%.2f · %s 调用 · %s" % (
        money, "{:,}".format(int(calls)) if calls is not None else "—",
        ("余额 ¥%.2f" % float(bal)) if bal is not None else "余额不可读")
    if bal is not None and float(bal) < BALANCE_LOW:
        fired = _alert_once("cost_balance_low", {
            "at": _now(), "level": "warn", "kind": "cost_balance_low", "src": "selfcheck",
            "balance": round(float(bal), 4), "money": round(money, 4),
            "note": "DSH 余额 ¥%.2f < ¥%.0f（今日已花 ¥%.2f）" % (float(bal), BALANCE_LOW, money)})
        return False, detail + ("（已写 warn）" if fired else "")
    # 日预算闸（人 2026-11-12 定）：钱/token 到线就是红，空闲循环那边同一条闸会停手。
    try:
        import persona_budget as _B
        _ok, _why = _B.allow()
        detail += " · " + _why
        if not _ok:
            return False, detail
    except Exception:                            # noqa: BLE001
        pass
    return True, detail


# ── 9 抓取 ───────────────────────────────────────────────────────────
# 阈值口径（2026-11-12 定）：白天 6 小时；当地 0–8 点放宽到 12 小时——
# 凌晨没人聊天，抓不到新正文不是断链，用同一个阈值会天天误报。
CAPTURE_MAX_H = 6.0
QUIET_UNTIL_HOUR = 8
MODEL_PROBE = os.path.join(OPS, "model-probe.json")
TASKS_BASE = os.path.join(OPS, "selfcheck-tasks.json")


def _limit_hours():
    return 12.0 if time.localtime().tm_hour < QUIET_UNTIL_HOUR else CAPTURE_MAX_H


def _hours_since(ts):
    return (time.time() - float(ts or 0)) / 3600.0


def _hm(hours):
    return "%.1f 小时" % hours if hours < 48 else "%.0f 小时" % hours


def _recent_session_dirs(n=2):
    base = os.path.join(ROOT, "sessions")
    try:
        days = sorted((d for d in os.listdir(base)
                       if os.path.isdir(os.path.join(base, d))), reverse=True)
    except OSError:
        return []
    return [os.path.join(base, d) for d in days[:n]]


def _newest_md(dirs):
    best, ts = "", 0.0
    for d in dirs:
        for dirpath, _sub, files in os.walk(d):
            for f in files:
                if not f.lower().endswith(".md"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    t = os.path.getmtime(p)
                except OSError:
                    continue
                if t > ts:
                    best, ts = p, t
    return best, ts


def item_capture():
    """抓取链还活着吗：最近两天里最新一份会话正文的年龄。

    今天断过一次（影子 ops 抢了路径），而断链是**静默**的——抓不到就是没有新文件，
    不会有人报错。所以这一项只看「最新正文多旧」，不看进程。
    """
    newest, ts = _newest_md(_recent_session_dirs(2))
    if not ts:
        return False, "最近两天一条会话正文都没有（sessions/ 下没有 .md）"
    hours = _hours_since(ts)
    lim = _limit_hours()
    detail = "最新正文 %s 前 · %s" % (_hm(hours), os.path.relpath(newest, ROOT))
    if lim > CAPTURE_MAX_H:
        detail += "（凌晨放宽到 %s）" % _hm(lim)
    return hours <= lim, detail


# ── 10 索引 ──────────────────────────────────────────────────────────
def item_index():
    """一级索引新不新：停建了检索会悄悄变瞎，但所有进程看起来都正常。"""
    path = os.path.join(ROOT, "index", "L1.jsonl")
    if not os.path.exists(path):
        return False, "一级索引不存在：index\\L1.jsonl"
    hours = _hours_since(os.path.getmtime(path))
    lim = _limit_hours()
    detail = "L1.jsonl %s 前更新（%d KB）" % (_hm(hours), os.path.getsize(path) // 1024)
    return hours <= lim, detail


# ── 11 备份 ──────────────────────────────────────────────────────────
COMMON_BACKUP_H = 48.0


def item_backup():
    """第二份备份还新不新：只有出事那天才发现没备份，是最贵的一种沉默故障。"""
    try:
        import persona_root as pr
        root = pr.backup_root()
    except Exception as e:                       # noqa: BLE001
        return False, "解析备份根出错：%s" % str(e)[:80]
    if not root:
        return False, "没配第二备份根（PERSONA_BACKUP2 / ops\\persona.conf.json 的 backup2_root）"
    if not os.path.isdir(root):
        return False, "第二备份根不存在：%s" % root
    best, ts, n = "", 0.0, 0
    for dirpath, _sub, files in os.walk(root):
        for f in files:
            n += 1
            p = os.path.join(dirpath, f)
            try:
                t = os.path.getmtime(p)
            except OSError:
                continue
            if t > ts:
                best, ts = p, t
    if not ts:
        return False, "第二备份根是空的：%s" % root
    hours = _hours_since(ts)
    return hours <= COMMON_BACKUP_H, "最新 %s 前 · %d 个文件 · %s" % (
        _hm(hours), n, os.path.basename(best))


# ── 12 磁盘 ──────────────────────────────────────────────────────────
DISK_MIN_FREE = 10.0


def item_disk():
    """盘满会以各种奇怪方式坏，而且原因难查：只看还剩百分之几。"""
    import shutil as _sh
    drives = []
    sd = os.environ.get("SystemDrive") or "C:"
    drives.append((sd, sd + "\\"))
    rd = os.path.splitdrive(ROOT)[0] or "D:"
    if rd.lower() != sd.lower():
        drives.append((rd, rd + "\\"))
    bad, bits = [], []
    for name, path in drives:
        try:
            u = _sh.disk_usage(path)
        except OSError:
            continue
        pct = 100.0 * u.free / u.total if u.total else 0.0
        bits.append("%s 剩 %.0f%%（%.1fG 空闲）" % (name, pct, u.free / float(1 << 30)))
        if pct < DISK_MIN_FREE:
            bad.append(name)
    if not bits:
        return False, "取不到磁盘用量"
    return not bad, " · ".join(bits) + (" · 低于 %.0f%%：%s" % (DISK_MIN_FREE, ",".join(bad)) if bad else "")


# ── 13 计划任务 ──────────────────────────────────────────────────────
def _task_names():
    """枚举 SysPersona-* 计划任务名。取不到就返回 None（判 skip 由上层处理）。"""
    import subprocess as _sp
    try:
        r = _sp.run(["schtasks", "/query", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, encoding="mbcs", errors="replace",
                    timeout=25)
    except (OSError, _sp.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return sorted(set(re.findall(r"SysPersona[-A-Za-z0-9_]+", r.stdout or "")))


def item_tasks():
    """计划任务还在不在：被删、被系统停用，肉眼看不出来，但开机就再也不自启了。

    第一次跑把当时的任务名记成基线（ops\\selfcheck-tasks.json）；以后只比较
    「基线里有的、现在少了的」。新增任务不算异常——只防丢，不妨碍加。
    """
    names = _task_names()
    if names is None:
        return False, "取不到计划任务列表（schtasks 不可用）"
    base = _read_json(TASKS_BASE) or {}
    known = base.get("names") or []
    if not known:
        try:
            _write_json(TASKS_BASE, {"at": _now(), "names": names})
        except OSError:
            pass
        return True, "已记基线：%d 个 SysPersona-* 任务" % len(names)
    lost = [n for n in known if n not in names]
    added = [n for n in names if n not in known]
    detail = "%d 个任务（基线 %d）" % (len(names), len(known))
    if added:
        detail += " · 新增 %s" % "、".join(added[:3])
    if lost:
        return False, detail + " · 少了 %s" % "、".join(lost[:3])
    return True, detail


# ── 14 模型 ──────────────────────────────────────────────────────────
MODEL_PROBE_TTL = 3600.0       # 1 小时探一次：够快发现问题，又不至于每分钟打网络


def item_model():
    """免费档/本地档还能不能用：换档后不知道能不能用，等你要用时才发现最糟。

    探测只 GET `/v1/models`——不花 token、不占并发；结果缓存 1 小时。
    两个档有一个活就算 OK：本地档优先（不走网络），再看免费档（要 key）。
    """
    cache = _read_json(MODEL_PROBE) or {}
    if cache and (_hours_since(cache.get("ts") or 0) * 3600.0) < MODEL_PROBE_TTL:
        return bool(cache.get("ok")), "（1h 内缓存）" + str(cache.get("detail") or "")
    # 本机策略：4G 内存的小主机不启用本地档（权重十几 GB，跑起来就把机器拖死）。
    # 这是人定的，不是故障——就不去探 8080 了；但**免费档照样要验**，
    # 因为这台机器真正在用的就是免费档（人 2026-11-12 给了 key）。
    try:
        import persona_root as _R
        local_on, local_why = _R.client_policy("local_model")
    except Exception:                             # noqa: BLE001
        local_on, local_why = True, ""
    bits, ok, rows = [], False, []
    if not local_on:
        bits.append("本地档本机不启用（%s）" % (local_why or "人定"))
    try:
        import persona_llm as L
    except Exception as e:                        # noqa: BLE001
        return False, "加载 persona_llm 失败：%s" % str(e)[:60]
    if local_on:
        try:
            rows = L.local_models() or []
        except Exception as e:                    # noqa: BLE001  本地档没起是常态，不是错误
            rows = []
            bits.append("本地档没起（%s）" % str(e)[:30])
    if rows:
        ok = True
        bits.insert(0, "本地档 %d 个模型" % len(rows))
    try:
        k = L.sf_key() if hasattr(L, "sf_key") else ""
        key = k[0] if isinstance(k, (tuple, list)) else k      # sf_key() 返回 (key, 来源)
        if key:
            data = L._json(L.SF_BASE.rstrip("/") + "/models",
                           headers={"Authorization": "Bearer " + key}, timeout=8)
            d = data[1] if isinstance(data, (tuple, list)) and len(data) == 2 else data
            n = len((d or {}).get("data") or [])
            if n:
                ok = True
            bits.append("免费档 %d 个模型" % n)
        else:
            bits.append("免费档没配 key")
    except Exception as e:                        # noqa: BLE001
        bits.append("免费档不通（%s）" % str(e)[:40])
    detail = " · ".join(bits)
    if not ok and not rows and any("没配 key" in b for b in bits):
        # 本机既没起本地档、也没配免费档 key：这是「这台机器不用这两档」，不是故障。
        detail = "跳过：本地档没起且没配 SILICONFLOW_API_KEY"
        ok = True
    try:
        _write_json(MODEL_PROBE, {"ts": time.time(), "ok": ok, "detail": detail})
    except OSError:
        pass
    return ok, detail


# ── 8 台账 ───────────────────────────────────────────────────────────
def item_manifest():
    """改动必须留痕（人 2026-11-12）：代码/配置和基线清单对不上、又没 --record 登记，这项就红。

    基线本体在 persona_manifest.py：ops\\manifest.json（指纹）+ ops\\manifest-changes.jsonl（台账）
    + 备份\\自检基线\\<rev>\\（改动前的版本）。这里只做一次比对，不动任何文件。
    """
    try:
        import persona_manifest as M
        return M.check(brief=True)
    except Exception as e:                       # 基线模块坏了也要说清楚，不能静默放过
        return False, "基线模块出错：%s" % str(e)[:100]


def item_paths():
    """路径变量和实际目录对不对得上（人 2026-11-12 定：「其他环境第一次重读，如果这个配置不对，
    就直接创建或者修改这个变量」）。

    这一项**会动手修**：变量指向不存在 / 过时的目录时，按本文件所在位置把用户环境变量写回去。
    换机器、搬目录之后不用人记得设变量——重读/自检各跑一次就自愈了。
    """
    try:
        import persona_root as R
        rows = R.heal_env(fix=True)
    except Exception as e:                       # noqa: BLE001
        return False, "路径模块出错：%s" % str(e)[:100]
    bad = [r for r in rows if r[3] not in ("ok", "修好了")]
    fixed = [r for r in rows if r[3] == "修好了"]
    if bad:
        return False, "；".join("%s %s" % (r[0], r[3]) for r in bad)
    if fixed:
        return True, "已修正 %d 个：%s" % (len(fixed), "、".join(r[0] for r in fixed))
    return True, "路径变量都对得上（%d 项）" % len(rows)


ITEMS = {"tray": item_tray, "daemon": item_daemon, "watermark": item_watermark,
         "cdp": item_cdp, "inject": item_inject, "queue": item_queue, "cost": item_cost,
         "capture": item_capture, "index": item_index, "backup": item_backup,
         "disk": item_disk, "tasks": item_tasks, "model": item_model,
         "paths": item_paths, "manifest": item_manifest}


# ── 汇总 / 输出 ──────────────────────────────────────────────────────
def run():
    items = {}
    for name in ORDER:
        why = WIN_ONLY.get(name)
        if why and not WIN:
            status, detail = STATUS_SKIP, "平台不符（非 Windows）：" + why
        else:
            try:
                ok, detail = ITEMS[name]()
            except Exception as e:                   # 单项失败不许拖垮整体
                ok, detail = False, "自检项异常：%s" % str(e)[:100]
            status = STATUS_OK if ok else STATUS_FAIL
        items[name] = {"ok": status == STATUS_OK, "status": status, "detail": str(detail)}
    doc = {"at": _now(), "items": items, "summary": ""}
    doc["summary"] = _summary(doc)
    return doc


def _status(v):
    """三态；兼容旧 selfcheck.json（只有 ok 布尔）。"""
    st = (v or {}).get("status")
    if st in (STATUS_OK, STATUS_SKIP, STATUS_FAIL):
        return st
    return STATUS_OK if (v or {}).get("ok") else STATUS_FAIL


def _short(name, detail):
    if name == "tray":
        return "缺"
    if name == "daemon":
        return "停" if "进程" in detail else "日志停"
    if name == "watermark":
        return "读数不可用"
    if name == "cdp":
        return "不通"
    if name == "inject":
        return "失败"
    if name == "capture":
        return "抓取断"
    if name == "index":
        return "索引旧"
    if name == "backup":
        return "备份旧"
    if name == "disk":
        return "盘快满"
    if name == "tasks":
        return "任务少了"
    if name == "model":
        return "模型不通"
    if name == "manifest":
        return "有未登记改动"
    if name == "queue":
        return "有积压"
    if name == "cost":
        if "余额 ¥" in detail:
            i = detail.find("余额 ¥")
            return detail[i:i + 12].strip()
        return "账本读不到"
    return detail[:20]


def _summary(doc):
    items = doc.get("items") or {}
    total = len(items)
    counts = {STATUS_OK: 0, STATUS_SKIP: 0, STATUS_FAIL: 0}
    for name in ORDER:
        counts[_status(items.get(name))] += 1
    # Windows 全绿时保持老格式（守护面板/log 都按 7/7 读）。
    if total and counts[STATUS_OK] == total:
        return "%d/%d OK" % (counts[STATUS_OK], total)
    line = "OK %d · SKIP %d · FAIL %d" % (
        counts[STATUS_OK], counts[STATUS_SKIP], counts[STATUS_FAIL])
    skipped = [LABEL.get(n, n) for n in ORDER
               if _status(items.get(n)) == STATUS_SKIP]
    if skipped:
        line += " · 跳过 " + "/".join(skipped)
    bad = []
    for name in ORDER:
        v = items.get(name) or {}
        if _status(v) != STATUS_FAIL:
            continue
        short = _short(name, v.get("detail") or "")
        if name == "cost" and short.startswith("余额 "):
            bad.append(short)          # 例：[自检] 5/7 · 托盘 缺 · 余额 ¥4.20
        else:
            bad.append("%s %s" % (LABEL.get(name, name), short))
    if bad:
        line += " · " + " · ".join(bad)
    return line


def _append_log(line):
    try:
        os.makedirs(OPS, exist_ok=True)
        fresh = (not os.path.exists(LOG)) or os.path.getsize(LOG) == 0
        with open(LOG, "a", encoding="utf-8") as f:
            if fresh:
                f.write("\ufeff")
            f.write("%s  %s\n" % (_now(), line))
    except OSError:
        pass


def _wake_on_fail(doc, prev):
    """自检转红时写一条唤醒事件（人 2026-11-12 定：心跳与自检要真串起来）。

    只在「失败集合」变化时写一次，不是每 120 秒刷一条；恢复正常也记一笔签名，
    下次再红还能再唤醒。写不进去（persona_wake 不可用）就静默——自检不能因为
    通知失败而失败。
    """
    items = (doc or {}).get("items") or {}
    bad = [n for n in ORDER if _status(items.get(n)) == STATUS_FAIL]
    sig = ",".join(bad)
    path = os.path.join(OPS, "selfcheck-wake.json")
    old = str((_read_json(path) or {}).get("sig") or "")
    if sig == old:
        return
    try:
        _write_json(path, {"at": _now(), "sig": sig})
    except OSError:
        pass
    if not sig:
        return                                  # 恢复正常：只更新签名，不唤醒
    try:
        import persona_wake as W
        labels = "、".join(LABEL.get(n, n) for n in bad)
        W.event("selfcheck", "自检转红：%s（%s）" % (labels, str(doc.get("summary") or "")[:60]),
                delay=0, source="selfcheck")
    except Exception:                           # noqa: BLE001
        pass


def _same_state(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    ai, bi = a.get("items") or {}, b.get("items") or {}
    if set(ai) != set(bi):
        return False
    # 比三态而不是 ok 布尔：fail → skip（或反过来）也算状态变了，要落一行日志。
    return all(_status(ai[k]) == _status(bi[k]) for k in ai)


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv[1:])

    prev = _read_json(OUT)
    doc = run()
    try:
        _write_json(OUT, doc)
    except OSError:
        pass
    _wake_on_fail(doc, prev)

    if a.quiet:
        # 心跳用：只在状态**变化**时写 persona.log 一行，不每 30 秒刷屏。
        if not _same_state(prev, doc):
            _append_log("[自检] %s" % doc["summary"])
        return 0
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    print("[自检] %s" % doc["summary"])
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
