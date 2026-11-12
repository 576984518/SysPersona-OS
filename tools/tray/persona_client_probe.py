# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_client_probe.py — 认得出客户端在哪：exe / 配置 / 工作目录 / 会话库
r"""persona_client_probe.py — 认得出客户端在哪：exe / 配置 / 工作目录 / 会话库

人 2026-11-11：「建议用程序自动拉起，重载。所以程序需要自动检测客户端配置文件和工作目录的能力」

所以这里只做"认路"这一件事，给三处用：托盘（打开配置/工作目录）、守护（发现客户端没了就报，
配置允许时自动拉起）、以后的重载（知道配置在哪才谈得上重载）。

配置写在 `ops\tray-clients.json` 每个客户端的 `detect` 段：

    "detect": {
      "exe":     ["%LOCALAPPDATA%\\Programs\\DeepSeek Harness\\DeepSeek Harness.exe"],
      "home":    ["%USERPROFILE%\\.dsh"],
      "config":  ["%USERPROFILE%\\.dsh\\.credentials.yaml"],
      "workdir": ["%USERPROFILE%\\.dsh"],
      "auto_launch": false
    }

**默认不自动拉起**：先"认得出来、报得出来"，等人说开再开——自动开客户端这种事，
一旦开错窗口就会打断人正在做的事（与"不抢焦点"同一条纪律）。
"""
import glob
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "ops")
SPEC = os.path.join(OPS, "tray-clients.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def now():
    return __import__("time").strftime("%Y-%m-%d %H:%M:%S")


def spec():
    try:
        with io.open(SPEC, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:                      # noqa: BLE001
        return {}


def expand(p):
    return os.path.expandvars(os.path.expanduser(p))


def _drive_variants(p):
    r"""`%ProgramData%` 在别的盘上也试一遍。

    有的机器壳装在**另一块盘**的 `ProgramData` 下，而 `%ProgramData%` 展开在系统盘，
    配置里写一个 %ProgramData% 就到了另一条路上——所以同一个相对路径，每块存在的盘都试一次。
    """
    out = [expand(p)]
    low = str(p).lower()
    for var in ("%programdata%", "%localappdata%", "%programfiles%"):
        if var in low:
            tail = str(p)[low.index(var) + len(var):]
            for letter in "CDEFGH":
                root = "%s:\\" % letter
                if not os.path.isdir(root):
                    continue
                out.append(expand(var.replace("%programdata%", os.path.join(root, "ProgramData"))
                                  .replace("%localappdata%", os.path.join(root, "Users", "Public", "AppData", "Local"))
                                  .replace("%programfiles%", os.path.join(root, "Program Files")) + tail))
    return out


def first_existing(paths):
    for p in paths or []:
        for q in _drive_variants(p):
            if os.path.exists(q):
                return q
    return ""


def run_pids(needle):
    """命令行里带 needle 的进程（WQL 里筛，别把整张进程表拖回来）。

    2026-11-12 本机（J1900 / 4G）实测：`Get-CimInstance Win32_Process | Where-Object {...}`
    是**全表 + 每行取 CommandLine**，这台机器 25 秒都跑不完，必然撞超时 → 返回空 →
    「DSH 明明开着」却报「没在跑」。改成在查询里过滤，WMI 只回我们那几条。
    """
    esc = str(needle).replace("'", "''")
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%%%s%%'\" "
             "| Select-Object -ExpandProperty ProcessId" % esc],
            capture_output=True, timeout=25, creationflags=NO_WINDOW)
        return [int(x) for x in (r.stdout or b"").decode("utf-8", "replace").split()
                if x.strip().isdigit()]
    except Exception:                      # noqa: BLE001
        return []




def window_pids_multi(needles):
    r"""一次问完全部「带可见窗口的进程」，回来自己分配。

    和 run_pids_multi 同一个理由：`Get-Process` 那一下在这台机器上要十来秒，
    每个客户端问一遍就是白花。一次拿全，再按名字筛。
    """
    needles = [n for n in dict.fromkeys(needles) if n]
    out = {n: [] for n in needles}
    if not needles:
        return out
    try:
        # 注意：这里不能写 PowerShell 的 $_ —— 这段代码曾被外壳吃掉变量，
        # 于是永远返回空、误判"没有界面"。把 Id/ProcessName 打出来，自己在 Python 里筛。
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-Process | Where-Object MainWindowHandle | "
             "ForEach-Object { \"{0}~{1}\" -f $_.Id, $_.ProcessName }"],
            capture_output=True, timeout=25, creationflags=NO_WINDOW)
        for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines():
            pid, _sep, nm = ln.partition("~")
            if not pid.strip().isdigit():
                continue
            for n in needles:
                if n.lower() in nm.lower():
                    out[n].append(int(pid))
    except Exception:                      # noqa: BLE001
        pass
    return out


def window_pids(needle):
    """带可见窗口的进程（MainWindowHandle != 0），单个版本。

    2026-11-11 15:18 教训：Hermes 是**两段式**——hermes-agent-cn-desktop.exe 才是界面，
    hermes-agent-cn-runtime-win32-x64.exe 是纯后端、无窗口。只用进程名判在跑，
    会出现说拉起来了、人却看不到界面。判界面在不在，只能看窗口。
    """
    return window_pids_multi([needle]).get(needle, [])


