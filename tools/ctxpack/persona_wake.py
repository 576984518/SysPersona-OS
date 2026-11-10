# -*- coding: utf-8 -*-
r"""persona_wake.py — 闹钟登记与唤醒简讯（v5.3）

唤醒分三类（2026-11-10 他重新定的口径，以此为准）：

1. **心跳唤醒**：静默维护，不打扰前台。守护的 `wake` 任务每 30 秒扫一次，只维护、只记账。
2. **自主定时唤醒**：**AI 自己**定的闹钟——每完成一个任务就给自己排下一次（「5 分钟后查编译结果」），
   到点把自己叫醒接着干。特征：登记人是分身自己，purpose 是下一步动作。
3. **云端服务唤醒**：由服务端（任务调度）决定谁该醒、醒了领哪件活，对应 **v5.5 任务调度**；
   本机只负责收指令 + 报到 + 领活，不自己排期。

> 曾经有一版写成「心跳 / 闹钟 / 日常」，那是按"谁触发"分的；他 2026-11-10 纠正：
> 分法要看**谁来决定什么时候醒**——守护定 / 自己定 / 云端定，这三类才互不重叠。

规则：
  * 闹钟必须带 purpose（醒来做什么），没有 purpose 一律拒绝登记——防「醒了不知道干什么」；
  * 到点不是「拉起来就算完」：先摘一份**总索引简讯**（最近的来源 + 派发中的任务），
    再执行 purpose、写触发记录、替 agent 报到（已读索引），然后才交给调用方；
  * 三种来源：定时（人设）/ 任务（智能体自设）/ 事件（调度器直接写记录）。

唤醒简讯在「醒后三件」之后还带三行（都只读，**都不执行任务**）：
  ⑦ 在办：`ops\inflight.json` 里没收尾的活，最多 3 项（persona_inflight.py 维护）；
  ⑧ 现在能做：auto / needs_human / needs_external / blocked 分类（persona_nextstep.py）；
  ⑨ 已自动开工：只认 `working: true`（`persona_inflight.py start` 写下的事实），不拿 ⑧ 冒充。

用法：
  python persona_wake.py add --in 90 "检查抓取结果" [--every 3600] [--agent hermes-v4]
  python persona_wake.py add --at 18:30 "跑一次索引"
  python persona_wake.py list
  python persona_wake.py rm <id>
  python persona_wake.py fire            # 手动触发一次（守护进程每 30 秒自动跑）
  python persona_wake.py brief [--k 8]   # 只打印「醒来该先看的那段」
  python persona_wake.py inject [--dry-run]   # 把简讯打进壳的输入框（**已打通**，2026-11-10）

存储：ops/wake.json（闹钟表）；ops/wake-fires.jsonl（触发记录，只追加）；
      ops/wake-brief.json + ops/wake-brief.jsonl（唤醒简讯，最新一份 + 历史）。
"""
import argparse
import io
import json
import os
import subprocess

try:
    import persona_win as _pw  # 无窗口：GUI 进程里 spawn 控制台程序不弹黑窗
except Exception:
    pass

import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
REG = os.path.join(OPS, "wake.json")
FIRES = os.path.join(OPS, "wake-fires.jsonl")
EVENTS = os.path.join(OPS, "wake-events.jsonl")
BRIEF = os.path.join(OPS, "wake-brief.json")
BRIEF_HIST = os.path.join(OPS, "wake-brief.jsonl")


def load():
    try:
        with io.open(REG, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(items):
    os.makedirs(OPS, exist_ok=True)
    with io.open(REG, "w", encoding="utf-8", newline="\n") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def parse_at(s):
    """'18:30' -> 今天的那个时刻；已过则顺延到明天。"""
    hh, mm = s.split(":")
    now = time.localtime()
    t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, int(hh), int(mm), 0, 0, 0, -1))
    if t <= time.time():
        t += 86400
    return t


def cmd_add(a):
    if not (a.purpose or "").strip():
        print("拒绝登记：必须写「醒来做什么」(purpose)，没有 purpose 的闹钟是空转")
        return 2
    now = time.time()
    due = parse_at(a.at) if a.at else now + (a.in_ or 60)
    wid = "w%d" % (int(now * 1000) % 100000000)
    rec = {"id": wid, "due": due, "every": a.every or 0, "purpose": a.purpose.strip(),
           "agent": a.agent or "", "run": a.run or "", "enabled": True,
           "created": time.strftime("%Y-%m-%d %H:%M:%S"), "last_run": 0}
    items = load()
    items.append(rec)
    save(items)
    print("已登记 %s：%s（%s）" % (wid, rec["purpose"],
                                time.strftime("%m-%d %H:%M", time.localtime(due))))
    return 0


