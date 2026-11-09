# -*- coding: utf-8 -*-
"""
persona_gauge.py — 上下文守卫：测当前上下文、到线就准备"接续包"

  python persona_gauge.py                 # 报当前上下文规模与水位
  python persona_gauge.py --prepare       # 生成接续包 v2（导出本会话 + 汇总状态）

原理：cost-meter 账本里 cacheRead/calls 就是"每次调用携带的上下文规模"。
     这是唯一不用猜的实测口径。

接续包 v2（2026-11-08 重定义）：
  L0 铁律（0.0 交互 + 5.3/5.4/5.6 红线）→ L1 现状 + 待办 → L2 指针。
  只带"必读"，其余用指针指回台账/待办/原文；硬上限 3000 字（约 9 KB）。
  超了就截断并注明——宁短，不要一份没人读完的长包。
"""
import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
LEDGER = os.path.join(HOME, ".dsh", "storages", "cost-meter", "ledger.json")
ROOT = os.environ.get("SYSPERSONA_ROOT") or r"D:\4590-PC-文件\AI会话检索记录"
NODE = r"D:\ProgramData\SysPersona-Runtime\node\bin\node.exe"

# 阈值只在一处定义：persona_limits.py（规范见 docs/BACKGROUND_SPEC.md §二）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from persona_limits import OK, CLEAN, WARN, MIRROR, LIMIT, PACK_LIMIT  # noqa: E402


def human(n):
    return "%.0fK" % (n / 1000) if n < 1_000_000 else "%.2fM" % (n / 1_000_000)


def current():
    data = json.loads(io.open(LEDGER, encoding="utf-8", errors="replace").read())
    days = data.get("days") or {}
    day = days[sorted(days)[-1]]
    rec = max(day.get("sessions") or [], key=lambda s: s.get("calls", 0))
    calls = max(1, rec.get("calls", 1))
    return {
        "session": rec.get("id", "?"),
        "calls": rec.get("calls", 0),
        "cache": rec.get("cacheRead", 0),
        "input": rec.get("input", 0),
        "output": rec.get("output", 0),
        "cost": rec.get("costCny", 0),
        # 上下文只认"最近一轮输入"；账本里的 cacheRead 是**累计**，除以调用数只是估算。
        "ctx": _estimate(rec, calls),
        "estimated": True,
    }


def _estimate(rec, calls):
    """账本估算：累计缓存 / 调用次数。只在拿不到实测时用，输出带 `~`。"""
    try:
        return int(rec.get("cacheRead", 0) / max(1, calls))
    except Exception:
        return 0


HERMES_UI_DB = os.environ.get("PERSONA_HERMES_UI_DB") or \
    r"D:\ProgramData\Hermes Agent CN Desktop\data\hermes-home\desktop-ui.sqlite"


def hermes_ctx():
    """上下文 = 最近 20 轮里**最大**的 `tokens_input`（含其模型与时间）。

    为什么取最大：同一会话里会夹着本地小模型（Qwen3:0.6B）的轮次，它的输入很小；
    只看"最后一轮"会把大水位误判成安全。宁可按保守值提醒，也不让人撞线。
    取不到返回 (0, "", "")。
    """
    if not os.path.exists(HERMES_UI_DB):
        return 0, "", ""
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % HERMES_UI_DB.replace("\\", "/"), uri=True)
        try:
            row = con.execute(
                "select tokens_input, model, id from turn_stats where tokens_input is not null "
                "order by id desc limit 20").fetchall()
        finally:
            con.close()
    except Exception:
        return 0, "", ""
    if not row:
        return 0, "", ""
    best = max(row, key=lambda r: int(r[0] or 0))
    return int(best[0] or 0), (best[1] or ""), (best[2] or "")


def level(ctx):
    """一维尺子：只按大小判，不跳档（2026-11-09 修：原来 HOT 先判，会越过 CLEAN）。"""
    if ctx >= CLEAN:
        return "该交接", "到 %s 了：先镜像、再出包；删记忆仍须你点头" % human(CLEAN)
    if ctx >= WARN:
        return "准备", "快到 %s：可以先把接续包备好" % human(CLEAN)
    if ctx >= OK:
        return "注意", "还能撑，但别再加长文档"
    return "OK", "健康"


