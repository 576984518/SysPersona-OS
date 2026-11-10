# -*- coding: utf-8 -*-
r"""persona_compact.py — 水位到线就自己压缩（用注入通道发 /compact）

链路（2026-11-10 定）：
  量水位（persona_gauge） → 到线了吗？
     ├─ 没到 → 什么都不做，只记一笔
     └─ 到了 → 冷却过了吗？
              ├─ 没冷却 → 跳过（防抖，不连着压）
              └─ 冷却过 → persona_inject 把 `/compact` 打进壳的输入框

为什么这么干：壳自己有 `/compact`，但只在"人空闲时手动敲"才生效；
我们已经有注入通道，就不用再实现一套压缩，直接替人敲一下。

用法：
  python persona_compact.py check            # 到线就压
  python persona_compact.py check --dry-run  # 只报告会不会压
  python persona_compact.py check --threshold 350000 --cooldown 900
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

try:
    import persona_win as _pw  # 无窗口
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
STATE = os.path.join(OPS, "compact-state.json")
LOG = os.path.join(OPS, "compact.jsonl")
PY = sys.executable
GAUGE = os.path.join(ROOT, "persona_gauge.py")
HOME = os.path.expanduser("~")
NODE = os.environ.get("PERSONA_NODE") or r"D:\ProgramData\SysPersona-Runtime\node\bin\node.exe"
EXPORTER = os.path.join(ROOT, "dsh_export.js")
MIRROR = os.path.join(ROOT, "persona_mirror.py")
DEFAULT_THRESHOLD = 350_000      # 到 350K 就该压了（壳的自动线是 ~367K，这里提前一点）
DEFAULT_COOLDOWN = 900           # 15 分钟内不连压
FRESH_EXPORT = 60                # 1 分钟内导过就不再导（同一分钟重试别重复落盘）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def measure():
    """跑 gauge 拿当前水位（K 为单位换算成 token）。

    编码坑（2026-11-10 踩）：gauge 子进程在计划任务里按 **GBK** 吐中文，
    按 utf-8 硬解会得到一串 U+FFFD，`[上下文]` 就对不上、误报「没有输出水位」。
    所以：**收字节，两种编码都试**；正则也不依赖中文，认第一个方括号后的数字。
    """
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([PY, GAUGE], capture_output=True, timeout=180,
                           cwd=ROOT, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = r.stdout or b""
        out = ""
        for enc in ("utf-8", "gbk"):
            try:
                out = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not out:
            out = raw.decode("utf-8", "replace")
        m = re.search(r"\[[^\]\r\n]*\]\s*~?([\d.]+)\s*([KM]?)", out)
        if not m:
            return 0, "gauge 没有输出水位（原始输出 %d 字节）" % len(raw)
        val = float(m.group(1))
        unit = m.group(2)
        tok = val * (1_000_000 if unit == "M" else 1000 if unit == "K" else 1)
        return int(tok), ""
    except Exception as e:
        return 0, "gauge 调用失败：%s" % e


def load_state():
    try:
        return json.load(io.open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(d):
    try:
        os.makedirs(OPS, exist_ok=True)
        tmp = STATE + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=1))
        os.replace(tmp, STATE)
    except OSError:
        pass


def log(rec):
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def latest_session_dir():
    """最近在写的 DSH 会话文件（导出入口）。"""
    import glob
    best, best_t = "", 0.0
    for f in glob.glob(os.path.join(HOME, ".dsh", "sessions", "*", "*", "session.v4.jsonl.zstd")):
        try:
            t = os.path.getmtime(f)
        except OSError:
            continue
        if t > best_t:
            best, best_t = f, t
    return best


def _decode(raw):
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def ensure_mirror(trigger=""):
    """**压之前先把原文落盘**（`BACKGROUND_SPEC` §七 唯一不可降级的一条）。

    顺序：导出当前 DSH 会话 → 镜像进 `archive\\` 并校验 sha256。
    返回 (ok, msg)。ok=False 时调用方**不许压缩**。

    为什么每次开火都现导，而不是复用半小时前那份：
    差的这几十分钟，正好是刚刚聊出来的内容——把它压掉，等于把最该留的丢了。
    """
    import glob
    sess = latest_session_dir()
    if not sess:
        return False, "找不到 DSH 会话，无法落盘"
    srcs = glob.glob(os.path.join(ROOT, "sessions", "*", "电脑会话", "续接前-*.md"))
    srcs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    src = ""
    if srcs and (time.time() - os.path.getmtime(srcs[0])) < FRESH_EXPORT:
        src = srcs[0]
    if not src:
        stamp = time.strftime("%Y%m%d-%H%M")
        src = os.path.join(ROOT, "sessions", time.strftime("%Y-%m-%d"), "电脑会话",
                           "续接前-%s.md" % stamp)
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            r = subprocess.run([NODE, EXPORTER, os.path.dirname(sess), src],
                               capture_output=True, timeout=300, cwd=ROOT,
                               creationflags=NO_WINDOW)
            if r.returncode != 0 or not os.path.exists(src):
                return False, "导出失败（rc=%s）：%s" % (r.returncode, _decode(r.stderr)[:160])
        except Exception as e:
            return False, "导出异常：%s" % e
    try:
        r = subprocess.run([PY, MIRROR, src, "--trigger", trigger or "compact"],
                           capture_output=True, timeout=180, cwd=ROOT,
                           creationflags=NO_WINDOW)
        out = _decode(r.stdout).strip()
        if r.returncode != 0:
            return False, "镜像校验失败：%s" % (out[:200] or _decode(r.stderr)[:200])
        return True, out.splitlines()[-1] if out else "已镜像"
    except Exception as e:
        return False, "镜像异常：%s" % e


def ask_reloader():
    """顺手问一句设定重载器：要不要因为「记忆开始不集中」补一次。

    重载不是按时间，是按水位带（`persona_constitution_reloader.band_due`）——
    这里只负责"到了检查的时候"，判不判、注不注由它自己看水位。
    """
    try:
        rl = os.path.join(ROOT, "persona_constitution_reloader.py")
        if not os.path.exists(rl):
            return
        subprocess.Popen([PY, rl, "--reason", "periodic", "--only-if-needed"],
                         cwd=ROOT, creationflags=NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def check(threshold=DEFAULT_THRESHOLD, cooldown=DEFAULT_COOLDOWN, dry_run=False,
          do_mirror=True, force_ctx=0, timeout=600):
    ctx, err = measure()
    if force_ctx:                      # 极限实测用：不改真实文件，只假装量到了这个数
        ctx, err = int(force_ctx), ""
        print("TEST 强制水位 %dK（真实值不参与判定）" % (ctx // 1000))
    if err:
        print("FAIL " + err)
        return 2
    st = load_state()
    last = float(st.get("last_fire", 0) or 0)
    age = time.time() - last
    if ctx < threshold:
        print("SKIP 水位 %dK < 线 %dK，不压" % (ctx / 1000, threshold / 1000))
        log({"at": now(), "ctx": ctx, "action": "idle"})
        ask_reloader()
        return 0
    if age < cooldown:
        print("SKIP 水位 %dK 到线了，但 %d 秒前刚压过（冷却 %d 秒）"
              % (ctx / 1000, int(age), cooldown))
        log({"at": now(), "ctx": ctx, "action": "cooldown"})
        return 0
    if dry_run:
        print("DRY  水位 %dK ≥ 线 %dK，会先导出并镜像原文，再发 /compact（冷却已过）"
              % (ctx / 1000, threshold / 1000))
        return 0
    if do_mirror:
        mok, mmsg = ensure_mirror()
        if not mok:
            print("FAIL 镜像没落盘，按规矩不许压：%s" % mmsg)
            log({"at": now(), "ctx": ctx, "action": "no-mirror", "msg": mmsg})
            return 1
        print("MIRROR " + mmsg)
    try:
        import persona_inject as pi
    except Exception as e:
        print("FAIL 导入注入模块失败：%s" % e)
        return 2
    # 先等**壳**空下来再敲：10:15 那次撞上正在生成的那一轮，回车被吞。
    iok, waited, imsg = pi.wait_shell_idle(stable=15, timeout=timeout)
    print("等待：%s（%.0f 秒）" % (imsg, waited))
    if not iok:
        log({"at": now(), "ctx": ctx, "action": "busy", "msg": imsg})
        print("FAIL 壳一直在动，这轮不压（下一轮再来）")
        return 1
    ok, msg = pi.inject("/compact", wait_idle=5)
    st["last_fire"] = time.time()
    st["last_ctx"] = ctx
    save_state(st)
    pi.log({"at": now(), "len": 8, "dry": False, "ok": ok, "msg": msg, "src": "compact"})
    log({"at": now(), "ctx": ctx, "action": "fired" if ok else "fail", "msg": msg})
    # 压缩之后由**独立的**设定重载器把宪法装回来（2026-11-10 定）：
    # 不是塞进心跳，而是另起一个进程——它卡住也不影响心跳，反之亦然。
    if ok:
        try:
            reloader = os.path.join(ROOT, "persona_constitution_reloader.py")
            if os.path.exists(reloader):
                subprocess.Popen([PY, reloader, "--reason", "compact"],
                                 cwd=ROOT, creationflags=NO_WINDOW,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("已叫设定重载器（--reason compact）")
        except Exception as e:
            print("（重载器没叫起来：%s）" % str(e)[:80])
    print(("OK   " if ok else "FAIL ") + "水位 %dK → 发送 /compact：%s" % (ctx / 1000, msg))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    c.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--ctx", type=int, default=0, help="极限实测：假装水位是这个数")
    c.add_argument("--no-mirror", action="store_true",
                   help="跳过镜像（只用于测试；生产环境不许用——没落盘就压等于拿记忆赌运气）")
    a = ap.parse_args()
    if a.cmd != "check":
        ap.print_help()
        return 0
    return check(a.threshold, a.cooldown, a.dry_run, not a.no_mirror, a.ctx)


if __name__ == "__main__":
    sys.exit(main())
