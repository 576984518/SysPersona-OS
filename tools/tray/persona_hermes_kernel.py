# -*- coding: utf-8 -*-
r"""persona_hermes_kernel.py — 只重启 Hermes 的「内核」（:9120），不动桌面窗口

人 2026-11-11 定：「其实最简单就是这里重启内核」+「不用动前端」。
所以他说的那个界面按钮，我们不点；用**同样的进程 + 同样的环境变量**在命令行里重起一遍，
快得多（整机关 40 秒级，内核 5-10 秒级），而且不碰界面、不注入任何东西。

这台机器上的事实（2026-11-11 实测）：

  * 内核 = `<versions>\<ver>\hermes-agent-cn-runtime-win32-x64.exe dashboard --host 127.0.0.1 --port 9120 --no-open`
    （桌面壳的子进程；健康检查 `GET http://127.0.0.1:9120/api/health` → `{"ok":true,...}`）
  * 网关 = 另一个进程 `… gateway run`（微信那条线靠它，**别跟着一起动**）
  * 改 `config.yaml` 要「内核」重读 → 重启内核就够

纪律：先杀后拉；杀完先看 3 秒——桌面壳若自己把内核拉回来了，就直接用它，不再拉第二份（免得抢端口）。

用法：
  python persona_hermes_kernel.py status      # 只看：进程在不在、健康检查通不通
  python persona_hermes_kernel.py restart     # 真重启（隐藏窗口）
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(ROOT, "ops", "hermes-keeper.json")     # 复用 keeper 那份 exe/home
LOG = os.path.join(ROOT, "ops", "hermes-kernel.jsonl")
NO_WINDOW = 0x08000000
PORT = int(os.environ.get("SYSPERSONA_HERMES_KERNEL_PORT") or 9120)
KERNEL_ARGS = ["dashboard", "--host", "127.0.0.1", "--port", str(PORT), "--no-open"]


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                      # noqa: BLE001
        return default


def _append(row):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:                      # noqa: BLE001
        pass


def conf():
    """exe / home：先读 keeper 的配置，没有再现场探一遍。"""
    c = _read(CONF) or {}
    if c.get("exe") and os.path.exists(c["exe"]):
        return c
    try:
        import persona_hermes_keeper as k
        d = k.discover()
    except Exception:                      # noqa: BLE001
        d = {}
    return d


def health(timeout=3.0):
    """内核健康：`{"ok":true}` 才算通（health 不需要鉴权，别去碰带 token 的那些接口）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT,
                                    timeout=timeout) as r:
            status = r.status
            body = r.read().decode("utf-8", "replace")
    except Exception as e:                 # noqa: BLE001
        return False, str(e)[:120]
    flat = body.replace(" ", "").replace("\n", "")
    return (status == 200 and '"ok":true' in flat), body[:120]


