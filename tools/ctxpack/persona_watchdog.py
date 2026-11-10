# -*- coding: utf-8 -*-
r"""persona_watchdog.py — 看门狗 + 容灾保底（一个显式开关，打开后才自动动手）

人 2026-11-10 定（原话）：
  「如果心跳没有了，网络是通的，这个时候要干什么？去 ping 一下当前使用模型的服务器
    是否连通。然后切换下一个可用的模型；当网络不通时才会触发保底。」
  「这个是容灾保底，需要的是一个显式的开关，打开后，容灾自动进行。」
  「计划任务不同的脚本，你要提前创建好，然后后台程序去执行，因为如果断网，你就没法执行。」

三条线分清楚：
  * **判定**（check）：心跳 → 网络 → 当前模型服务器 → 决定动作。永远只读，不碰系统。
  * **开关**（dr on/off）：`ops\dr-switch.json` 里的 `on`。默认关，关着只判定。
  * **执行**（enact）：每个动作都是一个**提前建好的计划任务**，现场只 `schtasks /Run`。
    断网时云端模型叫不动，所以现场这一步不能依赖任何联网或云端回话的东西。

    心跳还在？ ── 是 ──→ OK（什么都不做）；但客户端进程都没了 → 拉起客户端
        │
        否
        ↓
    守护进程在吗？ ── 否 ──→ 只拉起守护（不碰客户端），下一拍再看
        │ 在
        ↓
    网络通吗？
        ├─ 不通 ──→ ① 保底：切本地模型（断网只有这一条路）
        └─ 通
             ↓
        按「模型链」从当前模型的下一格往后找第一个通的：
             deepseek（收费） → 硅基免费 → 本地
             ├─ 找到别的 ──→ ② 切过去（杀 → 改配置 → 拉起）
             └─ 一个都不通 ──→ ① 保底：切本地
             当前那格就通 ──→ ③ 只是本地卡死：只重启，配置不动

命令：
  python persona_watchdog.py install     # 一次性：把 6 个任务全建好（可反复跑）
  python persona_watchdog.py tasks       # 看这 6 个任务的状态 / 上次结果
  python persona_watchdog.py dr on|off|status
  python persona_watchdog.py check [--act] [--dry]
  python persona_watchdog.py verify      # 真实演练一次「杀 + 拉起」（会断窗口几秒）
  python persona_watchdog.py enact local|free|paid|restart|daemon [--dry]
                                         # 分支本体，平时只由计划任务调用

安全闸（都在，且写进日志）：
  * 默认关；开关文件不存在也按「关」处理；
  * `ops\dr-hold` 文件存在 → 一律不动手（人按住它做实验时用）；
  * 冷却 600 秒、每小时最多 3 次，防止「反复杀 / 反复起」；
  * 心跳 = `persona.log` mtime 与 `persona-status.json` 的 at 取**较新**者，阈值 300 秒；
  * 客户端进程名 / 安装路径都从环境变量推，不写死（见 CLIENT_* 常量）。
"""
import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
LOG = os.path.join(OPS, "persona.log")
OUT = os.path.join(OPS, "watchdog.jsonl")
FB_CFG = os.path.join(OPS, "fallback.json")
SWITCH = os.path.join(OPS, "dr-switch.json")      # 开关的镜像（兼容读它的老代码）
PLAN = os.path.join(OPS, "dr-plan.json")          # **计划配置文件**：后台程序读它
STATE = os.path.join(OPS, "dr-state.json")        # 执行台账（冷却 / 次数）
HOLD = os.path.join(OPS, "dr-hold")               # 蹲住闸：有它就绝不动手
TASK_DIR = os.path.join(OPS, "tasks")             # 计划任务的 XML 配置（提前落盘）
NET_STATE = os.path.join(OPS, "dr-net.json")      # 网络连续不通的拍数
PY_EXE = sys.executable
PYW = os.path.join(os.path.dirname(PY_EXE), "pythonw.exe")

# 客户端位置：从环境变量拼，不写死盘符（%LOCALAPPDATA%\Programs\DeepSeek Harness）
CLIENT_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs",
                          "DeepSeek Harness")
CLIENT_EXE = os.path.join(CLIENT_DIR, "DeepSeek Harness.exe")
CLIENT_IMG = os.path.basename(CLIENT_EXE)

DEFAULT_CHAIN = [
    {"id": "deepseek", "label": "云端主（DeepSeek 收费）", "cmd": "paid",
     "provider": "deepseek-account", "model": "deepseek-flash",
     "probe": "api.deepseek.com:443", "cost": "收费"},
    {"id": "sf_free", "label": "云端备（硅基免费档）", "cmd": "free",
     "provider": "siliconflow", "model": "Qwen/Qwen2.5-7B-Instruct",
     "probe": "api.siliconflow.cn:443", "cost": "免费"},
    {"id": "local", "label": "本地（Herdsman 127.0.0.1:8080）", "cmd": "local",
     "provider": "herdsman-local", "model": "Qwen3:8B",
     "probe": "127.0.0.1:8080", "cost": "电费"},
]