def event(agent, purpose, delay=5, source="dispatch"):
    """事件唤醒（B74）：不是「到点了」，是「有事了」。

    由派发口直接调用——任务池里出现了一条给 A 的活，就在 `delay` 秒后给 A 排一个闹钟，
    不再等它下一次自己想起来。守护进程的 wake 任务本来 30 秒扫一次，到点就摘简讯。

    为什么仍算「心跳唤醒」：决定什么时候醒的还是**守护进程**，只是触发条件从"到点"
    改成"有事件"。三类唤醒的界线（谁决定）没有变，所以不多立第四类。
    """
    now = time.time()
    purpose = (purpose or "").strip() or "事件唤醒"
    agent = agent or ""
    items = load()
    # 同一个分身已经有没到点的事件闹钟 → 合并，不堆一串（一堆同一秒的闹钟只会连着吵）
    for r in items:
        if (r.get("enabled", True) and r.get("source") == "event"
                and r.get("agent", "") == agent and r.get("due", 0) > now):
            r["purpose"] = purpose
            r["due"] = now + max(0, delay)
            r["events"] = int(r.get("events", 1)) + 1
            save(items)
            _log_event({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "agent": agent,
                        "purpose": purpose, "merged_into": r["id"], "source": source})
            print("已合并到 %s：%s（%d 秒后）" % (r["id"], purpose, delay))
            return r["id"], False
    wid = "e%d" % (int(now * 1000) % 100000000)
    rec = {"id": wid, "due": now + max(0, delay), "every": 0, "purpose": purpose,
           "agent": agent, "run": "", "enabled": True, "source": "event", "events": 1,
           "created": time.strftime("%Y-%m-%d %H:%M:%S"), "last_run": 0}
    items.append(rec)
    save(items)
    _log_event({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "agent": agent,
                "purpose": purpose, "id": wid, "source": source,
                "due_in": max(0, delay)})
    print("已登记事件唤醒 %s：%s（%d 秒后）" % (wid, purpose, delay))
    return wid, True