def kernel_pids():
    """[(pid, cmdline)]：命令行里带 `dashboard` 的那个 runtime 进程就是内核。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'hermes-agent-cn-runtime%'\" | "
          "Where-Object { $_.CommandLine -like '*dashboard*' } | "
          "ForEach-Object { ('{0}|{1}' -f $_.ProcessId, $_.CommandLine) }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=40, creationflags=NO_WINDOW)
    except Exception:                      # noqa: BLE001
        return []
    out = (r.stdout or b"").decode("utf-8", "replace")
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid, cmd = line.split("|", 1)
        try:
            rows.append((int(pid), cmd))
        except ValueError:
            continue
    return rows


def _env(c):
    home = c.get("home") or ""
    exe = c.get("exe") or ""
    env = dict(os.environ)
    if home:
        env["HERMES_HOME"] = home
        env["VIRTUAL_ENV"] = os.path.join(os.path.dirname(home), "versions")
    env["PYTHONIOENCODING"] = "utf-8"
    env["HERMES_GATEWAY_DETACHED"] = "1"
    env["HERMES_SUPERVISED_CHILD"] = "1"
    if exe:
        internal = os.path.join(os.path.dirname(exe), "_internal")
        if os.path.isdir(internal):
            env["PYTHONPATH"] = internal + ((";" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
    return env


def start(c):
    """按内核原本的命令行隐藏拉起（CREATE_NO_WINDOW，不起黑框）。"""
    exe, home = c.get("exe") or "", c.get("home") or ""
    if not exe or not os.path.exists(exe):
        return False, "找不到 Hermes 运行时：%s" % exe
    try:
        subprocess.Popen([exe] + KERNEL_ARGS,
                         cwd=home or os.path.dirname(exe),
                         close_fds=True, creationflags=NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=_env(c))
    except Exception as e:                 # noqa: BLE001
        return False, "拉起失败：%s" % e
    return True, "已按原命令行拉起内核"


def _wait_health(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        ok, _body = health()
        if ok:
            return True, time.time() - t0
        time.sleep(0.7)
    return False, time.time() - t0


def _state_db():
    c = conf()
    home = c.get("home") or ""
    return os.path.join(home, "state.db") if home else ""


def busy():
    """有没有一轮正在生成。返回 (在跑?, 说明)。

    2026-11-11 人报「每次最下面一直卡一条我发的消息」：内核在生成中途被掐掉，
    那一轮就永远停在"已发送、没有回复"——重启内核、重启桌面都清不掉，只能重启聊天窗口。
    所以重启前先看这一眼：还在跑就先等，别硬掐。
    """
    p = _state_db()
    if not p or not os.path.exists(p):
        return False, ""
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True, timeout=2)
        try:
            row = con.execute("select conversation_id, expires_at from session_turn_leases "
                              "where expires_at > ? order by expires_at desc limit 1",
                              (time.time(),)).fetchone()
        finally:
            con.close()
    except Exception:                      # noqa: BLE001
        return False, ""
    if row:
        return True, "会话 %s 还有一轮没说完（租约到 %s）" % (
            row[0], time.strftime("%H:%M:%S", time.localtime(row[1])))
    return False, ""


def restart(wait=90, grace=120):
    """杀内核 → 拉起 → 等健康检查通。返回 (ok, msg)。

    grace：发现还有一轮在跑时最多等它多少秒；等不到就不重启（免得又把人的消息掐成卡死气泡）。
    """
    c = conf()
    if not c.get("exe"):
        return False, "不知道 Hermes 运行时在哪（先跑 persona_hermes_keeper.py --init）"
    b, why = busy()
    if b:
        t0 = time.time()
        while b and time.time() - t0 < grace:
            time.sleep(2)
            b, why = busy()
        if b:
            return False, "先不重启：%s。等它说完（或关掉那一轮）再来。" % why
    old = kernel_pids()
    t0 = time.time()
    for pid, _cmd in old:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        except Exception:                  # noqa: BLE001
            pass
    # 杀完先给它 3 秒：桌面壳可能自己把内核拉回来，那就别再拉第二份
    time.sleep(3.0)
    back = kernel_pids()
    if not back:
        ok, msg = start(c)
        if not ok:
            _append({"t": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "ok": False, "msg": msg, "killed": [p for p, _ in old]})
            return False, msg
    good, secs = _wait_health(wait)
    total = time.time() - t0
    row = {"t": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "ok": bool(good), "seconds": round(total, 1),
           "killed": [p for p, _ in old],
           "mode": "desktop 自己拉回来" if back else "我们重新拉起"}
    _append(row)
    if good:
        return True, "内核已重启（%.0f 秒，%s，健康检查通）" % (
            total, "桌面壳自己拉回来的" if back else "按原命令行拉起的")
    return False, "内核重启后 %.0f 秒健康检查仍不通（%s）" % (total, "桌面壳" if back else "我们拉起的")


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cmd = a[0] if a else "status"
    c = conf()
    if cmd == "status":
        pids = kernel_pids()
        ok, body = health()
        print("内核进程：%s" % (", ".join("PID %d" % p for p, _ in pids) or "没有"))
        print("健康检查：%s %s" % ("通" if ok else "不通", body))
        print("运行时：%s" % (c.get("exe") or "?"))
        return 0 if ok else 3
    if cmd == "restart":
        ok, msg = restart()
        print(("[OK] " if ok else "[FAIL] ") + msg)
        return 0 if ok else 4
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
