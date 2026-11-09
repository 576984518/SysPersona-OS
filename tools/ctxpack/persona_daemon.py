# -*- coding: utf-8 -*-
r"""persona_daemon.py — 本地常驻守护进程（抓取 / 索引 / 归档 / 保活 / 心跳 / 上下文）

只做这一件事：按各自的节奏把任务跑起来，并把"现在什么状态"写成一份 JSON。

设计要点
--------
* 单进程、单实例（lock 文件），退出前写 last exit。
* 主循环只负责调度与心跳；慢任务（索引、抓取）丢到工作线程，不阻塞心跳。
* 每个任务有超时上限，超时就记为 timeout，不拖死整个进程。
* 抓取不自动跑：等你按了 F5、说"1"，由外部 touch 一个 request 文件触发，
  避免它每隔几分钟就 reload 你正在看的页面。

用法
----
    python persona_daemon.py --daemon        # 常驻
    python persona_daemon.py --once export  # 只跑一个任务
    python persona_daemon.py --status       # 看状态（读 status.json）

任务与默认节奏（可在 ops\persona.conf.json 覆盖）
    heartbeat  60s   写心跳/状态
    herdsman   300s  8080 不通就拉起
    export     1800s 导出 Hermes 会话 → 归档
    index      3600s 增量索引 + 一级索引
    ctx        300s  刷新滚动上下文头（--live）
    watermark  900s  算上下文水位，过线写提醒
    capture    on-demand  抓网页会话（request 文件触发）
"""
import argparse, json, os, re, subprocess, sys, threading, time, traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
STATUS = os.path.join(OPS, "persona-status.json")
LOCK = os.path.join(OPS, "persona.lock")
CONF = os.path.join(OPS, "persona.conf.json")
LOG = os.path.join(OPS, "persona.log")
CAPTURE_REQ = os.path.join(OPS, "persona-capture.request")
EXPORT_LOCK = os.path.join(OPS, "export.lock")
BACKUP2 = os.environ.get("PERSONA_BACKUP2") or r"C:\SysPersona-Backup"

PY = sys.executable
NODE = os.environ.get("PERSONA_NODE") or r"C:\Users\Huang\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
CAPTURE_JS = os.environ.get("PERSONA_CAPTURE") or r"D:\文档\deepseek-harness\default-workspace\cdp\collect7.js"

# Hermes 的会话库：变了就立刻导出，不等 30 分钟的周期（2026-11-09）。
# 不把目录 junction 进库：state.db 带 WAL，被同步软件抓走会拿到半截快照。
HERMES_DB = os.environ.get("PERSONA_HERMES_DB") or \
    r"D:\ProgramData\Hermes Agent CN Desktop\data\hermes-home\state.db"
DB_QUIET = 3.0   # 连续这么久没写入，才认为这一轮答完了

# 迁移后的上下文水位：Hermes 的 UI 账本里有每轮输入 token，state.db 的 context_used 是空的
HERMES_UI_DB = os.environ.get("PERSONA_HERMES_UI_DB") or \
    r"D:\ProgramData\Hermes Agent CN Desktop\data\hermes-home\desktop-ui.sqlite"
HERMES_CTX_MAX = 1_000_000
HERMES_CTX_LIMIT = 450_000   # 与旧 ctxguard 的清理线同一把尺子
HERMES_MIRROR_LIMIT = 800_000  # 到这条线先把会话全文镜像落盘，再让它压

DEFAULTS = {
    "heartbeat": 60,
    "herdsman": 300,
    "export": 1800,
    "index": 3600,
    "ctx": 600,
    "watermark": 900,
    "merge": 1800,
    "navwatch": 120,
    "hctx": 120,
    "backup2": 300,
}

