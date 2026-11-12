# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_autoloop.py — 空闲时的全局自主循环（v2，自述意图版）
r"""persona_autoloop.py — 空闲时的全局自主循环（v2，自述意图版）

不是「管理员往里加任务」，是**记忆 + 底座里的那个自己，平常把想做的事写下来，
空闲唤醒时自己去做**。意图存在 `ops\intents.jsonl`（只追加，不改历史）。

优先级（高 → 低）：
  闹钟 > 守护定时 > 有活干活 > 空闲循环（本模块） > 休息。

只在**队列空**时跑：在办无未收尾 + 任务池无未完成 + 没有到点闹钟。
守护的 `t_wake`（30 秒那班）先发闹钟，再问 `idle()`；空了才 `tick()`。
有活时绝不 tick——有活先干活。

意图一行（`ops\intents.jsonl`，只追加；同 id 以后写的为准）：
  {"id":"i-xxxx","at":"…","by":"self","what":"我想做什么","why":"…","run":"本地命令或空",
   "priority":40,"min_gap":1800,"needs":"none|human|external",
   "status":"open|done|dropped|refused","done_at":"","result":"rc=0 …",
   "last_done_at":"","preempted_count":0,"promoted_at":""}

饥饿保护：连续被抢 ≥3 次或 >24 小时没做成 → 优先级上浮一档（只到 routine 之前、
闹钟之下，每条每天最多一次）；账本追一条 event=promoted，⑩ 标「因饥饿上浮」，
⑫ 列「被挤压的」。空闲期是它的时间，但不是无限自由。

执行边界（写在代码里）：
  * needs=none 且 run 非空 → 空闲 tick 直接执行（120 秒超时、CREATE_NO_WINDOW、输出截断记账）；
  * needs=human → 不执行，只在简讯 ⑩ 里报「要你点头」；
  * needs=external（联网/账号/花钱）→ 同样只报，不执行；
  * run 为空 → 当「想法」，⑩ 里报出来，等会话里自己做掉再 done；
  * `wish` 前过自设护栏：删/清记忆、改 kernel.json、换模型/微调、迁移合并、
    改交接格式、改注入四闸、改 350K/900s、出网（curl/wget/http）、pip install、
    git push → 一律拒绝并记一条 refused。

`rest` 兜底保留：没有「想做又能做且到点」的事，就休息（不再执行任何命令）。
全局开关：`ops\autoloop.json` 的 enabled（首次自动生成）；false 时整块停。

次数闸（persona_autonomy，按底座模型花不花钱分免费/收费两档）：
  * 只限「一小时/一天最多自己做几次」；免费/本地档放宽、收费档收紧；
  * tick 先问档位，超限就落 `rest`，账本记一条 `event=rate_limited`；
  * 其余（priority、needs、护栏、抢占、饥饿保护）**一律不动**；
  * 红线（删/清记忆、改 kernel、换模型、迁移合并、改交接格式、改注入四闸、
    改 350K/900s、出网、pip、git push）任何档位都不放宽，仍由下面 GUARDS 写死。

命令行：
  python persona_autoloop.py wish "我想做 X" [--why …] [--run "命令"] [--priority N]
                                 [--gap 秒] [--needs none|human|external] [--by self|human]
  python persona_autoloop.py intents|list|status
  python persona_autoloop.py done <id> [--result "rc=0 …"]
  python persona_autoloop.py drop <id>
  python persona_autoloop.py on | off
  python persona_autoloop.py --once [--dry-run]
库根：--root > SYSPERSONA_ROOT > PERSONA_ROOT > 本文件目录。
"""
import argparse
import persona_io as _pio  # 原子读写（v6.11.0，B85 第一批）
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

try:
    import persona_autonomy as AU      # 次数闸：缺模块时 tick 保守休息，不执行
except Exception:                      # noqa: BLE001 —— 坏部署不许把 tick 带崩
    AU = None

