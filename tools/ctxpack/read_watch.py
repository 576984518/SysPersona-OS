# -*- coding: utf-8 -*-
"""read_watch.py — "读会话"的后台重复读取（读到有新内容为止）

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
"""
import argparse
import io
import os
import re
import sys
import time

ROOT = os.environ.get("SYSPERSONA_ROOT") or r"D:\4590-PC-文件\AI会话检索记录"
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
    """LATEST.md 的 (mtime, 消息数)。消息数是判"正文有没有变"的现成口径。"""
    try:
        t = os.path.getmtime(LATEST)
        txt = io.open(LATEST, encoding="utf-8", errors="replace").read()
    except OSError:
        return 0.0, -1
    m = re.search(r"消息数：(\d+)", txt)
    return t, int(m.group(1)) if m else -1


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
    base_lm, base_n = latest_state()
    print("[watch] 起点 %.0f，%s；当前最新归档 %s（消息 %d）"
          % (since, ("最多等 %ds" % a.seconds) if a.seconds else ("读到为止，封顶 %d 分钟" % a.max_minutes),
             os.path.basename(base_p) or "-", base_n), flush=True)

    while time.time() - t0 < cap:
        time.sleep(min(a.interval, max(1, cap - int(time.time() - t0))))
        t, p = newest_md()
        lm, n = latest_state()
        if t > since:
            print("NEW 归档更新：%s（%.0f 秒后）" % (os.path.basename(p), time.time() - t0), flush=True)
            return 0
        # LATEST.md 每分钟都会被重写一次（那是心跳，不是新内容）；只有消息数变了才算新的。
        if n != base_n:
            print("NEW 页面更新：消息 %d → %d（%.0f 秒后）" % (base_n, n, time.time() - t0), flush=True)
            return 0

    print("TIMEOUT %.0f 秒内没有新内容（归档与 LATEST 都没动）" % (time.time() - t0), flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
