import os as _os_shim, sys as _sys_shim
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）
_sys_shim.path.insert(0, _os_shim.path.join(_os_shim.path.dirname(_os_shim.path.abspath(__file__)), "脚本"))
# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_daemon.py — 本地常驻守护进程（抓取 / 索引 / 归档 / 保活 / 心跳 / 上下文）
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
    capture    on-demand  抓网页会话（request 文件触发）；**抓前先 poll 会话表**
                         （persona_sessions.py：只看侧栏最上面几条，new_top = 新会话，
                          记 info 不报警 + 排进 ops\web-capture-queue.json）；
                         落盘按会话分存 sessions\<日期>\deepseek网页\<conv_id>.md，永不合并
    inject     120s  查「注入是否长期失败」，error 档写一条 ops/alerts.jsonl
    selfcheck  120s  七项心跳自检 → ops\selfcheck.json（托盘/守护/水位/CDP/注入/队列/成本）

路径与环境变量
--------------
路径一律走 persona_root（node / hermes_home / capture_js / backup_root / herdsman / repo /
root）。本机独有目录写 ops\persona.conf.json（**不入库**，模板 ops\persona.conf.example.json）；
环境变量 > persona_root 推导 > 本机配置 > 都没有就**记一条 warn 跳过该任务**，日志写明缺哪个键。
"""
import argparse, io, json, os, re, subprocess, sys, threading, time, traceback

try:
    import persona_win as _pw  # 无窗口：GUI 进程里 spawn 控制台程序不弹黑窗
except Exception:
    pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "ops")
STATUS = os.path.join(OPS, "persona-status.json")
STATE_FILE = os.path.join(OPS, "daemon-state.json")
LOCK = os.path.join(OPS, "persona.lock")
CONF = os.path.join(OPS, "persona.conf.json")
LOG = os.path.join(OPS, "persona.log")
CAPTURE_REQ = os.path.join(OPS, "persona-capture.request")
EXPORT_LOCK = os.path.join(OPS, "export.lock")
INDEX_LOCK = os.path.join(OPS, "index.lock")
ALERTS = os.path.join(OPS, "alerts.jsonl")
INJECT_ALERTED = os.path.join(OPS, "inject-alerted.json")

PY = sys.executable

# 路径唯一出口：persona_root（平铺部署时本文件所在目录就是库根）。
sys.path.insert(0, ROOT)
from persona_root import (backup_root as _backup_root,   # noqa: E402
                          capture_js as _capture_js,
                          hermes_home as _hermes_home,
                          herdsman as _herdsman,
                          node as _node,
                          repo as _repo,
                          root as _persona_root)

ROOT = _persona_root()

try:
    import persona_sched as _sched   # 闹钟/守护优先级仲裁；缺了就退化为互不跳过
except Exception:
    _sched = None


def _conf():
    """ops\\persona.conf.json（本机配置，不入库）：任务间隔 + 本机独有路径。"""
    try:
        with open(CONF, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_CONF = _conf()


def _pick_path(resolver, key):
    """persona_root 解析 → ops\\persona.conf.json 的 key → None（调用处记 warn 跳过）。"""
    try:
        v = resolver() if resolver else None
    except Exception:
        v = None
    if v:
        return v
    v = _CONF.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _missing(what, key):
    return ("缺 %s：设 PERSONA_* 环境变量，或写 ops\\persona.conf.json 的 \"%s\""
            "（模板见 ops\\persona.conf.example.json）" % (what, key))


BACKUP2 = _pick_path(_backup_root, "backup2_root")
NODE = _pick_path(_node, "node")
CAPTURE_JS = _pick_path(_capture_js, "capture_js")

# Hermes 的会话库：变了就立刻导出，不等 30 分钟的周期（2026-11-09）。
# 不把目录 junction 进库：state.db 带 WAL，被同步软件抓走会拿到半截快照。
_HERMES = _pick_path(_hermes_home, "hermes_home")
HERMES_DB = os.environ.get("PERSONA_HERMES_DB") or (
    os.path.join(_HERMES, "state.db") if _HERMES else None)
DB_QUIET = 3.0   # 连续这么久没写入，才认为这一轮答完了

# 迁移后的上下文水位：Hermes 的 UI 账本里有每轮输入 token，state.db 的 context_used 是空的
HERMES_UI_DB = os.environ.get("PERSONA_HERMES_UI_DB") or (
    os.path.join(_HERMES, "desktop-ui.sqlite") if _HERMES else None)
# 阈值只在一处定义：persona_limits.py（规范见 docs/BACKGROUND_SPEC.md §二）。
import persona_limits as _LIM  # noqa: E402
HERMES_CTX_MAX = _LIM.LIMIT
HERMES_CTX_LIMIT = _LIM.CLEAN        # 与 gauge 的"该清理了"同一把尺子
HERMES_MIRROR_LIMIT = _LIM.MIRROR    # 到这条线先把会话全文镜像落盘，再让它压

# 冷启动错峰：重活先等 BOOT_GRACE 秒，再每 STAGGER 秒放一个（2026-11-12）
BOOT_GRACE = 120
STAGGER = 25
HEAVY_TASKS = {"index", "export", "backup2", "backupzip", "repobackup", "merge",
               "settings", "backed", "maint", "handoff", "observe",
               "daily", "drpreflight", "sfbalance", "sfmodels", "ctx", "scan",
               "sweep", "webcap"}

DEFAULTS = {
    "settings": 7200,   # 设定/宪法级文件多副本快照（本机+网盘）
    "backed": 1800,     # 备份体检：zip / 设定快照 / NAS 可达 / 加密状态
    "localmodel": 120,  # 云端可用就停本机模型服务，云端不可达再拉起（2026-11-11 人定）
    "failover": 300,    # 模型自动接管：心跳→判定→切换→激活（2026-11-11 人定）
    # 以下原来都是独立计划任务，2026-11-11 收进守护（计划任务只留拉起守护的那一个）
    # compact 2026-11-12 从 300 → 120：人报「等了一会没反应」，5 分钟才看一眼太钝；
    # 光量水位不花钱，早 3 分钟发现就早 3 分钟压。
    "compact": 120, "ctxguard": 600, "heal": 300,
    "hermeskeeper": 60, "nodedrain": 60, "reloader": 300, "keeperbeat": 60,
    "gate": 300, "clients": 300, "sealsweep": 1800, "tray": 120, "cdp": 900, "sfmodels": 21600, "maint": 604800,
    "backupzip": 86400, "repobackup": 86400, "handoffrefresh": 1800,
    "webcap": 3600,          # 网页抓取节拍器：1 小时一次（人定，2026-11-12）
    # 清道夫：每小时看一眼，但**一天只真动手一次**（人定：不要实时清理，先搬进筐，
    # 最少放 7 天且确认没误删才真删）。见 t_janitor。
    "janitor": 3600,
    "heartbeat": 60,
    "herdsman": 300,
    "export": 1800,
    "index": 3600,
    "ctx": 600,
    "watermark": 900,
    "cost": 300,
    "merge": 1800,
    "navwatch": 120,
    "hctx": 120,
    "backup2": 300,
    "handoff": 600,
    "reload": 600,
    "uiskin": 120,
    "wake": 30,
    "agent": 60,
    "reap": 60,
    "proxy": 60,
    "todo": 1800,
    "tasks": 300,
    "observe": 21600,
    "daily": 21600,
    "inject": 120,
    "selfcheck": 120,
    "sfbalance": 21600,          # 硅基余额快照：6 小时一班（脚本自己还有 6 小时闸门，重复调用不重复抓）
    "dsbalance": 1800,           # DeepSeek 余额：官方接口，半小时一次（不花 token，只防见底）
    "dr": 60,                    # 容灾：每分钟读一次 dr-plan.json，条件到了触发预建任务
    "drpreflight": 21600,       # 容灾复核：6 小时一遍（每会话只在出现时建档一次）
}

TASK_TIMEOUT = {"index": 1800, "export": 900, "ctx": 240, "capture": 1800,
                "watermark": 120, "cost": 120, "selfcheck": 60, "sfbalance": 240,
                "dsbalance": 120, "dr": 90, "drpreflight": 120,
                "settings": 300, "backed": 180,
                "compact": 1500, "ctxguard": 600, "heal": 1200,
                "hermeskeeper": 180, "nodedrain": 180, "reloader": 120,
                "keeperbeat": 120, "gate": 120, "clients": 120, "sealsweep": 900, "tray": 120, "cdp": 900, "sfmodels": 1800,
                "maint": 1800, "backupzip": 2400,
                "janitor": 900,
                "repobackup": 1800, "handoffrefresh": 600}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = "%s  %s" % (now(), msg)
    try:
        os.makedirs(OPS, exist_ok=True)
        fresh = (not os.path.exists(LOG)) or os.path.getsize(LOG) == 0
        with open(LOG, "a", encoding="utf-8") as f:
            # 新建/空文件时先写 BOM：记事本靠 BOM 认 UTF-8，没有就整篇按 GBK 解 → 全乱码
            if fresh:
                f.write("\ufeff")
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
    exe = _pick_path(_herdsman, "herdsman_exe")
    if not exe:
        return False, _missing("herdsman.exe", "herdsman_exe")
    if not os.path.exists(exe):
        return False, "herdsman.exe missing：%s" % exe
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


def _dec(raw):
    """解子进程输出：先按 UTF-8 严格解，失败再按 GBK 解，最后才带替换。

    2026-11-11 13:47 人报「排版不对」：状态窗里出现 `?鵵 2 ??` 这种乱码。
    根因不是排版，是 PowerShell 子脚本用 GBK 输出、这里一律按 UTF-8 带替换解，
    中文被切成半个字。严格解码 + 回退才能两边都对。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run(cmd, timeout):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = _dec(r.stdout).strip().splitlines()
        tail = out[-1] if out else ""
        return r.returncode == 0, (tail[:160] or ("exit %d" % r.returncode))
    except subprocess.TimeoutExpired:
        return False, "timeout"