def _log_event(rec):
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(EVENTS, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def cmd_event(a):
    event(a.agent, a.purpose, a.delay, a.source)
    return 0


def cmd_list(_a):
    items = load()
    if not items:
        print("（无闹钟）")
        return 0
    for r in items:
        print("%-9s %s  %-6s %-14s %s" % (
            r["id"], time.strftime("%m-%d %H:%M", time.localtime(r["due"])),
            "每%ds" % r["every"] if r["every"] else "一次",
            ("→ " + r["agent"]) if r.get("agent") else "",
            ("（已停用）" if not r.get("enabled", True) else "") + r["purpose"]))
    return 0


def cmd_rm(a):
    items = [r for r in load() if r["id"] != a.id]
    save(items)
    print("已删 %s" % a.id)
    return 0


def index_brief(k=8):
    """唤醒简讯的索引段：所有来源里最近的 k 条 + 派发中的任务。"""
    try:
        import persona_master
        snap = persona_master.snapshot(k)
        return {"index_total": snap["total"], "recent": snap["sources"],
                "pending": snap["pending"]}
    except SystemExit:
        return {"index_total": 0, "recent": [], "pending": [], "note": "没有总索引"}
    except Exception as e:
        return {"index_total": 0, "recent": [], "pending": [], "note": "索引读不出来：%s" % str(e)[:60]}


def write_brief(rec, k=8):
    """写一份唤醒简讯：醒来第一眼看的就是它。"""
    body = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wake_id": rec.get("id", ""), "purpose": rec.get("purpose", ""),
            "agent": rec.get("agent", ""), "source": rec.get("source", "alarm")}
    body.update(index_brief(k))
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(BRIEF, "w", encoding="utf-8", newline="\n") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
        with io.open(BRIEF_HIST, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return body


REREAD = os.path.join(OPS, "reread.json")
REREAD_LOG = os.path.join(OPS, "reread.jsonl")
REREAD_EVERY = 300          # 重读提醒的节流：5 分钟（心跳扫描仍是 30 秒，事件唤醒要快）
REREAD_MAX_AGE = 1800       # 距上次重读超过 30 分钟就再提醒一次

SETTINGS = ["memory\\设定\\00-索引.md", "memory\\设定\\10-人格与红线.md",
            "memory\\设定\\20-交流与要求.md", "memory\\设定\\30-流程与运维.md",
            "memory\\设定\\40-事实与现状.md"]


def _sha(path):
    try:
        import hashlib
        return hashlib.sha256(io.open(path, "rb").read()).hexdigest()[:16]
    except OSError:
        return ""


def reread_state():
    try:
        return json.load(io.open(REREAD, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def reread_save(st):
    try:
        os.makedirs(OPS, exist_ok=True)
        tmp = REREAD + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, REREAD)
    except OSError:
        pass


def reread_need():
    """该不该重读设定。返回 (bool, 原因)。

    三条触发（2026-11-10 定）：
      ① 设定文件内容变了（hash 不同）——改了规则却还按旧的办事，最危险；
      ② 距上次重读超过 30 分钟——防止长会话里慢慢漂；
      ③ 刚压缩过——压缩把上下文换成摘要，摘要里没有的那部分设定，只能回文件读。
    """
    st = reread_state()
    if st.get("need"):
        return True, st.get("reason") or "被标记"
    for rel in SETTINGS:
        p = os.path.join(ROOT, rel)
        h = _sha(p)
        if h and h != (st.get("files") or {}).get(rel):
            return True, "设定有变：%s" % os.path.basename(rel)
    last = float(st.get("last_read") or 0)
    if last and time.time() - last > REREAD_MAX_AGE:
        return True, "距上次重读 %d 分钟" % int((time.time() - last) / 60)
    if not last:
        return True, "从未重读过"
    return False, ""


def mark_reread(reason):
    """外部喊一声「待会儿记得重读」——压缩之后由 persona_compact 调用。"""
    st = reread_state()
    st["need"] = True
    st["reason"] = reason
    st["marked"] = time.strftime("%Y-%m-%d %H:%M:%S")
    reread_save(st)


def cmd_reread(a):
    """--auto：闹钟每 5 分钟调；只**标记**，不刷屏（真正读文件的是 AI）。
       --check：exit 0 = 不用读，exit 1 = 该读了（把原因打出来）
       --ack  ：AI 真读完之后调用，清标记并记下文件 hash
    """
    if a.ack:
        st = reread_state()
        st["need"] = False
        st["reason"] = ""
        st["last_read"] = time.time()
        st["last_read_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["files"] = {rel: _sha(os.path.join(ROOT, rel)) for rel in SETTINGS}
        reread_save(st)
        try:
            with io.open(REREAD_LOG, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"at": st["last_read_at"], "action": "read",
                                    "files": len(st["files"])}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        print("已记下重读：%d 份设定（%s）" % (len(st["files"]), st["last_read_at"]))
        return 0
    need, why = reread_need()
    if a.check:
        print(("该重读了：%s" % why) if need else "不用重读（设定没变、且在 30 分钟内）")
        return 1 if need else 0
    if a.auto:
        st = reread_state()
        last_mark = float(st.get("marked_ts") or 0)
        if need and time.time() - last_mark >= REREAD_EVERY:
            mark_reread(why)
            st = reread_state()
            st["marked_ts"] = time.time()
            reread_save(st)
            try:
                with io.open(REREAD_LOG, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "action": "mark", "why": why},
                                       ensure_ascii=False) + "\n")
            except OSError:
                pass
            print("已标记重读：%s" % why)
        else:
            print("跳过（%s）" % (why or "不需要"))
        return 0
    print("用法：reread --auto | --check | --ack")
    return 0


def write_brief(rec, k=8):
    """写一份唤醒简讯：醒来第一眼看的就是它。"""
    body = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wake_id": rec.get("id", ""), "purpose": rec.get("purpose", ""),
            "agent": rec.get("agent", ""), "source": rec.get("source", "alarm")}
    need, why = reread_need()
    body["reread_need"] = need
    body["reread_why"] = why
    body.update(index_brief(k))
    try:                       # 简讯文件也留一份当时的 ⑦⑧⑨⑩⑪ 快照（命令行 brief 会现算，不用它）
        body["inflight"] = [_inflight_line()] + _nextstep_lines()
        body["autoloop"] = _autoloop_lines()
    except Exception:
        pass
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(BRIEF, "w", encoding="utf-8", newline="\n") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
        with io.open(BRIEF_HIST, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return body


def _pack_line():
    """交接包那一行：醒后也该重读，但得让人一眼看出它新不新（过期了读它等于读旧世界）。"""
    p = os.path.join(ROOT, "续接", "接续包-最新.md")
    try:
        age = (time.time() - os.path.getmtime(p)) / 60.0
    except OSError:
        return "④ 交接包：还没生成（先跑 persona_gauge.py --prepare --canonical --no-clip）"
    if age > 60:
        return "④ 交接包：续接\\接续包-最新.md（**已过期 %d 分钟**，先刷再读）" % int(age)
    return "④ 交接包：续接\\接续包-最新.md（%d 分钟前生成，可读）" % int(age)


def _inflight_line(limit=3):
    """⑦ 在办：台账里还没收尾的活。只读，不执行；最多 3 项——简讯要短。

    这是"醒来看得见还有活"，不是任务清单全文：全文在 `python persona_inflight.py list`。
    """
    try:
        sys.path.insert(0, ROOT)
        import persona_inflight as IF
        items = IF.open_items()
    except Exception:
        items = []

    def one(it):
        st = it.get("status") or "running"
        what = (it.get("what") or it.get("id") or "").strip()
        step = (it.get("step") or "").strip()
        if st in ("blocked", "waiting") and step:
            desc = "%s：%s" % (st, step)
        elif step:
            desc = "停在：%s" % step
        else:
            desc = ""
        s = ("%s（%s）" % (what, desc)) if desc else what
        return s if len(s) <= 44 else s[:43] + "…"

    if not items:
        return "⑦ 在办：无（都收尾了）"
    shown = [one(x) for x in items[:limit]]
    line = "⑦ 在办：" + "｜".join(shown)
    if len(items) > len(shown):
        line += "（…另 %d 项）" % (len(items) - len(shown))
    return line + "  共 %d 项" % len(items)


def _nextstep_lines():
    """⑧ 现在能做 / ⑨ 已自动开工：只读判定，**绝不在简讯里执行任务**。"""
    try:
        sys.path.insert(0, ROOT)
        import persona_nextstep as NS
        return NS.wake_lines()
    except Exception as e:
        return ["⑧ 现在能做：（判不出来：%s）" % str(e)[:40]]


def _autonomy_line():
    """⑨·5 自主档位：只读（模型 / 免费·收费档 / 本小时与今日用量），不执行。"""
    try:
        sys.path.insert(0, ROOT)
        import persona_autonomy as AU
        p = AU.profile(root=ROOT)
        return "⑨·5 自主档位：%s（%s｜%s）· 本小时 %d/%d 次 · 今日 %d/%d 次" % (
            p.get("tier"), p.get("tier_label"), p.get("tier_reason") or "—",
            p.get("used_hour", 0), p.get("runs_per_hour", 0),
            p.get("used_day", 0), p.get("runs_per_day", 0))
    except Exception as e:
        return "⑨·5 自主档位：（判不出来：%s）" % str(e)[:40]


def _autoloop_lines():
    """⑨·5 自主档位 / ⑩ 我留给自己的 / ⑪ 现在要做的：只读判定，**绝不在简讯里执行**。"""
    lines = [_autonomy_line()]
    root = ROOT
    try:
        sys.path.insert(0, root)
        import persona_autoloop as AL
        return lines + AL.wake_lines(root=root)
    except Exception as e:
        return lines + ["⑩ 我留给自己的：（判不出来：%s）" % str(e)[:40],
                        "⑪ 现在要做的：—"]


def _fallback_line():
    """⛑ 保底：欠费/断网时唤醒自动降级。判定在 persona_fallback，这里只取一行文字。"""
    try:
        sys.path.insert(0, ROOT)
        import persona_fallback as FB
        return FB.line(root=ROOT)
    except Exception as e:                     # noqa: BLE001（判定失败不挡唤醒）
        return "⛑ 保底：（判不出来：%s）" % str(e)[:40]


def _dr_line():
    """🛟 容灾保底：显式开关状态 + 看门狗上一拍判定（只读，不动手）。"""
    try:
        import io as _io
        import json as _json
        sw = {}
        p = os.path.join(ROOT, "ops", "dr-switch.json")
        if os.path.exists(p):
            with _io.open(p, encoding="utf-8-sig") as f:
                sw = _json.load(f) or {}
        on = bool(sw.get("on"))
        last = {}
        q = os.path.join(ROOT, "ops", "watchdog.jsonl")
        if os.path.exists(q):
            with _io.open(q, encoding="utf-8", errors="replace") as f:
                tail = f.read().splitlines()[-1:]
            if tail:
                last = _json.loads(tail[0])
        hold = os.path.exists(os.path.join(ROOT, "ops", "dr-hold"))
        return "🛟 容灾：%s%s ｜ 上一拍 %s（%s）" % (
            "**开**（自动动手）" if on else "关（只判定）",
            " · 蹲住闸" if hold else "",
            last.get("action") or "还没跑过", last.get("why") or "")
    except Exception as e:                     # noqa: BLE001
        return "🛟 容灾：（判不出来：%s）" % str(e)[:40]


def _drcheck_line():
    """🩺 容灾自检：读后台落下的 ops\\dr-preflight.json（谁跑谁写，这里只读）。"""
    try:
        import io as _io
        import json as _json
        p = os.path.join(ROOT, "ops", "dr-preflight.json")
        if not os.path.exists(p):
            return "🩺 容灾自检：还没跑过（python persona_watchdog.py preflight）"
        with _io.open(p, encoding="utf-8-sig") as f:
            d = _json.load(f) or {}
        sess = d.get("session") or {}
        tk = d.get("tasks") or {}
        total = int(tk.get("total") or 0)
        miss = len(tk.get("missing") or [])
        vers = "、".join("%s%s" % (v.get("name"), "正常" if v.get("ok") else "缺失")
                         for v in (d.get("versions") or []))
        return ("🩺 容灾自检（%s）：版本 %s ｜ 本会话 %s/%s → 备胎「%s」｜ 任务 %d/%d%s"
                % (str(d.get("at") or "")[11:16], vers,
                   sess.get("provider") or "?", sess.get("model") or "?",
                   sess.get("next") or "?", total - miss, total,
                   ("（缺 %d）" % miss) if miss else ""))
    except Exception as e:                     # noqa: BLE001
        return "🩺 容灾自检：（判不出来：%s）" % str(e)[:40]


def render_brief(b):
    head = "唤醒简讯 %s（%s）" % (b.get("at", ""), b.get("purpose", ""))
    lines = [head]
    if b.get("reread_need"):
        lines.append("⚠ 需要重读设定：%s（读完跑 persona_wake.py reread --ack）"
                     % (b.get("reread_why") or "被标记"))
    else:
        lines.append("设定已重读（30 分钟内、内容没变）")
    lines.append(_fallback_line())
    lines.append(_dr_line())
    lines.append(_drcheck_line())
    lines += ["",
              "醒后先做三件（顺序不许换）：",
              "  ① 读设定（按序）：" + " → ".join(s.split("\\")[-1] for s in SETTINGS),
              "     路径：memory\\设定\\",
              "  ② 读总索引：python persona_master.py --k 10",
              "  ③ 认领检查：python persona_claim.py check \"关键词\" --has text",
              "  " + _pack_line(),
              ""]
    # ⑦⑧⑨⑩⑪：重置/醒来先知道「还有哪些活、停在哪、当下哪件能先动、我给自己留了什么」。
    # 只读台账与判定，**不执行任何任务**；心跳周期（30 秒）与此无关，一个字没动。
    lines += [_inflight_line()]
    lines += _nextstep_lines()
    lines += _autoloop_lines()
    lines += ["",
              "总索引 %d 条来源，最近：" % b.get("index_total", 0)]
    for s in b.get("recent") or []:
        lines.append("  %s  %-6s %4d条  %s" % (s["time"], s["source"], s["msgs"], s["head"]))
    if b.get("pending"):
        lines.append("派发中的任务：")
        for r in b["pending"][-5:]:
            lines.append("  %s → %-14s %s" % (r.get("at", ""), r.get("agent", ""), r.get("task", "")))
    if b.get("note"):
        lines.append("（%s）" % b["note"])
    return "\n".join(lines)


def cmd_brief(a):
    if os.path.exists(BRIEF) and not a.fresh:
        try:
            b = json.load(io.open(BRIEF, encoding="utf-8-sig"))
        except (OSError, ValueError):
            b = {}
    else:
        b = write_brief({"id": "", "purpose": "手动查看", "agent": a.agent}, a.k)
    print(render_brief(b))
    return 0


def fire_due(limit=2, k=8):
    """守护进程每 30 秒调一次：到点就摘简讯、执行 purpose、写触发记录、推进下次时间。"""
    items = load()
    now = time.time()
    fired = 0
    for r in items:
        if not r.get("enabled", True) or r["due"] > now:
            continue
        brief = write_brief(r, k)
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "id": r["id"],
               "purpose": r["purpose"], "agent": r.get("agent", ""), "run": r.get("run", ""),
               "index_total": brief.get("index_total", 0),
               "brief": os.path.relpath(BRIEF, ROOT).replace("\\", "/")}
        run_cmd = (r.get("run") or "").strip()
        if run_cmd:
            env = dict(os.environ, PERSONA_PURPOSE=r["purpose"], PERSONA_WAKE_ID=r["id"],
                       PERSONA_BRIEF=BRIEF, PERSONA_ROOT=ROOT)
            job = (r.get("job") or "").strip()
            run_job = job or r["id"]
            try:
                import persona_sched as _sched
                # 闹钟默认优先级 1（最高）；登记的 job 以表里的数为准。
                prio = (_sched.priority(job, "alarm", root=ROOT, default=1)
                        if job else 1)
                rr = _sched.run_preemptible(run_job, prio, run_cmd, timeout=120,
                                            owner="alarm", root=ROOT, env=env)
                rec["rc"] = rr.get("rc")
                rec["out"] = (rr.get("out") or "")[-400:]
                if rr.get("preempted_by"):
                    rec["preempted_by"] = rr["preempted_by"]
            except Exception as exc:
                # 仲裁模块不可用时退回直接执行：闹钟不能因为仲裁挂掉。
                try:
                    pr = subprocess.run(run_cmd, shell=True, capture_output=True, text=True,
                                        timeout=120, env=env, cwd=ROOT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    rec["rc"] = pr.returncode
                    rec["out"] = ((pr.stdout or "") + (pr.stderr or ""))[-400:]
                except Exception as exc2:
                    rec["rc"] = -1
                    rec["out"] = str(exc2)[:200]
        try:
            os.makedirs(OPS, exist_ok=True)
            with io.open(FIRES, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if r.get("agent"):
            _auto_report(r, brief, now)
        r["last_run"] = now
        if r.get("every"):
            while r["due"] <= now:
                r["due"] += r["every"]
        else:
            r["enabled"] = False
        # 优先级仲裁：跑成功（rc==0）才占位，失败不 claim（失败不该挡住另一边）。
        # ttl 取这条闹钟的周期；next_due 是推进后的下一次 due，供 status / 让位说明里显示。
        job = (r.get("job") or "").strip()
        if job and run_cmd and rec.get("rc") == 0:
            try:
                import persona_sched as _sched
                _sched.claim(job, "alarm", ttl=int(r.get("every") or 300),
                             next_due=(r.get("due") if r.get("every") else None),
                             note=r.get("purpose", "")[:80], root=ROOT)
            except Exception:
                pass
        fired += 1
        if fired >= limit:
            break
    if fired:
        save(items)
    return fired


def _auto_report(r, brief, now):
    """替 agent 报到：醒来这件事本身就是一次心跳，且此时总索引已读过。"""
    try:
        import persona_report as _PR
        d = _PR.load()
        rec = d.get(r["agent"]) or {"first": now}
        rec.update({"agent": r["agent"], "last": now, "status": "woken",
                    "note": "闹钟唤醒：%s（简讯含 %d 条来源）" % (r["purpose"],
                                                          brief.get("index_total", 0)),
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"), "read_index": True})
        rec["count"] = rec.get("count", 0) + 1
        d[r["agent"]] = rec
        _PR.save(d)
    except Exception:
        pass


def cmd_inject(a):
    """日常唤醒：把「今天该干什么」拼成一句话，直接打进壳的输入框。

    来源（都在本地，不联网）：
      - 总索引简讯 `ops/wake-brief.json`（最近写了哪些来源）；
      - `BACKLOG.md` 里还开着的 P1 条目（前 N 条）。
    防打断：交给 `persona_inject.py`，他最近在打字就先等。
    """
    text = (a.text or "").strip()
    if not text:
        b = index_brief(a.k)
        lines = ["【唤醒简讯 %s】" % time.strftime("%m-%d %H:%M")]
        try:
            lines.append("最近写入：%d 个来源，最新 %s" %
                         (b.get("index_total", 0),
                          (b.get("index") or [{}])[0].get("date", "?")))
        except Exception:
            pass
        todo = _open_p1(limit=a.k)
        if todo:
            lines.append("还开着的 P1：")
            lines += ["  " + t for t in todo]
        lines.append("读 续接\\接续包-最新.md 后，回一句当前任务。")
        text = "\n".join(lines)
    try:
        import persona_inject as pi
    except Exception as e:
        print("FAIL 导入 persona_inject 失败：%s" % e)
        return 1
    ok, msg = pi.inject(text, dry_run=a.dry_run, wait_idle=a.wait_idle)
    pi.log({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "len": len(text),
            "dry": bool(a.dry_run), "ok": ok, "msg": msg, "src": "wake-daily"})
    print(("OK   " if ok else "FAIL ") + msg)
    return 0 if ok else 1


def _open_p1(limit=6):
    """从 BACKLOG.md 抽「还开着的 P1」——只读，不改。"""
    p = os.path.join(ROOT, "BACKLOG.md")
    if not os.path.exists(p):
        return []
    out = []
    for line in io.open(p, encoding="utf-8", errors="replace"):
        if not line.startswith("| B") or "【P1" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or "✅" in cells[3]:
            continue
        title = cells[1].replace("**", "")
        out.append("%s %s" % (cells[0], title[:60]))
        if len(out) >= limit:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("add")
    c.add_argument("purpose")
    c.add_argument("--in", dest="in_", type=int, default=0, help="多少秒后（默认 60）")
    c.add_argument("--at", default="", help="HH:MM，晚于当前则今天，否则明天")
    c.add_argument("--every", type=int, default=0, help="重复周期（秒）")
    c.add_argument("--agent", default="")
    c.add_argument("--run", default="", dest="run", help="触发时要执行的命令（可选）")
    sub.add_parser("list")
    c = sub.add_parser("rm"); c.add_argument("id")
    sub.add_parser("fire")
    c = sub.add_parser("brief")
    c.add_argument("--k", type=int, default=8)
    c.add_argument("--agent", default="")
    c.add_argument("--fresh", action="store_true", help="忽略已有简讯，现摘一份")
    c = sub.add_parser("inject", help="把「唤醒简讯」直接打进壳的输入框（日常唤醒）")
    c.add_argument("--k", type=int, default=6)
    c.add_argument("--text", default="", help="自定义正文；不给就用总索引 + 待办拼一份")
    c.add_argument("--wait-idle", type=int, default=5, help="他最近 N 秒在打字就先等（默认 5）")
    c.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("event", help="事件唤醒：有事了才叫（派发口自动调用）")
    c.add_argument("--agent", default="")
    c.add_argument("--purpose", required=True, help="醒来做什么（必填，同闹钟规矩）")
    c.add_argument("--delay", type=int, default=5, help="多少秒后醒（默认 5）")
    c.add_argument("--source", default="dispatch", help="谁触发的（dispatch/task/dep）")
    c = sub.add_parser("reread", help="重读设定：--auto（闹钟每 5 分钟）/ --check / --ack")
    c.add_argument("--auto", action="store_true", help="需要就标记，节流 5 分钟")
    c.add_argument("--check", action="store_true", help="exit 1 = 该重读了")
    c.add_argument("--ack", action="store_true", help="真读完之后调用，清标记")
    a = ap.parse_args()
    fn = {"add": cmd_add, "list": cmd_list, "rm": cmd_rm, "brief": cmd_brief,
          "reread": cmd_reread,
          "inject": cmd_inject, "event": cmd_event,
          "fire": lambda _a: print("触发 %d 个" % fire_due())}.get(a.cmd)
    if not fn:
        ap.print_help()
        return 0
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
