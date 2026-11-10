# -*- coding: utf-8 -*-
r"""persona_autonomy.py — 自主的次数闸（免费/收费两档）

只做一件事：按**底座模型花不花钱**决定空闲时每小时/每天最多自己做几次。
  * 免费/本地模型（ollama / lmstudio / siliconflow / *free* / deepseek-web …）
    只花电费，允许更频繁地自主做事：默认 12 次/时、120 次/日；
  * 收费模型（deepseek-account / deepseek-official / openai / anthropic …）
    做多了花钱，收紧：默认 3 次/时、20 次/日；
  * 匹配不上的模型键、拿不到模型 → 一律按收费档（保守）。

行为差异**只有次数上限**。优先级、needs、护栏、抢占、饥饿保护一律不动：
`persona_autoloop.py tick` 先问本模块，超限就落 `rest`，账本记一条
`event=rate_limited`（`out` 写清「免费档 12/时」这类原因）；没超就照旧。

红线（任何档都不放宽，包括免费档）：删除/清理记忆、改 kernel.json、换模型、
迁移合并记忆库、改交接格式、改注入四闸、改 350K/900s、出网、pip install、
git push —— 在 `persona_autoloop.py` 的 GUARDS 里逐条写死，与档位无关；
`wish` 时就拒，tick 里也没有任何「放宽」入口。

配置：`ops\autonomy.json`（首次自动生成；仓库交 `ops/autonomy.example.json`）：
  {"cur_by_model": {"free": {"runs_per_hour": 12, "runs_per_day": 120, "note": "…"},
                     "paid": {"runs_per_hour": 1,  "runs_per_day": 3,   "note": "…"},
 "auto_free": false   ← 默认关：识别到免费档也不放宽，要放宽得显式开},
   "patterns": {"free": ["local/*", "*free*", …], "paid": ["deepseek-account:*", …]},
   "current_model": ""}
`current_model` 非空 = 人工钉住；空 = 从成本账本
`~/.dsh/storages/cost-meter/ledger.json` 今日 `days[today].byProviderModel`
里取 calls 最大的那个键（可用环境变量 PERSONA_COST_LEDGER 换文件，测试用）。

命令行：
  python persona_autonomy.py show [--json]
  python persona_autonomy.py classify "ollama:qwen3:8b" [--json]
  python persona_autonomy.py set --tier free --runs-per-hour 20 --runs-per-day 200
  python persona_autonomy.py set --model "ollama:qwen3:8b" | --model auto
库根：--root > SYSPERSONA_ROOT > PERSONA_ROOT > 本文件目录。
"""
import argparse
import copy
import fnmatch
import io
import json
import os
import time

TIERS = ("free", "paid")            # 先 free 后 paid：同时命中时按「免费」算
TIER_LABEL = {"free": "免费档", "paid": "收费档"}
TIER_DESC = {"free": "免费/本地（只花电费）", "paid": "收费（做多了花钱）"}

# 默认值就是「放宽/收紧」的全部差别：只有次数。改这里等于改默认。
DEFAULT_CFG = {
    "cur_by_model": {
        "free": {"runs_per_hour": 12, "runs_per_day": 120,
                 "note": "免费/本地：只花电费，可以多做"},
        "paid": {"runs_per_hour": 1, "runs_per_day": 3,
                 "note": "收费：默认只给最小次数（1/时·3/日），要放宽自己改"},
    },
    "patterns": {
        "free": ["local/*", "*free*", "siliconflow*", "ollama*", "lmstudio*",
                 "deepseek-web*"],
        "paid": ["deepseek-account:*", "deepseek-official:*", "openai*",
                 "anthropic*"],
    },
    "current_model": "",
    # 默认最小：自动识别到免费/本地模型也不放宽，要放宽必须显式打开
    "auto_free": False,
}

RED_LINES = (
    "删除/清理记忆、改 kernel.json、换模型、迁移合并、改交接格式、"
    "改注入四闸、改 350K/900s、出网、pip install、git push "
    "—— 任何档位都不放宽（包括免费档）"
)


def root_of(root=None):
    """库根：显式参数 > SYSPERSONA_ROOT > PERSONA_ROOT > 本文件目录。"""
    return os.path.abspath(root or os.environ.get("SYSPERSONA_ROOT")
                           or os.environ.get("PERSONA_ROOT")
                           or os.path.dirname(os.path.abspath(__file__)))


def config_path(root=None):
    return os.path.join(root_of(root), "ops", "autonomy.json")