TASK_TIMEOUT = {"index": 1800, "export": 900, "ctx": 240, "capture": 900, "watermark": 120}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = "%s  %s" % (now(), msg)
    try:
        os.makedirs(OPS, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw 下没有 stdout


# ── 任务实现：每个返回 (ok, 摘要) ──────────────────────────────────────

def t_herdsman():
    import socket
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", 8080))
        return True, "8080 up"
    except OSError:
        pass
    finally:
        s.close()
    exe = r"D:\ProgramData\Herdsman\herdsman.exe"
    if not os.path.exists(exe):
        return False, "herdsman.exe missing"
    subprocess.Popen([exe], cwd=os.path.dirname(exe),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(20):
        time.sleep(2)
        s = socket.socket(); s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", 8080)); return True, "restarted"
        except OSError:
            pass
        finally:
            s.close()
    return False, "restart failed"


def run(cmd, timeout):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        tail = out[-1] if out else ""
        return r.returncode == 0, (tail[:160] or ("exit %d" % r.returncode))
    except subprocess.TimeoutExpired:
        return False, "timeout"


def t_export():
    """导出 Hermes 会话。带锁：手动跑、watch 触发、hctx 的镜像触发可能撞车，
    两个进程同时写同一个 .md 会 PermissionError（实测踩过）。"""
    if os.path.exists(EXPORT_LOCK):
        try:
            if time.time() - os.stat(EXPORT_LOCK).st_mtime < 600:
                return True, "上一次导出还在跑，跳过"
        except OSError:
            pass
    try:
        with open(EXPORT_LOCK, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    try:
        return run([PY, os.path.join(ROOT, "persona_bridge.py")], TASK_TIMEOUT["export"])
    finally:
        try:
            os.remove(EXPORT_LOCK)
        except OSError:
            pass


def t_index():
    ok1, s1 = run([PY, os.path.join(ROOT, "persona_index.py")], TASK_TIMEOUT["index"])
    ok2, s2 = run([PY, os.path.join(ROOT, "persona_index_l1.py")], 120)
    return ok1 and ok2, "%s | %s" % (s1, s2)


def t_ctx():
    return run([PY, os.path.join(ROOT, "persona_slim.py"), "--live"], TASK_TIMEOUT["ctx"])


def t_watermark():
    ok, s = run([PY, os.path.join(ROOT, "persona_gauge.py")], TASK_TIMEOUT["watermark"])
    return ok, s


def t_capture():
    anchor = os.path.join(OPS, "anchor.txt")
    out = os.path.join(ROOT, "sessions", time.strftime("%Y-%m-%d"), "网页对话",
                       "capture-%s.md" % time.strftime("%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    args = [NODE, CAPTURE_JS, out]
    if os.path.exists(anchor):
        args.append(anchor)
    ok, s = run(args, TASK_TIMEOUT["capture"])
    return ok, s


def t_scan():
    """DLP 哨兵：扫入口文件里有没有像密钥的东西（只报，不改）。"""
    sys.path.insert(0, r"D:\文档\SysPersona-OS\impl\launcher")
    try:
        from mask_secrets import mask
    except ImportError:
        return False, "mask_secrets 不可用"
    total = 0
    names = ("CONTEXT.md", "MEMORY.md", "USER.md", "INDEX.md", "SOP-壳运维.md")
    for name in names:
        p = os.path.join(ROOT, "memory", name)
        if os.path.exists(p):
            total += len(mask(open(p, encoding="utf-8").read())[1])
    return True, "敏感项 %d 处" % total



def t_hctx():
    """Hermes 侧的上下文水位（迁移前的 ctxguard 读的是 DSH 账本，那边已经没数据了）。

    口径：turn_stats 里最近一轮的 tokens_input = 这一轮实际带进去的上下文量。
    state.db 的 context_used 是空的，只能用这个；它跟 DSH 的 cacheRead 是同一把尺子。
    """
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % HERMES_UI_DB.replace("\\", "/"), uri=True)
    try:
        row = con.execute(
            "select session_id, model, tokens_input, api_calls from turn_stats "
            "where tokens_input is not null order by id desc limit 1").fetchone()
    finally:
        con.close()
    if not row:
        return True, "还没有可测的轮次"
    sid, model, ctx, calls = row
    pct = 100.0 * ctx / HERMES_CTX_MAX
    level = "HOT" if ctx >= HERMES_CTX_LIMIT else "OK"
    payload = {"at": now(), "session": sid, "model": model, "ctx": int(ctx),
               "max": HERMES_CTX_MAX, "pct": round(pct, 1), "level": level}
    try:
        tmp = os.path.join(OPS, "hctx.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(OPS, "hctx.json"))
    except OSError:
        pass
    msg = "%s %.0fK / %.0fK（%.0f%%）" % (level, ctx / 1000, HERMES_CTX_MAX / 1000, pct)
    # 快满了先做镜像：把当前会话完整落盘，再让 Hermes 自己就地压缩。
    # 压缩丢的是运行时上下文，正文必须已经在库里——这是"压缩不丢记忆"的唯一保证。
    if ctx >= HERMES_MIRROR_LIMIT:
        ok, emsg = t_export()
        msg += " | 已镜像：%s" % emsg
    return True, msg


def t_backup2():
    """第二份实时备份：主库在 D:，这一份落到 C:，两块盘同时挂的概率极低。

    只拷"能重建一切"的东西：当天会话正文、memory\\、几本台账。
    不做镜像删除（不用 /MIR 那种），只增量覆盖——宁可多留，不可误删。
    """
    import shutil
    src_root = ROOT
    pairs = []

    def add_dir(sub):
        base = os.path.join(src_root, sub)
        if not os.path.isdir(base):
            return
        for dp, _, fs in os.walk(base):
            for f in fs:
                if f.lower().endswith(".md"):
                    s = os.path.join(dp, f)
                    pairs.append((s, os.path.join(BACKUP2, os.path.relpath(s, src_root))))

    add_dir("sessions")      # 全部日期，不只今天：灾备要的是"能重建"
    add_dir("archive")       # 去重副本，宁可多留
    add_dir("memory")
    add_dir("续接")
    for f in ("BACKLOG.md", "DECISIONS.md", "SKILL.md", "先读我.md", "LATEST.md", "HANDOFF.md"):
        s = os.path.join(src_root, f)
        if os.path.exists(s):
            pairs.append((s, os.path.join(BACKUP2, f)))

    n, size, skipped = 0, 0, 0
    for s, d in pairs:
        try:
            if (os.path.exists(d) and os.path.getsize(d) == os.path.getsize(s)
                    and os.path.getmtime(d) >= os.path.getmtime(s)):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            n += 1
            size += os.path.getsize(d)
        except OSError:
            pass
    snap = {"at": now(), "copied": n, "skipped": skipped, "bytes": size,
            "root": BACKUP2, "source": src_root}
    try:
        os.makedirs(BACKUP2, exist_ok=True)
        tmp = os.path.join(BACKUP2, "备份状态.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(BACKUP2, "备份状态.json"))
    except OSError:
        pass
    return True, "新增 %d / 未变 %d（%.1f KB）" % (n, skipped, size / 1024.0)


def t_navwatch():
    """F5 监听器（watch_nav.js）必须常驻。以前只靠开机启动项，掉了没人管；
    这里用心跳文件的时效判断它是否还活着，掉了就按原样拉起来。"""
    hb = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SysPersona", "navwatch.heartbeat")
    try:
        age = time.time() - os.stat(hb).st_mtime
    except OSError:
        age = 1e9
    if age < 120:
        return True, "心跳 %.0fs 前" % age
    vbs = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SysPersona", "start_navwatch.vbs")
    if not os.path.exists(vbs):
        return False, "心跳断了且缺 start_navwatch.vbs"
    subprocess.Popen(["wscript.exe", vbs],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True, "心跳断了 %.0fs，已重拉" % age


def t_merge():
    """壳自带的 memory 写进 memory\\shell\\，这里把观察条目并进正式 USER.md。"""
    shell = os.path.join(ROOT, "memory", "shell")
    src = os.path.join(shell, "USER.md")
    dst = os.path.join(ROOT, "memory", "USER.md")
    if not os.path.exists(src):
        return True, "shell 无 USER.md"
    try:
        lines = [l.strip().lstrip("\ufeff") for l in open(src, encoding="utf-8-sig").read().splitlines()]
    except OSError as e:
        return False, str(e)[:80]
    body = open(dst, encoding="utf-8").read()
    marker = "<!-- OBSERVATIONS: append below this line -->"
    add = [l for l in lines if l.startswith("- [") and "→" in l and l not in body]
    if not add:
        return True, "无新增"
    if marker in body:
        body = body.replace(marker, marker + "\n" + "\n".join(add), 1)
    else:
        body = body.rstrip() + "\n\n" + "\n".join(add) + "\n"
    open(dst, "w", encoding="utf-8", newline="").write(body)
    # 归档 shell 文件内容，避免重复合并
    bak = os.path.join(shell, "USER.%s.bak" % time.strftime("%Y%m%d-%H%M%S"))
    os.replace(src, bak)
    return True, "并入 %d 条" % len(add)


TASKS = {
    "herdsman": t_herdsman,
    "export": t_export,
    "index": t_index,
    "ctx": t_ctx,
    "watermark": t_watermark,
    "merge": t_merge,
    "scan": t_scan,
    "navwatch": t_navwatch,
    "hctx": t_hctx,
    "backup2": t_backup2,
    "capture": t_capture,
}


# ── 检索：常用目录自动 / 指定目录手动 ────────────────────────────────

DEFAULT_DIRS = ["sessions", "archive", "memory"]


def search(keyword, dirs=None, k=20):
    """在记忆库里按关键词检索，返回 [(文件, 命中数, 首条片段)]。"""
    import glob
    kw = [keyword] if isinstance(keyword, str) else list(keyword)
    files = []
    for r in (dirs or DEFAULT_DIRS):
        base = r if os.path.isabs(r) else os.path.join(ROOT, r)
        if os.path.isfile(base):
            files.append(base)
        else:
            files.extend(glob.glob(os.path.join(base, "**", "*.md"), recursive=True))
    hits = []
    for f in files:
        try:
            t = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        n = sum(t.count(x) for x in kw)
        if n:
            pos = min([p for p in (t.find(x) for x in kw) if p >= 0] or [0])
            hits.append((f, n, re.sub(r"\s+", " ", t[max(0, pos - 60): pos + 140])))
    # 去重：archive 是 sessions 的副本，同一段内容只留一份（优先 sessions）
    seen, uniq = set(), []
    for f, n, snip in sorted(hits, key=lambda x: (x[0].startswith('archive'), -x[1])):
        key = re.sub(r'\s+', '', snip)[:80]
        if key in seen:
            continue
        seen.add(key)
        uniq.append((f, n, snip))
    uniq.sort(key=lambda x: -x[1])
    return uniq[:k]


# ── 调度 ──────────────────────────────────────────────────────────────

class PersonaDaemon:
    def __init__(self):
        os.makedirs(OPS, exist_ok=True)
        self.cfg = dict(DEFAULTS)
        if os.path.exists(CONF):
            try:
                self.cfg.update(json.load(open(CONF, encoding="utf-8")))
            except Exception as e:
                log("配置读取失败，用默认：%s" % e)
        self.state = {k: {"last": 0, "ok": None, "msg": "never", "runs": 0, "busy": False}
                      for k in TASKS}
        self.next_due = {k: 0 for k in TASKS}
        self.started = time.time()
        self.stop = False
        self._last_export = 0.0

    def status(self, extra=None):
        d = {
            "at": now(),
            "uptime_s": int(time.time() - self.started),
            "pid": os.getpid(),
            "tasks": {k: dict(v, next_in=int(max(0, self.next_due[k] - time.time())))
                      for k, v in self.state.items()},
        }
        if extra:
            d.update(extra)
        tmp = STATUS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATUS)

    def worker(self, name):
        fn = TASKS[name]
        self.state[name]["busy"] = True
        t0 = time.time()
        try:
            ok, msg = fn()
        except Exception:
            ok, msg = False, traceback.format_exc(limit=2).splitlines()[-1][:160]
        dt = time.time() - t0
        self.state[name].update(last=time.time(), ok=ok, msg=msg, busy=False,
                                runs=self.state[name]["runs"] + 1)
        self.next_due[name] = time.time() + float(self.cfg.get(name, 3600))
        log("TASK %-9s %-4s %5.1fs  %s" % (name, "OK" if ok else "FAIL", dt, msg))

    def loop(self):
        log("persona_daemon 启动 pid=%d，任务：%s" % (os.getpid(), ", ".join(sorted(TASKS))))
        heart_next = 0
        db_sig = None
        db_dirty = None
        while not self.stop:
            t = time.time()
            if t >= heart_next:
                self.status()
                heart_next = t + float(self.cfg.get("heartbeat", 60))

            # 会话库一变就把 export 提前：安静 3 秒 = 这一轮说完了
            try:
                st = os.stat(HERMES_DB)
                cur = (st.st_mtime, st.st_size)
            except OSError:
                cur = None
            if cur is not None:
                if cur != db_sig:
                    db_sig, db_dirty = cur, t
                elif db_dirty and t - db_dirty >= DB_QUIET:
                    db_dirty = None
                    # 冷却 30 秒：Hermes 空闲时也会写 state.db（心跳/遥测），
                    # 没有冷却就会被它牵着每几秒导一次。
                    if not self.state["export"]["busy"] and t - self._last_export >= 30:
                        self._last_export = t
                        self.next_due["export"] = 0

            for name in TASKS:
                st = self.state[name]
                if st["busy"] or t < self.next_due[name]:
                    continue
                if name == "capture":
                    if not os.path.exists(CAPTURE_REQ):
                        continue
                    os.remove(CAPTURE_REQ)
                threading.Thread(target=self.worker, args=(name,), daemon=True).start()

            time.sleep(2)

    def run_once(self, name):
        self.worker(name)
        self.status()
        return 0 if self.state[name]["ok"] else 1


def single_instance():
    os.makedirs(OPS, exist_ok=True)
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK).read().strip())
            os.kill(pid, 0)  # 存在且活着
            return False
        except (OSError, ValueError):
            pass
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--once", default="")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--search", default="", help="关键词（空格分隔=多词）")
    ap.add_argument("--dir", action="append", default=None, help="只搜指定目录（可多次）")
    ap.add_argument("--k", type=int, default=20)
    a = ap.parse_args()

    if a.search:
        res = search(a.search.split(), a.dir, a.k)
        if not res:
            print("没命中")
        for f, n, snip in res:
            print("%4d 次  %s\n        %s" % (n, os.path.relpath(f, ROOT), snip[:160]))
        sys.exit(0)

    if a.status:
        if os.path.exists(STATUS):
            print(open(STATUS, encoding="utf-8").read())
        else:
            print("没有状态文件（persona_daemon 没跑过）")
        sys.exit(0)

    k = PersonaDaemon()
    if a.once:
        if a.once not in TASKS:
            raise SystemExit("未知任务：%s（可选 %s）" % (a.once, ", ".join(TASKS)))
        sys.exit(k.run_once(a.once))

    if not single_instance():
        print("已有实例在跑（见 %s）" % LOCK)
        sys.exit(2)
    try:
        k.loop()
    except KeyboardInterrupt:
        k.stop = True
        log("收到中断，退出")
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass
