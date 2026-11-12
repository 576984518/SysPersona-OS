# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：每次任务前读会话：先比时间，时间对不上就自动抓取重试。
"""每次任务前读会话：先比时间，时间对不上就自动抓取重试。

他 2026-11-09 的原话：「我让你读取，肯定是刚才有的，时间不对就自动重试，检查一下刷新是否成功。」

判定规则（顺序写死）：
  0. **先看侧栏最上面几条的 id+标题变没变**（`persona_sessions.py poll`，2026-11-10）：
     判据只看侧栏前 N 条（默认 5）——新会话永远出现在最上面一条，点开就落到第二条。
     表里没有的 id = new_top（新会话）。**不比对消息指纹、不用锚点**
     （`ops\\anchor.txt` 保留，但不再参与跨会话判断）。
     有新会话就打印 `新会话：<标题>（<id>，第 1 位）`，并**按会话分存**
     （`sessions\\<日期>\\deepseek网页\\<conv_id>.md`，已存在就覆盖自己那份）。
     页面没选中会话（4）就让抓取照旧「选第一条，不行第二条」；CDP 不通（2）就明说。
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
"""
import argparse
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）
import io
import json
import os
import subprocess
try:
    import persona_win as _pw  # 无窗口：GUI 进程里 spawn 控制台程序不弹黑窗
except Exception:
    pass
try:
    import persona_vault_io as _vio  # 密文就地解（SPV1），明文直读
except Exception:
    _vio = None

import sys
import time
import urllib.request

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE = os.path.join(ROOT, "archive")
OPS = os.path.join(ROOT, "ops")
MARK = os.path.join(OPS, "last-read.json")
LATEST = os.path.join(ROOT, "文档/LATEST.md")
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
                # `hermes-*.md-<hash>` 是本机壳自己的会话镜像（每分钟一份），读它等于读自己刚说的话；
                # 不排除的话，每说一句都会把自己判成「有新内容」（2026-11-11 踩过）。
                if fn.startswith("hermes-"):
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


def sw_label(conv, title):
    """会话的人话标签：标题（id 前 8 位）。"""
    if not conv and not title:
        return "（空）"
    return "%s（%s）" % (("《%s》" % title) if title else "无标题", (conv or "")[:8] or "?")


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


def _read(path):
    """读会话文件：封了就地解，没封直读。

    2026-11-11 踩过：库迁到 SPV1 密文后，本脚本还按 utf-8 硬读，
    尾部打出来全是乱码二进制 —— 看着像"抓取坏了"，其实是没走解密。
    """
    if _vio is not None:
        try:
            return _vio.text_of(path)
        except Exception:
            pass
    return io.open(path, encoding="utf-8", errors="replace").read()


def page_time():
    try:
        for line in _read(LATEST)[:800].splitlines():
            if "更新时间" in line:
                return line.split("：")[-1].strip()
    except OSError:
        pass
    return "?"


def tail(path, n):
    return "\n".join(_read(path).splitlines()[-n:])


NODE = os.environ.get("PERSONA_NODE") or _loc.node()
RELOAD_JS = os.path.join(ROOT, "tools/js/reload_page.js")
SESSIONS = os.path.join(ROOT, "脚本/persona_sessions.py")
LAST_CAPTURE = os.path.join(OPS, "web-last-capture.json")

# 抓取要连的调试浏览器：不通就拉 start_cdp_browser.ps1（独立 profile，默认无头）。
CDP_PORT = int(os.environ.get("PERSONA_CDP_PORT") or 9222)
CDP_LAUNCHER = os.environ.get("PERSONA_CDP_LAUNCHER") or \
    r"C:\Users\Huang\AppData\Local\SysPersona\start_cdp_browser.ps1"
