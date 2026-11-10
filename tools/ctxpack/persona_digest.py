# -*- coding: utf-8 -*-
r"""persona_digest.py — 每日自检：六项一起看，结论落到一张纸上。

为什么要有它：水位、派发、告警、抓取、备份、索引分散在六个地方，平时一项一项查，
总会漏掉一项；等到检索不到东西才发现索引停了，已经晚了一天。这里把六项收成一条命令，
写进 `ops/daily-status.md`，并让计划任务每天 23:50 跑一次。人第二天只看一张表，
不用再挨个打开脚本。

判定口径（值 + OK/WARN/FAIL + 一句人话）：
  1. 水位：`persona_gauge.dsh_pressure()`；<150K OK，150K-250K WARN，
     250K-300K WARN（准备），>=300K CLEAN（该交接）。CLEAN 不是 FAIL。
  2. 任务：`persona_dispatch.state()` 的 pending / accepted 数；最近报到时间超过 24h 或
     有 pending 没人接 → WARN。
  3. 告警：`ops/alerts.jsonl` 最近 24 小时条数；文件不存在按 0 条算（OK）。
  4. 抓取：`sessions\` 下最新 `.md`（排除本机导出的 `续接前-*`）；超过 120 分钟 WARN。
  5. 备份：`备份\` 或 `%LOCALAPPDATA%\SysPersona` 下最新 zip；找不到 WARN，
     超过 48 小时 WARN。
  6. 索引：`index\L1.jsonl` 行数 + mtime；不存在或 0 行 FAIL，超过 6 小时没更新 WARN。

用法：
    python persona_digest.py            # 写 ops/daily-status.md 并打印
    python persona_digest.py --once     # 同上，给计划任务用
    python persona_digest.py --json     # 只打印 JSON，不写文件

退出码：0 = 全部 OK/WARN；1 = 有 FAIL。

环境变量：SYSPERSONA_ROOT/PERSONA_ROOT = 记忆库根；LOCALAPPDATA 用来找
`%LOCALAPPDATA%\SysPersona` 下的备份 zip（没有就只查库里的 `备份\`，不算错）。
库根不设就按本文件所在目录推导（要有 ops\），推不出来直接抛错。
"""
import argparse
import io
import json
import os
import sys
import time

from persona_root import localappdata as _localappdata, root as _persona_root

# 记忆库根：环境变量优先；在库里跑就用库目录（有 ops\）；推导不出来就报错，不再写死路径。
ROOT = _persona_root()
sys.path.insert(0, ROOT)

try:
    import persona_win  # noqa: F401  在 pythonw 里 spawn 子进程不弹黑窗
except Exception:
    pass

import persona_dispatch as D  # noqa: E402
import persona_report as R  # noqa: E402
import persona_gauge as G  # noqa: E402
from persona_limits import OK, WARN, CLEAN, LIMIT  # noqa: E402

OPS = os.path.join(ROOT, "ops")
OUT = os.path.join(OPS, "daily-status.md")
SESSIONS = os.path.join(ROOT, "sessions")
INDEX = os.path.join(ROOT, "index", "L1.jsonl")
ALERTS = os.path.join(OPS, "alerts.jsonl")

DAY = 24 * 3600
CAPTURE_WARN_MIN = 120
BACKUP_WARN_HOURS = 48
INDEX_WARN_HOURS = 6


