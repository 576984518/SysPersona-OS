# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_logs.py — 日志查看器（专治「打开全是乱码」）
r"""persona_logs.py — 日志查看器（专治「打开全是乱码」）

为什么要有它：
  `ops\*.jsonl` / `ops\*.log` **都是 UTF-8**，但记事本、Excel、部分编辑器默认按 GBK(ANSI)
  解码，于是 `水位` 显示成 `姘翠綅`。文件没坏，是解码方式错了。

它做三件事：
  1. 看：按名字看某个日志的最后 N 条，**自己解对编码**（UTF-8/UTF-8-BOM/UTF-16/GBK 都试）；
  2. 查：`--scan` 把 ops 下所有日志的真实编码列一遍，找出被写坏的文件；
  3. 修：`--fix` 把非 UTF-8 的文件就地转成 UTF-8（先备份 .bak），把坏字节换成 `?`。

用法：
  python persona_logs.py                    # 菜单：列出可看的日志
  python persona_logs.py 重载 --tail 30
  python persona_logs.py 后台 --tail 80
  python persona_logs.py --scan
  python persona_logs.py --fix
  python persona_logs.py --path "ops\compact.jsonl" --tail 20
"""
import argparse
import glob
import io
import json
import os
import sys

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")

# 名字 → 文件（按人嘴里会说的词命名，不按代码文件名）
SOURCES = [
    ("后台", "persona.log", "心跳/任务调度主日志（纯文本）"),
    ("压缩", "compact.jsonl", "水位检查与 /compact 发射记录"),
    ("重载", "reloader.jsonl", "设定重载器：跳过/排队/补发/注入"),
    ("唤醒", "wake-fires.jsonl", "心跳唤醒与三段式进度"),
    ("派发", "dispatch.jsonl", "任务派发与领取"),
    ("注入", "inject.jsonl", "往壳里敲字的记录"),
    ("重抓", "recapture.jsonl", "心跳驱动的随机重抓"),
    ("观察", "observe.jsonl", "守卫观察"),
    ("名单", "sf-models.jsonl", "硅基免费名单抓取"),
    ("评审", "review.jsonl", "二号模型评审"),
    ("交接", "handoff", None),          # 目录：跳过
    ("自检", "daily-status.md", "每日自检报告（markdown）"),
    ("状态", "persona-status.json", "当前状态快照"),
]

FIELDS = ("at", "time", "ts", "date")
ACTION = ("action", "event", "kind", "cmd", "task")
MSG = ("msg", "message", "detail", "reason", "note")


def _decode(raw):
    """把字节解成文本。顺序：UTF-8-BOM → UTF-8 → UTF-16LE → GBK。返回 (文本, 编码名)。"""
    if not raw:
        return "", "空"
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-bom"
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xff\xfe"):
        try:
            return raw[2:].decode("utf-16-le"), "utf-16le"
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xfe\xff"):
        try:
            return raw[2:].decode("utf-16-be"), "utf-16be"
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("utf-8", "replace"), "utf-8(有坏字节)"
    except Exception:
        pass
    try:
        return raw.decode("gbk", "replace"), "gbk"
    except Exception:
        return raw.decode("latin-1", "replace"), "latin-1"


def resolve(name_or_path):
    p = name_or_path
    if os.path.exists(p):
        return p, os.path.basename(p)
    for alias, fn, _desc in SOURCES:
        if name_or_path in (alias, fn):
            return os.path.join(OPS, fn), fn
    hits = [f for f in glob.glob(os.path.join(OPS, "*" + name_or_path + "*"))]
    if hits:
        return hits[0], os.path.basename(hits[0])
    return "", ""


