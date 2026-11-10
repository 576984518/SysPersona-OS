# -*- coding: utf-8 -*-
r"""persona_constitution_reloader.py — 设定重载器（Constitution Reloader）

**独立于心跳**。心跳（30 秒）只管"系统还活着"，本脚本只管"把宪法重新装回来"。
两件事物理隔离：不写进心跳主循环，不占它的时间片，也不会因为这里卡住而让心跳停摆。

为什么需要它：上下文被压缩后，摘要里没有的那部分规则就等于丢了；长会话里规则也会慢慢漂。
重载 = 把 `memory\设定\` 里的**硬条款**重新摆到对话最前面，让它们重新成为"当前生效"的约束。

三个触发（只允许这三种，别加）：
  ① `--reason compact`  压缩完成后立刻一次（由 persona_compact.py 自动调）
  ② `--reason daily`    每天早晨 / 开机后一次（计划任务，登录时 + 08:30）
  ③ `--reason manual`   托盘「强制重载」手动一次

冲突保护（硬要求）：**不许抢焦点打断他**。
  - 注入前先看 `GetLastInputInfo`：他最近 N 秒在敲 → 排队，不注入；
  - 排队不是丢：写 `ops/reloader-queue.json`，下一轮（或手动）接着来；
  - 真正注入走 `persona_inject`，它自己会点输入框、粘贴、回车，并**还原剪贴板**。

用法：
  python persona_constitution_reloader.py --reason manual --dry-run
  python persona_constitution_reloader.py --reason compact
  python persona_constitution_reloader.py --status
"""
import argparse
import io
import json
import os
import re
import sys
import time

try:
    import persona_win as _pw  # 无窗口
except Exception:
    pass

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
LOG = os.path.join(OPS, "reloader.jsonl")
QUEUE = os.path.join(OPS, "reloader-queue.json")
MAX_CHARS = 1600            # 注入正文上限：重载不是把设定全文灌进去
SETTINGS = [os.path.join(ROOT, "memory", "设定", n) for n in (
    "10-人格与红线.md", "20-交流与要求.md", "30-流程与运维.md")]


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read(path):
    try:
        return io.open(path, encoding="utf-8").read()
    except OSError:
        return ""


def _find_block(text, start_pat, end_pat, limit=1200):
    m = re.search(start_pat + r"(.*?)" + end_pat, text, re.S)
    return (m.group(1).strip() if m else "")[:limit]


def _bullets(text, after_pat, limit=8):
    """抽「- 」开头的条目（连同缩进续行），只取前 limit 条——不搬整节，搬整节就又变成读文件。"""
    seg = _find_block(text, after_pat, r"\n## ", limit=4000)
    out, cur = [], ""
    for line in seg.splitlines():
        if line.startswith("- "):
            if cur:
                out.append(cur)
            cur = line.rstrip()
        elif cur and line.startswith(("  ", "\t")) and line.strip():
            cur += "\n" + line.rstrip()
        elif cur and not line.strip():
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return "\n".join(out[:limit])


def _table(text, after_pat, limit=900):
    """抽一张 markdown 表格（含表头）。"""
    seg = _find_block(text, after_pat, r"\n\n", limit=limit)
    return seg.strip()


def build_text(reason="manual"):
    """从设定文件里**现抽**硬条款，不手抄——手抄的那份一定会过期。"""
    law = _read(SETTINGS[0])                       # 10-人格与红线
    talk = _read(SETTINGS[1])                      # 20-交流与要求
    ladder = _find_block(law, r"## 三、仲裁梯子.*?```", r"```")
    red = _bullets(law, r"## 四、人机边界", 6)
    rule00 = _find_block(talk, r"## 一、开场铁律.*?\n\n> ", r"\n\n", limit=200)
    steps = _table(talk, r"\| 步 \| 做什么 \| 不许 \|", 900)
    if steps:
        body = "\n".join(l for l in steps.splitlines()
                          if not re.match(r"^\|[\s:|-]+\|$", l.strip()))
        steps = "| 步 | 做什么 | 不许 |\n|---|---|---|\n" + body

    ctx = ""
    try:
        sys.path.insert(0, ROOT)
        import persona_gauge as G
        press, surface, win = G.dsh_pressure()
        if press:
            ctx = "水位：%s / %s（%.1f%%）" % (G.human(press), G.human(win or G.LIMIT),
                                             100.0 * press / (win or G.LIMIT))
    except Exception:
        pass

    parts = ["【宪法重载 · %s · %s】" % (reason, now()),
             "",
             "> 这不是提醒，是重载：下面几条现在重新生效。**读完先回一句「已重载」，再继续手上的事。**",
             ""]
    if ladder:
        parts += ["**一、仲裁梯子**（冲突时按这个顺序）", "```", ladder, "```", ""]
    if red:
        parts += ["**二、必须人点头的六件**", red, ""]
    if rule00:
        parts += ["**三、交互铁律**", "> " + rule00.strip(), ""]
    if steps:
        parts += ["**四、每回合四步**", steps, ""]
    parts += [
        "**五、压缩与落盘**：到线先镜像原文（sha256 自证），落不了盘就**不许压**；压完由本脚本重载一次。",
        "**六、唤醒三段**：醒 → 报 → 领；索引门查两次（派发时读过没有 / 接活时读的时刻晚于派发）。",
        "",
    ]
    if ctx:
        parts.append(ctx)
    text = "\n".join(parts)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…（超长截断；全文在 memory\\设定\\）"
    return text


