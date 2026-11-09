# -*- coding: utf-8 -*-
r"""persona_watch.py — Hermes 会话实时导出守护

问题：Hermes 把对话存在它自己的 state.db 里，keeper 每 30 分钟才导一次；
      中间那 30 分钟，换窗口/断线就会丢一段记忆。

两种做法里选了"实时导出"而不是"把目录 junction 到库里"：
  - state.db 是带 WAL 的 SQLite，正在被写入时被同步软件/网盘抓走，
    轻则拿到半截快照，重则冲突文件——数据库不能进同步范围。
  - 导出只碰纯文本（sessions\...\hermes-*.md），同会话幂等覆盖，随时可回滚。

机制：轮询 state.db 的 mtime/size，连续 3 秒不变（=这一轮写完了）就导出一次；
      导出后再增量索引一次，保证搜得到。

用法：
  pythonw persona_watch.py            # 常驻（无窗口）
  python  persona_watch.py --once     # 只跑一次，用于测试
  python  persona_watch.py --verbose  # 前台看日志
"""
import argparse, os, subprocess, sys, time

ROOT = r"D:\4590-PC-文件\AI会话检索记录"
DB = r"D:\ProgramData\Hermes Agent CN Desktop\data\hermes-home\state.db"
PY = sys.executable
POLL = 3          # 轮询间隔（秒）
QUIET = 3         # 连续多久没变化才认为写完了


def stamp():
    return time.strftime("%m-%d %H:%M:%S")


def sig():
    try:
        st = os.stat(DB)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def run(cmd, timeout, quiet=True):
    try:
        subprocess.run(cmd, cwd=ROOT, timeout=timeout,
                       stdout=subprocess.DEVNULL if quiet else None,
                       stderr=subprocess.DEVNULL if quiet else None)
        return True
    except Exception as e:
        print("%s  !! %s 失败: %s" % (stamp(), os.path.basename(cmd[1]), e), flush=True)
        return False


def once(verbose=True):
    if verbose:
        print("%s  导出…" % stamp(), flush=True)
    ok = run([PY, os.path.join(ROOT, "persona_bridge.py"), "--days", "1"], 180, quiet=not verbose)
    if ok:
        run([PY, os.path.join(ROOT, "persona_index.py")], 300, quiet=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.once:
        once(verbose=True)
        return

    if not os.path.exists(DB):
        raise SystemExit("找不到 state.db: %s" % DB)

    print("%s  守护启动：%s" % (stamp(), DB), flush=True)
    last = sig()
    dirty_since = None
    while True:
        time.sleep(POLL)
        cur = sig()
        if cur is None:
            continue
        if cur != last:
            last = cur
            dirty_since = time.time()
            continue
        if dirty_since and time.time() - dirty_since >= QUIET:
            dirty_since = None
            once(verbose=a.verbose)


if __name__ == "__main__":
    main()
