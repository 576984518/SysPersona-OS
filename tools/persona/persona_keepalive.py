# -*- coding: utf-8 -*-
r"""persona_keepalive.py — 保活（v5.3）：盯进程、没在就跑起来、记原因

不认死某个壳：目标写在 `ops/keepalive.json` 里（name / match / exe / args），
换壳只是改配置，不用改代码。这也是 v5.3 的分寸——「启动、监控、拉起」是通用能力，
针对某一家的界面注入、字体替换属于本地私有，不进仓库。

用法：
  python persona_keepalive.py add --name shell --match hermes-agent-cn-desktop \
      --exe "D:\path\app.exe" [--cooldown 120]
  python persona_keepalive.py list
  python persona_keepalive.py rm --name shell
  python persona_keepalive.py check [--json]      # 守护进程每 60 秒调一次

落盘：ops/keepalive.json（目标表）、ops/keepalive-state.json（上次拉起时间）、
      ops/keepalive.log（只在拉起/失败/查不了时写一行；正常在跑不写，免得日志被刷满）。
退出码：check 恒为 0（保活是巡检，不是门禁）。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
CONF = os.path.join(OPS, "keepalive.json")
STATE = os.path.join(OPS, "keepalive-state.json")
LOG = os.path.join(OPS, "keepalive.log")


def load_conf():
    try:
        d = json.load(io.open(CONF, encoding="utf-8-sig"))
        return d.get("targets") or []
    except (OSError, ValueError):
        return []


def save_conf(targets):
    os.makedirs(OPS, exist_ok=True)
    with io.open(CONF, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"targets": targets}, f, ensure_ascii=False, indent=1)


def load_state():
    try:
        return json.load(io.open(STATE, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def save_state(d):
    os.makedirs(OPS, exist_ok=True)
    with io.open(STATE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def log(line):
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


_SNAPSHOT = {"at": 0.0, "names": None}
_LAUNCHED = {}      # name -> Popen：本进程自己拉起的，句柄还活着就别再拉


def _procs():
    """枚举进程名（小写）。一次 Toolhelp32 快照，失败返回 None。

    为什么不用 tasklist / Get-Process：前者在受限令牌下直接 Access denied，
    后者会把「看不见」伪装成「没在跑」——两个假阴性都会导致重复拉起。
    快照是系统调用，没有控制台、没有编码问题，也不会多弹一个窗口。
    """
    now = time.time()
    if _SNAPSHOT["names"] is not None and now - _SNAPSHOT["at"] < 3:
        return _SNAPSHOT["names"]
    try:
        import ctypes
        from ctypes import wintypes as wt

        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                        ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k32.Process32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE32)]
        k32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE32)]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return None
        names, e = set(), PE32()
        e.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32First(ctypes.c_void_p(snap), ctypes.byref(e))
        while ok:
            names.add(e.szExeFile.decode("mbcs", "replace").lower())
            ok = k32.Process32Next(ctypes.c_void_p(snap), ctypes.byref(e))
        k32.CloseHandle(ctypes.c_void_p(snap))
        # 连自己都看不见 = 这份快照被过滤过，不可信
        if os.path.basename(sys.executable).lower() not in names:
            return None
        _SNAPSHOT.update({"at": now, "names": names})
        return names
    except Exception:
        return None


def running(match):
    """True=在跑 / False=确认没在跑 / None=查不了（权限被拒、快照被过滤）。

    **None 必须和 False 分开**：查不了就当成「没在跑」，会重复拉起——
    同一个壳开两份，比不保活更糟。所以查不了时宁可不动手。
    """
    name = match if match.lower().endswith(".exe") else match + ".exe"
    names = _procs()
    if names is None:
        return None
    return name.lower() in names


def check(do_start=True):
    targets = load_conf()
    st = load_state()
    now = time.time()
    out = []
    for t in targets:
        name, match, exe = t.get("name", "?"), t.get("match", ""), t.get("exe", "")
        if not match:
            out.append({"name": name, "ok": False, "msg": "配置缺 match"})
            continue
        # 自己拉起的：句柄还在就是活着，别信可能被过滤的进程表
        proc = _LAUNCHED.get(name)
        if proc is not None:
            if proc.poll() is None:
                out.append({"name": name, "ok": True,
                            "msg": "在跑（本进程拉起，pid %d）" % proc.pid})
                continue
            _LAUNCHED.pop(name, None)
        st_run = running(match)
        if st_run is None:
            out.append({"name": name, "ok": None, "msg": "查不了进程状态（权限被拒），先不动手"})
            log("TASK %s SKIP 查不了进程状态，不动手" % name)
            continue
        if st_run:
            out.append({"name": name, "ok": True, "msg": "在跑"})
            continue
        last = (st.get(name) or {}).get("last", 0)
        cool = int(t.get("cooldown") or 120)
        if do_start and now - last < cool:
            out.append({"name": name, "ok": False,
                        "msg": "没在跑，%ds 前刚试过拉起（冷却 %ds）" % (int(now - last), cool)})
            continue
        if not os.path.exists(exe):
            out.append({"name": name, "ok": False, "msg": "没在跑且找不到 exe：%s" % exe})
            log("TASK %s FAIL 找不到 exe：%s" % (name, exe))
            continue
        if not do_start:
            out.append({"name": name, "ok": False, "msg": "没在跑（只检查不拉起）"})
            continue
        try:
            _LAUNCHED[name] = subprocess.Popen(
                [exe] + list(t.get("args") or []),
                cwd=t.get("cwd") or os.path.dirname(exe),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            st[name] = {"last": now, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            out.append({"name": name, "ok": True, "msg": "没在跑，已拉起"})
            log("TASK %s OK 没在跑，已拉起" % name)
        except OSError as e:
            out.append({"name": name, "ok": False, "msg": "拉起失败：%s" % str(e)[:80]})
            log("TASK %s FAIL 拉起失败：%s" % (name, e))
    if do_start:
        save_state(st)
    return out


def cmd_add(a):
    targets = [t for t in load_conf() if t.get("name") != a.name]
    targets.append({"name": a.name, "match": a.match, "exe": a.exe,
                    "args": a.args or [], "cwd": a.cwd or "",
                    "cooldown": a.cooldown})
    save_conf(targets)
    print("已登记保活目标 %s：%s → %s" % (a.name, a.match, a.exe))
    return 0


def cmd_list(_a):
    targets = load_conf()
    if not targets:
        print("（未登记保活目标）")
        return 0
    for t in targets:
        print("%-14s %-30s %s" % (t.get("name"), t.get("match"), t.get("exe")))
    return 0


def cmd_rm(a):
    save_conf([t for t in load_conf() if t.get("name") != a.name])
    print("已删 %s" % a.name)
    return 0


def cmd_check(a):
    res = check(do_start=not a.no_start)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for r in res:
            print("%-14s %-4s %s" % (r["name"], "OK" if r["ok"] else "FAIL", r["msg"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("add")
    c.add_argument("--name", required=True)
    c.add_argument("--match", required=True, help="进程名，例如 hermes-agent-cn-desktop")
    c.add_argument("--exe", required=True)
    c.add_argument("--args", nargs="*", default=[])
    c.add_argument("--cwd", default="")
    c.add_argument("--cooldown", type=int, default=120)
    sub.add_parser("list")
    c = sub.add_parser("rm"); c.add_argument("--name", required=True)
    c = sub.add_parser("check")
    c.add_argument("--json", action="store_true")
    c.add_argument("--no-start", action="store_true", help="只查不拉")
    a = ap.parse_args()
    fn = {"add": cmd_add, "list": cmd_list, "rm": cmd_rm, "check": cmd_check}.get(a.cmd)
    if not fn:
        ap.print_help()
        return 0
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