# ---------------------------------------------------------------- 小工具
def _read_json(p, default=None):
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def cfg():
    return _read_json(FB_CFG, {}) or {}


def chain():
    c = (cfg().get("dr") or {}).get("chain")
    return c if isinstance(c, list) and c else DEFAULT_CHAIN


def heartbeat_age():
    """心跳 = 后台「还在动」的证据，取两个证据里最新的那个：

      * `ops\\persona.log` 的 mtime（守护每拍追加）
      * `ops\\persona-status.json` 的 at 字段（守护每拍刷新）

    只看一个会误判：某拍只写了状态没写日志，就会被当成「死了」而去杀客户端。
    两个都停 = 守护卡住或没了，那才是真该动手的时候。
    """
    ages = []
    try:
        ages.append(time.time() - os.path.getmtime(LOG))
    except OSError:
        pass
    st = _read_json(os.path.join(OPS, "persona-status.json"), {}) or {}
    at = _parse_ts(st.get("at"))
    if at:
        ages.append(time.time() - at)
    return min(ages) if ages else 1e9


def heartbeat_interval():
    """一拍多少秒：读守护自测的 beat_s；没有就退回它配置的 heartbeat；再没有 60。"""
    st = _read_json(os.path.join(OPS, "persona-status.json"), {}) or {}
    for k in ("beat_s", "beat_cfg"):
        v = st.get(k)
        if isinstance(v, (int, float)) and 1 <= v <= 3600:
            return float(v)
    return 60.0


def stale_after(stale=None):
    """判定阈值：显式给的秒数 > 计划里的 stale_after > 节拍 × missing_beats。"""
    if stale is not None:
        return float(stale)
    p = plan()
    fixed = p.get("stale_after")
    if isinstance(fixed, (int, float)) and fixed > 0:
        return float(fixed)
    return max(60.0, heartbeat_interval() * float(p.get("missing_beats", 3) or 3))


def net_fails(alive):
    """网络连续几拍不通了（跨进程记在 ops\\dr-net.json；通了就清零）。

    为什么要有它：断网时守护照样发心跳，光看心跳永远等不到「死」——
    所以断网必须自己数拍子，连续 N 拍不通才动手（N 在 dr-plan.json 的 net_fail_beats）。
    """
    st = _read_json(NET_STATE, {}) or {}
    n = 0 if alive else int(st.get("fails") or 0) + 1
    _write_json(NET_STATE, {"fails": n, "alive": bool(alive),
                            "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return n


def _parse_ts(s):
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _tcp(ep, timeout=1.2):
    host, _, port = str(ep).partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 443)),
                                      timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def probe(targets, timeout=1.2):
    targets = [t for t in (targets or []) if str(t).strip()]
    alive = [t for t in targets if _tcp(t, timeout)]
    return targets, alive