def ledger_path():
    """成本账本：本模块只读，绝不写 ~/.dsh。"""
    return (os.environ.get("PERSONA_COST_LEDGER")
            or os.path.join(os.path.expanduser("~"), ".dsh", "storages",
                            "cost-meter", "ledger.json"))


def _read_json(path, default):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _read_jsonl(path):
    rows = []
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        pass
    return rows


def _write_json(path, data):
    """原子写：同目录临时文件 → os.replace。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), time.time_ns())
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for _ in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _num(v, default):
    """数字兜底：坏值（null/字符串/负数）一律回默认，不炸。"""
    try:
        n = int(float(v))
    except (TypeError, ValueError, OverflowError):
        return int(default)
    return n if n >= 0 else int(default)


def load(root=None):
    """读配置；首次运行落一份默认。缺键补默认，用户改过的数不动。"""
    path = config_path(root)
    if not os.path.exists(path):
        cfg = copy.deepcopy(DEFAULT_CFG)
        _write_json(path, cfg)
        return cfg
    cfg = _read_json(path, {})
    if not isinstance(cfg, dict):
        cfg = {}
    for key in ("cur_by_model", "patterns"):
        if not isinstance(cfg.get(key), dict):
            cfg[key] = copy.deepcopy(DEFAULT_CFG[key])
    for tier in TIERS:
        got = cfg["cur_by_model"].get(tier)
        got = got if isinstance(got, dict) else {}
        base = DEFAULT_CFG["cur_by_model"][tier]
        for k in ("runs_per_hour", "runs_per_day", "note"):
            if k not in got:
                got[k] = base[k]
        cfg["cur_by_model"][tier] = got
        if not isinstance(cfg["patterns"].get(tier), list):
            cfg["patterns"][tier] = list(DEFAULT_CFG["patterns"][tier])
    if not isinstance(cfg.get("current_model"), str):
        cfg["current_model"] = ""
    return cfg


def save(cfg, root=None):
    _write_json(config_path(root), cfg)


def classify(model_key, root=None):
    """模型键 → free|paid（glob 忽略大小写）。拿不到或匹配不上 → paid。"""
    key = str(model_key or "").strip().lower()
    if not key:
        return "paid"                      # 不知道用什么模型 → 保守
    cfg = load(root)
    pats = cfg.get("patterns") or {}
    for tier in TIERS:                     # 免费档先匹配：同时命中按免费算
        for pat in (pats.get(tier) or []):
            try:
                if fnmatch.fnmatchcase(key, str(pat).strip().lower()):
                    return tier
            except (TypeError, ValueError):
                continue
    return "paid"


def _ledger_current():
    """今日 `days[today].byProviderModel` 里 calls 最大的键；读不到返回空。"""
    data = _read_json(ledger_path(), None)
    if not isinstance(data, dict):
        return ""
    days = data.get("days") or {}
    day = time.strftime("%Y-%m-%d")
    bucket = days.get(day) if isinstance(days, dict) else None
    by = bucket.get("byProviderModel") if isinstance(bucket, dict) else None
    if not isinstance(by, dict):
        return ""
    best, best_key = "", (-1, -1.0)
    for key, rec in by.items():
        if not isinstance(rec, dict):
            continue
        calls = _num(rec.get("calls"), 0)
        try:
            cost = float(rec.get("cost") or rec.get("apiCost") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        if (calls, cost) > best_key:
            best, best_key = str(key), (calls, cost)
    return best


def current_detail(root=None):
    """(模型键, 来源)。来源 ∈ config / ledger / none。"""
    cfg = load(root)
    pin = str(cfg.get("current_model") or "").strip()
    if pin:
        return pin, "config"
    got = _ledger_current()
    return (got, "ledger") if got else ("", "none")


def current(root=None):
    return current_detail(root)[0]


def _parse_ts(s):
    try:
        return time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def used_runs(root=None, now=None):
    """(本小时已做, 今日已做)：只数真正执行过的行。

    `ops\\autoloop.jsonl` 里 `rest` / `rate_limited` / rc 为空的行都不算一次执行；
    被打断（preempted_by）也算——它确实占用了一次机会。
    """
    now = float(now or time.time())
    lt = time.localtime(now)
    try:
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                0, 0, 0, 0, 0, -1))
    except (TypeError, ValueError, OverflowError):
        midnight = now - 86400.0
    hour = day = 0
    path = os.path.join(root_of(root), "ops", "autoloop.jsonl")
    for rec in _read_jsonl(path):
        if rec.get("event") or (rec.get("id") or "") == "rest":
            continue
        if rec.get("rc") is None:
            continue
        ts = _parse_ts(rec.get("at"))
        if not ts:
            continue
        if 0 <= now - ts < 3600:
            hour += 1
        if midnight <= ts <= now + 1:
            day += 1
    return hour, day


def profile(root=None, now=None):
    """当前生效的 {model, tier, 次数上限, 已用, 还能做}；只读。"""
    cfg = load(root)
    model, source = current_detail(root)
    tier_detected = classify(model, root)
    auto_free = bool(cfg.get("auto_free", False))
    # 默认最小：识别到免费档但没显式打开 auto_free → 仍按收费档的最小次数执行
    tier = tier_detected if (tier_detected != "free" or auto_free) else "paid"
    lv = (cfg.get("cur_by_model") or {}).get(tier) or {}
    base = DEFAULT_CFG["cur_by_model"][tier]
    rh = _num(lv.get("runs_per_hour"), base["runs_per_hour"])
    rd = _num(lv.get("runs_per_day"), base["runs_per_day"])
    used_h, used_d = used_runs(root, now)
    return {"model": model, "model_source": source,
            "tier": tier, "tier_detected": tier_detected, "auto_free": auto_free,
            "tier_label": TIER_LABEL[tier],
            "tier_note": lv.get("note") or TIER_DESC[tier],
            "runs_per_hour": rh, "runs_per_day": rd,
            "used_hour": used_h, "used_day": used_d,
            "left_hour": max(0, rh - used_h), "left_day": max(0, rd - used_d),
            "over_hour": used_h >= rh, "over_day": used_d >= rd}


def limit_reason(p):
    """超限原因（没超返回空串）。措辞固定带「免费档 12/时」「收费档 20/日」。"""
    label = p.get("tier_label") or TIER_LABEL.get(p.get("tier"), "收费档")
    parts = []
    if p.get("over_hour"):
        parts.append("%s %d/时（本小时已用 %d）" % (
            label, p.get("runs_per_hour", 0), p.get("used_hour", 0)))
    if p.get("over_day"):
        parts.append("%s %d/日（今日已用 %d）" % (
            label, p.get("runs_per_day", 0), p.get("used_day", 0)))
    return "；".join(parts)


def set_values(tier="", runs_per_hour=None, runs_per_day=None, model=None,
               auto_free=None, root=None):
    """改数/钉模型。返回 (profile, 变更说明列表)。只改 ops\\autonomy.json。"""
    cfg = load(root)
    changes = []
    if tier:
        if tier not in TIERS:
            return None, ["tier 只能是 %s" % "/".join(TIERS)]
        lv = cfg["cur_by_model"].setdefault(tier, {})
        if runs_per_hour is not None:
            lv["runs_per_hour"] = _num(runs_per_hour, lv.get("runs_per_hour", 0))
            changes.append("cur_by_model.%s.runs_per_hour=%d" % (
                tier, lv["runs_per_hour"]))
        if runs_per_day is not None:
            lv["runs_per_day"] = _num(runs_per_day, lv.get("runs_per_day", 0))
            changes.append("cur_by_model.%s.runs_per_day=%d" % (
                tier, lv["runs_per_day"]))
    if auto_free is not None:
        cfg["auto_free"] = bool(auto_free)
        changes.append("auto_free=%s（%s）" % (
            "true" if auto_free else "false",
            "允许免费档放宽" if auto_free else "一律按最小档"))
    if model is not None:
        m = str(model or "").strip()
        if m.lower() == "auto":
            cfg["current_model"] = ""
            changes.append("current_model=auto（回到看账本）")
        else:
            cfg["current_model"] = m
            changes.append("current_model=%s（只登记，不换模型）" % m)
    if not changes:
        return profile(root), []
    save(cfg, root)
    return profile(root), changes


def _fmt_show(p, cfg, root):
    src = {"config": "配置里钉住", "ledger": "账本今日 calls 最大",
           "none": "拿不到（按收费档）"}.get(p.get("model_source"), "?")
    lines = [
        "自主次数闸：免费/本地档放宽，收费档收紧（只限次数，别的都不动）",
        "配置：%s" % config_path(root),
        "当前模型：%s（来自 %s）" % (p.get("model") or "—", src),
        "归到：%s · %s" % (p.get("tier_label"), p.get("tier_note")),
        "本小时：已用 %d 次 / 上限 %d 次 / 还能做 %d 次" % (
            p.get("used_hour"), p.get("runs_per_hour"), p.get("left_hour")),
        "今日：已用 %d 次 / 上限 %d 次 / 还能做 %d 次" % (
            p.get("used_day"), p.get("runs_per_day"), p.get("left_day")),
        "两档（改 ops\\autonomy.json 或 set）：",
    ]
    for tier in TIERS:
        lv = (cfg.get("cur_by_model") or {}).get(tier) or {}
        pats = (cfg.get("patterns") or {}).get(tier) or []
        lines.append("  %-4s %2d/时 %3d/日  %s  ← %s" % (
            tier, lv.get("runs_per_hour", 0), lv.get("runs_per_day", 0),
            lv.get("note") or TIER_DESC[tier], ", ".join(pats)))
    lines += [
        "改数：python persona_autonomy.py set --tier free --runs-per-hour 20 --runs-per-day 200",
        "      python persona_autonomy.py set --model \"ollama:qwen3:8b\"（钉住）/ --model auto（回到看账本）",
        "红线：%s" % RED_LINES,
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="自主的次数闸（免费/收费两档）")
    ap.add_argument("cmd", nargs="?", default="show",
                    choices=["show", "set", "classify"])
    ap.add_argument("model_key", nargs="?", default="", help="classify 的模型键")
    ap.add_argument("--root", default="", help="库根（默认 SYSPERSONA_ROOT / 本文件目录）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--auto-free", dest="auto_free", choices=["on", "off"], default=None,
                    help="set 是否允许免费/本地档放宽（默认 off = 一律按最小档）")
    ap.add_argument("--tier", choices=list(TIERS), default="",
                    help="set 改哪一档（free/paid）")
    ap.add_argument("--runs-per-hour", type=int, default=None, dest="runs_per_hour")
    ap.add_argument("--runs-per-day", type=int, default=None, dest="runs_per_day")
    ap.add_argument("--model", dest="model_opt", default=None,
                    help="set 钉住模型键 / auto 清空；classify 也可用")
    a = ap.parse_args(argv)
    root = a.root or None

    if a.cmd == "classify":
        key = (a.model_key or a.model_opt or "").strip()
        tier = classify(key, root)
        if a.as_json:
            print(json.dumps({"model": key, "tier": tier,
                              "tier_label": TIER_LABEL[tier],
                              "desc": TIER_DESC[tier]},
                             ensure_ascii=False))
        else:
            print("%s → %s（%s）" % (key or "（空）", tier, TIER_DESC[tier]))
        return 0

    if a.cmd == "set":
        if not a.tier and a.model_opt is None and a.auto_free is None:
            print("set 要 --tier free|paid --runs-per-hour N [--runs-per-day N]，"
                  "或 --model <模型键|auto>")
            return 2
        if a.runs_per_hour is not None and a.runs_per_hour < 0:
            print("--runs-per-hour 不能为负（0 = 不自动做事）")
            return 2
        if a.runs_per_day is not None and a.runs_per_day < 0:
            print("--runs-per-day 不能为负（0 = 不自动做事）")
            return 2
        p, changes = set_values(a.tier, a.runs_per_hour, a.runs_per_day,
                                a.model_opt,
                                None if a.auto_free is None else (a.auto_free == "on"),
                                root)
        if p is None:
            print("；".join(changes))
            return 2
        if a.as_json:
            print(json.dumps({"changed": changes, "profile": p},
                             ensure_ascii=False, indent=1))
        elif changes:
            print("已改：%s" % "；".join(changes))
            print("现在：%s · %s · 本小时 %d/%d · 今日 %d/%d" % (
                p.get("tier"), p.get("model") or "—",
                p.get("used_hour"), p.get("runs_per_hour"),
                p.get("used_day"), p.get("runs_per_day")))
        else:
            print("没改任何东西（--tier 下至少要给 --runs-per-hour 或 --runs-per-day）")
        return 0

    cfg = load(root)
    p = profile(root)
    if a.as_json:
        print(json.dumps({"profile": p, "cur_by_model": cfg.get("cur_by_model"),
                          "patterns": cfg.get("patterns"),
                          "current_model": cfg.get("current_model"),
                          "config": config_path(root),
                          "ledger": ledger_path(), "red_lines": RED_LINES},
                         ensure_ascii=False, indent=1))
    else:
        print(_fmt_show(p, cfg, root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