# ── 抓之前先看侧栏最上面几条（2026-11-10 人定的机制）──────────────────
# 判据唯一权威：persona_sessions.py 的会话表 ops\web-sessions.json。
# 新会话永远出现在侧栏最上面一条；表里没有的 id = new_top（新会话）。
SESSIONS = os.path.join(ROOT, "脚本/persona_sessions.py")
SESSION_TABLE = os.path.join(OPS, "web-sessions.json")
CAPTURE_QUEUE = os.path.join(OPS, "web-capture-queue.json")
LAST_CAPTURE = os.path.join(OPS, "web-last-capture.json")


def _sw_label(conv, title):
    if not conv and not title:
        return "（空）"
    return "%s（%s）" % (("《%s》" % title) if title else "无标题", (conv or "")[:8] or "?")


def sessions_poll(timeout=60):
    """问一句：侧栏最上面几条变没变？返回 (退出码, info)。

    0 = 没有新会话、3 = 有新会话（new_top）、4 = 没选中会话、2 = CDP 不通。
    （与 persona_sessions.py 的退出码一致。）
    这一步**绝不抛异常**：抓取链路宁可带着"没查清"往下走，
    也不能因为查会话把自己搞停——但"没查清"必须写进日志，不许静默。
    """
    try:
        r = subprocess.run([PY, SESSIONS, "poll", "--json"], cwd=ROOT,
                           capture_output=True, timeout=timeout,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return 2, {"reason": "会话表 poll 调不起来：%s" % str(e)[:120]}
    text = _dec(r.stdout)
    info = {}
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                info = json.loads(line)
                break
            except ValueError:
                continue
    if not info:
        detail = _dec(r.stderr).strip()
        info = {"reason": (detail[:160] or "会话表 poll 没有输出")}
    return r.returncode, info


def load_capture_queue():
    try:
        q = json.load(io.open(CAPTURE_QUEUE, encoding="utf-8"))
        if isinstance(q, dict) and isinstance(q.get("items"), list):
            return q
    except (OSError, ValueError):
        pass
    return {"items": []}


def save_capture_queue(q):
    q["updated_at"] = now()
    try:
        os.makedirs(OPS, exist_ok=True)
        tmp = CAPTURE_QUEUE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(q, ensure_ascii=False, indent=1))
        os.replace(tmp, CAPTURE_QUEUE)
    except OSError:
        pass


def queue_new_sessions(new_top):
    """新会话排进抓取队列：**每个会话抓一次，不合并**。返回队列里现有的条数。

    只能抓当前打开的那一个（不点侧栏、不滚动）：没轮到的会话留在队列里，
    等它被点开、成为当前会话时再抓——那时它还是它自己那份文件。
    """
    q = load_capture_queue()
    have = {x.get("id") for x in q["items"]}
    for it in new_top or []:
        cid = it.get("id") or ""
        if not cid or cid in have:
            continue
        q["items"].append({"id": cid, "title": it.get("title") or "",
                           "pos": it.get("pos") or 0, "queued_at": now()})
        have.add(cid)
    save_capture_queue(q)
    return len(q["items"])


def queue_drop(conv_id):
    """这个会话已经抓过了：从队列里划掉（别的会话不动）。"""
    q = load_capture_queue()
    keep = [x for x in q["items"] if x.get("id") != conv_id]
    if len(keep) != len(q["items"]):
        q["items"] = keep
        save_capture_queue(q)


def session_table_rows():
    """读会话表 ops\\web-sessions.json（只读，不写）。"""
    try:
        t = json.load(io.open(SESSION_TABLE, encoding="utf-8"))
        if isinstance(t, dict) and isinstance(t.get("sessions"), dict):
            return t["sessions"]
    except (OSError, ValueError):
        pass
    return {}


def queue_pending_from_table():
    """把「基线之后出现、还没抓过」的会话排进队列（每个抓一次，不合并）。

    为什么不能只看本次 poll 的 new_top：readnew 抓之前也会 poll，表是它先更新的，
    daemon 再 poll 时 new_top 已经空了——只认 new_top 就会漏掉"在别的窗口新建的会话"。
    所以以表为准：`baseline=false`（基线之后登记的）且 `last_capture` 为空 = 待抓。
    老表（没有 baseline 字段）一律不算新，避免上线第一轮把整张表都排进队列。
    """
    rows = session_table_rows()
    todo = [{"id": rid, "title": r.get("title") or "", "pos": r.get("pos") or 0}
            for rid, r in rows.items()
            if r.get("baseline") is False and not r.get("last_capture")]
    if todo:
        queue_new_sessions(todo)
    return len(todo)


def _alert_new_session(it):
    """新会话的 info 级留痕：**不报警**，只写 ops\\alerts.jsonl。"""
    cid = it.get("id") or ""
    if not cid:
        return False
    rec = {"at": now(), "level": "info", "kind": "session_new", "src": "sessions",
           "msg": "新会话：%s（%s，第 %d 位）"
                  % (it.get("title") or "（无标题）", cid, it.get("pos") or 0),
           "to": cid, "pos": it.get("pos") or 0}
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(ALERTS, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _write_last_capture(rec):
    """把「这次抓的是哪个会话、落在哪个文件」写出来，readnew 据此更新会话表。"""
    try:
        os.makedirs(OPS, exist_ok=True)
        tmp = LAST_CAPTURE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=1))
        os.replace(tmp, LAST_CAPTURE)
    except OSError:
        pass


def _read_capture_head(path):
    """collect7 写在文件头的「# 标题」「- 消息数：N」。读不到就给 (0, '')。"""
    title, n = "", 0
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(12):
                line = f.readline()
                if not line:
                    break
                s = line.strip()
                if s.startswith("# ") and not title:
                    title = s[2:].strip()
                elif "消息数" in s:
                    m = re.search(r"(\d+)", s)
                    if m:
                        n = int(m.group(1))
    except OSError:
        pass
    return n, title


def t_export():
    """导出 Hermes 会话。带锁：手动跑、watch 触发、hctx 的镜像触发可能撞车，
    两个进程同时写同一个 .md 会 PermissionError（实测踩过）。"""
    # 路径可能是"按默认位置推出来的"，文件并不存在；这时跑 bridge 只会 exit 1。
    if not HERMES_DB or not os.path.isfile(HERMES_DB):
        return True, "跳过：本机没装 Hermes（没有 state.db）"
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
        return run([PY, os.path.join(ROOT, "脚本/persona_bridge.py")], TASK_TIMEOUT["export"])
    finally:
        try:
            os.remove(EXPORT_LOCK)
        except OSError:
            pass