def fmt_jsonl_line(line, width=118):
    """一条 JSONL → 一行给人看的话；解不开就原样输出（不吞证据）。"""
    line = line.rstrip("\r\n")
    if not line.strip():
        return ""
    try:
        rec = json.loads(line)
    except ValueError:
        return line
    if not isinstance(rec, dict):
        return line
    at = next((str(rec[k]) for k in FIELDS if rec.get(k)), "")
    act = next((str(rec[k]) for k in ACTION if rec.get(k)), "")
    msg = ""
    for k in MSG:
        v = rec.get(k)
        if v:
            msg = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            break
    extra = ""
    if msg:                           # 只有正文来自 msg 字段时才补数字摘要
        extra = " ".join("%s=%s" % (k, v) for k, v in rec.items()
                         if k not in FIELDS + ACTION + MSG and isinstance(v, (int, float, bool)))
    if not msg:                       # 没有 msg 就把剩余字段压成一行（常见字段说人话）
        rest = {k: v for k, v in rec.items() if k not in FIELDS + ACTION}
        friendly = []
        if isinstance(rest.get("ctx"), (int, float)):
            friendly.append("水位 %dK" % (int(rest.pop("ctx")) // 1000))
        if isinstance(rest.get("chars"), (int, float)):
            friendly.append("%d 字" % int(rest.pop("chars")))
        if rest.pop("flush", None):
            friendly.append("补发")
        if rest:
            friendly.append(json.dumps(rest, ensure_ascii=False))
        msg = "  ".join(friendly)
    if extra:
        msg = "%s  (%s)" % (msg, extra)
    if len(at) >= 19:
        at = at[:19]
    s = "%-19s %-10s %s" % (at, act[:10], msg)
    return s if len(s) <= width else s[:width - 1] + "…"


def show(name_or_path, tail=40, raw=False, grep=None):
    path, label = resolve(name_or_path)
    if not path:
        print("找不到这个日志：%s" % name_or_path)
        print("用 python persona_logs.py 看有哪些。")
        return 2
    raw_bytes = io.open(path, "rb").read()
    text, enc = _decode(raw_bytes)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if grep:
        lines = [l for l in lines if grep in l] or lines[:0]
    if tail and len(lines) > tail:
        lines = lines[-tail:]
    print("== %s（%s，编码 %s，共 %d 字节）==" % (label, path, enc, len(raw_bytes)))
    for line in lines:
        print(line if (raw or not path.endswith(".jsonl")) else fmt_jsonl_line(line))
    return 0


def scan(verbose=True):
    """列 ops 下每个文件的真实编码，标出不是干净 UTF-8 的。"""
    bad = []
    rows = []
    for f in sorted(glob.glob(os.path.join(OPS, "*"))):
        if not os.path.isfile(f):
            continue
        if os.path.splitext(f)[1].lower() not in (".jsonl", ".log", ".json", ".md", ".txt", ""):
            continue
        raw = io.open(f, "rb").read()
        if not raw:
            continue
        try:
            raw.decode("utf-8")
            enc = "utf-8" + ("-bom" if raw.startswith(b"\xef\xbb\xbf") else "")
            ok = True
        except UnicodeDecodeError:
            _, enc = _decode(raw)
            ok = False
            bad.append(f)
        rows.append((os.path.basename(f), len(raw), enc, ok))
    if verbose:
        print("%-28s %10s  %-18s %s" % ("文件", "字节", "编码", "状态"))
        for name, size, enc, ok in rows:
            print("%-28s %10d  %-18s %s" % (name, size, enc, "OK" if ok else "!! 不是干净 UTF-8"))
        print("\n合计 %d 个文件，%d 个有问题。" % (len(rows), len(bad)))
    return [os.path.basename(b) for b in bad]


def fix(path, backup=True):
    """把一个文件就地转成干净 UTF-8（BOM 保持原样：原来有就还有）。"""
    raw = io.open(path, "rb").read()
    if not raw:
        return False, "空文件", ""
    try:
        raw.decode("utf-8")
        return False, "本来就是干净 UTF-8", ""
    except UnicodeDecodeError:
        pass
    text, enc = _decode(raw)
    text = text.encode("utf-8", "replace").decode("utf-8")   # 坏字节 → ?
    if backup and not os.path.exists(path + ".bak"):
        io.open(path + ".bak", "wb").write(raw)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True, "已转成 UTF-8（原编码 %s，坏字节已替换）" % enc, path + ".bak"


def menu():
    print("后台日志（直接选数字）")
    print("-" * 56)
    print("  0  查所有日志的编码（看谁被写坏了）")
    print("  f  修复非 UTF-8 的日志（先备份 .bak）")
    for i, (alias, fn, desc) in enumerate(SOURCES, 1):
        mark = "" if os.path.exists(os.path.join(OPS, fn)) else "  （无）"
        print("  %-2d %-4s %-22s %s%s" % (i, alias, fn, desc or "", mark))
    print("-" * 56)
    print("用法：python persona_logs.py 重载 --tail 30 ｜ 或双击 看日志.cmd")


def interactive():
    """双击进来时的简易菜单：选数字看，回车再看，q 退出。"""
    while True:
        menu()
        try:
            pick = input("\n看哪个？（数字/别名，q 退出）> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if pick.lower() in ("q", "quit", "exit", ""):
            return 0
        if pick == "0":
            scan(verbose=True)
        elif pick.lower() == "f":
            bad = scan(verbose=False)
            for name in bad:
                changed, msg, _bak = fix(os.path.join(OPS, name))
                print("%-28s %s" % (name, msg))
            if not bad:
                print("全部是干净 UTF-8。")
        else:
            if pick.isdigit() and 1 <= int(pick) <= len(SOURCES):
                pick = SOURCES[int(pick) - 1][0]      # 数字 → 别名
            show(pick, 40, False, None)
        try:
            input("\n回车继续…")
        except (EOFError, KeyboardInterrupt):
            return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="看后台日志（自动识别编码，不乱码）")
    ap.add_argument("name", nargs="?", help="日志别名或文件名，如 重载 / reloader.jsonl")
    ap.add_argument("--tail", type=int, default=40, help="只看最后 N 条（默认 40，0 = 全部）")
    ap.add_argument("--raw", action="store_true", help="JSONL 不排版，原样输出")
    ap.add_argument("--grep", default="", help="只留包含这个词的行")
    ap.add_argument("--path", default="", help="直接给绝对路径")
    ap.add_argument("--scan", action="store_true", help="列出所有日志的编码")
    ap.add_argument("--fix", action="store_true", help="把所有非 UTF-8 日志转成 UTF-8")
    ap.add_argument("-i", "--interactive", action="store_true", help="菜单模式（双击用）")
    a = ap.parse_args()
    if a.interactive:
        return interactive()
    if a.fix:
        bad = scan(verbose=False)
        if not bad:
            print("全部是干净 UTF-8，没什么可修的。")
            return 0
        for name in bad:
            changed, msg, bak = fix(os.path.join(OPS, name))
            print("%-28s %s" % (name, msg))
        return 0
    if a.scan:
        scan(verbose=True)
        return 0
    if a.path:
        return show(a.path, a.tail, a.raw, a.grep or None)
    if not a.name:
        menu()
        return 0
    return show(a.name, a.tail, a.raw, a.grep or None)


if __name__ == "__main__":
    sys.exit(main())
