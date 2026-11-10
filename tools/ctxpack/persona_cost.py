# -*- coding: utf-8 -*-
r"""persona_cost.py — DSH token 成本尺子：日账 / 单价折算 / 超线告警

数据源（**只读**，一个字节都不写壳的目录）：
  %USERPROFILE%\.dsh\storages\session_projcache\sessions\session-<id>.json
    record.rows.tokenUsage.val.totals  ← 本会话累计（不是本轮）
    record.rows.tokenUsage.val.last    ← turn / step（采样点）
  totals 是**会话累计**，所以按会话存上次读数、只把差值记进日账；
  跨会话绝不把 totals 直接相加。壳正在写文件时 json 会半截，这里只跳过、不抛栈，
  下一轮（心跳 300 秒）再来。

账本（只写记忆库 ops\）：
  ops\cost-state.json   每个会话上次读数
                        {session_id: {uncached, output, cache_read, cache_write, at}}
  ops\cost.jsonl        增量日账，一行一次采样：
                        {at, session, d_uncached, d_output, d_cache_read,
                         d_cache_write, turn, step}
  ops\cost-prices.json  单价 / 告警线；不存在就生成模板。
                        单价 null = 只报 token、显示「单价未配置」，**绝不猜价格**。
  ops\cost-alert.json   同一天同类告警只报一次的 marker

口径：
  - 首次见到一个会话：若它是**今天创建**的（identity.createdAt），它的累计值就是今天的
    用量，整份记一笔；更早的会话首次建账不把历史算进今天，只从下次增量开始记。
  - 采样范围：mtime 在「今天或昨天」的会话文件（长会话可能昨天开、今天还在写）。
  - 子代理会话文件没有 `session-` 前缀（`<uuid>.json`），**默认计入**：它们走的是同一个底座，
    实测 22 个子代理会话 = +1.6M in / +1.7M out / +205.6M cache，是当天花销的主要来源，
    漏掉就等于把尺子做歪了。`--no-subs` 可排除（只算主会话）。

用法：
  persona_cost.py sample [--warn] [--no-subs]
  persona_cost.py report
  persona_cost.py sample --warn        # 心跳里跑这条，300 秒一次
"""
import glob
import io
import json
import os
import sys
import time

KEYS = ("uncached", "output", "cache_read", "cache_write")
SRC_KEYS = {
    "uncached": "uncachedInputTokens",
    "output": "outputTokens",
    "cache_read": "cacheReadTokens",
    "cache_write": "cacheWriteTokens",
}
PRICE_KEYS = {
    "uncached": "uncached_input",
    "output": "output",
    "cache_read": "cache_read",
    "cache_write": "cache_write",
}

HOME = os.path.expanduser("~")
PROJCACHE = os.path.join(HOME, ".dsh", "storages", "session_projcache", "sessions")

# 控制台是 UTF-8（pwsh / 守护的 run() 也按 utf-8 解）；pythonw 下 stdout 可能是 None。
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None:
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── 路径：一律走 persona_root，不写死盘符 ────────────────────────────
def _load_persona_root():
    """记忆库根：仓库那份有 root()，记忆库里那份只有 find_root()。都没有就 None。"""
    try:
        from persona_root import root as _r
        return lambda: _r(strict=False)
    except ImportError:
        pass
    try:
        from persona_root import find_root as _f
        return lambda: _f()
    except ImportError:
        return None


_PERSONA_ROOT = _load_persona_root()


def _resolve_root():
    env = (os.environ.get("SYSPERSONA_ROOT") or os.environ.get("PERSONA_ROOT")
           or os.environ.get("MEM") or os.environ.get("SYSPERSONA_MEM"))
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "ops")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if _PERSONA_ROOT is not None:
        try:
            found = _PERSONA_ROOT()
        except Exception:
            found = None
        if found and os.path.isdir(found):
            return found
    return here


ROOT = _resolve_root()
OPS = os.path.join(ROOT, "ops")
STATE = os.path.join(OPS, "cost-state.json")
LEDGER = os.path.join(OPS, "cost.jsonl")
PRICES = os.path.join(OPS, "cost-prices.json")
ALERT_MARK = os.path.join(OPS, "cost-alert.json")
ALERTS = os.path.join(OPS, "alerts.jsonl")