NEEDS = ("none", "human", "external")
TICK_TIMEOUT = 120
OUT_MAX = 400
ROUTINE_PRIORITY = 50     # routine 的分界：routine 之前 = 数字 < 50
PROMOTE_STEP = 10         # 每次上浮一档
PROMOTE_FLOOR = 2         # 再高也不许越过闹钟（1）
STARVE_PREEMPT = 3        # 连续被抢占这么多次 → 上浮
STARVE_HOURS = 24         # 超过这么久没做成 → 上浮
_TICK_LOCK = threading.Lock()

# 自设护栏：命中就拒绝，记一条 refused。理由统一是「属于需人点头项，先问」。
GUARDS = [
    ("删除/清理记忆", re.compile(
        # 顺序无关：「删除 X」和「X 删掉」都要拦（2026-11-10 实测漏过后者）
        r"(?:(删除|删掉|清空|清理|清掉|移除|抹除|erase|delete|purge).{0,20}"
        r"(记忆|memory|会话|记录|归档|archive|sessions|索引|index|续接|文件|日志|log)"
        r"|(记忆|memory|会话|记录|归档|archive|sessions|索引|index|续接|文件|日志|log)"
        r".{0,20}(删除|删掉|清空|清理|清掉|移除|抹除|erase|delete|purge))", re.I)),
    ("递归删除记忆/会话/索引", re.compile(
        r"(rm\s+-rf|Remove-Item\b.{0,40}-Recurse|del\s+/[sq]).{0,60}"
        r"(记忆|memory|sessions|会话|索引|ops)", re.I)),
    ("改 kernel.json", re.compile(r"kernel\.json", re.I)),
    ("换模型/微调", re.compile(
        r"(换|更换|切换|改|替换)模型|换底座|微调|fine[-\s]?tun", re.I)),
    ("迁移/合并记忆库", re.compile(
        r"迁移|合并.{0,10}(库|记忆|会话|索引)|merge.{0,15}(memory|session|index|database|db)",
        re.I)),
    ("改交接文件格式", re.compile(
        r"交接.{0,12}格式|handoff.{0,15}format|改.{0,10}交接包|交接包.{0,10}格式", re.I)),
    ("改注入四闸", re.compile(r"注入.{0,10}闸|四闸|inject.{0,12}gate", re.I)),
    ("改 350K/900s 阈值", re.compile(r"350\s*[kK]|900\s*(s|秒)|350k|900s", re.I)),
    ("联网/出网", re.compile(
        r"\bcurl\b|\bwget\b|https?://|https?\b|Invoke-WebRequest|Invoke-RestMethod|"
        r"requests\.(get|post|put)|urllib|fetch\(", re.I)),
    ("pip install", re.compile(r"\bpip3?\s+install\b|-m\s+pip\s+install", re.I)),
    ("git push", re.compile(r"\bgit\s+push\b", re.I)),
]


def root_of(root=None):
    """库根：显式参数 > SYSPERSONA_ROOT > PERSONA_ROOT > 本文件目录。"""
    return os.path.abspath(root or os.environ.get("SYSPERSONA_ROOT")
                           or os.environ.get("PERSONA_ROOT")
                           or os.path.dirname(os.path.abspath(__file__)))


def _paths(root=None):
    ops = os.path.join(root_of(root), "ops")
    return (ops,
            os.path.join(ops, "autoloop.json"),
            os.path.join(ops, "intents.jsonl"),
            os.path.join(ops, "autoloop.jsonl"))


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path, default):
    return _pio.read_json_any(path, default)


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
    """原子写：委托 persona_io（临时文件 → os.replace，Windows 上重试）。"""
    return _pio.write_json(path, data, indent=1)


def load(root=None):
    """全局开关；首次运行落一份 {"enabled": true}。坏文件按开处理。"""
    _, spath, _, _ = _paths(root)
    if not os.path.exists(spath):
        _write_json(spath, {"enabled": True})
        return {"enabled": True}
    cfg = _read_json(spath, {})
    if not isinstance(cfg, dict):
        return {"enabled": True}
    cfg.setdefault("enabled", True)
    return cfg