def exe_version(path):
    try:
        import ctypes
        import ctypes.wintypes as w

        class VS(ctypes.Structure):
            _fields_ = [("dwSignature", w.DWORD), ("dwStrucVersion", w.DWORD),
                        ("dwFileVersionMS", w.DWORD), ("dwFileVersionLS", w.DWORD),
                        ("dwProductVersionMS", w.DWORD), ("dwProductVersionLS", w.DWORD),
                        ("dwFileFlagsMask", w.DWORD), ("dwFileFlags", w.DWORD),
                        ("dwFileOS", w.DWORD), ("dwFileType", w.DWORD),
                        ("dwFileSubtype", w.DWORD), ("dwFileDateMS", w.DWORD),
                        ("dwFileDateLS", w.DWORD)]

        info = ctypes.windll.version
        size = info.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        info.GetFileVersionInfoW(path, 0, size, buf)
        p = ctypes.c_void_p()
        ln = w.UINT()
        if info.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(ln)):
            v = ctypes.cast(p, ctypes.POINTER(VS)).contents
            ms, ls = v.dwFileVersionMS, v.dwFileVersionLS
            return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:                      # noqa: BLE001
        pass
    return ""


def run_pids_multi(needles):
    r"""一次查完多个 needle（WQL 里 OR），回来自己按命令行分配。

    为什么合成一条：这里每条 `Get-CimInstance Win32_Process -Filter "CommandLine LIKE …"`
    在本机（J1900 / 4G）实测约 9 秒；两个客户端各查一次 + 各自再取窗口，
    一轮 `all_clients()` 要 60 秒，而守护每 5 分钟就要问一遍。合成一条，代价只付一次。
    """
    needles = [n for n in dict.fromkeys(needles) if n]
    out = {n: [] for n in needles}
    if not needles:
        return out
    cond = " OR ".join("CommandLine LIKE '%%%s%%'" % n.replace("'", "''") for n in needles)
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"%s\" | "
             "ForEach-Object { \"{0}~{1}\" -f $_.ProcessId, $_.CommandLine }" % cond],
            capture_output=True, timeout=30, creationflags=NO_WINDOW)
        for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines():
            pid, _sep, cmd = ln.partition("~")
            if not pid.strip().isdigit():
                continue
            low = cmd.lower()
            for n in needles:
                if n.lower() in low:
                    out[n].append(int(pid))
    except Exception:                      # noqa: BLE001
        pass
    return out


def detect(name, _pids=None, _wins=None):
    c = (spec().get("clients") or {}).get(name) or {}
    d = c.get("detect") or {}
    exe = first_existing(d.get("exe") or c.get("img") or [])
    home = first_existing(d.get("home") or [])
    config = ""
    for pat in d.get("config") or []:
        hits = sorted(glob.glob(expand(pat)))
        if hits:
            config = hits[-1]
            break
    workdir = first_existing(d.get("workdir") or [])
    base = os.path.basename(exe) if exe else ""
    if _pids and base:
        pids = list(_pids.get(base) or [])
    else:
        pids = run_pids(base) if exe else []
    # window_pids 以前在这里被调了两次（判断 + 取值），一次调用省一半
    wneedle = base.replace(".exe", "") or name
    if _wins is not None:
        wins = list(_wins.get(wneedle) or [])
    else:
        wins = window_pids(wneedle) if exe else []
    return {
        "name": name, "label": c.get("label") or name,
        "exe": exe, "exe_exists": bool(exe),
        "version": exe_version(exe) if exe else "",
        "home": home, "config": config, "workdir": workdir or home,
        "running": bool(pids), "pids": pids,
        "window_pids": wins,
        "has_window": bool(wins),
        "auto_launch": bool(d.get("auto_launch")),
        "at": now(),
    }


def all_clients():
    spec_clients = spec().get("clients") or {}
    need, wneed = [], []
    for name in spec_clients:
        c = spec_clients.get(name) or {}
        d = c.get("detect") or {}
        exe = first_existing(d.get("exe") or c.get("img") or [])
        if exe:
            base = os.path.basename(exe)
            need.append(base)
            wneed.append(base.replace(".exe", ""))
    pids = run_pids_multi(need)
    wins = window_pids_multi(wneed)
    return {n: detect(n, _pids=pids, _wins=wins) for n in spec_clients}


def launch(name):
    st = detect(name)
    if st["running"]:
        return True, "%s 已经在跑（pid %s）" % (st["label"], ",".join(map(str, st["pids"][:3])))
    if not st["exe"]:
        return False, "%s 找不到 exe（detect.exe 没配或路径变了）" % name
    try:
        subprocess.Popen([st["exe"]], cwd=st["workdir"] or None,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:                 # noqa: BLE001
        return False, "拉不起来：%s" % str(e)[:120]
    return True, "已拉起 %s（工作目录 %s）" % (st["label"], st["workdir"] or "-")


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cmd = a[0] if a else "detect"
    if cmd == "detect":
        which = a[1] if len(a) > 1 else ""
        data = detect(which) if which else all_clients()
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if cmd == "check":
        bad = []
        for n, st in all_clients().items():
            flag = "OK  " if (st["exe_exists"] and st["config"]) else "MISS"
            if flag == "MISS":
                bad.append(n)
            print("%s %-8s exe=%s｜config=%s｜workdir=%s｜跑着=%s｜版本=%s" % (
                flag, n, "有" if st["exe_exists"] else "无",
                os.path.basename(st["config"]) or "无", st["workdir"] or "无",
                "是" if st["running"] else "否", st["version"] or "?"))
        return 0 if not bad else 1
    if cmd == "launch":
        ok, msg = launch(a[1] if len(a) > 1 else "")
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