def human_tokens(n):
    """token 数说人话：43.2K / 1.05M。"""
    n = int(n or 0)
    if n >= 1_000_000:
        return "%.2fM" % (n / 1_000_000.0)
    if n >= 1000:
        return "%.1fK" % (n / 1000.0)
    return str(n)


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def age_text(seconds):
    """距今多久：刚刚 / 12 分钟前 / 3.5 小时前 / 2.1 天前。"""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return "%d 分钟前" % int(seconds // 60)
    if seconds < 48 * 3600:
        return "%.1f 小时前" % (seconds / 3600.0)
    return "%.1f 天前" % (seconds / 86400.0)


def parse_ts(s):
    """把 `YYYY-MM-DD HH:MM:SS` 解析成本地时间戳；解析不了返回 0。"""
    try:
        return time.mktime(time.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def check_gauge():
    pressure, surface, window = G.dsh_pressure()
    win = window or LIMIT
    if not pressure:
        return {
            "key": "gauge", "item": "水位",
            "value": "没读到（pressure=0，窗口 %s）" % human_tokens(win),
            "level": "WARN",
            "human": "壳没留水位：会话缓存可能没在写，这项只能算没看见",
        }
    pct = 100.0 * pressure / win
    value = "pressure %s / 窗口 %s（%.1f%%）；surface %s" % (
        human_tokens(pressure), human_tokens(win), pct, human_tokens(surface))
    if pressure >= CLEAN:
        return {"key": "gauge", "item": "水位", "value": value, "level": "CLEAN",
                "human": "到 %s 了：先镜像、再出包，别等壳自己压" % human_tokens(CLEAN)}
    if pressure >= WARN:
        return {"key": "gauge", "item": "水位", "value": value, "level": "WARN",
                "human": "过了 250K：先把接续包备好，还能撑一会儿"}
    if pressure >= OK:
        return {"key": "gauge", "item": "水位", "value": value, "level": "WARN",
                "human": "过了 150K：还能撑，但别再往里塞长文档"}
    return {"key": "gauge", "item": "水位", "value": value, "level": "OK",
            "human": "水位健康，离 150K 还有余量"}


def check_tasks():
    st = D.state()
    pending = [r for r in st.values() if (r.get("status") or "") == "pending"]
    accepted = [r for r in st.values()
                if (r.get("status") or "") in ("accepted", "running")]

    reps = R.load() or {}
    latest_agent, latest_ts = "", 0.0
    for agent, rec in reps.items():
        ts = float(rec.get("last") or 0)
        if not ts and rec.get("at"):
            ts = parse_ts(rec["at"])
        if ts > latest_ts:
            latest_agent, latest_ts = agent, ts

    now = time.time()
    if latest_ts:
        report = "最近报到 %s（%s，%s）" % (
            fmt_ts(latest_ts), latest_agent, age_text(now - latest_ts))
    else:
        report = "最近报到 无"
    value = "pending %d / accepted %d；%s" % (len(pending), len(accepted), report)

    if pending:
        return {"key": "tasks", "item": "任务", "value": value, "level": "WARN",
                "human": "有 %d 条 pending 没人接：看看是分身没醒，还是派错了人" % len(pending)}
    if not latest_ts:
        return {"key": "tasks", "item": "任务", "value": value, "level": "WARN",
                "human": "没有任何报到记录：分身池没醒过"}
    age = now - latest_ts
    if age > DAY:
        return {"key": "tasks", "item": "任务", "value": value, "level": "WARN",
                "human": "一天没人报到：分身池可能停了"}
    if accepted:
        return {"key": "tasks", "item": "任务", "value": value, "level": "OK",
                "human": "%d 条在办，没有积压" % len(accepted)}
    return {"key": "tasks", "item": "任务", "value": value, "level": "OK",
            "human": "没有积压，最近有人报到"}


def check_alerts():
    if not os.path.exists(ALERTS):
        return {"key": "alerts", "item": "告警", "value": "0 条（文件不存在）",
                "level": "OK", "human": "24 小时没有新告警"}

    now = time.time()
    count, latest = 0, 0.0
    try:
        f = io.open(ALERTS, encoding="utf-8-sig", errors="replace")
    except OSError:
        return {"key": "alerts", "item": "告警", "value": "读不到 alerts.jsonl",
                "level": "WARN", "human": "告警文件打不开，先看看权限或占用"}
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = parse_ts(rec.get("at", ""))
            if not ts or now - ts > DAY:
                continue
            count += 1
            latest = max(latest, ts)

    if count == 0:
        return {"key": "alerts", "item": "告警", "value": "24 小时 0 条",
                "level": "OK", "human": "24 小时没有新告警"}
    value = "24 小时 %d 条（最近 %s）" % (count, fmt_ts(latest))
    return {"key": "alerts", "item": "告警", "value": value, "level": "WARN",
            "human": "有 %d 条告警：翻一眼 ops/alerts.jsonl，别让小毛病过夜" % count}


def latest_session_md():
    """sessions 下最新 .md；排除本机会话导出的 `续接前-*`。"""
    newest, newest_mtime = "", 0.0
    for dirpath, _dirnames, filenames in os.walk(SESSIONS):
        for fn in filenames:
            if not fn.lower().endswith(".md") or fn.startswith("续接前-"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = p, mtime
    return newest, newest_mtime


def check_capture():
    p, mtime = latest_session_md()
    if not p:
        return {"key": "capture", "item": "抓取", "value": "sessions 下没有 .md",
                "level": "WARN", "human": "一条抓取都没有：抓取链路可能没跑"}
    seconds = time.time() - mtime
    rel = os.path.relpath(p, ROOT).replace("\\", "/")
    value = "%s（%s）%s" % (fmt_ts(mtime), age_text(seconds), rel)
    if seconds > CAPTURE_WARN_MIN * 60:
        return {"key": "capture", "item": "抓取", "value": value, "level": "WARN",
                "human": "最新抓取是 %d 分钟前：超过两小时，抓取链路可能卡了"
                         % int(seconds // 60)}
    return {"key": "capture", "item": "抓取", "value": value, "level": "OK",
            "human": "抓取在走，最新的不到两小时"}


def latest_backup_zip():
    bases = [os.path.join(ROOT, "备份")]
    local = _localappdata()
    if local:
        bases.append(os.path.join(local, "SysPersona"))
    newest, newest_mtime = "", 0.0
    for base in bases:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if not fn.lower().endswith(".zip"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest, newest_mtime = p, mtime
    return newest, newest_mtime


def check_backup():
    p, mtime = latest_backup_zip()
    if not p:
        return {"key": "backup", "item": "备份", "value": "没找到备份", "level": "WARN",
                "human": "没找到备份：备份\\ 和 %LOCALAPPDATA%\\SysPersona 下都没有 zip"}
    hours = (time.time() - mtime) / 3600.0
    value = "%s（%.1f 小时前）%s" % (fmt_ts(mtime), hours, os.path.basename(p))
    if hours > BACKUP_WARN_HOURS:
        return {"key": "backup", "item": "备份", "value": value, "level": "WARN",
                "human": "最新备份超过 48 小时：机器可能没在打，手动补一次"}
    return {"key": "backup", "item": "备份", "value": value, "level": "OK",
            "human": "两天内备份过，能兜住"}


def check_index():
    if not os.path.exists(INDEX):
        return {"key": "index", "item": "索引", "value": "index/L1.jsonl 不存在",
                "level": "FAIL", "human": "索引不存在：检索没有入口，先跑 persona_index_l1.py"}
    try:
        lines = 0
        with io.open(INDEX, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if line.strip():
                    lines += 1
        mtime = os.path.getmtime(INDEX)
    except OSError as e:
        return {"key": "index", "item": "索引", "value": "读不到 index/L1.jsonl",
                "level": "FAIL", "human": "索引读不了：%s" % str(e)[:60]}

    seconds = time.time() - mtime
    value = "%d 行；%s（%s）" % (lines, fmt_ts(mtime), age_text(seconds))
    if lines == 0:
        return {"key": "index", "item": "索引", "value": value, "level": "FAIL",
                "human": "索引是空的：检索会瞎，先跑 persona_index_l1.py"}
    if seconds > INDEX_WARN_HOURS * 3600:
        return {"key": "index", "item": "索引", "value": value, "level": "WARN",
                "human": "索引超过 %d 小时没更新：守护可能没在刷" % INDEX_WARN_HOURS}
    return {"key": "index", "item": "索引", "value": value, "level": "OK",
            "human": "索引在，最新一次更新是 %s" % age_text(seconds)}


CHECKS = (check_gauge, check_tasks, check_alerts, check_capture,
          check_backup, check_index)


def collect():
    checks = []
    for fn in CHECKS:
        try:
            checks.append(fn())
        except Exception as e:  # 单项炸了不能拖垮整张表
            checks.append({"key": fn.__name__.replace("check_", ""),
                           "item": fn.__name__.replace("check_", ""),
                           "value": "检查异常：%s" % str(e)[:80],
                           "level": "FAIL", "human": "这项自己挂了，看上面的异常"})
    return checks


def summarize(checks):
    fails = sum(1 for c in checks if c["level"] == "FAIL")
    warns = sum(1 for c in checks if c["level"] == "WARN")
    cleans = sum(1 for c in checks if c["level"] == "CLEAN")
    overall = "FAIL" if fails else "OK"
    parts = ["%d 项 FAIL" % fails, "%d 项 WARN" % warns]
    if cleans:
        parts.append("%d 项 CLEAN" % cleans)
    return overall, "总判定：%s（%s）" % (overall, "，".join(parts))


def render(checks, at):
    lines = [
        "# 每日自检 %s" % at,
        "",
        "| 项 | 值 | 判定 | 一句人话 |",
        "|---|---|---|---|",
    ]
    for c in checks:
        lines.append("| %s | %s | %s | %s |" % (
            c["item"], c["value"], c["level"], c["human"]))
    _overall, tail = summarize(checks)
    lines += ["", tail]
    return "\n".join(lines) + "\n"


def write_stdout(text):
    """pythonw 下 sys.stdout 是 None：有就打印，没有就只落文件。"""
    try:
        if sys.stdout is not None:
            sys.stdout.write(text)
            sys.stdout.flush()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="每日自检：六项写进一张表")
    ap.add_argument("--once", action="store_true", help="给计划任务用；与默认行为相同")
    ap.add_argument("--json", action="store_true", help="只打印 JSON，不写文件")
    a = ap.parse_args()

    # 被管道捕获时按 UTF-8 输出，免得 GBK 把中文和箭头吃成问号；真控制台保持原编码。
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    now = time.time()
    at = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    checks = collect()
    overall, tail = summarize(checks)
    exit_code = 1 if overall == "FAIL" else 0

    if a.json:
        payload = {
            "at": at,
            "overall": overall,
            "exit": exit_code,
            "checks": [{"key": c["key"], "item": c["item"], "value": c["value"],
                        "level": c["level"], "human": c["human"]} for c in checks],
            "summary": tail,
        }
        write_stdout(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
        return exit_code

    text = render(checks, at)
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as e:
        try:
            if sys.stderr is not None:
                sys.stderr.write("写 %s 失败：%s\n" % (OUT, e))
        except Exception:
            pass
        return 1
    write_stdout(text)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
