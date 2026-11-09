# -*- coding: utf-8 -*-
r"""persona_dispatch.py — 派发生命周期（v5.3）

一条派发的生命周期：

    pending ──接手──> done
        └────超时 ttl ──> expired ──有别的分身醒着──> 新 pending（改派）

存储 `ops/dispatch.jsonl`，**只追加**：状态变化不回头改旧行，而是追加一条同 id 的更新记录，
读的时候按 id 取最后一条为准。理由：派发是「谁在什么时候把什么交给了谁」，
这条链本身是证据，覆盖写就查不到中途变过什么。

id：新记录自带；老记录没有 id 的，用 (at|agent|task) 的哈希补一个稳定 id，
保证同一行永远算同一条，过期与改派才能指得准。

用法（一般由 persona_report.py 命令行走）：
    import persona_dispatch as D
    r = D.create("hermes-v4", "跑一次索引")
    D.update(r["id"], "done", "验收通过")
    D.reap(ttl=600)
"""
import hashlib
import io
import json
import os
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
STORE = os.path.join(OPS, "dispatch.jsonl")

TTL = 600        # 一条 pending 超过这么久没人接手，就算过期
WINDOW = 600     # 改派只考虑这个窗口内报到过的分身
ALIVE = ("pending", "running")


def _path(path=None):
    return path or STORE


def _key(r):
    """稳定 id：有 id 用 id；老记录按内容哈希补。"""
    rid = (r.get("id") or "").strip()
    if rid:
        return rid
    raw = "%s|%s|%s" % (r.get("at", ""), r.get("agent", ""), r.get("task", ""))
    return "L" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def events(path=None):
    """读原始记录（含更新记录），按写入顺序。"""
    p = _path(path)
    if not os.path.exists(p):
        return []
    out = []
    for line in io.open(p, encoding="utf-8-sig", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue          # 半截写入的行跳过，不让一条坏行毁掉整份台账
        if isinstance(r, dict):
            out.append(r)
    return out


def state(path=None):
    """按 id 合并到「当前状态」：{id: {id, at, agent, task, status, updated, note, redispatch_of}}"""
    st = {}
    for r in events(path):
        rid = _key(r)
        cur = st.get(rid)
        if cur is None:
            st[rid] = {"id": rid, "at": r.get("at", ""), "agent": r.get("agent", ""),
                       "task": r.get("task", ""), "status": r.get("status") or "pending",
                       "updated": r.get("at", ""), "note": r.get("note", ""),
                       "redispatch_of": r.get("redispatch_of", "")}
            continue
        # 更新记录：只覆盖状态类字段，不覆盖原始内容
        for k in ("status", "note", "updated", "agent", "task"):
            if k in r and r[k]:
                cur[k] = r[k]
        cur["updated"] = r.get("at", cur.get("updated", ""))
    return st


def pending(path=None):
    """还没接手的（pending / running），按派发时间正序。"""
    rows = [r for r in state(path).values() if (r.get("status") or "") in ALIVE]
    rows.sort(key=lambda r: r.get("at", ""))
    return rows


def append(rec, path=None):
    p = _path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def create(agent, task, redispatch_of="", path=None):
    now = time.time()
    rec = {"id": "d%d" % (int(now * 1000) % 100000000),
           "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
           "agent": agent, "task": task, "status": "pending"}
    if redispatch_of:
        rec["redispatch_of"] = redispatch_of
    append(rec, path)
    return rec


def update(rid, status, note="", path=None):
    rec = {"id": rid, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "status": status}
    if note:
        rec["note"] = note
    append(rec, path)
    return rec


def _age(rec, now):
    """按 'YYYY-mm-dd HH:MM:SS' 算秒数；算不出来就按「很老」处理。"""
    try:
        return now - time.mktime(time.strptime(rec.get("at", ""), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        return 1e9


def _alive_agents(window, exclude, busy, now):
    """能接活的：最近 window 内报到过、不是原收件人、手上没有没做完的活。"""
    try:
        import persona_report
        reps = persona_report.load()
    except Exception:
        return []
    out = []
    for name, v in reps.items():
        if name == exclude or name in busy:
            continue
        last = v.get("last") or 0
        if now - last > window:
            continue
        out.append({"agent": name, "status": v.get("status", ""), "last": last})
    # 空闲的优先，其次谁报得晚谁先来
    out.sort(key=lambda x: (x["status"] != "idle", -x["last"]))
    return out


def reap(ttl=TTL, window=WINDOW, now=None, path=None):
    """把超时的 pending 标过期；有醒着的分身就改派一条新的。

    返回 {"expired": [...], "redispatched": [...]}，每条都带 id / agent / task / 说明。
    """
    now = time.time() if now is None else now
    st = state(path)
    live = [r for r in st.values() if (r.get("status") or "") in ALIVE]
    busy = {r["agent"] for r in live}
    expired, redispatched = [], []
    for r in sorted(live, key=lambda x: x.get("at", "")):
        age = _age(r, now)
        if age <= ttl:
            continue
        rid = r["id"]
        cands = _alive_agents(window, r.get("agent", ""), busy, now)
        if cands:
            nxt = cands[0]["agent"]
            update(rid, "expired", "超时 %ds 未接手，改派给 %s" % (int(age), nxt), path)
            new = create(nxt, r.get("task", ""), redispatch_of=rid, path=path)
            busy.add(nxt)
            expired.append({"id": rid, "agent": r.get("agent", ""), "task": r.get("task", ""),
                            "age_s": int(age), "next": new["id"], "to": nxt})
            redispatched.append({"id": new["id"], "agent": nxt, "task": r.get("task", ""),
                                 "from": rid})
        else:
            update(rid, "expired", "超时 %ds 未接手，无人可改派" % int(age), path)
            expired.append({"id": rid, "agent": r.get("agent", ""), "task": r.get("task", ""),
                            "age_s": int(age), "next": "", "to": ""})
    return {"expired": expired, "redispatched": redispatched}