def report(ctx=None):
    """上下文口径：**每轮真正带进去的 token**，不是累计缓存。

    两个读数都摆出来，取大者判定：
      - 账本均值：`cacheRead / calls`（估算，历史全量，标 `~`）；
      - 实测最大：`turn_stats.tokens_input` 最近 20 轮的最大值（Hermes 侧）。
    取大不取小：低估水位会让人撞线，高估最多早出一次包。
    """
    c = current()
    real, model, tid = hermes_ctx()
    est = c["ctx"]
    use = max(est, real)
    if real and real > est:
        c["estimated"] = False
    if ctx:
        use = ctx               # 测试/复算用：不依赖账本也能验证到线行为
        c["estimated"] = False
    c["ctx"] = use
    lv, tip = level(use)
    pct = 100.0 * use / LIMIT
    print("[上下文] %s%s · %s（%.1f%% / 上限 %s）· 调用 %d 次 · ￥%.3f"
          % ("~" if c.get("estimated") else "", human(use), lv, pct,
             human(LIMIT), c["calls"], c["cost"]))
    if not ctx:
        print("         读数：账本均值 ~%s（累计缓存÷调用 %d 次）· 实测最大 %s%s"
              % (human(est), c["calls"], human(real) if real else "（取不到）",
                 ("（%s）" % model) if model else ""))
    print("         建议：%s" % tip)
    return c


def latest_session_dir():
    best, best_t = None, 0
    for f in glob.glob(os.path.join(HOME, ".dsh", "sessions", "*", "*", "session.v4.jsonl.zstd")):
        t = os.path.getmtime(f)
        if t > best_t:
            best, best_t = f, t
    return best


def copy_to_clipboard(text):
    """把文本塞进剪贴板。返回成功与否——失败不该让整个出包流程算失败。"""
    try:
        p = subprocess.run("clip", input=text.encode("utf-16-le"), shell=True)
        return p.returncode == 0
    except Exception as e:
        print("复制剪贴板异常: %s" % e)
        return False


# ---- 从台账里"按小节取、按行压缩"：不手抄，免得台账改了包没改 ----

def _section(txt, start, stops=("\n## ", "\n### ")):
    i = txt.find(start)
    if i < 0:
        return ""
    j = len(txt)
    for stop in stops:
        k = txt.find(stop, i + len(start))
        if k > 0:
            j = min(j, k)
    return txt[i:j].strip()


def _bullets(txt, start=None, cap=99):
    """取条目行；不以条目符号开头的后续行接回上一条——否则一句话会被腰斩。

    start 为 None 时，txt 本身已经是某一小节的正文（调用方已经切好）。
    """
    sec = _section(txt, start) if start else txt
    out = []
    for line in sec.splitlines():
        s = line.strip()
        if re.match(r"^(-|>|\d+\.)\s", s):
            out.append(s)
        elif out and s and not s.startswith(("#", "|", "```")):
            out[-1] += s
    return out[:cap]


def _fence(sec):
    m = re.search(r"```[^\n]*\n(.*?)```", sec, re.S)
    if not m:
        return []
    return [l.rstrip() for l in m.group(1).splitlines() if l.strip()]


def _rows(sec, limit=None):
    rows = [l for l in sec.splitlines()
            if l.startswith("|") and not re.match(r"^\|[\s:|-]+\|?$", l)]
    rows = [r for r in rows if not re.match(r"^\|\s*#\s*\|", r)]
    rows = [r for r in rows if _cells(r)[0] not in ("—", "-", "")]
    return rows[-limit:] if limit else rows


