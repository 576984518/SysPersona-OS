# -*- coding: utf-8 -*-
r"""persona_todo.py — 把 BACKLOG 摊成一份「待办备忘」

为什么要有它：交接包有字节上限，待办一多就会被截断；被截断的那几条，正是下次最容易丢的。
所以交接包只管「下一件」，完整的摊在这里，谁都能一眼看完。

用法：
  python persona_todo.py            # 生成 续接\待办备忘.md
  python persona_todo.py --print    # 只在屏幕上打，不落盘
  python persona_todo.py --check     # 看备忘是否过期（BACKLOG 比它新就提示）

规则：
  * 只收「还没做完」的（状态以 ⬜ / ⏸ 开头，或状态列为空）；
  * 【你】= 等你拍板，单独一段，排前面；
  * 每条保留验收标准——没有判据的待办等于没说。
"""
import argparse
import io
import os
import re
import sys
import time

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
BACKLOG = os.path.join(ROOT, "BACKLOG.md")
OUT = os.path.join(ROOT, "续接", "待办备忘.md")
OPEN = ("⬜", "⏸")


def rows():
    """从 BACKLOG 的表格里抽行：| # | 事项 | 谁做 | 状态 | 验收 |"""
    out = []
    if not os.path.exists(BACKLOG):
        return out
    for line in io.open(BACKLOG, encoding="utf-8-sig", errors="replace"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        bid = cells[0]
        if not re.match(r"^(B\d+|#|—)$", bid) or bid in ("#", "—"):
            continue
        status = cells[3] if len(cells) > 3 else ""
        if not (status == "" or status.startswith(OPEN)):
            continue
        out.append({"id": bid, "title": cells[1], "who": cells[2],
                    "status": status, "accept": cells[4] if len(cells) > 4 else ""})
    return out


def render(items):
    mine = [x for x in items if "【你】" not in x["who"]]
    yours = [x for x in items if "【你】" in x["who"]]
    ts = time.strftime("%Y-%m-%d %H:%M")
    L = []
    L.append("# 待办备忘（自动生成）")
    L.append("")
    L.append("> 生成：%s　｜　来源：`BACKLOG.md`　｜　共 **%d** 条（等你拍板 %d / 我可做 %d）"
             % (ts, len(items), len(yours), len(mine)))
    L.append("> 为什么有这份：交接包有上限，装不下的待办会在这里，**别只信交接包的截断**。")
    L.append("> 重新生成：`python persona_todo.py`")
    L.append("")

    def block(title, xs, tip):
        L.append("## " + title)
        L.append("")
        if not xs:
            L.append("（空）")
            L.append("")
            return
        L.append(tip)
        L.append("")
        for x in xs:
            L.append("### %s %s" % (x["id"], x["title"]))
            L.append("")
            if x["accept"]:
                L.append("- 验收：%s" % x["accept"])
            if x["status"]:
                L.append("- 状态：%s" % x["status"])
            L.append("")

    block("等你拍板（你不动就停在这）", yours, "> 只有这些会停住；其余我直接推。")
    block("我可做（按编号顺序推）", mine, "> 一次只推一件；做完回 BACKLOG 打勾。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="do_print", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    items = rows()
    text = render(items)
    if a.do_print:
        print(text)
        return 0
    if a.check:
        if not os.path.exists(OUT):
            print("备忘还没生成过")
            return 1
        if os.path.getmtime(BACKLOG) > os.path.getmtime(OUT):
            print("备忘过期：BACKLOG 比它新，跑一次 persona_todo.py")
            return 1
        print("备忘是最新的（%d 条）" % len(items))
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    print("已生成 %s（%d 条）" % (os.path.relpath(OUT, ROOT), len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