def save_switch(cfg, root=None):
    _, spath, _, _ = _paths(root)
    _write_json(spath, cfg)


def _append_line(path, rec):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _append_intent(rec, root=None):
    _, _, ipath, _ = _paths(root)
    return _append_line(ipath, rec)


def _append_ledger(rec, root=None):
    _, _, _, lpath = _paths(root)
    return _append_line(lpath, rec)


def _parse_ts(s):
    try:
        return time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def latest_intents(root=None):
    """id → 最新一条意图（同 id 后面的行覆盖前面的字段）。"""
    _, _, ipath, _ = _paths(root)
    state = {}
    for rec in _read_jsonl(ipath):
        iid = rec.get("id")
        if not iid:
            continue
        cur = dict(state.get(iid) or {})
        cur.update(rec)
        state[iid] = cur
    return state


def last_runs(root=None):
    """id → 上次执行时间（epoch），来自执行账本 autoloop.jsonl。"""
    _, _, _, lpath = _paths(root)
    out = {}
    for rec in _read_jsonl(lpath):
        cid = rec.get("id")
        if not cid or rec.get("event"):   # rate_limited/rest 等不是一次执行
            continue
        ts = _parse_ts(rec.get("at"))
        if ts and ts > out.get(cid, 0):
            out[cid] = ts
    return out


def _last_entry(root=None):
    _, _, _, lpath = _paths(root)
    rows = _read_jsonl(lpath)
    return rows[-1] if rows else {}


def _update_intent(it, root=None, **fields):
    upd = dict(it)
    upd.update(fields)
    upd.pop("event", None)
    return _append_intent(upd, root)


def _age_seconds(it, now):
    """上一次做成（或创建）到现在多久；用于「24 小时没做成」。"""
    base = _parse_ts(it.get("last_done_at")) or _parse_ts(it.get("at"))
    return max(0.0, now - base) if base else 0.0


