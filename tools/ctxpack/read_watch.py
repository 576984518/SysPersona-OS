# -*- coding: utf-8 -*-
r"""read_watch.py — "读会话"的后台重复读取（读到有新内容为止）

用户发 `1` 或说"读会话"时，要**记住他发消息的时间**，然后一直重复读，直到读到新内容——
不是等 60 秒就下结论，也不是只看一次。

  先回「正在读取…」→ 起本脚本后台循环（`--since` = 用户发消息的时间戳）
  → 一有新内容立刻总结并插播；到他再也刷不出新东西为止。

用法：
  python read_watch.py --since 1794125239    # 从用户发消息那一刻算起，读到有为止
  python read_watch.py --max-minutes 60      # 封顶 60 分钟（默认 30）
  python read_watch.py --seconds 120         # 只要等 120 秒的老用法

退出码：0 = 有新内容；2 = 到顶仍无变化；1 = 参数/环境问题。
输出一行结论，方便后台任务直接贴回来。

环境变量：SYSPERSONA_ROOT 指记忆库根（旧名 PERSONA_ROOT 也认），要读
`<根>\archive\` 与 `<根>\LATEST.md`；不设就按脚本所在目录推导，推不出来直接抛错。
"""
import argparse
import io
import json
import os
import re
import sys
import time

from persona_root import root as _persona_root

ROOT = _persona_root()
ARCHIVE = os.path.join(ROOT, "archive")
LATEST = os.path.join(ROOT, "LATEST.md")


def newest_md():
    """归档里最新的 .md 的 (mtime, 路径)。"""
    best_t, best_p = 0.0, ""
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in fns:
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dp, fn)
            try:
                t = os.path.getmtime(p)
            except OSError:
                continue
            if t > best_t:
                best_t, best_p = t, p
    return best_t, best_p


def latest_state():
    """LATEST.md 的 (mtime, 消息数, 状态, 页面更新时间)。

    "页面未渲染"时消息数会变 0——那是空壳，不是新内容，不能当"读到了"。
    """
    try:
        t = os.path.getmtime(LATEST)
        txt = io.open(LATEST, encoding="utf-8", errors="replace").read()
    except OSError:
        return 0.0, -1, "", ""
    m = re.search(r"消息数：(\d+)", txt)
    s = re.search(r"状态：(.+)", txt)
    u = re.search(r"更新时间：(.+)", txt)
    return (t, (int(m.group(1)) if m else -1),
            (s.group(1).strip() if s else ""), (u.group(1).strip() if u else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=0,
                    help="最多等多久（秒）；0 = 一直重复读到有新内容（封顶 --max-minutes）")
    ap.add_argument("--max-minutes", type=int, default=30, help="无限模式的安全上限（分钟），默认 30")
    ap.add_argument("--interval", type=int, default=8, help="多久看一次（秒），默认 8")
    ap.add_argument("--since", type=float, default=0.0,
                    help="用户发消息的时间戳（epoch）；只认这之后落盘的内容")
    a = ap.parse_args()

    if not os.path.isdir(ARCHIVE):
        print("NO_ARCHIVE %s" % ARCHIVE)
        return 1

    since = a.since or time.time()
    t0 = time.time()
    cap = a.seconds if a.seconds else a.max_minutes * 60
    base_t, base_p = newest_md()
    base_lm, base_n, base_st, base_upd = latest_state()

    # 页面重载会把消息数打到 0 再回到原值（402 → 0 → 402），那不是新内容。
    # 记住见过的最大条数，只有"严格变多"才算新的。
    state_path = os.path.join(ROOT, ".read_watch_state.json")
    seen = {}
    try:
        seen = json.load(io.open(state_path, encoding="utf-8"))
    except Exception:
        pass
    last_max = max(base_n, int(seen.get("max", 0)))

    print("[watch] 起点 %.0f，%s；页面更新于 %s；最新归档 %s（消息 %d，历史最大 %d）"
          % (since, ("最多等 %ds" % a.seconds) if a.seconds else ("读到为止，封顶 %d 分钟" % a.max_minutes),
             base_upd or "-", os.path.basename(base_p) or "-", base_n, last_max), flush=True)

    while time.time() - t0 < cap:
        time.sleep(min(a.interval, max(1, cap - int(time.time() - t0))))
        t, p = newest_md()
        lm, n, st, upd = latest_state()
        if t > since:
            print("NEW 归档更新：%s（%.0f 秒后）" % (os.path.basename(p), time.time() - t0), flush=True)
            return 0
        if n > last_max and "未渲染" not in st:
            try:
                json.dump({"max": n, "at": time.time()}, io.open(state_path, "w", encoding="utf-8"))
            except Exception:
                pass
            print("NEW 页面更新：消息 %d → %d（%.0f 秒后）" % (last_max, n, time.time() - t0), flush=True)
            return 0

    print("TIMEOUT %.0f 秒内没有新内容（归档与 LATEST 都没动）" % (time.time() - t0), flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