POWERSHELL = os.environ.get("PERSONA_POWERSHELL") or \
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def poll_sessions(timeout=60):
    """抓之前先问一句：侧栏最上面几条变没变？返回 (退出码, 信息 dict)。

    退出码与 persona_sessions.py 的约定一致：
    0 没有新会话、3 有新会话（new_top）、4 没选中会话、2 CDP 不通/读不到侧栏。
    """
    try:
        r = subprocess.run([PY, SESSIONS, "poll", "--json"], cwd=ROOT,
                           capture_output=True, timeout=timeout,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return 2, {"reason": "调不起 persona_sessions.py：%s" % str(e)[:120]}
    raw = r.stdout or b""
    text = ""
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
    info = {}
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                info = json.loads(line)
                break
            except ValueError:
                continue
    if not info:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        info = {"reason": (err or "persona_sessions.py 没有输出")[:160]}
    return r.returncode, info


def read_last_capture():
    """daemon 抓完写的「抓的是哪个会话、落在哪个文件」。"""
    try:
        return json.load(io.open(LAST_CAPTURE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark_capture(lc):
    """抓完更新会话表（消息数 / 最后抓取时间 / 文件路径），只动它自己那一行。"""
    try:
        r = subprocess.run([PY, SESSIONS, "mark", "--json",
                            "--id", lc.get("conv_id") or "",
                            "--file", lc.get("file") or "",
                            "--msg-count", str(int(lc.get("msg_count") or 0))],
                           cwd=ROOT, capture_output=True, timeout=60,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0, (r.stdout or b"").decode("utf-8", "replace").strip()[-220:]
    except Exception as e:
        return False, str(e)[:120]


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
    if not os.path.exists(CDP_LAUNCHER):
        return False, "CDP 起不来：找不到启动脚本 %s" % CDP_LAUNCHER
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
    """跑一次抓取，返回 (ok, 摘要)。

    2026-11-12 人定：「暂时不要抓了，以后就在前台我手动 F5 抓」——
    后台（webcap / 心跳随机重抓 / 定时 capture）全部关停，只留**前台按需**这一条。
    所以这里显式给抓取总闸放行：`PERSONA_CAPTURE_NOW=1` 只有这条路会设，
    别的调用方（定时、心跳）拿不到，仍然被 `persona.conf.json` 的 `capture: 0` 拦住。
    """
    cmd = [PY, os.path.join(ROOT, "脚本/persona_daemon.py"), "--once", "capture"]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                       cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8",
                                          PERSONA_CAPTURE_NOW="1"))
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

    # ① 抓之前先看侧栏最上面几条（2026-11-10 人定的机制）：
    #    新会话永远出现在最上面一条，点开就落到第二条；只看 id+标题，不比对消息指纹。
    new_session = False
    sess_code, sess = poll_sessions()
    if sess_code == 3:
        new_session = True
        for it in sess.get("new_top") or []:
            print("新会话：%s（%s，第 %d 位）"
                  % (it.get("title") or "（无标题）", it.get("id") or "", it.get("pos") or 0))
        print("  判据：只看了侧栏前 %d 条的 id+标题（不比对消息指纹、不用锚点）。"
              % (sess.get("top") or 5))
        print("  分存：每个会话一份，%s（已存在就覆盖自己那份，不合并）"
              % (os.path.relpath(sess["current_file"], ROOT) if sess.get("current_file")
                 else "抓完按 conv_id 落到 sessions\\<日期>\\deepseek网页\\<id>.md"))
    elif sess_code == 4:
        print("页面没选中会话（地址停在根地址）：抓取时按「选第一条，不行第二条」先把会话选上。")
    elif sess_code == 2:
        print("✗ %s" % (sess.get("reason") or "CDP 不通：读不到页面"))
        print("  （抓取链路这一环断了：抓之前没能确认是哪个会话；抓完再按会话 id 归位）")
    else:
        print("侧栏前 %d 条没变：%s ｜ 第 %d 位 ｜ %d 条消息"
              % (sess.get("top") or 5,
                 sw_label(sess.get("current"), sess.get("current_title")),
                 sess.get("current_pos") or 0, sess.get("msg_count") or 0))
        for it in sess.get("moved") or []:
            print("  位置变化：%s 第 %d 位 → 第 %d 位"
                  % (it.get("title") or "?", it.get("from") or 0, it.get("to") or 0))
        for it in sess.get("renamed") or []:
            print("  标题变化：%s《%s》→《%s》"
                  % (it.get("id") or "", it.get("from_title") or "", it.get("to_title") or ""))

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

    # ② 抓完更新会话表：只动本次抓的那个会话（消息数 / 最后抓取时间 / 文件路径）。
    #    daemon 把「抓的是哪个会话、落在哪个文件」写进 ops\web-last-capture.json。
    if tries:
        lc = read_last_capture()
        if lc.get("ok") and lc.get("conv_id"):
            okm, _mmsg = mark_capture(lc)
            print("会话表%s：%s（%s）｜ %d 条 ｜ %s"
                  % ("已更新" if okm else "更新失败",
                     sw_label(lc.get("conv_id"), lc.get("title")),
                     lc.get("conv_id") or "", int(lc.get("msg_count") or 0),
                     os.path.relpath(lc["file"], ROOT) if lc.get("file") else "（无文件）"))
        elif lc:
            print("会话表没更新：本次抓取没成功（%s）"
                  % (lc.get("reason") or "没有抓到会话"))

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
        rec = {"at": fresh[0][0], "file": os.path.relpath(fresh[0][1], ROOT),
               "when": hm(fresh[0][0]),
               # 本次抓取是不是「新会话的第一份」：抓取链路据此知道
               # 这份是从新会话头上抓的，落的是它自己那份文件。
               "new_session": bool(new_session and tries > 0)}
        save_mark(rec)
        if rec["new_session"]:
            print("\n（本次是新会话的第一份抓取：new_session: true）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