def _cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _clip(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def _session_id(path):
    try:
        head = io.open(path, encoding="utf-8", errors="replace").read(800)
    except OSError:
        return ""
    m = re.search(r"- 会话：`([^`]+)`", head)
    return m.group(1) if m else ""


def _last_assistant(path, cap=300):
    """上一轮收尾：取会话原文里最后两条消息（你一句、我一句），压成一段。

    2026-11-09 改：原来取「另一个会话」的最后一条，DSH 会话不导出时会拿到很旧的段；
    现在优先取本次会话的导出（target），两条并排，读的人一眼知道刚才聊到哪。
    """
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    blocks = re.split(r"\n## ", text)
    msgs = [b for b in blocks[1:] if b.startswith(("用户", "助手"))]
    if not msgs:
        return ""
    pick = msgs[-2:] if len(msgs) >= 2 else msgs[-1:]
    out = []
    for b in pick:
        role, _, body = b.partition("\n")
        body = "\n".join(l for l in body.strip().splitlines()
                         if not l.strip().startswith(("|", "#", "```")))
        body = re.sub(r"\s*\n\s*", " ", body).strip()
        body = body.split("🔧")[0].strip(" -—")   # 工具调用不入收尾段
        out.append("%s：%s" % ("你" if role.startswith("用户") else "我", _clip(body, cap // len(pick))))
    return " / ".join(out)


def _l1_rows(limit=6):
    p = os.path.join(ROOT, "index", "L1.jsonl")
    if not os.path.exists(p):
        return []
    try:
        rows = [json.loads(l) for l in io.open(p, encoding="utf-8") if l.strip()]
    except Exception:
        return []
    return rows[:limit]


def prepare(c=None, clip=True, canonical=False):
    """canonical=True：写到固定的 `接续包-最新.md`（覆盖），给周期任务用。

    定时快照不该每 30 分钟就堆一份会话原文，所以 1 小时内已有导出就直接复用。
    """
    c = c or current()
    out_dir = os.path.join(ROOT, "续接")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    md = os.path.join(out_dir, "接续包-最新.md" if canonical else "接续包-%s.md" % stamp)

    # 先记住"上一轮"的原文，再导出本轮——否则 newest 会变成刚导出的这一份。
    old = sorted(glob.glob(os.path.join(ROOT, "sessions", "*", "电脑会话", "续接前-*.md")),
                 key=os.path.getmtime, reverse=True)

    sess = latest_session_dir()
    export_line = "（未导出）"
    target = ""
    fresh = old and (time.time() - os.path.getmtime(old[0]) < 3600)
    if canonical and fresh:
        export_line = old[0]          # 一小时内的导出够用，别每个周期都生成一份
        target = old[0]               # 让"上一轮"仍取另一个会话
    elif sess:
        target = os.path.join(ROOT, "sessions", time.strftime("%Y-%m-%d"), "电脑会话", "续接前-%s.md" % stamp)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            r = subprocess.run([NODE, os.path.join(ROOT, "dsh_export.js"), os.path.dirname(sess), target],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            export_line = target if r.returncode == 0 else "导出失败: " + r.stderr[:120]
        except Exception as e:
            export_line = "导出异常: %s" % e

    dec_path = os.path.join(ROOT, "DECISIONS.md")
    dec_txt = io.open(dec_path, encoding="utf-8").read() if os.path.exists(dec_path) else ""

    parts = [
        "# 接续包 %s · v2（极简）" % stamp,
        "",
        "> 上下文 %s（%.0f%% 上限）。**读完先回一句「已读完，当前任务=…」，然后等指示**；"
        "没指示不动文件、不推待办。" % (human(c["ctx"]), 100.0 * c["ctx"] / LIMIT),
        "> 本包只带必读；全文在 `DECISIONS.md` / `BACKLOG.md` / `HANDOFF.md`。",
        "",
    ]

    if c["ctx"] >= CLEAN:
        parts += [
            "## 清理提示（只准备，不删除）",
            "",
            "- 水位 **%s**，已过 %s：**该清理了**；本次只出包 + 导出原文，**没删任何记忆**。"
            % (human(c["ctx"]), human(CLEAN)),
            "- 删记忆须你确认（5.4）；删光记忆是内核红线，单次对话不执行。",
            "",
        ]

    if dec_txt:
        zero = _section(dec_txt, "## 0.0 异步非阻塞")
        if zero:
            parts += [_clip(zero, 520), ""]

        phi = _section(dec_txt, "## 〇、最高架构哲学")
        if phi:
            keep = [l.strip() for l in phi.splitlines()
                    if l.strip().startswith((">", "1.", "2.", "3.", "4."))]
            parts += ["## 〇、哲学", "", _clip("\n".join(keep), 520), ""]

        red = ["## 红线（5.3 仲裁 / 5.4 边界 / 5.6 拒绝权）", ""]
        s53 = _section(dec_txt, "### 5.3 冲突仲裁")
        ladder = _fence(s53)
        if ladder:
            red += ["```"] + ladder + ["```", ""]
        # 代码块里那几行是"优先级梯子"，上面已经原样贴过了；再当条目抓一遍会重复。
        red += _bullets(re.sub(r"```.*?```", "", s53, flags=re.S)) + [""]

        # 5.4 / 5.6 用整段原文（裁掉标题与引用行）：条目之外还有"删光记忆=红线"这类散句，
        # 只抓条目会把最要紧的那句漏掉。
        body54 = _section(dec_txt, "### 5.4 人机边界").split("\n", 1)[-1].strip()
        body54 = "\n".join(l for l in body54.splitlines() if not l.strip().startswith(">"))
        if body54:
            red += ["**5.4 人机边界：**", _clip(body54, 520), ""]

        body56 = _section(dec_txt, "### 5.6 拒绝权").split("\n", 1)[-1].strip()
        body56 = "\n".join(l for l in body56.splitlines() if not l.strip().startswith(">"))
        if body56:
            red += ["**5.6 拒绝权：**", _clip(body56, 480), ""]
        parts += red
    # 上一轮收尾：可变段（每次重生成），限 300 字。
    # "上一轮"必须是**另一个会话**的最后一条助手消息；本会话自己的历史快照不算。
    summary = ""
    if target and os.path.exists(target):
        summary = _last_assistant(target)      # 本次会话的最后两条
    if not summary:
        cur_id = _session_id(target) if target else ""
        for p in old:
            sid = _session_id(p)
            if sid and sid != cur_id:
                summary = _last_assistant(p)
                break
    if summary:
        parts += ["## 上一轮收尾（自动段，≤300 字）", "", summary, ""]

    # 现状 = 台账最新 2 个二级小节标题 +「一、已经落地」最后 2 条（B48）。
    # 旧版只读「一、已经落地」，而新决策都写在「八/九/十/十一」这些节里，
    # 于是接续包里的"现状"永远停在几天前——不是数据旧，是来源固定。
    heads = re.findall(r"^## ([一二三四五六七八九十]+、[^\n]+)$", dec_txt, flags=re.M)
    facts = _rows(_section(dec_txt, "## 一、已经落地"))
    def _pick(c):
        # 「一、已经落地」里两种行混排：编号行 | 1 | 决定 | 证据 |，非编号行 | 决定 | 说明 | 日期 |
        return c[1] if c[0].isdigit() and len(c) >= 3 else c[0]
    picks = heads[-2:] + [_pick(c) for c in (_cells(r) for r in facts[-2:]) if len(c) >= 2]
    if picks:
        parts += ["## 现状（最近 %d 条，全文见台账）" % len(picks), "", "| # | 决定 |", "|---|---|"]
        for i, p in enumerate(picks, 1):
            parts.append("| %d | %s |" % (i, _clip(p, 58)))
        parts.append("")

    # L1-2 待办：进行中 + 待办表（B11 子表不带，细节回 BACKLOG）
    bk_path = os.path.join(ROOT, "BACKLOG.md")
    if os.path.exists(bk_path):
        bk = io.open(bk_path, encoding="utf-8").read()
        doing = _rows(_section(bk, "## 进行中"))
        todo = _rows(_section(bk, "## 待办（按顺序推进）", stops=("\n## ", "\n**B11")))
        lines = ["## 待办（下一件 + 待你定）", "", "| # | 事项 | 谁做 |", "|---|---|---|"]
        for r in doing + todo:
            cells = _cells(r)
            if len(cells) >= 3:
                # 已完成的不进接续包：BACKLOG 里打勾的行还留在原表，不过滤就会
                # 把 B38/B39 这种早就做完的当成"待办"带进新窗口（B48）。
                if any("✅" in c for c in cells):
                    continue
                lines.append("| %s | %s | %s |" % (cells[0], _clip(cells[1], 46), cells[2]))
        lines += ["", "> 细节（B11a–f 拆分等）见 `BACKLOG.md`；标 **【你】** 的才停下等决定。", ""]
        parts += lines

    # 一级索引在出包前重建（0.5 秒）：否则交接包里的目录会静默过期，
    # 新窗口看到的是昨天的清单，还看不出来它过期了。
    l1_script = os.path.join(ROOT, "persona_index_l1.py")
    if os.path.exists(l1_script):
        try:
            subprocess.run([sys.executable, l1_script, "--quiet"], timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # 一级索引：只在这里读一次；要细节去 persona_search / HANDOFF（二级）。
    l1 = _l1_rows(5)
    if l1:
        parts += ["## 一级索引（每源一行；细节再 persona_search 二次读）", "",
                  "| 日期 | 来源 | 文件 | 消息 |", "|---|---|---|---|"]
        for r in l1:
            parts.append("| %s | %s | %s | %d |"
                         % (r["day"], r["source"], _clip(os.path.basename(r["file"]), 34), r["msgs"]))
        parts.append("")

    tail = [
        "## 开工四步（先跑，再动手；回话也算动作）",
        "",
        "0. 读会话：`python persona_readnew.py`（0 有新 / 2 没有 / 3 刷新失败；时间不符会自动抓取重试）",
        "1. 读设定：`memory\\设定\\00-索引.md`（按序读完 10/20/30/40）",
        "2. 读总索引：`python persona_master.py --k 10`",
        "3. 认领检查：`python persona_claim.py check \"关键词\" --has text`（0 可做 / 1 已被认领 / 2 能力不足）",
        "",
        "## 指针",
        "",
        "- 会话原文：`%s`" % export_line,
        "- 检索：`python persona_search.py \"关键词\" --k 100`（默认混合，`--no-vector` 退关键词）",
        "- 水位：`python persona_gauge.py`；到 %s 会自动出包，**删记忆仍须人确认**" % human(CLEAN),
        "",
    ]

    # 截断只砍正文，指针永远保留——把"原文在哪、怎么检索"砍掉，包就废了。
    # 尾部预留按实际长度算（2026-11-09：写死 380，加了第 0 步就撑破 3600 上限）。
    body = "\n".join(parts)
    tail_len = len("\n".join(tail)) + 8
    if len(body) > PACK_LIMIT - tail_len:
        cut = body[:PACK_LIMIT - tail_len - 40]
        cut = cut[:cut.rfind("\n")]          # 切在行边界，别把表格切半行
        body = cut.rstrip() + "\n\n…（正文按 %d 字上限截断）\n" % PACK_LIMIT
    text = body.rstrip() + "\n\n" + "\n".join(tail)
    io.open(md, "w", encoding="utf-8", newline="\n").write(text)

    print("接续包已生成: %s（%d 字 / %.1f KB）" % (md, len(text), len(text.encode("utf-8")) / 1024.0))

    # 带时间戳的包只留最近 30 份：周期任务写的是固定文件，不占这份额度。
    if not canonical:
        olds = sorted(glob.glob(os.path.join(out_dir, "接续包-2*.md")), key=os.path.getmtime)
        for f in olds[:-30]:
            try:
                os.remove(f)
            except OSError:
                pass
    print("会话原文: %s" % export_line)
    if c["ctx"] >= CLEAN:
        print("提示：水位 %s 已过 %s —— 该清理了（本次只出包，未删除任何记忆）"
              % (human(c["ctx"]), human(CLEAN)))

    if clip:
        if copy_to_clipboard(text):
            print("已复制到剪贴板：开新会话 → Ctrl+V → 回车，就能接上")
        else:
            print("复制剪贴板失败（可手动打开上面的文件）")
    else:
        print("（--no-clip：未写入剪贴板）")
    return md


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="上下文水位：到 %s 出接续包并提示清理（不删除）" % human(CLEAN))
    ap.add_argument("--prepare", action="store_true", help="生成接续包 v2（默认并复制到剪贴板）")
    ap.add_argument("--no-clip", action="store_true", help="出包但不覆盖剪贴板")
    ap.add_argument("--clip", action="store_true", help="只复制最近生成的包到剪贴板")
    ap.add_argument("--ctx", type=int, default=0, help="强制指定上下文规模（测试到线行为）")
    ap.add_argument("--canonical", action="store_true", help="写到固定的 接续包-最新.md（覆盖），给周期任务用")
    a = ap.parse_args()

    c = report(a.ctx or None)
    if a.prepare:
        print()
        prepare(c, clip=not a.no_clip, canonical=a.canonical)
    elif a.clip:
        files = sorted(glob.glob(os.path.join(ROOT, "续接", "接续包-*.md")))
        if not files:
            print("还没有接续包，先跑 --prepare")
        else:
            txt = io.open(files[-1], encoding="utf-8").read()
            print("已复制 %s" % files[-1] if copy_to_clipboard(txt) else "复制失败")
