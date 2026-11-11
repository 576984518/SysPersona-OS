# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_selfcheck.py — 心跳自检：7 项，单项失败不拖垮整体，目标 < 8 秒
r"""persona_selfcheck.py — 心跳自检：7 项，单项失败不拖垮整体，目标 < 8 秒

每次心跳（守护 TASK selfcheck，120 秒）跑一遍，逐项判定后写 `ops\selfcheck.json`：
    {at, items: {名: {ok, status, detail}}, summary}
status 三态：`ok` / `skip`（平台不符，非 Windows 上判不了）/ `fail`。

七项：
    tray      托盘进程在不在（命令行含 persona_tray.py）       ← 仅 Windows
    daemon    守护进程 + ops\persona.log 最后一行是否 < 5 分钟  ← 仅 Windows（进程表）
    watermark 壳口径 pressureTokens 与 ops\dsh-ctx.json 最近一次读数是否一致（≤5%）
    cdp       127.0.0.1:9222/json/version 是否有响应；recapture.lock 超 35 分钟只报 lock_stale
    inject    注入闸：连续失败 ≥3 或距上次成功 >60 分钟（阈值只认 persona_inject）← 仅 Windows
    queue     inflight 条数 / tasks 未完成条数（只报数字，不判红）
    cost      今日 ¥ + calls + 余额（ledger 读不到就判红；余额 < ¥5 每天一次 warn）

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
ORDER = ("tray", "daemon", "watermark", "cdp", "inject", "queue", "cost")
LABEL = {"tray": "托盘", "daemon": "守护", "watermark": "水位", "cdp": "CDP",
         "inject": "注入", "queue": "队列", "cost": "成本"}

# 平台归类：判据本身只能在 Windows 上取到的项。非 Windows 上判 skip、不判 fail——
# 把「平台没这个能力」记成缺陷，会把真正的失败淹掉。Windows 上这里完全不生效。
WIN_ONLY = {
    "tray": "托盘进程靠 wmic/PowerShell 枚举进程表，非 Windows 上取不到",
    "daemon": "守护进程靠 wmic/PowerShell 枚举进程表，非 Windows 上取不到",
    "inject": "注入闸依赖 ctypes.wintypes / user32，非 Windows 上导入不了",
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
    """返回 [(pid, commandline)]；读不到返回 [] 并在 _PROC_ERR 里写原因。"""
    global _PROCS, _PROC_ERR
    if _PROCS is not None:
        return _PROCS
    out = []
    try:
        r = subprocess.run(["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                           capture_output=True, timeout=5, creationflags=NO_WINDOW)
        text = _decode(r.stdout)
        if r.returncode == 0 and "CommandLine" in text:
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 3:
                    continue
                cmd, pid = row[1], row[2].strip()
                if pid.isdigit():
                    out.append((int(pid), cmd))
            if out:
                _PROCS = out
                return _PROCS
        _PROC_ERR = "wmic 没给出进程表"
    except (OSError, subprocess.SubprocessError) as e:
        _PROC_ERR = "wmic 不可用：%s" % str(e)[:60]

    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, timeout=6, creationflags=NO_WINDOW)
        text = _decode(r.stdout).strip()
        data = json.loads(text) if text else []
        if isinstance(data, dict):
            data = [data]
        for it in data or []:
            try:
                out.append((int(it.get("ProcessId")), str(it.get("CommandLine") or "")))
            except (TypeError, ValueError):
                continue
        if out:
            _PROC_ERR = ""
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        _PROC_ERR = "进程表读不到：%s" % str(e)[:60]
    _PROCS = out
    return _PROCS


def _py_cmd(cmd, script, flag=""):
    """这条命令行是不是「python(w).exe ...\\<script> [flag]」本身。

    不能只做 `"persona_daemon.py" in cmd`：守护自己的 PowerShell 启动器 / DSH 的
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
    hits = [pid for pid, cmd in procs if _py_cmd(cmd, "persona_tray.py")]
    if hits:
        return True, "PID %s · 命令行含 persona_tray.py" % ",".join(str(p) for p in hits[:3])
    return False, "进程表里没有 persona_tray.py"


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
    path = os.path.join(ROOT, "persona_tray.py")
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
def item_cdp():
    ok, detail = False, ""
    try:
        with urllib.request.urlopen(CDP_URL, timeout=2) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        ok = isinstance(doc, dict) and ("Browser" in doc or "webSocketDebuggerUrl" in doc)
        detail = "9222 有响应（%s）" % (doc.get("Browser") or "JSON OK")
    except Exception as e:
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
    return True, detail


ITEMS = {"tray": item_tray, "daemon": item_daemon, "watermark": item_watermark,
         "cdp": item_cdp, "inject": item_inject, "queue": item_queue, "cost": item_cost}


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
