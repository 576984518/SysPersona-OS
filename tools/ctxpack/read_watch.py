# -*- coding: utf-8 -*-
"""read_watch.py — "读会话"没读到新内容时的后台重试

用户按 F5 之后，抓取可能要十几秒才落盘；如果第一次读得太快，就会误判"没有新内容"。
所以默认动作不是回一句"没有新内容"，而是：

  先回「正在读取…」→ 起本脚本后台等 60 秒 → 有变化立刻总结；确实没有才说没有。

用法：
  python read_watch.py                       # 等 60 秒，每 8 秒看一次
  python read_watch.py --seconds 120 --interval 5
  python read_watch.py --since 1794122000    # 只认这个时间点之后落盘的内容

退出码：0 = 有新内容；2 = 超时无变化；1 = 参数/环境问题。
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
    ap.add_argument("--seconds", type=int, default=60, help="最多等多久（秒），默认 60")
    ap.add_argument("--interval", type=int, default=8, help="多久看一次（秒），默认 8")
    ap.add_argument("--since", type=float, default=0.0,
                    help="只认这个 epoch 之后落盘的内容；默认从现在开始算")
    a = ap.parse_args()

    if not os.path.isdir(ARCHIVE):
        print("NO_ARCHIVE %s" % ARCHIVE)
        return 1

    since = a.since or time.time()
    t0 = time.time()
    base_t, base_p = newest_md()
    base_lm, base_n = latest_state()
    print("[watch] 起点 %.0f，最多等 %ds；当前最新归档 %s（消息 %d）"
          % (since, a.seconds, os.path.basename(base_p) or "-", base_n), flush=True)

    while time.time() - t0 < a.seconds:
        time.sleep(min(a.interval, max(1, a.seconds - int(time.time() - t0))))
        t, p = newest_md()
        lm, n = latest_state()
        if t > since and t > base_t:
            print("NEW 归档更新：%s（%.1f 秒后）" % (os.path.basename(p), time.time() - t0), flush=True)
            return 0
        if lm > since and (lm > base_lm or n != base_n):
            print("NEW 页面刷新：LATEST 更新（消息 %d → %d，%.1f 秒后）"
                  % (base_n, n, time.time() - t0), flush=True)
            return 0

    print("TIMEOUT %ds 内没有新内容（归档与 LATEST 都没动）" % a.seconds, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