def http_ok(url, timeout=2.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:                      # noqa: BLE001
        return False


_PROC_CACHE = {"at": 0.0, "rows": None}


def proc_table(force=False):
    """[(pid, 镜像名小写)]，5 秒内复用（一次 check 会问好几遍）。"""
    now = time.time()
    if not force and _PROC_CACHE["rows"] is not None and now - _PROC_CACHE["at"] < 5:
        return _PROC_CACHE["rows"]
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=25,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = (out.stdout or b"").decode("mbcs", "replace")
        rows = []
        for ln in txt.splitlines():
            if not ln.startswith('"'):
                continue
            parts = ln.split('","')
            if len(parts) >= 2:
                try:
                    rows.append((int(parts[1].strip('"')), parts[0].strip('"').lower()))
                except ValueError:
                    pass
    except Exception:                      # noqa: BLE001
        rows = []
    _PROC_CACHE.update({"at": now, "rows": rows})
    return rows


def _pid_alive(pid):
    """进程还在吗：ctypes 直问内核，不依赖 tasklist / wmic（后者在计划任务里不一定可用）。"""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259
    except Exception:                      # noqa: BLE001
        return False


def daemon_pid():
    """守护的 pid：先看 persona.lock（常驻进程启动时写），再看 persona-status.json。

    两个都看，是因为 `--once` 跑一拍时 status.json 里的 pid 会被那个短命进程顶掉。
    """
    at = ""
    st = _read_json(os.path.join(OPS, "persona-status.json"), {}) or {}
    at = st.get("at") or ""
    cands = []
    try:
        cands.append(int(io.open(os.path.join(OPS, "persona.lock")).read().strip()))
    except (OSError, ValueError):
        pass
    if isinstance(st.get("pid"), int):
        cands.append(st["pid"])
    for pid in cands:
        if _pid_alive(pid):
            return pid, at
    return (cands[0] if cands else 0), at


def daemon_alive():
    """进程在 = 活着（在但心跳不动 = 卡住了，这两种要分开）。"""
    pid, _ = daemon_pid()
    return bool(pid) and _pid_alive(pid)


def client_alive():
    rows = proc_table()
    if not rows:                           # tasklist 没跑起来：判不了就别当成「客户端没了」
        return True
    return any(n == CLIENT_IMG.lower() for _, n in rows)


# ------------------------------------------------- 计划配置（提前写好，后台读它执行）
# 人 2026-11-10 定：「最好是，提前写好配置文件给后台程序，读，条件到了，自动运行。」
# 所以：**一份 `ops\dr-plan.json` 就是全部规则**——开关、阈值、模型链、每个条件对应触发哪个任务、
# 以及要预建的任务清单本身。后台程序（计划任务，每分钟）读它；条件到了只做 `schtasks /Run`。
# 改规则 = 改这一份文件，不用改代码、不用我（云端）在场。
def default_plan():
    T = {
        "watchdog": "SysPersona-Watchdog",
        "restart": "SysPersona-DR-Restart",
        "daemon": "SysPersona-DR-Daemon",
        "local": "SysPersona-DR-Local",
        "free": "SysPersona-DR-Free",
        "paid": "SysPersona-DR-Paid",
    }
    return {
        "note": "容灾计划：后台程序每分钟读这一份；条件到了按 task 触发（任务要先用 install 建好）。"
                "改这里不用改代码。",
        "enabled": False,                  # 总开关；dr on / dr off 就是改这一行
        "missing_beats": 3,              # 心跳允许漏几拍才算死（按实测节拍算）
        "net_fail_beats": 2,             # 网络连续几拍不通就保底（断网不受心跳影响）
        "stale_after": None,             # 显式覆盖秒数；None = 用 节拍 × 漏拍数
        "cooldown": 600,                   # 两次动手之间最少隔多少秒
        "max_per_hour": 3,                 # 每小时最多动手几次
        "hold_file": "ops/dr-hold",        # 有这个文件就一律不动手
        "chain": DEFAULT_CHAIN,            # 模型链：当前模型挂了往下一格找
        "branches": {                      # 判定结果 → 触发哪个（预建好的）任务
            "revive_daemon": {"task": T["daemon"],
                              "why": "守护没了：只拉守护，不碰窗口"},
            "fallback_local": {"task": T["local"],
                               "why": "网络不通：保底切本地模型"},
            "restart_client": {"task": T["restart"],
                               "why": "只是本地卡死：重启客户端，配置不动"},
            "switch_backup_cloud": {"task_by_target": {"local": T["local"],
                                                       "free": T["free"],
                                                       "paid": T["paid"]},
                                    "why": "当前服务器不通：切链上下一个可用"},
        },
        "tasks": [                         # 要预建的任务（install 按这张表建 + 落 XML）
            {"name": T["watchdog"], "args": "check --act",
             "schedule": ["/SC", "MINUTE", "/MO", "1"],
             "note": "每 1 分钟判定一次；开关开着才动手"},
            {"name": T["restart"], "args": "enact restart",
             "note": "只重启客户端（配置不动）"},
            {"name": T["daemon"], "args": "enact daemon",
             "note": "只拉起守护，不碰窗口"},
            {"name": T["local"], "args": "enact local",
             "note": "切本地模型（127.0.0.1:8080）再重启"},
            {"name": T["free"], "args": "enact free",
             "note": "切硅基免费档再重启"},
            {"name": T["paid"], "args": "enact paid",
             "note": "切回 DeepSeek 收费档再重启"},
        ],
    }


def plan():
    """读计划文件；缺的键用默认值补齐（老的配置文件也能用）。"""
    d = _read_json(PLAN, None)
    base = default_plan()
    if not isinstance(d, dict):
        return base
    for k, v in base.items():
        if k not in d:
            d[k] = v
    return d


def save_plan(d):
    _write_json(PLAN, d)


def ensure_plan():
    """发现没有计划文件就写一份默认的（install 时调）。"""
    if not os.path.exists(PLAN):
        save_plan(default_plan())
        return True
    return False


# ---------------------------------------------------------------- 显式开关
def switch():
    d = plan()
    return {"on": bool(d.get("enabled")), "at": d.get("at") or "",
            "by": d.get("by") or "", "note": d.get("note") or ""}


def set_switch(on, by="cli"):
    d = plan()
    d["enabled"] = bool(on)
    d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["by"] = by
    save_plan(d)
    # 镜像一份，给还在读 dr-switch.json 的旧代码（唤醒简讯）
    _write_json(SWITCH, {"on": bool(on), "at": d["at"], "by": by,
                         "note": "镜像：真开关在 ops\\dr-plan.json 的 enabled"})
    return d


# ---------------------------------------------------------------- 判定
def current_model():
    """从 patch 读当前默认模型（provider / model）。"""
    p = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop",
                     "cordis.patch.yml")
    prov = model = ""
    try:
        with io.open(p, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return "", ""
    in_block = False
    for ln in txt.splitlines():
        if ln.startswith("- id: agent-default-model"):
            in_block = True
            continue
        if in_block:
            if ln.startswith("- id:"):
                in_block = False
            elif ln.strip().startswith("provider:"):
                prov = ln.split(":", 1)[1].strip()
            elif ln.strip().startswith("model:"):
                model = ln.split(":", 1)[1].strip()
    return prov, model


def decide(stale=None, net_override=None, prov_override=None):
    """返回 dict：{action, why, ...}。只读，不动手。阈值取自 dr-plan.json。

    action ∈ ok | revive_daemon | fallback_local | switch_backup_cloud |
             restart_client
    """
    ch = chain()
    age = heartbeat_age()
    stale = stale_after(stale)
    d = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "heartbeat_age": round(age, 1),
         "stale_after": stale, "beat_s": heartbeat_interval(),
         "switch": switch()["on"]}
    p = plan()

    # 网线探针每拍都打：断网这条要独立算，不能等心跳死（守护在断网时心跳照常，等不来）
    net_t = ([net_override] if net_override else
             (cfg().get("offline_targets") or ["223.5.5.5:443"]))
    net_t, net_up = probe(net_t)
    d["network"] = {"targets": net_t, "alive": net_up}
    pf = net_fails(bool(net_up))
    d["net_fails"] = pf

    cur_p, cur_m = current_model()
    d["current"] = {"provider": cur_p, "model": cur_m}
    local = next((c for c in ch if c.get("cmd") == "local"), ch[-1])
    is_local = cur_p == local.get("provider")

    if age <= stale:
        d.update({"action": "ok", "why": "心跳正常（%.0f 秒前）" % age})
        need = int(p.get("net_fail_beats", 2) or 2)
        if not net_up and pf >= need:
            if is_local:
                d.update({"why": "断网中（连续 %d 拍不通），但已经是本地档 → 不动" % pf})
            else:
                d.update({"action": "fallback_local", "target": local,
                          "why": "网络连续 %d 拍不通（心跳还在）→ 保底：切本地模型" % pf})
        elif not client_alive():           # 心跳正常但客户端没了：崩了 / 被人关了
            d["client_alive"] = False
            d.update({"action": "restart_client",
                      "why": "心跳正常但客户端进程不在了 → 重新拉起"})
        return d

    d["daemon_alive"] = daemon_alive()
    d["client_alive"] = client_alive()

    if not net_up:
        d.update({"action": "fallback_local", "target": local,
                  "why": "心跳 %.0f 秒没动 + 网络不通 → 保底：切本地模型" % age})
        return d

    # 有网：按链找「下一个可用」。
    # --prov 只覆盖「当前模型」那格的探针（模拟它自己挂了，别的还活）。
    i = next((k for k, c in enumerate(ch)
              if c.get("provider") == cur_p and c.get("model") == cur_m), 0)
    alive = []
    for k, c in enumerate(ch):
        ep = c.get("probe")
        if prov_override and k == i:
            ep = prov_override
        alive.append(bool(ep) and _tcp(ep))
    d["chain"] = [{"id": c.get("id"), "probe": c.get("probe"), "alive": a}
                  for c, a in zip(ch, alive)]
    if alive[i]:
        d.update({"action": "restart_client",
                  "why": "心跳 %.0f 秒没动 + 有网 + 当前模型服务器通 → 只是本地卡死：重启客户端" % age})
        return d

    order = list(range(i + 1, len(ch))) + list(range(0, i))
    nxt = next((k for k in order if alive[k]), None)
    if nxt is None:
        tgt = next((c for c in ch if c.get("cmd") == "local"), ch[-1])
        d.update({"action": "fallback_local", "target": tgt,
                  "why": "心跳 %.0f 秒没动 + 有网但所有云端点都不通 → 保底：切本地" % age})
        return d
    tgt = ch[nxt]
    if tgt.get("provider") == cur_p and tgt.get("model") == cur_m:
        d.update({"action": "restart_client", "target": tgt,
                  "why": "心跳 %.0f 秒没动 + 当前模型服务器通 → 只是本地卡死：重启客户端" % age})
        return d
    d.update({"action": "switch_backup_cloud", "target": tgt,
              "why": "心跳 %.0f 秒没动 + 有网 + 当前服务器不通 → 换下一个可用：%s"
                     % (age, tgt.get("label"))})
    return d