def t_index():
    """增量索引。带锁 + 失败重试一次：导出正在写文件时哈希会撞上锁，
    那一轮必然失败，但下一轮就好了——重试比让人盯着强。"""
    if os.path.exists(INDEX_LOCK):
        try:
            if time.time() - os.stat(INDEX_LOCK).st_mtime < 1800:
                return True, "上一次索引还在跑，跳过"
        except OSError:
            pass
    try:
        with open(INDEX_LOCK, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    try:
        ok1, s1 = run([PY, os.path.join(ROOT, "脚本/persona_index.py")], TASK_TIMEOUT["index"])
        if not ok1:
            time.sleep(5)
            ok1, s1 = run([PY, os.path.join(ROOT, "脚本/persona_index.py")], TASK_TIMEOUT["index"])
        ok2, s2 = run([PY, os.path.join(ROOT, "脚本/persona_index_l1.py")], 120)
        return ok1 and ok2, "%s | %s" % (s1, s2)
    finally:
        try:
            os.remove(INDEX_LOCK)
        except OSError:
            pass


def t_ctx():
    return run([PY, os.path.join(ROOT, "脚本/persona_slim.py"), "--live"], TASK_TIMEOUT["ctx"])


def t_watermark():
    ok, s = run([PY, os.path.join(ROOT, "脚本/persona_gauge.py")], TASK_TIMEOUT["watermark"])
    return ok, s


def t_cost():
    """成本尺子：扫壳的会话缓存记日账（只读壳的目录），超线追加 alerts。

    只跑 `persona_cost.py sample --warn`；超时 120 秒，正常 < 1 秒，绝不拖住心跳。
    """
    return run([PY, os.path.join(ROOT, "脚本/persona_cost.py"), "sample", "--warn"],
               TASK_TIMEOUT["cost"])


def t_clients():
    """客户端体检 + 按需拉起（2026-11-11 加，5 分钟一次）。

    人：「建议用程序自动拉起，重载。所以程序需要自动检测客户端配置文件和工作目录的能力」
    所以这里用 persona_client_probe 认出 exe / 配置 / 工作目录，报版本与是否在跑；
    detect.auto_launch=true 的客户端才会被抓起来——**默认不自动开窗口**（不抢焦点）。
    """
    import persona_client_probe as P
    msgs = []
    bad = 0
    try:
        clients = P.all_clients()
    except Exception as e:                 # noqa: BLE001
        return False, "客户端探测失败：%s" % str(e)[:80]
    for n, st in clients.items():
        if not st.get("exe_exists"):
            # 换台机器时这条会立刻出现：不是路径写错，是这台压根没装那个客户端。
            # 所以只记一句，不算故障（真装了的机器上，exe 找不到才是问题，见 tray-clients.json 的候选列表）。
            msgs.append("%s 未安装" % n)
            continue
        if not st.get("config"):
            bad += 1
            msgs.append("%s 找不到配置" % n)
        if st.get("running"):
            msgs.append("%s %s" % (n, st.get("version") or "?"))
        elif st.get("auto_launch"):
            ok, msg = P.launch(n)
            msgs.append(("拉起 " + n) if ok else msg[:40])
        else:
            msgs.append("%s 没在跑" % n)
    return bad == 0, "｜".join(msgs)[:160]


def t_settings():
    """设定/宪法级文件的多副本快照（2026-11-11 加，2 小时一次）。

    人：「每一个设定文件都不能只有一份……这些都按同样的逻辑备份。」
    所以交给 `persona_settings_backup.py`：本机 + 网盘各一份，分级保留，带 manifest。
    """
    ok, s = run([PY, os.path.join(ROOT, "脚本/persona_settings_backup.py"), "run"],
                TASK_TIMEOUT["settings"])
    return ok, s[-160:]


def t_backed():
    """备份体检（2026-11-11 加，半小时一次）：**只看"能不能回滚"，不看流程跑没跑**。

    查四件事，任何一件不满足就报出来（托盘会变琥珀）：
      ① 每日记忆库 zip：最近一份不超过 36 小时；
      ② 设定快照：每份文件两处副本齐全、内容一致、不超过 26 小时；
      ③ 网盘密文：manifest 在、且不比本地少太多（漏推会在这里露出来）；
      ④ 钥匙：本机有、网盘上没有（串了就是事故）。
    """
    import time as _t
    msgs, bad = [], []

    # ① 每日 zip
    try:
        bdir = os.path.join(ROOT, "备份")
        zips = [os.path.join(bdir, n) for n in os.listdir(bdir)
                if n.startswith("记忆库-") and n.endswith(".zip")] if os.path.isdir(bdir) else []
        if not zips:
            bad.append("没有记忆库 zip")
        else:
            newest = max(zips, key=os.path.getmtime)
            age_h = (_t.time() - os.path.getmtime(newest)) / 3600.0
            if age_h > 36:
                bad.append("记忆库 zip %.0f 小时前（>36h）" % age_h)
            msgs.append("zip %.1fh" % age_h)
    except Exception as e:                 # noqa: BLE001
        bad.append("zip 检查失败 %r" % e)

    # ② 设定快照
    try:
        import persona_settings_backup as SB
        ok2, why2 = SB.verify()
        if not ok2:
            bad.append(why2[:100])
        else:
            msgs.append(why2[-40:])
    except Exception as e:                 # noqa: BLE001
        bad.append("设定快照检查失败 %r" % e)

    # ③ 外部链路第一站：NAS 通不通（同步/备份/镜像盘都不归我们，但断了就没有异地副本）
    try:
        nas = _loc.mirror()
        if os.path.isdir(nas):
            msgs.append("NAS 可达")
        else:
            msgs.append("NAS 不可达（外部链路，不计异常）")
    except Exception as e:                 # noqa: BLE001
        msgs.append("NAS 检查失败（不计异常）：%s" % str(e)[:40])

    # ④ 本机加密：开了就不能有明文残留；没开就如实说"未开"
    try:
        import persona_vault as V
        c = V.conf()
        if not os.path.exists(V.key_path()):
            # 没钥匙 = 这台机器没启用加密（B96 默认全明文），不是备份坏了
            msgs.append("加密未启用（没有钥匙）")
        if c.get("enabled"):
            base = os.path.join(ROOT, "sessions")
            plain = 0
            for dp, _ds, fs in os.walk(base):
                for fn in fs:
                    if fn.endswith(".md") and not V.is_sealed(os.path.join(dp, fn)):
                        plain += 1
            if plain:
                bad.append("加密已开但 sessions 里还有 %d 份明文" % plain)
            msgs.append("加密已开")
        else:
            msgs.append("加密未开（本地仍是明文）")
    except Exception as e:                 # noqa: BLE001
        bad.append("加密状态检查失败 %r" % e)

    if bad:
        return False, "备份体检不过：" + "；".join(bad[:3])
    return True, "｜".join(msgs)[:160]


def t_selfcheck():
    """心跳自检（2026-11-10 加）：7 项，120 秒一次，正常 < 2 秒。

    只跑 `persona_selfcheck.py --quiet`：它自己把结果写 ops\\selfcheck.json，
    只在状态**变化**时往 persona.log 追加一行，不每 30 秒刷屏。
    这里把 summary 带回任务状态，守护面板上直接能看到 7/7。
    """
    ok, s = run([PY, os.path.join(ROOT, "脚本/persona_selfcheck.py"), "--quiet"],
                TASK_TIMEOUT["selfcheck"])
    try:
        with open(os.path.join(OPS, "selfcheck.json"), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("summary"):
            return ok, str(d["summary"])[:160]
    except (OSError, ValueError):
        pass
    return ok, s


def _capture_disabled():
    """总闸：`ops\\persona.conf.json` 里写 "capture": 0 就**彻底不抓**。

    2026-11-12 定（人：「先不要后台抓取了，估计是风控了，手机和电脑都被踢了」）：
    光把 webcap 的节拍改 0 不够——readnew / 唤醒重抓会直接跑 `--once capture`，
    绕过节拍继续去戳网页。所以闸门放在这里，定时和按需两条路一起关。

    例外（同日又定：「以后就在前台我手动 F5 抓」）：`PERSONA_CAPTURE_NOW=1` 放行。
    这个环境变量只有 `persona_readnew.capture()`（他打 1 / 说读会话时那条前台路径）会设，
    后台的定时与心跳拿不到，所以「后台一律不抓、前台按需才抓」是靠它分界的。
    """
    if os.environ.get("PERSONA_CAPTURE_NOW") == "1":
        return False
    try:
        with open(CONF, encoding="utf-8") as f:
            d = json.load(f) or {}
        v = d.get("capture")
        if v is None:
            return False
        return float(v) <= 0          # 注意：不能写 `v or 1`——0 是假值，会被当成"没配"
    except Exception:                      # noqa: BLE001
        return False


def t_capture():
    r"""抓网页会话：**按会话分存**，一个会话一份文件，永不合并。

    顺序（2026-11-10 人定的机制）：
      ① `poll` 会话表：只看侧栏最上面几条的 id+标题。new_top → 记 info（**不报警**）
         + 排进抓取队列（每个会话抓一次）；
      ② 抓「当前打开的那个会话」，直接写
         `sessions\<日期>\deepseek网页\<conv_id>.md`（已存在就覆盖自己那份）；
         **不再传 anchor**——不再拿别的会话的尾巴当对照，整份快照；
      ③ 抓完登记会话表（消息数 / 最后抓取时间 / 文件路径）、从队列里划掉这个会话、
         写 `ops\web-last-capture.json` 给 readnew 看。

    没选中会话（4）或读不到侧栏（2）时，先落 `capture-HHMMSS.md` 临时名，
    抓完按**抓后的**会话 id 归位到它自己那份——宁可多一步，也不写进别人的文件。
    """
    at = now()
    if _capture_disabled():
        return True, "抓取已停用（ops/persona.conf.json 里 capture=0，风控期间不碰网页）"
    if not NODE:
        return False, _missing("node", "node")
    if not CAPTURE_JS:
        return False, _missing("抓取脚本 collect7.js", "capture_js")
    code, info = sessions_poll()
    conv = (info.get("current") or "") if code != 2 else ""
    target_file = ""

    if code == 2:
        log("✗ %s" % (info.get("reason") or "会话表读不到（CDP 不通）"))
        log("  （抓取照旧：先落临时文件，抓完再按会话 id 归位）")
    elif code == 4:
        log("页面没选中会话（地址停在根地址）：按「选第一条，不行第二条」先选上")
    else:
        new = info.get("new_top") or []
        if new:
            log("新会话 %d 条（info，不报警）：" % len(new))
            for it in new:
                log("  %s（%s，第 %d 位）"
                    % (it.get("title") or "（无标题）", it.get("id") or "", it.get("pos") or 0))
                _alert_new_session(it)
            nq = queue_new_sessions(new)
            log("  已排进抓取队列 %s（队里共 %d 个；每个会话抓一次，不合并）"
                % (os.path.relpath(CAPTURE_QUEUE, ROOT), nq))
        else:
            log("侧栏前 %d 条没变：%s ｜ 第 %d 位 ｜ %d 条消息"
                % (info.get("top") or 5,
                   _sw_label(conv, info.get("current_title")),
                   info.get("current_pos") or 0, info.get("msg_count") or 0))
        for it in info.get("moved") or []:
            log("  位置变化：%s 第 %s 位 → 第 %s 位"
                % (it.get("title") or "?", it.get("from") or 0, it.get("to") or 0))
        for it in info.get("renamed") or []:
            log("  标题变化：%s《%s》→《%s》"
                % (it.get("id") or "", it.get("from_title") or "", it.get("to_title") or ""))
        if conv:
            target_file = info.get("current_file") or ""
            # poll 给的是绝对路径；万一拿到相对路径，按库根解析——否则 cwd 一换就写到别处
            if target_file and not os.path.isabs(target_file):
                target_file = os.path.join(ROOT, target_file)

    # 表里还有"新出现过、没抓过"的会话（可能是 readnew 先 poll 掉的）：一起排队。
    # 放在 if/elif 之外：没选中会话（4）时也要能把表里的新会话排进队列。
    if code != 2:
        npend = queue_pending_from_table()
        if npend:
            q = load_capture_queue()
            log("  待抓：表里有 %d 个新会话还没抓，已排进队列（共 %d 个）"
                % (npend, len(q["items"])))

    if target_file:
        out = target_file
    else:
        out = os.path.join(ROOT, "sessions", time.strftime("%Y-%m-%d"), "deepseek网页",
                           "capture-%s.md" % time.strftime("%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # 不传 anchor：每个会话整份快照，覆盖自己那份，不跟别人的尾巴比。
    ok, s = run([NODE, CAPTURE_JS, out], TASK_TIMEOUT["capture"])
    if not ok:
        log("抓取失败：%s" % s)
        _write_last_capture({"at": at, "ok": False, "conv_id": conv,
                             "file": "", "msg_count": 0, "reason": s})
        return False, s

    if not target_file:
        code2, info2 = sessions_poll()
        conv2 = (info2.get("current") or "") if code2 != 2 else ""
        dst = info2.get("current_file") or ""
        if dst and not os.path.isabs(dst):
            dst = os.path.join(ROOT, dst)
        if conv2 and dst:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(out, dst)
                out, conv = dst, conv2
                log("  已按抓后的会话归位：%s" % os.path.relpath(dst, ROOT))
            except OSError as e:
                log("  ✗ 归位失败（临时文件留在 %s）：%s"
                    % (os.path.relpath(out, ROOT), e))

    n, head_title = _read_capture_head(out)
    title = info.get("current_title") or head_title
    marked = False
    if conv:
        okm, m = run([PY, SESSIONS, "mark", "--id", conv, "--file", out,
                      "--msg-count", str(n)], 60)
        marked = okm
        if not okm:
            log("✗ 会话表登记失败：%s" % m)
        queue_drop(conv)
    rel_out = os.path.relpath(out, ROOT)
    _write_last_capture({"at": at, "ok": True, "conv_id": conv, "title": title,
                         "file": out, "msg_count": n, "marked": marked})
    if not conv:
        log("⚠ 抓到了内容，但没认出是哪个会话：先留在 %s（会话表未登记）" % rel_out)
    # 抓完顺手做一小批增量（人 2026-11-12 定：抓完就触发一次，别等每小时那个大任务）。
    # 上限 2000 块、超时 300 秒：到量就退，剩下的等下一次抓取；大块补嵌另有人跑，
    # 这边撞上锁会自己让路（persona_index 与守护共用 ops\index.lock）。
    inc = ""
    if n:
        okx, sx = run([PY, os.path.join(ROOT, "脚本/persona_index.py"),
                       "--once-blocks", "2000"], 300)
        lines = [l for l in (sx or "").strip().splitlines() if l.strip()]
        inc = " ｜ 增量索引：%s" % (lines[-1][:70] if lines else ("ok" if okx else "失败"))
        log(inc.strip(" ｜"))
    return True, "《%s》（%s）→ %s（%d 条）%s" % (title or "无标题", (conv or "?")[:12],
                                                  rel_out, n, inc)


def t_scan():
    """DLP 哨兵：扫入口文件里有没有像密钥的东西（只报，不改）。"""
    base = _pick_path(_repo, "repo")
    if not base:
        return False, _missing("代码仓库（含 impl\\launcher）", "repo")
    sys.path.insert(0, os.path.join(base, "impl", "launcher"))
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
    # 本机没装 Hermes 时，HERMES_UI_DB 可能是"按默认位置推出来的"、文件并不存在；
    # 这时直接 connect 会抛 unable to open database file，被当成故障挂红。
    if not HERMES_UI_DB or not os.path.isfile(HERMES_UI_DB):
        return True, "跳过：本机没装 Hermes（没有 desktop-ui.sqlite）"
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
        msg += " | 已导出：%s" % emsg
        # 规范 §四：先镜像落盘（含 sha256 + 幂等），再谈压缩。导出只是让文件变新。
        try:
            import persona_mirror as mir
            src = mir.newest_session()
            if src:
                dst, state = mir.mirror(src, "hctx=%d" % int(ctx))
                good, note = mir.verify(dst) if state != "skipped" else (True, "幂等跳过")
                msg += " | 镜像%s：%s（%s）" % (state, os.path.basename(dst), note)
                # 镜像进了 archive，L1 立刻重建一次——不然要等下一轮 index（1 小时）才检索得到。
                ok2, m2 = run([PY, os.path.join(ROOT, "脚本/persona_index_l1.py")], 120)
                msg += " | L1 %s" % (m2.strip().splitlines()[0][:60] if m2 else ("ok" if ok2 else "失败"))
            else:
                msg += " | 镜像：找不到会话文件"
        except Exception as e:                      # 镜像失败不许静默，也不许继续压缩
            msg += " | 镜像失败：%s" % e
    return True, msg


def t_backup2():
    """第二份实时备份：主库在 D:，这一份落到 C:，两块盘同时挂的概率极低。

    只拷"能重建一切"的东西：当天会话正文、memory\\、几本台账。
    不做镜像删除（不用 /MIR 那种），只增量覆盖——宁可多留，不可误删。
    """
    import shutil
    if not BACKUP2:
        return False, _missing("第二备份根", "backup2_root")
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
    for f in ("文档/BACKLOG.md", "文档/DECISIONS.md", "文档/SKILL.md", "文档/先读我.md", "文档/LATEST.md", "文档/HANDOFF.md"):
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


HANDFOFF_FILE = os.path.join(ROOT, "memory", "HANDOFFS.md")
HANDOFF_STATE = os.path.join(OPS, "handoff-state.json")


def t_handoff():
    """新会话 = 一次交接，写进 memory\\HANDOFFS.md。

    只记"开始时间晚于上次记录"的会话，避免把历史一次性灌进来。
    触发原因没法自动判断（满 / 手动 / 迁移），先写"待补"，人再补一句。
    """
    import sqlite3
    # 路径可能是"按默认位置推出来的"，文件并不存在；这时跑 bridge 只会 exit 1。
    if not HERMES_DB or not os.path.isfile(HERMES_DB):
        return True, "跳过：本机没装 Hermes（没有 state.db）"
    try:
        seen = json.load(io.open(HANDOFF_STATE, encoding="utf-8"))
    except Exception:
        seen = {"last": time.time(), "ids": []}
    last, ids = seen.get("last", 0), set(seen.get("ids", []))

    try:
        con = sqlite3.connect("file:%s?mode=ro" % HERMES_DB.replace("\\", "/"), uri=True)
        rows = list(con.execute(
            "select id, started_at, message_count, input_tokens from sessions "
            "where started_at > ? order by started_at", (last,)))
        con.close()
    except Exception as e:
        return False, "读 state.db 失败: %s" % str(e)[:60]

    if not rows:
        return True, "无新会话"
    if not os.path.exists(HANDFOFF_FILE):
        return True, "缺 HANDOFFS.md"

    body = io.open(HANDFOFF_FILE, encoding="utf-8").read()
    add = []
    for sid, started, nmsg, tin in rows:
        if sid in ids:
            continue
        ids.add(sid)
        when = time.strftime("%m-%d %H:%M", time.localtime(started or time.time()))
        add.append("| %s | → 新会话 `%s` | 自动记录 | 无 | 待补（消息 %d / 输入 %d token） |"
                   % (when, sid, nmsg or 0, tin or 0))
    if not add:
        return True, "无新会话"

    anchor = "\n\n## 四、规则"
    if anchor in body:
        body = body.replace(anchor, "\n".join(add) + anchor, 1)
    else:
        body = body.rstrip() + "\n" + "\n".join(add) + "\n"
    io.open(HANDFOFF_FILE, "w", encoding="utf-8", newline="").write(body)

    json.dump({"last": time.time(), "ids": sorted(ids)[-200:]},
              io.open(HANDOFF_STATE, "w", encoding="utf-8"), ensure_ascii=False)
    return True, "记 %d 次交接" % len(add)


RELOAD_STATE = os.path.join(OPS, "reload-state.json")
NAVWATCH_LOG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SysPersona", "navwatch.log")


def t_reload():
    """每一次页面重载 / 抓取，都是一次"缓存失效、文件续命"的现场。

    这些行不是运维日志，是**证据**：证明"窗口里的只是缓存，写下来的才是记忆"。
    数据源是 tools/js/debug/watch_nav.js 自己写的 navwatch.log，成对取"触发抓取 → 抓取结束"。
    """
    # 优先级仲裁：reload 与闹钟的 compact-check 是同一件事，闹钟优先、守护让位。
    # 放在函数最前面：直接调用 t_reload() 的入口（--once / 自检）也走同一条判据。
    if _sched:
        try:
            job = _sched.job_for_task("reload", root=ROOT)
            if job:
                skip, reason = _sched.should_skip(job, "daemon", root=ROOT)
                if skip:
                    return True, reason
                # 开跑前先请求抢占：正在跑的低优先级任务（如空闲循环）会让位。
                _sched.request_preempt(job, _sched.priority(job, "daemon", root=ROOT),
                                       owner="daemon", root=ROOT)
        except Exception:
            pass
    try:
        st = json.load(io.open(RELOAD_STATE, encoding="utf-8"))
    except Exception:
        st = {"seen": 0}
    seen = int(st.get("seen", 0))

    try:
        lines = io.open(NAVWATCH_LOG, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return True, "无 navwatch.log"

    trig = None
    rows = []
    for i, line in enumerate(lines):
        if i < seen:
            continue
        if "触发抓取" in line:
            trig = line
        elif "抓取结束" in line and trig:
            t1 = trig.split("  ")[0]          # 11-09 12:44:10
            code = line.split("code=")[-1].strip()
            rows.append("| %s | %s | 抓取 code=%s |" % (t1[6:], trig.split("（")[-1].rstrip("）"), code))
            trig = None

    if not rows:
        return True, "无新重载"
    if not os.path.exists(HANDFOFF_FILE):
        return True, "缺 HANDOFFS.md"

    body = io.open(HANDFOFF_FILE, encoding="utf-8").read()
    if "## 六、重载记录" not in body:
        body = body.rstrip() + (
            "\n\n## 六、重载记录（自动，充当证据）\n\n"
            "> 这里每一行都是「缓存失效、文件续命」的现场。窗口会重载，文件不会。\n\n"
            "| 时间 | 触发 | 结果 |\n|---|---|---|\n")
    body = body.rstrip() + "\n" + "\n".join(rows) + "\n"
    io.open(HANDFOFF_FILE, "w", encoding="utf-8", newline="").write(body)

    json.dump({"seen": len(lines)}, io.open(RELOAD_STATE, "w", encoding="utf-8"))
    return True, "记 %d 次重载" % len(rows)


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


def _with_lock(path, fn):
    """跨进程互斥：多个分身（DSH / Hermes / 手机）可能同时写同一份记忆。

    做法是最土的 O_CREAT|O_EXCL 锁文件 + 50 次重试（5 秒）。
    拿不到锁也照写——宁可冒一次覆盖风险，也不丢这条观察。"""
    lock = path + ".lock"
    fd = None
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.1)
    try:
        return fn()
    finally:
        if fd is not None:
            try:
                os.close(fd)
                os.unlink(lock)
            except OSError:
                pass


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
    _with_lock(dst, lambda: open(dst, "w", encoding="utf-8", newline="").write(body))
    # 归档 shell 文件内容，避免重复合并
    bak = os.path.join(shell, "USER.%s.bak" % time.strftime("%Y%m%d-%H%M%S"))
    os.replace(src, bak)
    return True, "并入 %d 条" % len(add)


def t_uiskin():
    """壳的界面注入器必须常驻：壳重启/页面重载后靠它补样式。
    心跳文件 ops/uiskin.heartbeat，超过 30 秒没更新就重新拉起。"""
    beat = os.path.join(OPS, "uiskin.heartbeat")
    try:
        age = time.time() - os.stat(beat).st_mtime
    except OSError:
        age = 1e9
    if age < 30:
        return True, "心跳 %.0fs 前" % age
    script = os.path.join(ROOT, "persona_uiskin.js")
    if not os.path.exists(script):
        return False, "缺 persona_uiskin.js"
    if not NODE:
        return False, _missing("node", "node")
    subprocess.Popen([NODE, script], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "注入器未在跑，已拉起"


def _kick_recapture():
    """B40：心跳末尾只问一句「这次要不要重抓」（读 JSON + 抽签，毫秒级）。

    抓取要 30–90 秒，**绝不能**在心跳线程里同步跑；判定该抓就丢后台线程，
    心跳立刻返回。`decide()` 会先占位（last_try），30 秒后的下一次心跳不会重复起线程。
    """
    try:
        import persona_recapture as R
        due, why = R.decide()
        if not due:
            return "重抓：%s" % why
        threading.Thread(target=R.run_once, kwargs={"force": True}, daemon=True).start()
        return "重抓已起：%s" % why
    except Exception as e:
        return "重抓判断失败：%s" % str(e)[:80]


def t_wake():
    """闹钟（v5.3 第一块）：到点就记一条触发记录。
    真正的「唤醒某个智能体并让它先读总索引」由调用方按 purpose 执行；
    这里只负责守时和留痕，不做模型调用。

    B40 的重抓判断挂在这里（而不是另加一个 30 秒任务）：两者同频，
    多一个任务只多一个线程和一份和心跳抢跑的窗口，没有收益。
    """
    try:
        import persona_wake
    except Exception as e:
        return False, "导入 persona_wake 失败：%s" % str(e)[:60]
    n = persona_wake.fire_due()
    kick = _kick_recapture()
    # 空闲循环：只在队列空时跑（有活先干活）。闹钟已经先发完，优先级不受影响。
    auto = ""
    try:
        import persona_autoloop as _AL
        free, why = _AL.idle(root=ROOT)
        if free:
            r = _AL.tick(root=ROOT)
            auto = " | 空闲循环：%s" % (r.get("msg") or r.get("id") or "")
        else:
            auto = " | 空闲循环：有活（%s）" % why
    except Exception as e:
        auto = " | 空闲循环：跳过（%s）" % str(e)[:40]
    return True, ((("触发 %d 个闹钟" % n) if n else "无到点闹钟") + " | " + kick + auto)


def t_agent():
    """保活（v5.3）：不再认死某个壳——目标写在 ops/keepalive.json。
    查不了就不动手：受限令牌下「看不见」和「没在跑」长得一样，照着重启会开出第二份。"""
    return t_keepalive()


def t_reap():
    """派发超时回收（v5.3）：超过 ttl 还挂着的 pending 标过期，有人醒着就改派。"""
    try:
        import persona_dispatch
    except Exception as e:
        return False, "导入 persona_dispatch 失败：%s" % str(e)[:60]
    r = persona_dispatch.reap(ttl=600)
    if not r["expired"]:
        return True, "无超时派发"
    return True, "过期 %d，改派 %d" % (len(r["expired"]), len(r["redispatched"]))


def t_proxy():
    """交互代理（v5.3）：8787 不在听就拉起来；只拉进程，不接请求。"""
    try:
        import persona_proxy
    except Exception as e:
        return False, "导入 persona_proxy 失败：%s" % str(e)[:60]
    if persona_proxy.is_up():
        return True, "代理在跑"
    script = os.path.join(ROOT, "脚本/persona_proxy.py")
    if not os.path.exists(script):
        return False, "缺 persona_proxy.py"
    subprocess.Popen([PY, script, "serve"], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "代理没在跑，已拉起"


def t_keepalive():
    """保活（v5.3）：目标写在 ops/keepalive.json；查不了就跳过，不重复拉起。"""
    try:
        import persona_keepalive as K
    except Exception as e:
        return False, "导入 persona_keepalive 失败：%s" % str(e)[:60]
    res = K.check()
    if not res:
        return True, "未登记保活目标"
    if len(res) == 1:
        return bool(res[0].get("ok")), res[0]["msg"]
    bad = [r for r in res if not r.get("ok")]
    return (not bad), "%d 个目标，%d 个异常" % (len(res), len(bad))



def t_todo():
    """待办备忘（v5.3）：把 BACKLOG 摊成一份完整的，交接包截断也不丢。"""
    script = os.path.join(ROOT, "脚本/persona_todo.py")
    if not os.path.exists(script):
        return False, "缺 persona_todo.py"
    ok, msg = run([PY, script], 60)
    return ok, msg


def t_tasks():
    """任务池（v5.5）：outbox 补交 + 同义合并。池子不属于记忆，只进 ops/。"""
    try:
        import persona_tasks as T
    except Exception as e:
        return False, "persona_tasks 不可用：%s" % e
    sent = T.flush()
    merged = T.merge_pool()
    size = T.pool_size()
    return True, "补交 %d / 合并 %d / 池中 %d" % (sent, merged, size)


def t_observe():
    """表达观察（v5.6）：只留痕，不判定。底座自带性格，没有零点。"""
    script = os.path.join(ROOT, "脚本/persona_observe.py")
    if not os.path.exists(script):
        return False, "缺 persona_observe.py"
    ok, msg = run([PY, script, "snapshot", "--days", "3"], 120)
    line = (msg or "").strip().splitlines()[0][:70] if msg else ("ok" if ok else "失败")
    return ok, line


def t_daily():
    """日度高光（v5.6）：只写当天定下的事，带来源文件；同一天覆盖重写。"""
    script = os.path.join(ROOT, "脚本/persona_daily.py")
    if not os.path.exists(script):
        return False, "缺 persona_daily.py"
    ok, msg = run([PY, script], 120)
    return ok, (msg or "").strip()[:80]


def t_inject():
    """注入告警（2026-11-10 加）：error 档写一条 ops/alerts.jsonl，同一 streak 只记一次。

    判定口径与托盘共用 persona_inject.evaluate_state（三档阈值只定义在那一处）。
    不重复刷：ops/inject-alerted.json 记已报过的 streak；成功清零后下次再坏还能再报。
    """
    try:
        import persona_inject as inj
    except Exception as e:
        return True, "persona_inject 不可用：%s" % e
    if not hasattr(inj, "evaluate_state"):
        return True, "persona_inject 是旧版（没有 evaluate_state），跳过注入告警"
    ev = inj.evaluate_state()
    try:
        with io.open(INJECT_ALERTED, encoding="utf-8") as f:
            last = json.load(f)
    except Exception:
        last = {}
    try:
        last_streak = int(last.get("streak") or 0)
    except Exception:
        last_streak = 0

    if ev["level"] != "error":
        if last_streak:                      # 恢复了：清掉"报过"标记，下次再坏还能报
            try:
                tmp = "%s.%d.tmp" % (INJECT_ALERTED, os.getpid())
                with io.open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"streak": 0, "at": now()}, f, ensure_ascii=False)
                os.replace(tmp, INJECT_ALERTED)
            except OSError:
                pass
        return True, "注入 %s（连续失败 %d）" % (ev["level"], ev["streak"])

    if last_streak == ev["streak"]:
        return True, "注入 error（连续 %d 次），本档已告警过，不重复刷" % ev["streak"]

    rec = {"at": now(), "level": "error", "kind": "inject_stalled", "src": "inject",
           "streak": ev["streak"], "since_ok_min": ev["since_min"],
           "note": ev["text"]}
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(ALERTS, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp = "%s.%d.tmp" % (INJECT_ALERTED, os.getpid())
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"streak": ev["streak"], "at": now()}, f, ensure_ascii=False)
        os.replace(tmp, INJECT_ALERTED)
    except OSError as e:
        return False, "写注入告警失败：%s" % e
    return True, "已告警：连续 %d 次发不出去（%s 分钟没成功）" % (
        ev["streak"], ev["since_min"] if ev["since_min"] is not None else "?")


def t_dsbalance():
    r"""DeepSeek 余额（2026-11-10 加）：官方接口 `GET /user/balance`，半小时一次。

    客户端里那个数字不是实时的（高消耗时会从"还有一块多"直接欠费），
    所以走官方接口、用 `DEEPSEEK_API_KEY`（守护进程开机起，读不到后加的变量——
    `persona_fallback._key_of` 会退到用户级注册表）。
    只读、不花 token；产物 `ops\ds-balance.json` 给降级判定用（低于 ¥5 就保底）。
    """
    try:
        r = subprocess.run([PY, os.path.join(ROOT, "脚本/persona_fallback.py"),
                            "balance", "--fetch"],
                           cwd=ROOT, capture_output=True,
                           timeout=TASK_TIMEOUT["dsbalance"],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = r.returncode == 0
        tail = (_dec(r.stdout).strip().splitlines() or [""])[-1]
        err = _dec(r.stderr).strip().splitlines()
        if not ok and err:
            tail = "%s｜stderr: %s" % (tail[:90], err[-1][:90])
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if not ok and "没配 DEEPSEEK_API_KEY" in tail:
        # 没钥匙就等于余额读不到，属于「本机没配」，不是故障；余额本来也允许人工记。
        return True, "跳过：没配 DEEPSEEK_API_KEY（余额可人工记）"
    try:
        with open(os.path.join(OPS, "ds-balance.json"), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("balance") is not None:
            return ok, "余额 ¥%s（%s）%s" % (
                d["balance"], d.get("at", ""), "" if ok else "｜" + tail[:120])
    except (OSError, ValueError):
        pass
    return ok, tail


def t_sfbalance():
    r"""硅基流动余额快照（2026-11-10 加）：读登录态页面，落 `ops\sf-balance.json`。

    为什么不在 API 里读：`GET /v1/user/info` 已 410 Gone，余额只在
    `cloud.siliconflow.cn/me/expensebill` 页面上。人给的页，读一次一个数字。
    频率：脚本自己有 6 小时闸门 + 24 小时最多 6 次，这里 6 小时叫一次不会多抓。
    产物给 `persona_fallback` 当余额快照：低于阈值就降级（欠费→只做本机活）。
    """
    ok, s = run([NODE, os.path.join(ROOT, "tools/js/sf_balance.js")],
                TASK_TIMEOUT["sfbalance"])
    try:
        with open(os.path.join(OPS, "sf-balance.json"), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("balance") is not None:
            return ok, "余额 ¥%s（%s）" % (d["balance"], d.get("at", ""))
    except (OSError, ValueError):
        pass
    return ok, s


def t_dr():
    r"""容灾保底（2026-11-10 加）：每分钟读 `ops\dr-plan.json`，条件到了触发预建任务。

    人定的口径：判定的规则和「条件→动作」的对应关系都在配置里，后台程序只负责读它；
    开关（plan 的 enabled）关着就只判定、只记账，不动手。
    为什么放在守护里：守护本来就是「一直转着」的那个程序；它是本地的，
    断网时也在跑。计划任务 `SysPersona-Watchdog` 是同一套逻辑的外围网（守护自己挂掉时用），
    两条线共享 `ops\dr-state.json` 的冷却，不会重复动手。
    """
    sys.path.insert(0, ROOT)
    import persona_watchdog as W
    d = W.run_once(act=True)
    why = (d.get("why") or "").strip()
    note = ""
    try:
        prof = W.session_profile(W.newest_session_id())
        if prof.get("status") == "created":
            note = " ｜ 新会话已建档"
    except Exception:                  # noqa: BLE001
        pass
    return d.get("action") == "ok", "%s ｜ %s%s" % (d.get("action"), why[:50], note)


def t_drpreflight():
    r"""容灾自检（2026-11-10 加）：三套版本在不在、当前会话的备胎是谁、任务齐不齐。

    人问的：「每次心跳唤醒还有维护任务不是有检查吗？检查当前会话是否有容灾备份配置。」
    它只读、不动手；结果落 ops\dr-preflight.json，唤醒简讯里也带一行。
    """
    sys.path.insert(0, ROOT)
    import persona_watchdog as W
    d = W.preflight()
    line = W.preflight_line()
    return bool(d.get("ok")), line[:120]


# ── 外挂脚本：把散在计划任务里的活全部收进守护（2026-11-11 人定）────────
# 人：「所有任务全部放到后台程序，计划任务只保留拉起后台的那一个——现在太分散了。」
# 所以这里一张表把原来 13 个计划任务一一对上；计划任务只剩 `SysPersona-Daemon`。
LAUNCHER = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "SysPersona")


def _ps(name):
    return ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", os.path.join(LAUNCHER, name)]


SCRIPTS = {
    "compact": [PY, "脚本/persona_compact.py", "check"],                    # 原 SysPersona-Compact 5m
    "ctxguard": _ps("ctxguard.ps1"),                                   # 原 SysPersona-CtxGuard 10m
    "heal": [PY, "脚本/persona_steps.py", "run", "all", "--act"],           # 原 SysPersona-Heal 5m
    "hermeskeeper": [PY, "脚本/persona_hermes_keeper.py", "check", "--act"],  # 原 -Hermes-Keeper 1m
    "nodedrain": [PY, "脚本/persona_node_cmd.py", "drain"],                 # 原 -NodeDrain 1m
    "reloader": [PY, "脚本/persona_constitution_reloader.py", "--reason", "startup",
                 "--only-if-needed"],                                  # 原 -Reloader 5m
    "keeperbeat": [PY, "脚本/persona_session_keeper.py", "beat", "--quiet"],  # 原 -SessionKeeper 1m
    "gate": [PY, "脚本/persona_vault_gate.py", "ensure"],
    # 每半小时把"新写出来的明文"补封一遍（Hermes 会不断重写自己的 mirror，所以是常扫不是一次）
    "sealsweep": [PY, "脚本/persona_vault_bulk.py", "seal"],                   # I/O 门常驻（原来靠托盘）
    # 云端可用 → 停本机模型服务（否则 Hermes 新会话默认钉到本地小模型，长会话装不下）
    "localmodel": [PY, "脚本/persona_localmodel.py", "ensure"],
    # 模型自动接管：主档崩了换下一档，并把客户端重新激活
    "failover": [PY, "脚本/persona_model_failover.py", "ensure"],
    "webcap": [PY, "脚本/persona_webcap.py"],                                   # 网页抓取节拍器（1h）：只下单，不抓
    # 清道夫：先搬进 清理\待删\，放满 7 天才真删；脚本自己管「一天只动一次」+「4 点后」
    "janitor": [PY, "脚本/persona_janitor.py", "daily"],
    "maint": _ps("maint.ps1"),                                         # 原 -Memory-Maint 周
    "backupzip": ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                  "-ExecutionPolicy", "Bypass", "-File", os.path.join(ROOT, "backup.ps1")],
    "repobackup": _ps("repo_backup.ps1"),                              # 原 -Repo-Backup 日
    "handoffrefresh": _ps("handoff_refresh.ps1"),                      # 原 -Handoff-Refresh 30m
    # 这三件原来在「启动」文件夹里各占一个快捷方式；现在由守护按需拉起，启动项只剩一个。
    "cdp": _ps("start_cdp_browser.ps1"),                               # 原 启动项 SysPersona-CDP
    "sfmodels": [PY, "脚本/persona_sf_models.py", "--refresh"],             # 原 启动项 SysPersona-SFModels
}


def _procs_matching(needle):
    """哪些进程的命令行里带 needle（用来判"托盘/看门狗还活着吗"）。"""
    hits = []
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*%s*' } "
             "| Select-Object -ExpandProperty ProcessId" % needle],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for ln in _dec(r.stdout).split():
            if ln.strip().isdigit():
                hits.append(int(ln))
    except Exception:                      # noqa: BLE001
        pass
    return hits


def t_tray():
    """托盘保活：没在跑就拉起来（原启动项 SysPersona-Tray 的职责）。

    2026-11-11 13:40：判重改用**命名互斥量**。原来靠命令行匹配，
    提权进程的 CommandLine 从非提权进程读出来是空的 → 每 120 秒都以为没跑
    → 重复拉起，人看到两个、三个图标。互斥量跨权限级别有效。
    """
    try:
        import persona_tray_multi as _tm
        if _tm.mutex_alive():
            return True, "托盘在跑（互斥量）"
    except Exception:                      # noqa: BLE001
        if _procs_matching("persona_tray"):
            return True, "托盘在跑（命令行）"
    pyw = os.path.join(os.path.dirname(PY), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else PY
    subprocess.Popen([exe, os.path.join(ROOT, "脚本/persona_tray_multi.py")], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return True, "托盘没在跑，已拉起"


# 「本机没装 / 没配」——不是故障，是这台机器还没有那个东西
SKIP_HINTS = (
    "没配 DEEPSEEK_API_KEY",
    "配置里找不到 provider=",
    "本机没装",
    "本机不启用",
    "没有 desktop-ui.sqlite",
    "hermes-home",
)


def _hermes_present():
    r"""这台机器到底装没装 Hermes——按 hermes_home 目录真实存在判定，不按配置里写着什么。"""
    try:
        import persona_root as _R
        h = _R.hermes_home()
        return bool(h and os.path.isdir(h))
    except Exception:                      # noqa: BLE001
        return False


# 某些任务只对「装了那个客户端」的机器有意义：没装就直接跳过，别每 5 分钟报一次红。
NEEDS = {
    "hermeskeeper": (_hermes_present, "本机没装 Hermes"),
}

# 「装了」≠「本机要用」。人 2026-11-12 定：这台小主机只有 4G 内存，
# Hermes / Herdsman / 本地模型一个都不拉——装着是装着，跑起来就把机器拖死。
# 机器配置里 `clients.<名字>.enabled=false` 写了哪个，对应任务就整条跳过，
# 理由照抄配置里的 why（不是「没装」，是「本机不用」）。
CLIENT_OF = {
    "herdsman": "herdsman",
    "localmodel": "local_model",
    "hermeskeeper": "hermes",
    "keeperbeat": "hermes",
    "failover": "hermes",
    "gate": "hermes",
    "reloader": "hermes",
    "cdp": "hermes",
}


def _client_off(name):
    r"""本机不启用这个客户端时返回理由（空串 = 启用）。"""
    key = CLIENT_OF.get(name)
    if not key:
        return ""
    try:
        import persona_root as _R
        on, why = _R.client_policy(key)
        return "" if on else (why or "本机不启用")
    except Exception:                      # noqa: BLE001
        return ""


def _off_guard(name, fn):
    r"""把「本机不启用」挡在真干活之前——herdsman 会真去拉起一个 100MB 的 exe。"""
    if name not in CLIENT_OF:
        return fn

    def task():
        off = _client_off(name)
        if off:
            return True, "跳过：本机不启用（%s）" % off
        return fn()
    task.__name__ = getattr(fn, "__name__", "t_" + name)
    return task


def _make_script_task(name):
    def task():
        # 用 -File 引的外部 ps1（ctxguard / maint / repobackup / handoffrefresh / cdp）
        # 不一定装：它们是各机器上自己放进去的。缺了就跳过，别拿 PowerShell 的启动横幅当错误。
        cmd = SCRIPTS[name]
        if "-File" in cmd:
            f = cmd[cmd.index("-File") + 1]
            if not os.path.isfile(f):
                return True, "跳过：本机没有 %s" % os.path.basename(f)
        off = _client_off(name)
        if off:
            return True, "跳过：本机不启用（%s）" % off
        need = NEEDS.get(name)
        if need and not need[0]():
            return True, "跳过（本机未配置）：" + need[1]
        ok, msg = run(SCRIPTS[name], TASK_TIMEOUT.get(name, 300))
        # 换台机器第一次跑时，缺密钥、缺 provider、缺某个客户端都会让脚本非零退出。
        # 这时报红没人能修，只会把真正的故障淹掉——统一降级成「跳过（本机未配置）」，
        # 原文留在消息里，谁想看还看得见。
        if not ok and any(h in (msg or "") for h in SKIP_HINTS):
            last = ((msg or "").strip().splitlines() or [""])[-1]
            return True, "跳过（本机未配置）：" + last[:110]
        return ok, msg
    task.__name__ = "t_" + name
    return task


for _n in SCRIPTS:
    globals()["t_" + _n] = _make_script_task(_n)


TASKS = {
    "herdsman": t_herdsman,
    "export": t_export,
    "index": t_index,
    "ctx": t_ctx,
    "watermark": t_watermark,
    "cost": t_cost,
    "selfcheck": t_selfcheck,
    "merge": t_merge,
    "scan": t_scan,
    "navwatch": t_navwatch,
    "hctx": t_hctx,
    "backup2": t_backup2,
    "handoff": t_handoff,
    "reload": t_reload,
    "capture": t_capture,
    "uiskin": t_uiskin,
    "wake": t_wake,
    "agent": t_agent,
    "reap": t_reap,
    "tasks": t_tasks,
    "observe": t_observe,
    "daily": t_daily,
    "proxy": t_proxy,
    "todo": t_todo,
    "inject": t_inject,
    "sfbalance": t_sfbalance,
    "dsbalance": t_dsbalance,
    "dr": t_dr,
    "drpreflight": t_drpreflight,
    "settings": t_settings,
    "backed": t_backed,
    "localmodel": t_localmodel,
    "failover": t_failover,
    "tray": t_tray,
    "clients": t_clients,
}

# 外挂任务登记（t_compact / t_heal / … 由循环生成）
for _n in SCRIPTS:
    TASKS[_n] = globals()["t_" + _n]

# 最后一道：显式写的那几个（herdsman / localmodel / failover …）也过「本机不启用」。
for _n in list(TASKS):
    TASKS[_n] = _off_guard(_n, TASKS[_n])


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
                data = json.load(open(CONF, encoding="utf-8"))
                if isinstance(data, dict):
                    # 只收任务间隔（数值）；路径键（capture_js/backup2_root/...）不许混进 cfg，
                    # 否则 worker 里 float(self.cfg.get(name)) 会炸（2026-11-10 实测）。
                    self.cfg.update({k: v for k, v in data.items()
                                     if k in DEFAULTS and isinstance(v, (int, float))
                                     and not isinstance(v, bool)})
            except Exception as e:
                log("配置读取失败，用默认：%s" % e)
        self.state = {k: {"last": 0, "ok": None, "msg": "never", "runs": 0, "busy": False}
                      for k in TASKS}
        self.next_due = {k: 0 for k in TASKS}
        self.load_state()
        self.started = time.time()
        self.stop = False
        self.beats = []          # 最近几次心跳的时刻，用来自测「一拍多少秒」
        self._last_export = 0.0

    def load_state(self):
        """把上次的"各任务上次跑的时间"读回来。

        为什么要有：2026-11-11 把计划任务全收进守护后，一重启就**所有任务重新到期**，
        于是 2 分钟里连打了 4 个 120MB 的记忆库备份（interval 是 1 天，本该一天一次）。
        记到 ops\\daemon-state.json，重启就接着算，不重跑。
        """
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        last = d.get("last") or {}
        runs = d.get("runs") or {}
        heavy_i = 0
        light_i = 0
        for name in TASKS:
            ts = float(last.get(name) or 0)
            if ts > 0:
                self.state[name]["last"] = ts
                self.state[name]["runs"] = int(runs.get(name) or 0)
                iv = float(self.cfg.get(name, 1) or 0)
                self.next_due[name] = ts + iv if iv > 0 else 0
                # 开机错峰（2026-11-12 人：「开机那一会大量运行，鼠标都卡」）：
                # 冷启动时所有任务的下次时间都落在过去，会一起冲。这里把重活按
                # STAGGER 秒排队，轻活只错 2 秒，避免磁盘/CPU 同时打满。
                if self.next_due[name] <= time.time() + 2:
                    if name in HEAVY_TASKS:
                        self.next_due[name] = time.time() + BOOT_GRACE + heavy_i * STAGGER
                        heavy_i += 1
                    else:
                        self.next_due[name] = time.time() + light_i * 2
                        light_i += 1
                log("恢复 %-14s 上次 %s，下次 %s 秒后" % (
                    name, time.strftime("%m-%d %H:%M", time.localtime(ts)),
                    int(max(0, self.next_due[name] - time.time()))))

    def save_state(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": now(),
                           "last": {k: v["last"] for k, v in self.state.items() if v["last"]},
                           "runs": {k: v.get("runs", 0) for k, v in self.state.items()}},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    def beat_interval(self):
        """实测心跳节拍（秒）：最近几次心跳间隔的中位数；样本不够就退回配置值。"""
        b = self.beats[-9:]
        diffs = sorted(b[i + 1] - b[i] for i in range(len(b) - 1))
        diffs = [x for x in diffs if 0 < x < 3600]
        if len(diffs) >= 2:
            return diffs[len(diffs) // 2]
        return float(self.cfg.get("heartbeat", 60) or 60)

    def status(self, extra=None):
        if _IS_DAEMON or not self.beats:
            self.beats.append(time.time())
            self.beats = self.beats[-9:]
        d = {
            "at": now(),
            "uptime_s": int(time.time() - self.started),
            # pid 只报「真正常驻的那个」：--once 跑一拍时不能把守护的 pid 顶掉——
            # 看门狗靠这个 pid 判守护死活，2026-11-10 实测被顶掉过（误判 → 起了第二个守护）。
            "pid": _resident_pid(),
            "beat_s": round(self.beat_interval(), 1),
            "beat_cfg": float(self.cfg.get("heartbeat", 60) or 60),
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
        # 优先级仲裁：登记为 job 的任务，动手前先看闹钟有没有占位。
        # 让位也照常推进 next_due（任务频率不变），日志里能看到「让位给闹钟：…」。
        job = None
        if _sched:
            try:
                job = _sched.job_for_task(name, root=ROOT)
            except Exception:
                job = None
        if job:
            try:
                skip, reason = _sched.should_skip(job, "daemon", root=ROOT)
            except Exception:
                skip, reason = False, ""
            if skip:
                dt = time.time() - t0
                self.state[name].update(last=time.time(), ok=True, msg=reason, busy=False,
                                        runs=self.state[name]["runs"] + 1)
                self.next_due[name] = time.time() + float(self.cfg.get(name, 3600))
                self.save_state()
                log("TASK %-9s %-4s %5.1fs  %s" % (name, "OK", dt, reason))
                return
            try:
                # 开跑前请求抢占：更低优先级的运行中任务（空闲循环）下一轮询让位。
                _sched.request_preempt(job, _sched.priority(job, "daemon", root=ROOT),
                                       owner="daemon", root=ROOT)
            except Exception:
                pass
        try:
            ok, msg = fn()
        except Exception:
            ok, msg = False, traceback.format_exc(limit=2).splitlines()[-1][:160]
        dt = time.time() - t0
        self.state[name].update(last=time.time(), ok=ok, msg=msg, busy=False,
                                runs=self.state[name]["runs"] + 1)
        self.next_due[name] = time.time() + float(self.cfg.get(name, 3600))
        self.save_state()
        # 反向不占位：守护跑完只写一条租约供 status 观察；优先级低，闹钟侧天然不受阻。
        if job and ok:
            try:
                ttl = float(self.cfg.get(name, 3600))
                _sched.claim(job, "daemon", ttl=ttl,
                             next_due=time.time() + ttl, root=ROOT)
            except Exception:
                pass
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
                self.save_state()
                heart_next = t + float(self.cfg.get("heartbeat", 60))
                # 心跳必带时间（2026-11-11 人定）：做没做事都写一条，缺席本身要被看见
                try:
                    import persona_heartbeat as HB
                    HB.beat("daemon", True, "tick")
                except Exception:          # noqa: BLE001
                    pass

            # 会话库一变就把 export 提前：安静 3 秒 = 这一轮说完了
            try:
                st = os.stat(HERMES_DB) if HERMES_DB else None
                cur = (st.st_mtime, st.st_size) if st else None
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

            self.cfg["dr"] = max(3.0, self.beat_interval())   # 容灾巡检 = 一拍一次
            for name in TASKS:
                st = self.state[name]
                if float(self.cfg.get(name, 1) or 0) <= 0:
                    continue            # 间隔 0 = 停用这个任务（配置里写 0 就关掉）
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


_IS_DAEMON = False          # 常驻分支里置 True；--once 时保持 False


def _pid_alive(pid):
    """进程还在吗（ctypes 直问内核，不依赖 tasklist / wmic）。"""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not h:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259      # STILL_ACTIVE
    except Exception:                              # noqa: BLE001
        return False


def _resident_pid():
    """status 里该报哪个 pid：常驻守护的，不是「跑一拍就退」的那个。"""
    if _IS_DAEMON:
        return os.getpid()
    cands = []
    try:
        cands.append(int(open(LOCK).read().strip()))
    except (OSError, ValueError):
        pass
    try:
        with open(STATUS, encoding="utf-8") as f:
            p = json.load(f).get("pid")
        if isinstance(p, int):
            cands.append(p)
    except (OSError, ValueError, AttributeError):
        pass
    for pid in cands:
        if _pid_alive(pid):
            return pid
    return os.getpid()


# ── 单实例：全局命名互斥量（2026-11-11 14:37） ──
# 原来只靠 pid 锁文件 + os.kill(pid,0)。问题是：提权进程的 pid 从非提权进程
# 查活会失败，于是以为没在跑→ 再拉一个，现场出现过 4 个守护互刷状态。
# 互斥量跨权限级别都认，和托盘用同一套办法。
DAEMON_MUTEX = r"Global\SysPersonaDaemon-v1"
_ERROR_ALREADY_EXISTS = 183
_daemon_mutex_handle = None


def _daemon_mutex_alive():
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenMutexW(0x00100000, False, DAEMON_MUTEX)
        if h:
            k32.CloseHandle(h)
            return True
        return ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    except Exception:                      # noqa: BLE001
        return False


def _claim_daemon_mutex():
    global _daemon_mutex_handle
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, DAEMON_MUTEX)
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            if h:
                k32.CloseHandle(h)
            return False
        _daemon_mutex_handle = h
        return bool(h)
    except Exception:                      # noqa: BLE001
        return True                        # 拿不到就当能跑，别把守护弄没了


def single_instance():
    os.makedirs(OPS, exist_ok=True)
    if not _claim_daemon_mutex():
        return False
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK).read().strip())
            os.kill(pid, 0)  # 存在且活着
            if pid != os.getpid() and _pid_alive(pid):
                return False
        except (OSError, ValueError):
            pass
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    return True


def daemon_pid():
    """守护自己的 pid（活着才算，否则 0）。"""
    try:
        with open(LOCK, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        return pid
    return 0


def ensure():
    """计划任务唯一入口：守护不在就跑起来，在就什么都不做。

    人 2026-11-11：「所有任务全部放到后台程序，计划任务只保留拉起后台的那一个。」
    所以计划任务只剩 `SysPersona-Daemon`，每 5 分钟叫一次这里。
    """
    if _daemon_mutex_alive():
        return True, "守护已在跑（互斥量）"
    pid = daemon_pid()
    if pid:
        return True, "守护已在跑（pid %d）" % pid
    pyw = os.path.join(os.path.dirname(PY), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else PY
    subprocess.Popen([exe, os.path.abspath(__file__), "--daemon"], cwd=ROOT,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return True, "守护没在跑，已拉起（%s）" % os.path.basename(exe)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--ensure", action="store_true")
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

    if a.ensure:
        # 2026-11-11 14:38 修：--ensure 原来根本没被处理，计划任务每 5 分钟
        # 直接跑起一个**完整守护**并一直驻留 —— 这就是越跑越多的真凶。
        ok, msg = ensure()
        print(("OK   " if ok else "NO   ") + msg)
        sys.exit(0 if ok else 1)

    k = PersonaDaemon()
    if a.once:
        if a.once not in TASKS:
            raise SystemExit("未知任务：%s（可选 %s）" % (a.once, ", ".join(TASKS)))
        sys.exit(k.run_once(a.once))

    if not single_instance():
        print("已有实例在跑（见 %s）" % LOCK)
        sys.exit(2)
    globals()["_IS_DAEMON"] = True          # status() 里据此报「本进程就是守护」
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