# DSH 官方账本（**只读**：open(p, encoding="utf-8")，绝不写壳的目录）。
# 今日 ¥ / calls / token 三件都以它为准；读不到才退回 token×单价。
DSH_LEDGER = os.path.join(HOME, ".dsh", "storages", "cost-meter", "ledger.json")


# ── 小工具 ───────────────────────────────────────────────────────────
def _now(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _today(t=None):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _human(n):
    """紧凑 token 数：838K / 1.6M / 265M（接近整数兆就不带小数）。"""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1000000:
        return "%.0fK" % (n / 1000.0)
    m = n / 1000000.0
    if abs(m - round(m)) < 0.05:
        return "%dM" % round(m)
    return "%.1fM" % m


def _money(v, currency="CNY"):
    if v is None:
        return "¥—"
    sym = "¥" if (currency or "CNY").upper() == "CNY" else (currency + " ")
    v = float(v)
    return "%s%s" % (sym, ("%.3f" % v) if abs(v) < 0.01 else ("%.2f" % v))


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        f.write("\n")
    os.replace(tmp, path)


def _template():
    return {
        "currency": "CNY",
        "per_mtok": {"uncached_input": None, "output": None,
                     "cache_read": None, "cache_write": None},
        "daily_warn": None,
        "hourly_warn": None,
    }


# ── 单价 / 配置 ──────────────────────────────────────────────────────
def ensure_prices(create=True):
    """读 ops\\cost-prices.json；不存在就生成模板。返回 (配置, 错误说明)。"""
    if os.path.exists(PRICES):
        try:
            with open(PRICES, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d, ""
            return _template(), "cost-prices.json 不是 JSON 对象"
        except (OSError, ValueError) as e:
            return _template(), "cost-prices.json 解析失败：%s" % e
    if create:
        try:
            _write_json(PRICES, _template())
        except OSError:
            pass
    return _template(), ""


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def price_ready(prices):
    pm = (prices or {}).get("per_mtok") or {}
    return all(_is_num(pm.get(PRICE_KEYS[k])) for k in KEYS)


def cost_of(totals, prices):
    """按 per_mtok 折算金额；任一单价为 null 就返回 None（只报 token，不猜价）。"""
    if not price_ready(prices):
        return None
    pm = prices.get("per_mtok") or {}
    total = 0.0
    for k in KEYS:
        n = int(totals.get(k) or 0)
        if n > 0:
            total += n / 1000000.0 * float(pm[PRICE_KEYS[k]])
    return total


def _warn_line(v):
    return "%s" % (("%.2f" % float(v)) if _is_num(v) else "未设")


# ── DSH 官方账本（只读；金额/调用数/今日 token 的唯一权威）─────────────
def _official_balance(doc):
    """在 ledger 里找「界面余额」那个数；找不到返回 (None, 原因)。

    实测（2026-11-10）ledger 里**没有存**界面上那个官方余额：
      - config.balance 只是显示设置（display / refreshMinutes / budgetCap），没有值；
      - customBalance.enabled=false；goQuota.apiKey 为空；gatewayQuotas.sources=[]；
      - balanceRef.total（20.37）是**充值/余额参考**，不是界面余额 —— 不许拿它冒充。
    以后壳若把余额写进 config.officialBalance / config.balance.value / goQuota.balance，
    这里会自动认；认不出就明写「余额不可读」。
    """
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    for get in (lambda: cfg.get("officialBalance"),
                lambda: cfg.get("balanceValue"),
                lambda: (cfg.get("balance") or {}).get("value"),
                lambda: (cfg.get("goQuota") or {}).get("balance")):
        try:
            v = get()
        except (AttributeError, TypeError):
            continue
        if _is_num(v):
            return float(v), ""
    return None, "不可读（ledger 无官方余额字段）"


def read_ledger(day=None):
    """读壳的官方账本，返回 (今日账 dict, 错误说明)。

    只读：`open(p, encoding="utf-8")`，绝不写 `~/.dsh` 下任何文件。
    文件缺 / 半截 / JSONDecodeError → 账为 None，由调用方退回 token×单价。
    """
    day = day or _today()
    try:
        with open(DSH_LEDGER, encoding="utf-8") as f:     # 只读
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return None, "ledger 读不到：%s" % e
    if not isinstance(doc, dict):
        return None, "ledger 不是 JSON 对象"
    days = doc.get("days") or {}
    d = days.get(day)
    if not isinstance(d, dict):
        return None, "ledger 里没有 %s 的账" % day

    sessions = []
    for s in d.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        sessions.append({
            "session": str(s.get("id") or "?"),
            "in": int(s.get("input") or 0),
            "out": int(s.get("output") or 0),
            "cache": int(s.get("cacheRead") or 0) + int(s.get("cacheWrite") or 0),
            "calls": int(s.get("calls") or 0),
            "money": float(s.get("costCny") or 0.0),
        })
    sessions.sort(key=lambda x: -x["money"])
    bal, bnote = _official_balance(doc)
    return {
        "day": day,
        "input": int(d.get("input") or 0),
        "output": int(d.get("output") or 0),
        "cache_read": int(d.get("cacheRead") or 0),
        "cache_write": int(d.get("cacheWrite") or 0),
        "calls": int(d.get("calls") or 0),
        "money": float(d.get("costCny") or 0.0),
        "api_money": float(d.get("apiCostCny") or 0.0),
        "sessions": sessions,
        "balance": bal,
        "balance_note": bnote,
        "at": _now(),
    }, ""


# ── 读壳的会话（只读） ───────────────────────────────────────────────
def _created_day(ident, path):
    """会话创建日：identity.createdAt（毫秒）优先；没有就用文件创建时间。"""
    try:
        ms = float(ident.get("createdAt"))
        if ms > 1e12:
            ms /= 1000.0
        return time.strftime("%Y-%m-%d", time.localtime(ms))
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            return time.strftime("%Y-%m-%d", time.localtime(os.path.getctime(path)))
        except OSError:
            return ""


def _read_session(path):
    """解析一份 session-*.json；半截/坏文件抛 ValueError，由调用方跳过。"""
    with open(path, encoding="utf-8") as f:      # 只读；绝不写壳的账本
        doc = json.load(f)
    rec = doc.get("record") or {}
    rows = rec.get("rows") or {}
    val = ((rows.get("tokenUsage") or {}).get("val") or {})
    totals = val.get("totals") or {}
    last = val.get("last") or {}
    ident = rec.get("identity") or {}
    sid = os.path.basename(path)[:-5] if path.lower().endswith(".json") else os.path.basename(path)
    if sid.startswith("session-"):
        sid = sid[len("session-"):]
    return {
        "id": sid,
        "totals": {k: int(totals.get(s) or 0) for k, s in SRC_KEYS.items()},
        "turn": int(last.get("turn") or 0),
        "step": int(last.get("step") or 0),
        "created_day": _created_day(ident, path),
        "mtime": os.path.getmtime(path),
    }


def scan_sessions(include_subs=True, now=None):
    """扫 mtime 在今天或昨天的会话。返回 (记录列表, 跳过数)。"""
    now = now or time.time()
    y = time.strptime(_today(now), "%Y-%m-%d")
    cutoff = time.mktime((y.tm_year, y.tm_mon, y.tm_mday - 1, 0, 0, 0, 0, 0, -1))
    pattern = "*.json" if include_subs else "session-*.json"
    out, skipped = [], 0
    for p in glob.glob(os.path.join(PROJCACHE, pattern)):
        try:
            if os.path.getmtime(p) < cutoff:
                continue
            rec = _read_session(p)
        except (OSError, ValueError, TypeError, AttributeError):
            skipped += 1          # 壳正在写 → 半截 JSON，下轮再来
            continue
        out.append(rec)
    out.sort(key=lambda r: r["mtime"])
    return out, skipped


# ── 账本读写 ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def read_rows():
    rows = []
    if not os.path.exists(LEDGER):
        return rows
    try:
        with io.open(LEDGER, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue          # 坏行跳过；不删任何已有账本行
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        pass
    return rows


def append_rows(rows):
    if not rows:
        return
    os.makedirs(OPS, exist_ok=True)
    with io.open(LEDGER, "a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── sample ───────────────────────────────────────────────────────────
def sample(include_subs=True):
    """扫一遍壳的会话，把增量记进 cost.jsonl；返回本次摘要 dict。"""
    ensure_prices()
    state = load_state()
    recs, skipped = scan_sessions(include_subs)
    day = _today()
    stamp = _now()
    new_state = dict(state)
    added, delta = [], {k: 0 for k in KEYS}

    for rec in recs:
        prev = state.get(rec["id"])
        cur = rec["totals"]
        if isinstance(prev, dict):
            d = {k: max(0, cur[k] - int(prev.get(k) or 0)) for k in KEYS}
        elif rec["created_day"] == day:
            d = dict(cur)          # 今天建的会话：累计值就是今天的用量
        else:
            d = {k: 0 for k in KEYS}   # 更早的会话：只从下次增量开始记
        if any(d[k] > 0 for k in KEYS):
            added.append({
                "at": stamp,
                "session": rec["id"],
                "d_uncached": d["uncached"],
                "d_output": d["output"],
                "d_cache_read": d["cache_read"],
                "d_cache_write": d["cache_write"],
                "turn": rec["turn"],
                "step": rec["step"],
            })
            for k in KEYS:
                delta[k] += d[k]
        new_state[rec["id"]] = {
            "uncached": cur["uncached"], "output": cur["output"],
            "cache_read": cur["cache_read"], "cache_write": cur["cache_write"],
            "at": stamp,
        }

    append_rows(added)
    try:
        _write_json(STATE, new_state)
    except OSError:
        pass                  # 状态写不下去也不能把账本回滚

    return {"at": stamp, "sessions": len(recs), "skipped": skipped,
            "rows": len(added), "delta": delta}


def sample_text(s):
    return ("[采样] %s  会话 %d 个 · 新账 %d 行 · 跳过 %d 个正在写\n"
            "  Δin %s · Δout %s · Δcache %s · Δcache_w %s\n"
            % (s["at"], s["sessions"], s["rows"], s["skipped"],
               _human(s["delta"]["uncached"]), _human(s["delta"]["output"]),
               _human(s["delta"]["cache_read"]), _human(s["delta"]["cache_write"])))


# ── report ───────────────────────────────────────────────────────────
def _sum_rows(rows):
    out = {k: 0 for k in KEYS}
    for r in rows:
        out["uncached"] += int(r.get("d_uncached") or 0)
        out["output"] += int(r.get("d_output") or 0)
        out["cache_read"] += int(r.get("d_cache_read") or 0)
        out["cache_write"] += int(r.get("d_cache_write") or 0)
    return out


def _epoch(at):
    try:
        return time.mktime(time.strptime(str(at), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


def _turn_deltas(rows):
    """采样到的 turn/step 增量：同一会话相邻两行相减；step 在换 turn 时按新值算。"""
    by = {}
    for r in rows:
        by.setdefault(r.get("session"), []).append(r)
    d_turn = d_step = 0
    for rs in by.values():
        rs.sort(key=lambda x: str(x.get("at") or ""))
        p_t = p_s = None
        for r in rs:
            t, s = int(r.get("turn") or 0), int(r.get("step") or 0)
            if p_t is not None:
                d_turn += max(0, t - p_t)
                d_step += s if t != p_t else max(0, s - p_s)
            p_t, p_s = t, s
    return d_turn, d_step


def report_data(now=None):
    now = now or time.time()
    prices, perr = ensure_prices()
    currency = str(prices.get("currency") or "CNY")
    rows = read_rows()
    day = _today(now)
    todays = [r for r in rows if str(r.get("at") or "")[:10] == day]
    sampled = _sum_rows(todays)
    led, lerr = read_ledger(day)

    cut = now - 3600
    hour_rows = []
    for r in rows:
        e = _epoch(r.get("at"))
        if e is not None and e >= cut:
            hour_rows.append(r)
    hour = _sum_rows(hour_rows)
    d_turn, d_step = _turn_deltas(todays)

    if led:
        # 官方账本优先：今日 ¥ / calls / token 三件都来自它，比 token 折算权威。
        source = "ledger"
        totals = {"uncached": led["input"], "output": led["output"],
                  "cache_read": led["cache_read"], "cache_write": led["cache_write"]}
        money, priced, price_error = led["money"], True, ""
        calls, sessions = led["calls"], led["sessions"]
        balance, balance_note = led["balance"], led["balance_note"]
        hour_money = None          # 账本没有逐小时金额；不拿 token 反推
        ledger_error = ""
    else:
        # 账本读不到：退回原来的 token×单价；金额仍可能为 None（单价未配置）。
        source = "token"
        totals = sampled
        money = cost_of(totals, prices)
        priced = money is not None
        price_error = perr
        calls, balance, balance_note = None, None, lerr or "ledger 不可用"
        hour_money = cost_of(hour, prices)
        ledger_error = lerr
        sess = {}
        for r in todays:
            sid = str(r.get("session") or "?")
            s = sess.setdefault(sid, {"session": sid, "in": 0, "out": 0,
                                      "cache": 0, "turn": 0, "step": 0})
            s["in"] += int(r.get("d_uncached") or 0)
            s["out"] += int(r.get("d_output") or 0)
            s["cache"] += int(r.get("d_cache_read") or 0) + int(r.get("d_cache_write") or 0)
            s["turn"] = int(r.get("turn") or 0)
            s["step"] = int(r.get("step") or 0)
        sessions = sorted(sess.values(), key=lambda x: -(x["cache"] + x["in"]))

    daily_warn = prices.get("daily_warn")
    hourly_warn = prices.get("hourly_warn")
    return {
        "day": day,
        "source": source,
        "totals": totals,
        "money": money,
        "currency": currency,
        "priced": priced,
        "calls": calls,
        "balance": balance,
        "balance_note": balance_note,
        "ledger_error": ledger_error,
        "per_mtok": (prices.get("per_mtok") or {}),
        "daily_warn": daily_warn if _is_num(daily_warn) else None,
        "hourly_warn": hourly_warn if _is_num(hourly_warn) else None,
        "over_daily": money is not None and _is_num(daily_warn) and money > float(daily_warn),
        "over_hourly": (hour_money is not None and _is_num(hourly_warn)
                        and hour_money > float(hourly_warn)),
        "hour": {"totals": hour, "money": hour_money},
        "sessions": sessions,
        "samples": len(todays),
        "turn_delta": d_turn,
        "step_delta": d_step,
        "price_error": price_error,
    }


def _src_tag(d):
    return "[来源] " + ("ledger" if d.get("source") == "ledger" else "token折算")


def _calls(n):
    return "{:,}".format(int(n)) if n is not None else "—"


def report_text(d):
    t = d["totals"]
    out = ["成本尺子 · 今日 %s  %s" % (d["day"], _src_tag(d))]
    out.append("  今日：in %s · out %s · cache_read %s · cache_write %s"
               % (_human(t["uncached"]), _human(t["output"]),
                  _human(t["cache_read"]), _human(t["cache_write"])))
    if d["priced"]:
        extra = " · %s 次调用" % _calls(d.get("calls")) if d.get("calls") is not None else ""
        out.append("  金额：%s%s（日线 %s%s）"
                   % (_money(d["money"], d["currency"]), extra,
                      _money(d["daily_warn"], d["currency"]),
                      "，已超线" if d["over_daily"] else ""))
    else:
        why = d.get("price_error") or d.get("ledger_error") or "per_mtok 四个单价有 null"
        out.append("  金额：单价未配置且账本读不到（%s）→ 只报 token" % why)
    if d.get("source") == "ledger":
        if d.get("balance") is not None:
            out.append("  余额：%s（ledger）" % _money(d["balance"], d["currency"]))
        else:
            out.append("  余额：%s；balanceRef 是充值参考，不冒充界面余额"
                       % (d.get("balance_note") or "余额不可读"))
        out.append("  近 1 小时：in %s · out %s · cache %s（账本没有小时金额，token 采样仅参考）"
                   % (_human(d["hour"]["totals"]["uncached"]), _human(d["hour"]["totals"]["output"]),
                      _human(d["hour"]["totals"]["cache_read"])))
    else:
        h = d["hour"]["totals"]
        out.append("  近 1 小时：in %s · out %s · cache %s · 金额 %s%s"
                   % (_human(h["uncached"]), _human(h["output"]), _human(h["cache_read"]),
                      _money(d["hour"]["money"], d["currency"]),
                      "（已超小时线）" if d["over_hourly"] else ""))
    out.append("  采样：%d 行 · Δturn %d · Δstep %d（逐会话增量，不再用来推金额）"
               % (d["samples"], d["turn_delta"], d["step_delta"]))
    if d["sessions"]:
        out.append("  会话（今日，按%s降序）：" % ("金额" if d["source"] == "ledger" else "cache"))
        for s in d["sessions"][:8]:
            if d["source"] == "ledger":
                out.append("    %-14s %8s · calls %-5d in %-7s out %-7s cache %-7s"
                           % (s["session"][-14:], _money(s["money"], d["currency"]),
                              s["calls"], _human(s["in"]), _human(s["out"]), _human(s["cache"])))
            else:
                out.append("    %-12s in %-8s out %-8s cache %-8s turn %d/step %d"
                           % (s["session"][:12], _human(s["in"]), _human(s["out"]),
                              _human(s["cache"]), s["turn"], s["step"]))
        if len(d["sessions"]) > 8:
            out.append("    …还有 %d 个会话" % (len(d["sessions"]) - 8))
    else:
        out.append("  会话：（今天还没有采样到增量）")
    if d.get("source") != "ledger" and d.get("ledger_error"):
        out.append("  （账本不可用，已退回 token×单价：%s）" % d["ledger_error"])
    return "\n".join(out)


# ── --warn：超线告警（同一天同类只报一次） ───────────────────────────
def _load_mark():
    try:
        with open(ALERT_MARK, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _append_alert(rec):
    os.makedirs(OPS, exist_ok=True)
    with io.open(ALERTS, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def check_warn(d=None):
    """日金额超 daily_warn / 近 1 小时超 hourly_warn → 追加 alerts；一天一类只报一次。"""
    d = d or report_data()
    if not d["priced"]:
        return [], "单价未配置 → 不判金额"
    day = d["day"]
    mark = _load_mark()
    marks = mark.get(day)
    if not isinstance(marks, dict):
        marks = {}
        mark[day] = marks
    fired, stamp = [], _now()

    if d["over_daily"] and not marks.get("cost_daily"):
        note = "今日 %s 超日线 %s（in %s / out %s / cache %s）" % (
            _money(d["money"], d["currency"]), _money(d["daily_warn"], d["currency"]),
            _human(d["totals"]["uncached"]), _human(d["totals"]["output"]),
            _human(d["totals"]["cache_read"]))
        rec = {"at": stamp, "level": "warn", "kind": "cost_daily", "src": "cost",
               "money": round(float(d["money"]), 4),
               "warn": float(d["daily_warn"]), "note": note}
        _append_alert(rec)
        marks["cost_daily"] = stamp
        fired.append(rec)

    if d["over_hourly"] and not marks.get("cost_hourly"):
        h = d["hour"]["totals"]
        note = "近 1 小时 %s 超小时线 %s（in %s / out %s / cache %s）" % (
            _money(d["hour"]["money"], d["currency"]),
            _money(d["hourly_warn"], d["currency"]),
            _human(h["uncached"]), _human(h["output"]), _human(h["cache_read"]))
        rec = {"at": stamp, "level": "warn", "kind": "cost_hourly", "src": "cost",
               "money": round(float(d["hour"]["money"]), 4),
               "warn": float(d["hourly_warn"]), "note": note}
        _append_alert(rec)
        marks["cost_hourly"] = stamp
        fired.append(rec)

    if fired:
        try:
            _write_json(ALERT_MARK, mark)
        except OSError:
            pass
    return fired, "" if fired else "未超线"


# ── CLI ──────────────────────────────────────────────────────────────
def _final_line(d):
    t = d["totals"]
    if d["priced"]:
        head = "[成本] 今日 %s" % _money(d["money"], d["currency"])
        if d.get("calls") is not None:
            head += " · %s 调用" % _calls(d["calls"])
    else:
        head = "[成本] 今日 ¥—（单价未配置）"
    return ("%s · in %s · out %s · cache %s  %s"
            % (head, _human(t["uncached"]), _human(t["output"]),
               _human(t["cache_read"]), _src_tag(d)))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    include_subs = "--no-subs" not in argv
    if cmd == "sample":
        s = sample(include_subs)
        print(sample_text(s), end="")
        if "--warn" in argv:
            d = report_data()
            fired, why = check_warn(d)
            for rec in fired:
                print("[告警] %s  %s" % (rec["kind"], rec["note"]))
            if not fired:
                print("[告警] 未触发（%s）" % why)
        else:
            d = report_data()
        print(_final_line(d))
        return 0
    if cmd == "report":
        d = report_data()
        print(report_text(d))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