# ---------------------------------------------------------------- 执行
def _run(cmd, timeout=180):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
        return r.returncode == 0, txt.replace("\r", "").splitlines()[-1][:200] if txt else ""
    except Exception as e:                 # noqa: BLE001
        return False, str(e)[:160]


def switch_model(cmd, model=""):
    args = [PY_EXE, os.path.join(ROOT, "persona_model_switch.py"), cmd]
    if model:
        args += ["--model", model]
    return _run(args)


def session_model_sync(drop_reasoning=True):
    r"""把「当前会话记住的模型」也改成配置里的那个。

    不做这一步的话，配置改了也白改：会话开窗时恢复的是它自己记着的 lastUsed
    （~\\.dsh\\storages\\session_projcache\\sessions\\session-<id>.json）。
    """
    prov, model = current_model()
    if not (prov and model):
        return False, "读不到当前配置里的 provider/model"
    args = [PY_EXE, os.path.join(ROOT, "persona_session_model.py"), "set", prov, model]
    if drop_reasoning:
        args.append("--drop-reasoning")
    return _run(args)


def revive_daemon():
    """把守护拉起来。用 Popen 起（`subprocess.run` 会一直等守护退出 → 任务挂住）。"""
    if daemon_alive():
        return True, "守护本来就在"
    env = dict(os.environ)
    env.setdefault("SYSPERSONA_ROOT", ROOT)
    try:
        subprocess.Popen([PYW, os.path.join(ROOT, "persona_daemon.py"), "--daemon"],
                         cwd=ROOT, env=env, close_fds=True,
                         creationflags=0x00000008,      # DETACHED_PROCESS
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:                 # noqa: BLE001
        return False, "拉起失败：%s" % e
    for _ in range(20):
        time.sleep(1.0)
        if daemon_alive():
            return True, "已拉起"
    return False, "起了但 20 秒内没看到守护"


def kill_client(wait=25.0):
    """只杀，不拉。杀不干净就返回失败，免得开两份。"""
    if not client_alive():
        return True, "客户端本来就没在跑"
    _run(["taskkill", "/F", "/T", "/IM", CLIENT_IMG], timeout=40)
    t0 = time.time()
    while time.time() - t0 < wait and client_alive():
        time.sleep(0.5)
    if client_alive():
        return False, "杀了 %0.f 秒还在跑" % (time.time() - t0)
    return True, "已退出（%.0f 秒）" % (time.time() - t0)


def launch_client(wait=25.0):
    """只拉，不杀。"""
    if not os.path.exists(CLIENT_EXE):
        return False, "找不到客户端可执行文件：%s" % CLIENT_EXE
    env = dict(os.environ)
    env.setdefault("HERDSMAN_API_KEY", "local-placeholder")
    flags = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([CLIENT_EXE], cwd=CLIENT_DIR, env=env,
                         close_fds=True, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:                 # noqa: BLE001
        return False, "拉起失败：%s" % e
    t0 = time.time()
    while time.time() - t0 < wait:
        if client_alive():
            return True, "已拉起（%.0f 秒）" % (time.time() - t0)
        time.sleep(1.0)
    return False, "已启动但 %0.f 秒内没看到进程" % wait


def restart_client(wait=25.0):
    """杀 → 拉起（不改配置）。配置那一档走 enact 的三步。"""
    ok, msg = kill_client(wait)
    if not ok:
        return False, msg
    return launch_client(wait)


# ------------------------------------------------------- 计划任务（提前建好，现场只触发）
# 人 2026-11-10 定：「计划任务不同的脚本，你要提前创建好，然后后台程序去执行，
# 因为如果断网，你就没法执行。」「最好是，提前写好配置文件给后台程序，读，条件到了，自动运行。」
# 所以：任务清单本身在 `ops\dr-plan.json` 的 `tasks` 里，条件与动作的对应在 `branches` 里；
# 代码只负责两件事——按表建任务（install），按 branches 触发（execute）。
_ONCE_ST = "04:00"                          # 手动任务用的一次性时间（2099 年，永不自动跑）
_ONCE_SD = "2099/01/01"


def plan_tasks():
    return [t for t in (plan().get("tasks") or []) if t.get("name")]


SWITCH_TASK = {"local": "SysPersona-DR-Local", "free": "SysPersona-DR-Free",
               "paid": "SysPersona-DR-Paid"}


def action_task(d):
    """判定 → 要触发的任务名（空串表示不用动手）。规则来自配置，不写死。"""
    br = (plan().get("branches") or {}).get(d["action"]) or {}
    if d["action"] == "switch_backup_cloud":
        cmd = (d.get("target") or {}).get("cmd") or "free"
        return ((br.get("task_by_target") or {}).get(cmd)) or br.get("task") or ""
    return br.get("task") or ""


def _task_cmdline(args):
    return '"%s" "%s" %s' % (PYW, os.path.abspath(__file__), args)


def task_exists(name):
    ok, _ = _run(["schtasks", "/Query", "/TN", name], timeout=20)
    return ok


def task_run(name):
    return _run(["schtasks", "/Run", "/TN", name], timeout=30)


def task_info(name):
    """(状态, 上次运行, 上次结果)，读不到就空。"""
    ok, _ = _run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"], timeout=20)
    info = {}
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"],
                             capture_output=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for ln in (out.stdout or b"").decode("mbcs", "replace").splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                info[k.strip()] = v.strip()
    except Exception:                      # noqa: BLE001
        pass
    return (info.get("状态") or info.get("Status") or ("已建" if ok else "缺失"),
            info.get("上次运行时间") or info.get("Last Run Time") or "",
            info.get("上次结果") or info.get("Last Result") or "")


def export_task(name):
    r"""把已建好的任务导出成 `ops\tasks\<名字>.xml`（提前落盘，现场可 `/XML` 导入）。

    人 2026-11-10 说：「或者提取创建好计划任务的配置文件，需要拉起时，
    后台程序把配置加入，然后执行。」——XML 就是那份「配置」。
    它比 `schtasks /Create` 那串命令行更耐久：任务被删、机器换人、脚本挪了位置，
    后台程序只要 `schtasks /Create /TN <名> /XML <那份>` 就能把任务原样装回去。
    """
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"],
                           capture_output=True, timeout=25,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not (r.stdout or b"").strip():
            return False, "导出失败：%s" % (r.stdout or b"").decode("mbcs", "replace")[:80]
        os.makedirs(TASK_DIR, exist_ok=True)
        p = os.path.join(TASK_DIR, name + ".xml")
        with open(p, "wb") as f:            # schtasks 的 XML 是 UTF-16，原样落盘才能原样导入
            f.write(r.stdout)
        return True, p
    except Exception as e:                 # noqa: BLE001
        return False, str(e)[:120]


def import_task(name):
    r"""从 `ops\tasks\<名字>.xml` 把任务装回去（现场兜底用）。"""
    p = os.path.join(TASK_DIR, name + ".xml")
    if not os.path.exists(p):
        return False, "没有 XML：%s" % p
    return _run(["schtasks", "/Create", "/TN", name, "/XML", p, "/F"], timeout=40)


def install(quiet=False):
    """按 `dr-plan.json` 的 tasks 表把任务全建好，并把 XML 一起导出（幂等）。"""
    rows = []
    if ensure_plan() and not quiet:
        print("没有计划文件，已写一份默认的：%s" % PLAN)
    for t in plan_tasks():
        name, args = t["name"], t.get("args") or ""
        sched, note = t.get("schedule"), t.get("note") or ""
        cmd = ["schtasks", "/Create", "/TN", name, "/TR", _task_cmdline(args), "/F"]
        cmd += sched if sched else ["/SC", "ONCE", "/ST", _ONCE_ST, "/SD", _ONCE_SD]
        ok, msg = _run(cmd, timeout=40)
        ok = ok and task_exists(name)
        xml_ok, _ = (export_task(name) if ok else (False, "-"))
        rows.append((name, ok and xml_ok, note if ok else msg))
        if not quiet:
            print("%s %-28s %s%s" % ("✔" if (ok and xml_ok) else "✘", name,
                                     note if ok else msg,
                                     (" ｜ XML 已落盘" if xml_ok else " ｜ XML 缺失")))
    return rows


def ensure_tasks():
    """现场兜底：任务被人删了，就先从 XML 装回来；XML 也没有才重建。"""
    fixed = []
    for t in plan_tasks():
        name = t["name"]
        if task_exists(name):
            continue
        ok, _ = import_task(name)
        if not ok:
            install(quiet=True)
        fixed.append(name)
    return fixed


def enact(what, dry=False):
    """分支本体：由计划任务调用，自带「杀 → 改配置 → 拉起」全流程。

    这里是**唯一**真动手的地方；看门狗只负责判定 + 触发任务。
    """
    # 顺序：先杀 → 再改配置 → 再拉起（人 2026-11-10 纠的：反了）
    # 先改配置的话，正在跑的那个进程还可能把文件写回去，改了也白改。
    todo = {"daemon": [("拉起守护", "daemon")],
            "restart": [("杀客户端", "kill"), ("拉起客户端", "launch")],
            "local": [("杀客户端", "kill"), ("改配置 → 本地档", "switch"),
                       ("本会话也切到本地", "session"), ("拉起客户端", "launch")],
            "free": [("杀客户端", "kill"), ("改配置 → 硅基免费档", "switch"),
                      ("本会话也切到硅基", "session"), ("拉起客户端", "launch")],
            "paid": [("杀客户端", "kill"), ("改配置 → DeepSeek 收费档", "switch"),
                      ("本会话也切回 DeepSeek", "session"), ("拉起客户端", "launch")]}.get(what)
    if todo is None:
        print("未知动作：%s（可选 daemon/restart/local/free/paid）" % what)
        return 2
    out = []
    for label, kind in todo:
        if dry:
            out.append({"step": label, "ok": None, "msg": "（dry：没执行）"})
            continue
        if kind == "daemon":
            ok, msg = revive_daemon()
        elif kind == "kill":
            ok, msg = kill_client()
        elif kind == "switch":
            time.sleep(0.5)                # 等文件句柄彻底放开
            ok, msg = switch_model(what)
        elif kind == "session":
            ok, msg = session_model_sync(drop_reasoning=(what != "paid"))
        else:
            time.sleep(1.5)                # 让触发我们的人先退干净
            ok, msg = launch_client()
        out.append({"step": label, "ok": ok, "msg": msg})
        if not ok and kind in ("daemon", "kill"):
            break                          # 没杀干净就别改、也别开第二份
        if not ok and kind == "switch":
            continue                       # 配置没改成就按原配置拉起，别留个死窗口
    try:
        with io.open(os.path.join(OPS, "dr-actions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "what": what,
                                "steps": out}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    for x in out:
        print("%s：%s" % (x["step"], x["msg"] or ("ok" if x["ok"] else "失败")))
    if dry:
        return 0
    return 0 if all(x["ok"] for x in out) else 4


def pick(which, dry=False):
    """手动选一个模型档并执行：cloud（云端免费）/ local（本地）/ paid（云端收费）。

    人 2026-11-10 要的：「在后台程序添加切换云端模型、切换本地模型两个选择，
    对应不同的配置，我手动选择执行，验证生效了，你再用保底心跳测试。」
    顺序和容灾一致：先关窗口 → 改配置 → 再拉起。
    """
    m = {"cloud": ("free", "云端 · 硅基免费档（Qwen/Qwen2.5-7B-Instruct）"),
         "local": ("local", "本地 · Herdsman（Qwen3:8B，127.0.0.1:8080）"),
         "paid": ("paid", "云端主 · DeepSeek 收费（deepseek-flash）")}
    t = m.get((which or "").strip().lower())
    if not t:
        print("用法：python persona_watchdog.py pick cloud | local | paid")
        print("  cloud = 云端免费档（硅基 Qwen2.5-7B）")
        print("  local = 本地档（Herdsman Qwen3:8B）")
        print("  paid  = 云端收费档（DeepSeek deepseek-flash）")
        return 2
    cmd, label = t
    task = SWITCH_TASK.get(cmd) or "SysPersona-DR-%s" % cmd.title()
    prov, model = current_model()
    print("当前：%s · %s" % (prov or "?", model or "?"))
    print("切到：%s" % label)
    print("步骤：关窗口 → 改配置 → 重新拉起（会话不丢，登录保留）。")
    if dry:
        print("会触发：%s（dry，没触发）" % task)
        return 0
    if not task_exists(task):
        install(quiet=True)
    ok, msg = task_run(task)
    print("已触发 %s：%s" % (task, msg or ("ok" if ok else "失败")))
    return 0 if ok else 4


def _gate(d):
    """动手前的闸门。返回 (allowed, reason)。阈值来自 dr-plan.json。"""
    if not d.get("switch"):
        return False, "开关关着（dr off）→ 只判定不动手"
    if os.path.exists(HOLD):
        return False, "有 ops\\dr-hold 蹲住闸 → 不动手"
    p = plan()
    st = _read_json(STATE, {}) or {}
    cool = float(p.get("cooldown", 600))
    cap = int(p.get("max_per_hour", 3))
    hist = st.get("history") or []
    now = time.time()
    if hist and now - float(hist[-1].get("ts", 0)) < cool:
        return False, "冷却中（%d 秒内刚动过手）" % int(cool)
    recent = [h for h in hist if now - float(h.get("ts", 0)) < 3600]
    if len(recent) >= cap:
        return False, "本小时已动手 %d 次（上限 %d）" % (len(recent), cap)
    return True, ""


def execute(d, dry=False):
    """判定 → 触发一个**先建好的**计划任务。返回执行记录（list）。

    这里刻意只说一句 `schtasks /Run`：不建任务、不写配置、不联网、不等模型回话。
    """
    name = action_task(d)
    if not name:
        return []
    if dry:
        return [{"step": "触发 %s" % name, "ok": None, "msg": "（dry：没触发）"}]
    if not task_exists(name):
        ensure_tasks()                     # 任务被人删了就重建（正常不该发生）
    ok, msg = task_run(name)
    return [{"step": "触发 %s" % name, "ok": ok, "msg": msg or "已触发"}]


def run_once(act=True, dry=False, stale=None, net_override=None, prov_override=None):
    """跑一拍：判定 → 过闸 → 触发任务 → 记账。返回判定 dict。

    后台程序（persona_daemon 的 `dr` 任务、计划任务 SysPersona-Watchdog）都只调这一个入口，
    所以「什么时候动手」只有一处实现，不会两边打架（冷却看同一份 dr-state.json）。
    """
    d = decide(stale, net_override=net_override, prov_override=prov_override)
    acted = []
    if act and d["action"] != "ok":
        if dry:                            # 演练不动手，就不看冷却
            acted = execute(d, dry=True)
        else:
            allowed, why = _gate(d)
            if not allowed:
                acted = [{"step": "不执行", "ok": False, "msg": why}]
            else:
                acted = execute(d, dry=False)
                if any(x.get("ok") for x in acted):
                    st = _read_json(STATE, {}) or {}
                    st.setdefault("history", []).append(
                        {"ts": time.time(), "at": d["at"], "action": d["action"],
                         "steps": acted})
                    st["history"] = st["history"][-50:]
                    _write_json(STATE, st)
    elif act and d["action"] == "ok":
        if not daemon_alive():
            ok, msg = revive_daemon()
            acted = [{"step": "守护没在跑 → 拉起", "ok": ok, "msg": msg}]
    d["acted"] = acted
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description="看门狗 / 容灾保底（心跳 → 网络 → 模型链 → 动作）")
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["check", "dr", "install", "enact", "pick", "verify", "tasks"])
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("--stale", type=float, default=None,
                    help="心跳超过多少秒算死（默认取 dr-plan.json 的 stale_after）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--act", action="store_true", help="开关开着时真执行")
    ap.add_argument("--dry", action="store_true", help="只打印会做哪几步，不执行")
    ap.add_argument("--restart-client", action="store_true", dest="restart_client",
                    help="（兼容旧参数，现在由开关决定）")
    ap.add_argument("--net", default="", help="测试用：覆盖网络探针目标")
    ap.add_argument("--prov", default="", help="测试用：覆盖模型点探针目标")
    a = ap.parse_args(argv)

    if a.cmd == "install":
        print("建/更新任务（幂等）：")
        rows = install()
        bad = [r for r in rows if not r[1]]
        print("结果：%d 个就绪，%d 个失败" % (len(rows) - len(bad), len(bad)))
        return 0 if not bad else 4

    if a.cmd == "tasks":
        for t in plan_tasks():
            st, last, res = task_info(t["name"])
            print("%-28s %-6s 上次 %s 结果 %s ｜ %s"
                  % (t["name"], st, last or "-", res or "-", t.get("note") or ""))
        return 0

    if a.cmd == "enact":
        return enact(a.arg, dry=a.dry)

    if a.cmd == "pick":
        return pick(a.arg, dry=a.dry)

    if a.cmd == "verify":
        if not task_exists("SysPersona-DR-Restart"):
            install(quiet=True)
        ok, msg = task_run("SysPersona-DR-Restart")
        print("真实演练：%s\n窗口会在几秒内关掉再打开（会话不丢，登录状态保留）。\n"
              "结果记在 ops\\dr-actions.jsonl。" % (msg or "已触发"))
        return 0 if ok else 4

    if a.cmd == "dr":
        if a.arg == "on":
            d = set_switch(True)
            print("容灾开关：**已打开**（%s）\n从现在起，心跳死 + 判定成立 → 自动杀进程 / 改配置 / 重新拉起。"
                  "关掉：python persona_watchdog.py dr off" % d["at"])
            return 0
        if a.arg == "off":
            d = set_switch(False)
            print("容灾开关：**已关闭**（%s）\n只判定，不动手。" % d["at"])
            return 0
        s = switch()
        print("容灾开关：%s%s" % ("**开**" if s["on"] else "关", ("（%s 由 %s）" % (s["at"], s["by"])) if s["at"] else ""))
        print("蹲住闸：%s" % ("有 ops\\dr-hold（不会动手）" if os.path.exists(HOLD) else "无"))
        print("计划配置：%s" % PLAN)
        names = [t["name"] for t in plan_tasks()]
        miss = [n for n in names if not task_exists(n)]
        print("计划任务：%d/%d 就绪%s" % (len(names) - len(miss), len(names),
                                     ("（缺：%s，跑 install）" % "、".join(miss)) if miss else ""))
        return 0

    d = run_once(act=a.act, dry=a.dry, stale=a.stale,
                 net_override=a.net or None, prov_override=a.prov or None)
    acted = d.get("acted") or []

    if a.as_json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print("看门狗：%s ｜ %s" % (d["action"], d["why"]))
        print("  开关：%s" % ("开" if d["switch"] else "关"))
        mb = plan().get("missing_beats") if not plan().get("stale_after") else None
        print("  心跳：%.0f 秒/拍%s → %.0f 秒算死"
              % (d.get("beat_s") or 0,
                 (" × 允许漏 %s 拍" % mb) if mb else "（阈值显式指定）",
                 d.get("stale_after") or 0))
        if d.get("current"):
            print("  当前模型：%s · %s" % (d["current"]["provider"] or "?",
                                        d["current"]["model"] or "?"))
        if d.get("network"):
            print("  网络：%s" % ("通" if d["network"]["alive"] else "不通"))
        for c in d.get("chain") or []:
            print("  链 %s（%s）：%s" % (c["id"], c["probe"], "通" if c["alive"] else "不通"))
        if d.get("target"):
            print("  目标：%s" % d["target"].get("label"))
        for x in acted:
            print("  %s：%s" % (x["step"], x["msg"] or ("ok" if x.get("ok") else "失败")))
    return 0 if d["action"] == "ok" else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
