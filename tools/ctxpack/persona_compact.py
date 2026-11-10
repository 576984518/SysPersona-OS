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
DEFAULT_THRESHOLD = 350_000      # 到 350K 就该压了（壳的自动线是 ~367K，这里提前一点）
DEFAULT_COOLDOWN = 900           # 15 分钟内不连压


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


def check(threshold=DEFAULT_THRESHOLD, cooldown=DEFAULT_COOLDOWN, dry_run=False):
    ctx, err = measure()
    if err:
        print("FAIL " + err)
        return 2
    st = load_state()
    last = float(st.get("last_fire", 0) or 0)
    age = time.time() - last
    if ctx < threshold:
        print("SKIP 水位 %dK < 线 %dK，不压" % (ctx / 1000, threshold / 1000))
        log({"at": now(), "ctx": ctx, "action": "idle"})
        return 0
    if age < cooldown:
        print("SKIP 水位 %dK 到线了，但 %d 秒前刚压过（冷却 %d 秒）"
              % (ctx / 1000, int(age), cooldown))
        log({"at": now(), "ctx": ctx, "action": "cooldown"})
        return 0
    if dry_run:
        print("DRY  水位 %dK ≥ 线 %dK，会发 /compact（冷却已过）" % (ctx / 1000, threshold / 1000))
        return 0
    try:
        import persona_inject as pi
    except Exception as e:
        print("FAIL 导入注入模块失败：%s" % e)
        return 2
    ok, msg = pi.inject("/compact", wait_idle=5)
    st["last_fire"] = time.time()
    st["last_ctx"] = ctx
    save_state(st)
    pi.log({"at": now(), "len": 8, "dry": False, "ok": ok, "msg": msg, "src": "compact"})
    log({"at": now(), "ctx": ctx, "action": "fired" if ok else "fail", "msg": msg})
    print(("OK   " if ok else "FAIL ") + "水位 %dK → 发送 /compact：%s" % (ctx / 1000, msg))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    c.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    c.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd != "check":
        ap.print_help()
        return 0
    return check(a.threshold, a.cooldown, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