def load_queue():
    try:
        return json.load(io.open(QUEUE, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def save_queue(d):
    try:
        os.makedirs(OPS, exist_ok=True)
        tmp = QUEUE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE)
    except OSError:
        pass


def log(rec):
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def human_busy(seconds=5):
    """他最近是否在敲键/点鼠标——是就别抢焦点。取不到信息时**当他在忙**（保守）。"""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return True
        ms = ctypes.windll.kernel32.GetTickCount() - li.dwTime
        return ms < seconds * 1000
    except Exception:
        return True


def run(reason="manual", dry_run=False, idle_wait=5, queue_if_busy=True,
        only_if_needed=False):
    if only_if_needed:
        try:
            sys.path.insert(0, ROOT)
            import persona_wake as W
            need, why = W.reread_need()
        except Exception:
            need, why = False, "判定不了"
        if not need:
            log({"at": now(), "reason": reason, "action": "skip",
                 "msg": why or "不需要"})
            print("跳过：设定没变、也没被标记（%s）" % (why or "不需要"))
            return 0
        print("需要重载：%s" % why)
    text = build_text(reason)
    if dry_run:
        print(text)
        print("\n[DRY] %d 字，未注入" % len(text))
        return 0
    if human_busy(idle_wait):
        if queue_if_busy:
            q = load_queue()
            q[reason] = {"at": now(), "chars": len(text)}
            save_queue(q)
            log({"at": now(), "reason": reason, "action": "queued",
                 "msg": "他最近 %d 秒在操作，不抢焦点" % idle_wait})
            print("已排队：他正在操作，等他这一轮结束再重载（ops/reloader-queue.json）")
            return 0
        print("放弃：他正在操作")
        return 1
    try:
        import persona_inject as pi
    except Exception as e:
        print("FAIL 导入注入模块失败：%s" % e)
        log({"at": now(), "reason": reason, "action": "fail", "msg": str(e)[:120]})
        return 2
    ok, msg = pi.inject(text, wait_idle=idle_wait)
    log({"at": now(), "reason": reason, "action": "fired" if ok else "fail",
         "chars": len(text), "msg": msg})
    if ok:                                       # 重载成功 = 设定这一轮读过了
        try:
            sys.path.insert(0, ROOT)
            import persona_wake as W
            W.cmd_reread(argparse.Namespace(ack=True, check=False, auto=False))
        except Exception:
            pass
        q = load_queue()
        q.pop(reason, None)
        save_queue(q)
    print(("OK   " if ok else "FAIL ") + "重载（%s，%d 字）：%s" % (reason, len(text), msg))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="设定重载器（独立于心跳）")
    ap.add_argument("--reason", default="manual",
                    choices=["compact", "daily", "manual", "periodic"])
    ap.add_argument("--dry-run", action="store_true", help="只打印要注入的正文")
    ap.add_argument("--wait-idle", type=int, default=5, help="他最近 N 秒在操作就排队（默认 5）")
    ap.add_argument("--no-queue", action="store_true", help="忙就放弃，不排队")
    ap.add_argument("--only-if-needed", action="store_true", dest="only_if_needed",
                    help="只在「设定变了 / 被标记」时才注入（5 分钟巡检用）")
    ap.add_argument("--status", action="store_true", help="看队列与最近一次重载")
    a = ap.parse_args()
    if a.status:
        print("队列：%s" % (json.dumps(load_queue(), ensure_ascii=False) or "（空）"))
        try:
            last = [json.loads(l) for l in io.open(LOG, encoding="utf-8") if l.strip()][-3:]
            for r in last:
                print("  %s  %-6s %-6s %s" % (r.get("at"), r.get("reason"),
                                              r.get("action"), (r.get("msg") or "")[:60]))
        except (OSError, ValueError):
            print("  （还没有重载记录）")
        return 0
    return run(a.reason, a.dry_run, a.wait_idle, not a.no_queue, a.only_if_needed)


if __name__ == "__main__":
    sys.exit(main())
