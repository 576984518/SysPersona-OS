# -*- coding: utf-8 -*-
r"""persona_watchdog.py — 看门狗 + 容灾保底（一个显式开关，打开后才自动动手）

人 2026-11-10 定（原话）：
  「如果心跳没有了，网络是通的，这个时候要干什么？去 ping 一下当前使用模型的服务器
    是否连通。然后切换下一个可用的模型；当网络不通时才会触发保底。」
  「这个是容灾保底，需要的是一个显式的开关，打开后，容灾自动进行。」

两条线分清楚：
  * **判定**（check）：心跳 → 网络 → 当前模型服务器 → 决定动作。永远只读，不碰系统。
  * **执行**（容灾）：只有开关打开（`ops\dr-switch.json` 里 `on: true`）才动手，
    动手就是三步里的若干步：改配置（persona_model_switch.py）→ 杀客户端 → 重新拉起。

    心跳还在？ ── 是 ──→ OK（什么都不做）
        │
        否
        ↓
    守护进程在吗？ ── 否 ──→ 先拉起守护（不碰客户端），下一拍再看
        │ 在
        ↓
    网络通吗？
        ├─ 不通 ──→ ① 保底：切本地模型（断网只有这一条路）
        └─ 通
             ↓
        按「模型链」从当前模型的下一格往后找第一个通的：
             deepseek（收费） → 硅基免费 → 本地
             ├─ 找到别的 ──→ ② 切过去（改配置 + 重启）
             └─ 一个都不通 ──→ ① 保底：切本地
             当前那格就通 ──→ ③ 只是本地卡死：只重启，配置不动

开关怎么用（就三条命令）：
  python persona_watchdog.py dr on      # 打开：从这一刻起容灾自动进行
  python persona_watchdog.py dr off     # 关掉：只判定不执行
  python persona_watchdog.py dr status  # 现在是开还是关

手动判定（不动手）：
  python persona_watchdog.py check
  python persona_watchdog.py check --act          # 开关开着才会真动手
  python persona_watchdog.py check --act --dry    # 只打印「会做哪几步」，不执行
  python persona_watchdog.py check --stale 0 --net 192.0.2.1:9   # 测试用：伪造断网

安全闸（都在，且写进日志）：
  * 默认关；开关文件不存在也按「关」处理；
  * `ops\dr-hold` 文件存在 → 一律不动手（人按住它做实验时用）；
  * 冷却 600 秒、每小时最多 3 次，防止「反复杀 / 反复起」；
  * 心跳 = `ops\persona.log` 的 mtime（守护每拍都写），阈值默认 180 秒（六拍）；
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
SWITCH = os.path.join(OPS, "dr-switch.json")      # 显式开关（就这一个文件）
STATE = os.path.join(OPS, "dr-state.json")        # 执行台账（冷却 / 次数）
HOLD = os.path.join(OPS, "dr-hold")               # 蹲住闸：有它就绝不动手
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
                             capture_output=True, timeout=25)
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


def daemon_pid():
    """守护的 pid：优先 persona-status.json（每拍刷），退而取 persona.lock。"""
    st = _read_json(os.path.join(OPS, "persona-status.json"), {}) or {}
    pid = st.get("pid")
    if isinstance(pid, int) and pid > 0:
        return pid, st.get("at") or ""
    try:
        return int(io.open(os.path.join(OPS, "persona.lock")).read().strip()), ""
    except (OSError, ValueError):
        return 0, ""


def daemon_alive():
    """进程在 = 活着（在但心跳不动 = 卡住了，这两种要分开）。"""
    pid, _ = daemon_pid()
    if not pid:
        return False
    return any(p == pid for p, _ in proc_table())


def client_alive():
    return any(n == CLIENT_IMG.lower() for _, n in proc_table())


# ---------------------------------------------------------------- 显式开关
def switch():
    d = _read_json(SWITCH, {}) or {}
    return {"on": bool(d.get("on")), "at": d.get("at") or "", "by": d.get("by") or "",
            "note": d.get("note") or ""}


def set_switch(on, by="cli"):
    d = {"on": bool(on), "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by,
         "note": ("打开：心跳死 + 判定成立时自动改配置 / 重启客户端"
                  if on else "关闭：只判定，不动手")}
    _write_json(SWITCH, d)
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


def decide(stale=300.0, net_override=None, prov_override=None):
    """返回 dict：{action, why, ...}。只读，不动手。

    action ∈ ok | revive_daemon | fallback_local | switch_backup_cloud |
             restart_client
    """
    ch = chain()
    age = heartbeat_age()
    d = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "heartbeat_age": round(age, 1),
         "stale_after": stale, "switch": switch()["on"]}
    if age <= stale:
        d.update({"action": "ok", "why": "心跳正常（%.0f 秒前）" % age})
        # 心跳正常但客户端没了（崩了 / 被人关了）：容灾最后一道，重启客户端
        if not client_alive():
            d["client_alive"] = False
            d.update({"action": "restart_client",
                      "why": "心跳正常但客户端进程不在了 → 重新拉起"})
        return d

    d["daemon_alive"] = daemon_alive()
    d["client_alive"] = client_alive()

    # 网线探针：不通 → 只有本地这一条路
    net_t = ([net_override] if net_override else
             (cfg().get("offline_targets") or ["223.5.5.5:443"]))
    net_t, net_up = probe(net_t)
    d["network"] = {"targets": net_t, "alive": net_up}

    cur_p, cur_m = current_model()
    d["current"] = {"provider": cur_p, "model": cur_m}

    if not net_up:
        tgt = next((c for c in ch if c.get("cmd") == "local"), ch[-1])
        d.update({"action": "fallback_local", "target": tgt,
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


def revive_daemon():
    if daemon_alive():
        return True, "守护本来就在"
    return _run([PYW, os.path.join(ROOT, "persona_daemon.py"), "--daemon"])


def restart_client(wait=25.0):
    """杀客户端 → 等它退干净 → 重新拉起（有占位凭据时顺手带上）。"""
    if not os.path.exists(CLIENT_EXE):
        return False, "找不到客户端可执行文件：%s" % CLIENT_EXE
    _run(["taskkill", "/F", "/T", "/IM", CLIENT_IMG], timeout=40)
    t0 = time.time()
    while time.time() - t0 < wait and client_alive():
        time.sleep(1.0)
    if client_alive():
        return False, "杀了 %0.f 秒还在跑，放弃拉起（避免开两份）" % (time.time() - t0)
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
            return True, "已重新拉起（%.0f 秒）" % (time.time() - t0)
        time.sleep(1.0)
    return False, "已启动但 %0.f 秒内没看到进程" % wait


DR_TASK = "SysPersona-DR-Restart"          # 专门用来「杀+拉起」的计划任务（按需触发）
_ONCE_ST = "04:00"                          # 建任务用的一次性时间（2099 年，永不自动跑）
_ONCE_SD = "2099/01/01"


def spawn_detached(extra=(), wait=0.0):
    r"""把「杀客户端 + 重新拉起」交给**任务计划**去跑。

    为什么不用 wmic：本机（Win11 24H2）已经没有 wmic 了；也不直接 Popen——
    从客户端窗口里起的进程是它的子孙，`taskkill /T` 会把它一起杀掉，
    结果就是「杀了，但没拉起来」。Task Scheduler 起的进程父级是服务，杀不到。
    教训记在 memory\设定\30-流程与运维.md §十。
    """
    tr = '"%s" "%s" _restart %s' % (PYW, os.path.abspath(__file__), " ".join(extra))
    ok_c, msg_c = _run(["schtasks", "/Create", "/TN", DR_TASK, "/TR", tr,
                        "/SC", "ONCE", "/ST", _ONCE_ST, "/SD", _ONCE_SD, "/F"], timeout=40)
    if not ok_c:
        return False, "建任务失败：%s" % msg_c
    ok_r, msg_r = _run(["schtasks", "/Run", "/TN", DR_TASK], timeout=40)
    if not ok_r:
        return False, "触发任务失败：%s" % msg_r
    if wait:
        time.sleep(wait)
    return True, "已交给计划任务 %s（不在客户端进程树里）" % DR_TASK


def do_restart(delay=2.0):
    """真正的「杀 + 拉起」，只由独立进程调用（见 spawn_detached）。"""
    time.sleep(delay)                      # 让调动我们的人先退干净
    ok, msg = restart_client()
    try:
        with io.open(os.path.join(OPS, "dr-restart.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "ok": ok, "msg": msg}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return ok, msg


def _gate(d):
    """动手前的闸门。返回 (allowed, reason)。"""
    if not d.get("switch"):
        return False, "开关关着（dr off）→ 只判定不动手"
    if os.path.exists(HOLD):
        return False, "有 ops\\dr-hold 蹲住闸 → 不动手"
    st = _read_json(STATE, {}) or {}
    cool = float((cfg().get("dr") or {}).get("cooldown", 600))
    cap = int((cfg().get("dr") or {}).get("max_per_hour", 3))
    hist = st.get("history") or []
    now = time.time()
    if hist and now - float(hist[-1].get("ts", 0)) < cool:
        return False, "冷却中（%d 秒内刚动过手）" % int(cool)
    recent = [h for h in hist if now - float(h.get("ts", 0)) < 3600]
    if len(recent) >= cap:
        return False, "本小时已动手 %d 次（上限 %d）" % (len(recent), cap)
    return True, ""


def execute(d, dry=False):
    """按判定执行；返回执行记录（list）。dry=True 只列步骤。"""
    steps = []
    tgt = d.get("target") or {}
    need_switch = d["action"] in ("switch_backup_cloud", "fallback_local")
    need_restart = d["action"] in ("switch_backup_cloud", "fallback_local", "restart_client")

    if d["action"] == "revive_daemon":
        steps.append(("拉起守护", "daemon", None, None))
    if need_switch and tgt.get("cmd"):
        steps.append(("改配置 → %s" % tgt.get("label"), "switch",
                      tgt.get("cmd"), tgt.get("model")))
    if need_restart:
        steps.append(("重启客户端", "restart", None, None))
    if dry or not steps:
        return [{"step": s[0], "ok": None, "msg": "（dry：没执行）"} for s in steps]

    done = []
    for label, kind, cmd, model in steps:
        if kind == "daemon":
            ok, msg = revive_daemon()
        elif kind == "switch":
            ok, msg = switch_model(cmd, model if cmd == "local" else "")
        else:
            ok, msg = spawn_detached()   # 不在这里杀：交给计划任务去杀，免得把自己带走
        done.append({"step": label, "ok": ok, "msg": msg})
        if not ok:
            break
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description="看门狗 / 容灾保底（心跳 → 网络 → 模型链 → 动作）")
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["check", "dr", "verify", "_restart"])
    ap.add_argument("arg", nargs="?", default="", choices=["", "on", "off", "status"])
    ap.add_argument("--stale", type=float, default=300.0, help="心跳超过多少秒算死（默认 300：给长任务留余量，别在它跑大活时误杀）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--act", action="store_true", help="开关开着时真执行")
    ap.add_argument("--dry", action="store_true", help="只打印会做哪几步，不执行")
    ap.add_argument("--restart-client", action="store_true", dest="restart_client",
                    help="（兼容旧参数，现在由开关决定）")
    ap.add_argument("--net", default="", help="测试用：覆盖网络探针目标")
    ap.add_argument("--prov", default="", help="测试用：覆盖模型点探针目标")
    a = ap.parse_args(argv)

    if a.cmd == "_restart":
        ok, msg = do_restart()
        print("%s ｜ %s" % ("ok" if ok else "失败", msg))
        return 0 if ok else 4

    if a.cmd == "verify":
        ok, msg = spawn_detached(wait=1.0)
        print("真实演练：%s\n窗口会在几秒内关掉再打开（会话不丢，登录状态保留）。\n"
              "结果记在 ops\\dr-restart.jsonl。" % msg)
        return 0 if ok else 4

    if a.cmd == "dr":
        if a.arg == "on":
            d = set_switch(True)
            print("容灾开关：**已打开**（%s）\n从现在起，心跳死 + 判定成立 → 自动改配置 / 杀进程 / 重新拉起。"
                  "关掉：python persona_watchdog.py dr off" % d["at"])
            return 0
        if a.arg == "off":
            d = set_switch(False)
            print("容灾开关：**已关闭**（%s）\n只判定，不动手。" % d["at"])
            return 0
        s = switch()
        print("容灾开关：%s%s" % ("**开**" if s["on"] else "关", ("（%s 由 %s）" % (s["at"], s["by"])) if s["at"] else ""))
        print("蹲住闸：%s" % ("有 ops\\dr-hold（不会动手）" if os.path.exists(HOLD) else "无"))
        print("开关文件：%s" % SWITCH)
        return 0

    d = decide(a.stale, net_override=a.net or None, prov_override=a.prov or None)

    acted = []
    if a.act and d["action"] != "ok":
        allowed, why = _gate(d)
        if not allowed:
            acted = [{"step": "不执行", "ok": False, "msg": why}]
        else:
            acted = execute(d, dry=a.dry)
            if not a.dry and any(x.get("ok") for x in acted):
                st = _read_json(STATE, {}) or {}
                st.setdefault("history", []).append(
                    {"ts": time.time(), "at": d["at"], "action": d["action"],
                     "steps": acted})
                st["history"] = st["history"][-50:]
                _write_json(STATE, st)
    elif a.act and d["action"] == "ok":
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

    if a.as_json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print("看门狗：%s ｜ %s" % (d["action"], d["why"]))
        print("  开关：%s" % ("开" if d["switch"] else "关"))
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
