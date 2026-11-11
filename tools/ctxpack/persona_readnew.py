# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：每次任务前读会话：先比时间，时间对不上就自动抓取重试。
r"""每次任务前读会话：先比时间，时间对不上就自动抓取重试。

他 2026-11-09 的原话：「我让你读取，肯定是刚才有的，时间不对就自动重试，检查一下刷新是否成功。」

判定规则（顺序写死）：
  1. 你说的时间 = `--since`（默认：现在往前 3 分钟）；
  2. 归档里最新 .md 的 mtime **早于** 那个时间 = **没刷到**（不是你记错，是我没抓到）；
  3. 没刷到就 **跑一次抓取**（`persona_daemon.py --once capture`），然后重新比；
  4. 最多重试 `--retries` 次（默认 3）；仍对不上，如实报「刷新没成功」，并给证据（条数、时间）。

退出码：0 = 有新内容（时间对得上）；2 = 抓了但页面没变；3 = 抓取本身失败。

用法：
  python persona_readnew.py                     # 默认按"现在-3 分钟"判
  python persona_readnew.py --since 1794225000  # 用他说话那一刻的 epoch
  python persona_readnew.py --minutes 10        # 放宽到 10 分钟
  python persona_readnew.py --no-capture        # 只读不抓（排查用）
  python persona_readnew.py --mark              # 记水位，收尾用

环境变量：SYSPERSONA_ROOT/PERSONA_ROOT = 记忆库根；PERSONA_NODE = node；
PERSONA_CDP_LAUNCHER = 拉起调试浏览器的 start_cdp_browser.ps1（不设就用
%LOCALAPPDATA%\SysPersona\ 下那份）；PERSONA_POWERSHELL = powershell.exe（不设就按
%SystemRoot% 拼）。缺哪个就少哪一步，报错时写明是哪个变量。
"""
import argparse
import io
import json
import os
import subprocess
try:
    import persona_win as _pw  # 无窗口：GUI 进程里 spawn 控制台程序不弹黑窗
except Exception:
    pass

import sys
import time
import urllib.request

from persona_root import app_local as _app_local, node as _node, root as _persona_root

ROOT = _persona_root()
ARCHIVE = os.path.join(ROOT, "archive")
OPS = os.path.join(ROOT, "ops")
MARK = os.path.join(OPS, "last-read.json")
LATEST = os.path.join(ROOT, "LATEST.md")
PY = sys.executable


def newest():
    """归档 + 会话两处都看。

    抓取先落 `sessions\\deepseek网页\\capture-*.md`，再由导出同步进 `archive\\`；
    只看 archive 会漏掉刚抓下来的内容（2026-11-09 踩过：抓了 3 次都判「没刷到」）。
    """
    out = []
    for base in (ARCHIVE, os.path.join(ROOT, "sessions")):
        for dp, _, fns in os.walk(base):
            for fn in fns:
                if not fn.endswith(".md"):
                    continue
                # `续接前-*.md` 是 gauge 出包时顺手导出的**本机会话**，不是外部新内容；
                # 不排除的话，每次出包都会把它自己判成「有新内容」（2026-11-09 踩过）。
                if fn.startswith("续接前-"):
                    continue
                p = os.path.join(dp, fn)
                try:
                    out.append((os.path.getmtime(p), p))
                except OSError:
                    continue
    out.sort(reverse=True)
    return out


