# -*- coding: utf-8 -*-
r"""handoff_rotate.py — 交接文件分级保留 + 目录归拢

规则（2026-11-08 用户定）：
  - 外层 `续接\` **只留一份**：`接续包-最新.md`
  - 其余全部进 `续接\历史\`，按时间排列
  - 在历史里按周期各留一份：1 月 / 1 周 / 3 天 / 1 天 / 12h / 6h / 3h / 2h / 1h / 30m / 20m / 10m / 5m / 1m
  - 没被任何周期选中的，进 `续接\历史\其余\`（不删，等你确认再清）

用法：
  python handoff_rotate.py            # 归拢 + 分级
  python handoff_rotate.py --dry-run  # 只报会做什么

环境变量：SYSPERSONA_ROOT 指记忆库根（旧名 PERSONA_ROOT 也认），整理的是
`<根>\续接\`；不设就按脚本所在目录推导，推不出来直接抛错。
"""
import argparse
import glob
import io
import os
import re
import shutil
import sys
import time

from persona_root import root as _persona_root

ROOT = _persona_root()
HANDOFF = os.path.join(ROOT, "续接")
HIST = os.path.join(HANDOFF, "历史")
REST = os.path.join(HIST, "其余")
LATEST = os.path.join(HANDOFF, "接续包-最新.md")

# 周期：名字 → 秒
TIERS = [
    ("1月", 30 * 86400), ("1周", 7 * 86400), ("3天", 3 * 86400), ("1天", 86400),
    ("12时", 12 * 3600), ("6时", 6 * 3600), ("3时", 3 * 3600), ("2时", 2 * 3600),
    ("1时", 3600), ("30分", 1800), ("20分", 1200), ("10分", 600),
    ("5分", 300), ("1分", 60),
]

NAME = re.compile(r"接续包-(\d{8})-(\d{4})\.md$")


def stamp_of(p):
    m = NAME.search(os.path.basename(p))
    if not m:
        return None
    return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    for d in (HANDOFF, HIST, REST):
        if not os.path.isdir(d):
            if a.dry_run:
                print("[dry] mkdir", d)
            else:
                os.makedirs(d, exist_ok=True)

    # 1) 外层只留最新：其他带时间戳的统统进历史
    outer = [p for p in glob.glob(os.path.join(HANDOFF, "接续包-*.md"))
             if os.path.basename(p) != "接续包-最新.md"]
    for p in outer:
        dst = os.path.join(HIST, os.path.basename(p))
        print(("[dry] move " if a.dry_run else "move ") + os.path.basename(p) + " → 历史/")
        if not a.dry_run:
            shutil.move(p, dst)

    # 2) 分级：每个周期选一份最接近的（误差不超过该周期的一半）
    files = sorted(glob.glob(os.path.join(HIST, "接续包-*.md")), key=os.path.getmtime)
    now = time.time()
    keep = set()
    for name, span in TIERS:
        target = now - span
        cand = [(abs(t - target), p) for p in files if (t := stamp_of(p)) and abs(t - target) <= span / 2]
        if cand:
            cand.sort()
            keep.add(cand[0][1])
            print("保留 [%s] %s" % (name, os.path.basename(cand[0][1])))

    # 3) 没被选中的进「其余」，不删
    moved = 0
    for p in files:
        if p in keep:
            continue
        dst = os.path.join(REST, os.path.basename(p))
        if not a.dry_run:
            shutil.move(p, dst)
        moved += 1
    print("分级完成：历史保留 %d 份，其余 %d 份移入 历史\\其余\\（未删除）" % (len(keep), moved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