def _age_txt(age):
    if age < 3600:
        return "%d 分钟" % max(1, int(age // 60))
    return "%d 小时" % int(age // 3600)


def _promoted_today(it, now):
    pa = _parse_ts(it.get("promoted_at"))
    if not pa:
        return False
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    return time.strftime("%Y-%m-%d", time.localtime(pa)) == day


def maybe_promote(root=None):
    """饥饿保护：连续被抢 ≥3 次或 >24h 没做成 → 上浮一档。

    上限在 routine 之上、闹钟之下（PROMOTE_FLOOR=2）；每条意图每天最多一次。
    返回 [(id, 旧 priority, 新 priority)]，并往 intents.jsonl 追一条 event=promoted。
    """
    state = latest_intents(root)
    now = time.time()
    promoted = []
    for iid, it in state.items():
        if it.get("status") != "open" or (it.get("needs") or "none") != "none":
            continue
        cnt = int(it.get("preempted_count") or 0)
        stale = _age_seconds(it, now) > STARVE_HOURS * 3600
        if cnt < STARVE_PREEMPT and not stale:
            continue
        if _promoted_today(it, now):
            continue
        p = _prio(it)
        new = max(PROMOTE_FLOOR, min(p - PROMOTE_STEP, ROUTINE_PRIORITY - 1))
        if new >= p:
            continue
        upd = dict(it)
        upd.update({"priority": new, "promoted_at": _now_str(), "promoted": True,
                    "promoted_from": p,
                    "promoted_reason": (("连续被抢占 %d 次" % cnt)
                                        if cnt >= STARVE_PREEMPT
                                        else "超过 24 小时没做成"),
                    "event": "promoted"})
        _append_intent(upd, root)
        promoted.append((iid, p, new))
    return promoted


def _prio(it):
    try:
        return int(it.get("priority"))
    except (TypeError, ValueError):
        return 100


def _gap(it):
    try:
        return max(0, int(it.get("min_gap") or 0))
    except (TypeError, ValueError):
        return 0


def _open_intents(root=None):
    st = latest_intents(root)
    return [it for it in st.values() if it.get("status") == "open"]


def _executable(it):
    return (it.get("status") == "open" and (it.get("needs") or "none") == "none"
            and bool((it.get("run") or "").strip()))


def _wait(it, last, now):
    if not last:
        return 0.0
    return max(0.0, (last + _gap(it)) - now)


def _sorted_open(root=None):
    """按 priority 升序；同优先级按「到点时间」早的在前。"""
    rows = _open_intents(root)
    last = last_runs(root)

    def key(it):
        l = float(last.get(it.get("id"), 0) or 0)
        due = (l + _gap(it)) if l else _parse_ts(it.get("at"))
        return (_prio(it), due or 0, _parse_ts(it.get("at")))

    rows.sort(key=key)
    return rows


def pick_detail(root=None):
    """挑一条「open + needs=none + run 非空 + min_gap 到点」的意图；没有 → rest。"""
    now = time.time()
    last = last_runs(root)
    for it in _sorted_open(root):
        if not _executable(it):
            continue
        l = float(last.get(it.get("id"), 0) or 0)
        if _wait(it, l, now) <= 0:
            return it, {"last": l, "wait": 0.0}
    return {"id": "rest", "what": "休息", "status": "rest", "priority": 99,
            "needs": "none", "min_gap": 0, "run": ""}, {"last": 0.0, "wait": 0.0}


def pick(root=None):
    return pick_detail(root)[0]


def _short(s, n=48):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _last_desc(ts, now):
    if not ts:
        return "还没跑过"
    age = max(0, int(now - ts))
    if age < 60:
        return "刚跑过"
    return "%d 分钟前跑过" % (age // 60)


def _wait_txt(wait):
    if wait <= 0:
        return "现在"
    if wait < 60:
        return "%d 秒" % max(1, int(wait))
    return "%d 分钟" % int((wait + 59) // 60)


def _needs_tag(it):
    needs = it.get("needs") or "none"
    if needs == "human":
        return "要你点头"
    if needs == "external":
        return "要联网/外部"
    if (it.get("run") or "").strip():
        return "有命令，等空闲"
    return "只是想法（run 空）"


def _squeezed_line(root=None):
    """⑫ 被挤压的：谁被高优先级抢过几次、多久没做成。只读。"""
    now = time.time()
    rows = []
    for it in _sorted_open(root):
        if (it.get("needs") or "none") != "none":
            continue
        cnt = int(it.get("preempted_count") or 0)
        age = _age_seconds(it, now)
        if cnt <= 0 and age <= STARVE_HOURS * 3600:
            continue
        rows.append((it, cnt, age))
    if not rows:
        return "⑫ 被挤压的：无"
    rows.sort(key=lambda r: (-r[1], -r[2]))
    parts = []
    for it, cnt, age in rows[:3]:
        if cnt:
            parts.append("%s（被抢 %d 次，%s没做）" % (
                _short(it.get("what"), 24), cnt, _age_txt(age)))
        else:
            parts.append("%s（没被抢，%s没做）" % (
                _short(it.get("what"), 24), _age_txt(age)))
    line = "⑫ 被挤压的：" + "；".join(parts)
    if len(rows) > 3:
        line += "；…另 %d 条" % (len(rows) - 3)
    return line


def wake_lines(root=None):
    """简讯 ⑩/⑪/⑫：只读判定，不执行任何命令。"""
    cfg = load(root)
    if not cfg.get("enabled", True):
        return ["⑩ 我留给自己的：空闲循环已关（enabled=false）",
                "⑪ 现在要做的：—",
                "⑫ 被挤压的：—"]
    rows = _sorted_open(root)
    lines = ["⑩ 我留给自己的（最多 3 条，按优先级）"]
    if not rows:
        lines.append("   （没有，先休息）")
    else:
        for it in rows[:3]:
            tag = _needs_tag(it)
            mark = "（因饥饿上浮）" if it.get("promoted_at") else ""
            lines.append("   - %s（%s）%s" % (
                _short(it.get("what") or it.get("id")), tag, mark))
        if len(rows) > 3:
            lines.append("   - …另 %d 条" % (len(rows) - 3))
    cand, meta = pick_detail(root)
    now = time.time()
    if cand.get("id") == "rest":
        lines.append("⑪ 现在要做的：休息（没有想做又能做的）")
    else:
        lines.append("⑪ 现在要做的：%s（%s，现在可跑）" % (
            _short(cand.get("what")), _last_desc(meta.get("last"), now)))
    lines.append(_squeezed_line(root))
    return lines


def _expand(cmd):
    py = subprocess.list2cmdline([sys.executable])
    return (cmd or "").replace("<python>", py).replace("{python}", py)


def _run(cmd, root, job="", priority=99):
    """所有候选一律走可抢占执行；高优先级来了会 terminate 并记 preempted_by。"""
    try:
        import persona_sched as S
        return S.run_preemptible(job or "autoloop", priority, _expand(cmd),
                                 timeout=TICK_TIMEOUT, owner="autoloop", root=root)
    except Exception as e:
        return {"rc": -1, "out": ("执行失败：%s" % e)[:OUT_MAX],
                "preempted_by": "", "state": "idle", "started": time.time()}


def _autonomy_profile(root):
    """当前档位与用量；模块缺失/坏配置 → None（tick 保守休息，不执行）。"""
    if AU is None:
        return None
    try:
        p = AU.profile(root=root)
        return p if isinstance(p, dict) else None
    except Exception:                  # noqa: BLE001 —— 判不出来就休息，别乱跑
        return None


def _append_rate_limited(root, prof, reason, min_gap=120):
    """rate_limited 记账：30 秒一 tick，同一条最多 2 分钟记一次（不刷屏）。"""
    last = _last_entry(root)
    if last.get("event") == "rate_limited":
        ts = _parse_ts(last.get("at"))
        if ts and time.time() - ts < min_gap:
            return False
    rec = {"at": _now_str(), "id": "rest", "rc": 0, "out": reason,
           "event": "rate_limited",
           "tier": prof.get("tier"), "model": prof.get("model"),
           "model_source": prof.get("model_source"),
           "used_hour": prof.get("used_hour"),
           "runs_per_hour": prof.get("runs_per_hour"),
           "used_day": prof.get("used_day"),
           "runs_per_day": prof.get("runs_per_day")}
    return _append_ledger(rec, root)


def tick(dry_run=False, root=None):
    """跑一次空闲循环。返回 dict（id / what / rc / out / dry / recorded / msg）。"""
    root = root_of(root)
    out = {"id": "", "what": "", "rc": None, "out": "", "dry": bool(dry_run),
           "recorded": False, "msg": ""}
    cfg = load(root)
    if not cfg.get("enabled", True):
        out["msg"] = "空闲循环已关"
        return out
    if not _TICK_LOCK.acquire(False):
        out["msg"] = "上一轮还没跑完，跳过"
        return out
    try:
        # ── 次数闸（persona_autonomy）：免费/本地档放宽、收费档收紧。 ──
        # 只限次数：超了落 rest 并记 rate_limited；优先级/needs/护栏/抢占/
        # 饥饿保护都不在这里改，红线任何档位都不放宽。
        prof = _autonomy_profile(root)
        if prof is None:
            out.update(rc=0, out="休息",
                       msg="自主档位不可用，本轮休息（保守，不执行）")
            return out
        out["autonomy"] = prof
        if prof.get("over_hour") or prof.get("over_day"):
            reason = AU.limit_reason(prof)
            out.update(id="rest", what="rest", rc=0, out=reason,
                       msg="次数闸：%s → 落 rest" % reason)
            if not dry_run:
                out["recorded"] = _append_rate_limited(root, prof, reason)
            return out
        if not dry_run:
            maybe_promote(root)
        cand, meta = pick_detail(root)
        iid = cand.get("id") or ""
        out.update(id=iid, what=cand.get("what") or iid)
        now_s = _now_str()
        if iid == "rest":
            out.update(rc=0, out="休息", msg="休息（没有想做又能做的）")
            if not dry_run and _last_entry(root).get("id") != "rest":
                out["recorded"] = _append_ledger(
                    {"at": now_s, "id": "rest", "rc": 0, "out": "休息"}, root)
            return out
        cmd = (cand.get("run") or "").strip()
        if dry_run:
            out.update(msg=("将执行：%s" % cmd) if cmd else "无 run，只记账")
            return out
        t0 = time.time()
        rr = _run(cmd, root, iid, _prio(cand))
        rc = rr.get("rc", -1)
        summary = rr.get("out", "")
        preempted_by = rr.get("preempted_by", "")
        out.update(rc=rc, out=summary)
        if preempted_by:
            out["msg"] = "被打断（by %s）" % preempted_by
        else:
            out["msg"] = "跑了 %s（rc=%s）" % (_short(cand.get("what"), 30), rc)
        # 饥饿账：被抢就 +1（连续计数）；做成一次就清零并记 last_done_at。
        cur = latest_intents(root).get(iid)
        if cur:
            if preempted_by:
                _update_intent(cur, root,
                               preempted_count=int(cur.get("preempted_count") or 0) + 1,
                               last_preempted_at=_now_str(),
                               last_preempted_by=preempted_by)
            elif rc == 0:
                _update_intent(cur, root, last_done_at=_now_str(),
                               preempted_count=0)
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(rr.get("started", t0))),
               "id": iid, "rc": rc, "out": summary}
        if preempted_by:
            rec["preempted_by"] = preempted_by
            rec["status"] = "interrupted"
        out["recorded"] = _append_ledger(rec, root)
        return out
    finally:
        _TICK_LOCK.release()


def idle(root=None):
    """队列空？返回 (bool, 原因)。三项都不忙才算空。"""
    root = root_of(root)
    ops = os.path.join(root, "ops")
    inflight = _read_json(os.path.join(ops, "inflight.json"), {})
    items = inflight.get("items") if isinstance(inflight, dict) else None
    busy = [it for it in (items or []) if isinstance(it, dict)
            and (it.get("status") or "running") in ("running", "waiting", "blocked")]
    if busy:
        return False, "在办 %d 项" % len(busy)

    latest = {}
    for rec in _read_jsonl(os.path.join(ops, "tasks.jsonl")):
        if rec.get("id"):
            latest[rec["id"]] = rec
    pending = [r for r in latest.values()
               if r.get("status") in ("pending", "claimed", "running")]
    if pending:
        return False, "任务池 %d 项未完成" % len(pending)

    wake = _read_json(os.path.join(ops, "wake.json"), [])
    now = time.time()
    due = [r for r in (wake if isinstance(wake, list) else [])
           if isinstance(r, dict) and r.get("enabled", True)
           and float(r.get("due") or 0) <= now]
    if due:
        return False, "有 %d 个到点闹钟" % len(due)
    return True, ""


def _guard(what, run):
    """返回命中的护栏名；没命中返回 None。只检查 what + run 文本。"""
    text = "%s\n%s" % (what or "", run or "")
    for label, pat in GUARDS:
        if pat.search(text):
            return label
    return None


def _next_id(state):
    base = "i-%d" % (int(time.time() * 1000) % 100000000)
    iid, n = base, 1
    while iid in state:
        n += 1
        iid = "%s-%d" % (base, n)
    return iid


def _new_intent(what, why="", run="", priority=50, gap=1800, needs="none", by="self"):
    return {"id": "", "at": _now_str(), "by": by, "what": what, "why": why,
            "run": run, "priority": int(priority), "min_gap": int(gap),
            "needs": needs, "status": "open", "done_at": "", "result": "",
            "last_done_at": "", "preempted_count": 0,
            "last_preempted_at": "", "last_preempted_by": "",
            "promoted_at": "", "promoted": False}


def cmd_wish(a, root):
    what = " ".join((a.what or "").split())
    if not what:
        print("wish 需要一句话：我想做什么")
        return 2
    hit = _guard(what, a.run or "")
    state = latest_intents(root)
    rec = _new_intent(what, a.why or "", a.run or "", a.priority, a.gap,
                      a.needs, a.by)
    rec["id"] = _next_id(state)
    if hit:
        rec["status"] = "refused"
        rec["result"] = "guard:%s（属于需人点头项，先问）" % hit
        _append_intent(rec, root)
        print("拒绝（属于需人点头项，先问）：命中护栏「%s」；已记 refused %s"
              % (hit, rec["id"]))
        return 1
    _append_intent(rec, root)
    print("已记下 %s：%s（priority=%s, needs=%s, run=%s）"
          % (rec["id"], rec["what"], rec["priority"], rec["needs"],
             rec["run"] or "（空，只是想法）"))
    return 0


def _find(a, root):
    state = latest_intents(root)
    iid = (getattr(a, "id", "") or getattr(a, "what", "") or "").strip()
    if not iid:
        return None, state
    return state.get(iid), state


def cmd_done(a, root):
    rec, _ = _find(a, root)
    if rec is None:
        print("没有 id=%s（先用 intents 看 id）" % (getattr(a, "id", "")))
        return 1
    if rec.get("status") != "open":
        print("拒绝：%s 已是 %s" % (rec.get("id"), rec.get("status")))
        return 1
    upd = dict(rec)
    upd["status"] = "done"
    upd["done_at"] = _now_str()
    upd["last_done_at"] = upd["done_at"]
    upd["preempted_count"] = 0
    upd["result"] = a.result if a.result is not None else (rec.get("result") or "done")
    upd.pop("event", None)
    _append_intent(upd, root)
    print("已完成 %s：%s" % (upd["id"], _short(upd.get("what"), 60)))
    return 0


def cmd_drop(a, root):
    rec, _ = _find(a, root)
    if rec is None:
        print("没有 id=%s（先用 intents 看 id）" % (getattr(a, "id", "")))
        return 1
    if rec.get("status") != "open":
        print("拒绝：%s 已是 %s" % (rec.get("id"), rec.get("status")))
        return 1
    upd = dict(rec)
    upd["status"] = "dropped"
    upd["done_at"] = _now_str()
    upd["preempted_count"] = 0
    upd["result"] = "dropped"
    upd.pop("event", None)
    _append_intent(upd, root)
    print("已丢弃 %s：%s" % (upd["id"], _short(upd.get("what"), 60)))
    return 0


def _hms(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "?"


def _fmt_last(ts):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "—"


def _fmt_next(it, last, now):
    if not last:
        return "现在"
    due = last + _gap(it)
    if due <= now:
        return "现在"
    return "%s（还差 %s）" % (_hms(due), _wait_txt(due - now))


def print_intents(root=None):
    """意图列表：open 按 priority + 到点排前，收尾的排后面。"""
    state = latest_intents(root)
    last = last_runs(root)
    now = time.time()
    rows = list(state.values())
    def _row_key(it):
        l = float(last.get(it.get("id"), 0) or 0)
        due = (l + _gap(it)) if l else _parse_ts(it.get("at"))
        return (0 if it.get("status") == "open" else 1, _prio(it), due or 0,
                _parse_ts(it.get("at")))
    rows.sort(key=_row_key)
    print("意图（ops\\intents.jsonl）：%d 条" % len(rows))
    print("id         status  prio needs    gap    上次               下次                by    at                  what")
    print("-" * 140)
    if not rows:
        print("（还没有：用 wish \"我想做 X\" 写一条）")
    for it in rows:
        iid = it.get("id") or "?"
        l = float(last.get(iid, 0) or 0)
        print("%-10s %-7s %-4s %-7s %-6s %-18s %-18s %-5s %-19s %s" % (
            iid, it.get("status") or "?", _prio(it), it.get("needs") or "none",
            _gap(it), _fmt_last(l),
            _fmt_next(it, l, now) if _executable(it) else "—",
            it.get("by") or "—", it.get("at") or "—",
            _short(it.get("what"), 40)))
        extra = []
        cnt = int(it.get("preempted_count") or 0)
        age = _age_seconds(it, now)
        if cnt or it.get("promoted_at") or age > STARVE_HOURS * 3600:
            extra.append("饥饿：被抢 %d 次／%s没做%s" % (
                cnt, _age_txt(age), "／已上浮" if it.get("promoted_at") else ""))
        if it.get("last_done_at"):
            extra.append("last_done_at: %s" % it.get("last_done_at"))
        if (it.get("run") or "").strip():
            extra.append("run: %s" % _short(it.get("run"), 70))
        if it.get("why"):
            extra.append("why: %s" % _short(it.get("why"), 60))
        if it.get("result"):
            extra.append("result: %s" % _short(it.get("result"), 60))
        if extra:
            print("    " + " ／ ".join(extra))
    _, spath, ipath, lpath = _paths(root)
    print("")
    print("开关：%s（%s）" % ("开" if load(root).get("enabled", True) else "关", spath))
    print("意图：%s" % ipath)
    print("账本：%s" % lpath)


def cmd_switch(a, root):
    on = a.cmd == "on"
    cfg = load(root)
    cfg["enabled"] = on
    save_switch(cfg, root)
    print("空闲循环：%s" % ("开" if on else "关"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="空闲时的全局自主循环（自述意图）")
    ap.add_argument("cmd", nargs="?", default="intents",
                    choices=["intents", "list", "status", "wish", "done", "drop", "on", "off"])
    ap.add_argument("--root", default="", help="库根（默认 SYSPERSONA_ROOT / 本文件目录）")
    ap.add_argument("what", nargs="?", default="", help="wish 的一句话")
    ap.add_argument("--why", default="")
    ap.add_argument("--run", default="")
    ap.add_argument("--priority", type=int, default=50)
    ap.add_argument("--gap", type=int, default=1800)
    ap.add_argument("--needs", choices=NEEDS, default="none")
    ap.add_argument("--by", choices=["self", "human"], default="self")
    ap.add_argument("--id", default="")
    ap.add_argument("--result", default=None)
    ap.add_argument("--once", action="store_true", help="手动 tick 一次")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="只看会挑到谁，不执行、不记账")
    a = ap.parse_args(argv)
    root = a.root or None

    if a.once or a.dry_run:
        r = tick(dry_run=a.dry_run, root=root)
        aut = r.get("autonomy") or {}
        if a.dry_run and aut:
            print("[dry-run] 档位：%s（%s）· 本小时 %d/%d · 今日 %d/%d" % (
                aut.get("tier"), aut.get("tier_label"),
                aut.get("used_hour", 0), aut.get("runs_per_hour", 0),
                aut.get("used_day", 0), aut.get("runs_per_day", 0)))
        if a.dry_run:
            if r.get("id") == "rest":
                print("[dry-run] %s" % (r.get("msg") or "休息"))
            else:
                print("[dry-run] 选中：%s；%s" % (r.get("what") or r.get("id") or "—",
                                                r.get("msg") or ""))
        elif r.get("id") == "rest":
            print("%s（rc=%s，记账=%s）" % (r.get("msg") or "休息",
                                          r.get("rc"), r.get("recorded")))
        else:
            print("%s（rc=%s，记账=%s）%s" % (r.get("msg") or r.get("what") or r.get("id") or "—",
                                          r.get("rc"), r.get("recorded"),
                                          ("：" + r["out"]) if r.get("out") else ""))
        return 0

    if a.cmd in ("intents", "list", "status"):
        print_intents(root)
        return 0
    if a.cmd == "wish":
        return cmd_wish(a, root)
    if a.cmd == "done":
        return cmd_done(a, root)
    if a.cmd == "drop":
        return cmd_drop(a, root)
    if a.cmd in ("on", "off"):
        return cmd_switch(a, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