def hm(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def load_mark():
    try:
        return json.load(io.open(MARK, encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": 0.0, "file": ""}


def save_mark(rec):
    os.makedirs(OPS, exist_ok=True)
    tmp = MARK + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(json.dumps(rec, ensure_ascii=False, indent=1))
    os.replace(tmp, MARK)


def page_time():
    try:
        for line in io.open(LATEST, encoding="utf-8", errors="replace").read(800).splitlines():
            if "更新时间" in line:
                return line.split("：")[-1].strip()
    except OSError:
        pass
    return "?"


def tail(path, n):
    return "\n".join(io.open(path, encoding="utf-8", errors="replace").read().splitlines()[-n:])


NODE = _node()
RELOAD_JS = os.path.join(ROOT, "reload_page.js")

# 抓取要连的调试浏览器：不通就拉 start_cdp_browser.ps1（独立 profile，默认无头）。
CDP_PORT = int(os.environ.get("PERSONA_CDP_PORT") or 9222)
CDP_LAUNCHER = os.environ.get("PERSONA_CDP_LAUNCHER") or _app_local("start_cdp_browser.ps1")
POWERSHELL = os.environ.get("PERSONA_POWERSHELL") or os.path.join(
    os.environ.get("SystemRoot") or r"C:\Windows",
    "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def cdp_alive(port=CDP_PORT, timeout=1.5):
    """CDP 端点通不通。通了就不用再拉浏览器（毫秒级，别拖慢读会话）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port,
                                    timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def ensure_cdp(port=CDP_PORT, wait_sec=20):
    """CDP 不通就拉起调试浏览器（独立 profile，默认无头）。返回 (ok, msg)。

    「每个抓取都要人先开一个调试浏览器」——这件事不该由人记得。
    失败**不抛异常**：让调用方如实报「CDP 起不来」，而不是误报「没有新内容」。
    msg 只有三种：「已在跑」/「已拉起」/「CDP 起不来：…」。
    """
    if cdp_alive(port):
        return True, "已在跑"
    if not CDP_LAUNCHER or not os.path.exists(CDP_LAUNCHER):
        return False, "CDP 起不来：找不到启动脚本 %s（设 PERSONA_CDP_LAUNCHER）" % CDP_LAUNCHER
    if not os.path.exists(POWERSHELL):
        return False, "CDP 起不来：找不到 powershell.exe（%s）" % POWERSHELL
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", CDP_LAUNCHER, "-Port", str(port)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=wait_sec + 10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "CDP 起不来：启动脚本 %.0f 秒没返回" % (wait_sec + 10)
    except Exception as e:
        return False, "CDP 起不来：%s" % str(e)[:120]
    raw = r.stdout or b""
    text = ""
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    if cdp_alive(port):
        return True, "已拉起"
    detail = lines[-1] if lines else (r.stderr or b"").decode("utf-8", "replace").strip()[:160]
    return False, "CDP 起不来：%s" % (detail or ("启动脚本退出码 %d" % r.returncode))


def force_reload():
    """CDP 强制刷新网页会话——**不指望他按 F5**（2026-11-10 读会话铁律第 3 条）。"""
    if not os.path.exists(RELOAD_JS):
        return False, "没有 reload_page.js"
    if not NODE:
        return False, "没配置 PERSONA_NODE（找不到 node：设变量，或把 <runtime>\\node\\bin 放进 PATH）"
    try:
        r = subprocess.run([NODE, RELOAD_JS], capture_output=True, timeout=30,
                           cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or b"")
        for enc in ("utf-8", "gbk"):
            try:
                out = out.decode(enc)
                break
            except UnicodeDecodeError:
                out = out.decode("utf-8", "replace")
        return r.returncode == 0, (out or "").strip()[:160]
    except Exception as e:
        return False, str(e)[:120]


def capture():
    """跑一次抓取，返回 (ok, 摘要)。"""
    cmd = [PY, os.path.join(ROOT, "persona_daemon.py"), "--once", "capture"]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                       cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = (r.stdout or "").strip().splitlines()
    last = out[-1] if out else (r.stderr or "").strip()[:160]
    ok = r.returncode == 0
    return ok, (last or "")[-220:]


def main():
    ap = argparse.ArgumentParser(description="读会话：先比时间，没刷到就抓取重试")
    ap.add_argument("--since", type=float, default=0.0, help="他说话那一刻的 epoch（默认=现在-3 分钟）")
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--lines", type=int, default=60)
    ap.add_argument("--no-capture", action="store_true")
    ap.add_argument("--settle", type=float, default=6.0, help="刷新后等几秒再抓（默认 6）")
    ap.add_argument("--mark", action="store_true")
    a = ap.parse_args()

    since = a.since or (time.time() - a.minutes * 60)
    files = newest()
    if not files:
        print("归档是空的：%s" % ARCHIVE)
        return 3

    if a.mark:
        save_mark({"at": files[0][0], "file": os.path.relpath(files[0][1], ROOT),
                   "when": hm(files[0][0])})
        print("水位已记：%s（%s）" % (os.path.relpath(files[0][1], ROOT), hm(files[0][0])))
        return 0

    print("你说话时刻 ≈ %s；归档最新 = %s（%s）"
          % (hm(since), hm(files[0][0]), os.path.relpath(files[0][1], ROOT)))

    tries = 0
    cdp_ok = None
    cdp_msg = ""
    while files[0][0] < since and tries < a.retries and not a.no_capture:
        tries += 1
        if cdp_ok is None:
            cdp_ok, cdp_msg = ensure_cdp()
            print(("CDP：%s" % cdp_msg) if cdp_ok else cdp_msg)
            if not cdp_ok:
                break
        print("→ 归档比你说的时间旧，判定**没刷到**。第 %d/%d 次：先强制刷新页面，再抓取…"
              % (tries, a.retries))
        rok, rmsg = force_reload()
        print("  刷新%s：%s" % ("成功" if rok else "失败", rmsg))
        if rok:
            time.sleep(a.settle)
        ok, msg = capture()
        print("  抓取%s：%s" % ("成功" if ok else "失败", msg))
        if not ok:
            time.sleep(2)
            continue
        files = newest()
        print("  抓完：最新 = %s（%s）" % (hm(files[0][0]), os.path.relpath(files[0][1], ROOT)))
        if files[0][0] >= since:
            break

    m0 = float(load_mark().get("at") or 0)
    fresh = [(t, p) for t, p in files if t > max(m0, since) - 1]

    if files[0][0] < since:
        if cdp_ok is False:
            print("\n结论：%s —— 这次**没抓到**，不是没有新内容。" % cdp_msg)
            print("（抓取链路断了：先跑一次 start_cdp_browser.ps1 -Login 完成首次登录；"
                  "实在抓不到再加 -Headful 回落）")
            return 3
        print("\n结论：抓了 %d 次，归档仍停在 %s（页面更新于 %s）——**刷新没成功**，不是没有新内容。"
              % (tries, hm(files[0][0]), page_time()))
        if tries == 0:
            print("（用了 --no-capture，没有尝试抓取）")
        return 3 if tries == 0 else 2

    print("\n结论：时间对得上，有新内容 %d 份" % len(fresh))
    for t, p in fresh[:5]:
        print("  - %s  %s" % (hm(t), os.path.relpath(p, ROOT)))
    if fresh:
        print("\n—— 最新那份的尾部 ——\n")
        print(tail(fresh[0][1], a.lines))
        save_mark({"at": fresh[0][0], "file": os.path.relpath(fresh[0][1], ROOT),
                   "when": hm(fresh[0][0])})
    return 0


if __name__ == "__main__":
    sys.exit(main())
