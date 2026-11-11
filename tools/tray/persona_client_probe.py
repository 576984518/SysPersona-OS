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

ROOT = os.path.dirname(os.path.abspath(__file__))
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


def first_existing(paths):
    for p in paths or []:
        q = expand(p)
        if os.path.exists(q):
            return q
    return ""


def run_pids(needle):
    """命令行里带 needle 的进程（不区分大小写）。"""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*%s*' } "
             "| Select-Object -ExpandProperty ProcessId" % needle],
            capture_output=True, timeout=25, creationflags=NO_WINDOW)
        return [int(x) for x in (r.stdout or b"").decode("utf-8", "replace").split()
                if x.strip().isdigit()]
    except Exception:                      # noqa: BLE001
        return []


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


def detect(name):
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
    pids = run_pids(os.path.basename(exe)) if exe else []
    return {
        "name": name, "label": c.get("label") or name,
        "exe": exe, "exe_exists": bool(exe),
        "version": exe_version(exe) if exe else "",
        "home": home, "config": config, "workdir": workdir or home,
        "running": bool(pids), "pids": pids,
        "auto_launch": bool(d.get("auto_launch")),
        "at": now(),
    }


def all_clients():
    return {n: detect(n) for n in (spec().get("clients") or {})}


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
