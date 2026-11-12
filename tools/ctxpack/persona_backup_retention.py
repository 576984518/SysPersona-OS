# -*- coding: utf-8 -*-
r"""persona_backup_retention.py — 备份轮转：按「日 / 周 / 月」分桶保留，且**永不删太新的**（v6.9.0）

为什么要有它（人 2026-11-12 报的账）：
「3 天的会话备份现在都快 2 个 G」。查下来不是会话长得快，是**每份快照都是全量**
（每份把 archive + sessions 重打一遍，约 170 MB），而 11-11 那天 20 分钟内打了 8 份。
原来 backup.ps1 只做「留最近 N 份」——N=8 时，8 份全量 ≈ 1.4 GB，而且一小时内打 8 份也照留。

这一版换成**三档轮转**（GFS）：

  日：最近 `--daily 7` 天，每天留最新的一份
  周：最近 `--weekly 4` 周，每周留最新的一份
  月：最近 `--monthly 3` 月，每月留最新的一份

叠两条底线：

  1. **年龄底线**：比 `--min-age-days`（默认 7）新的**一律不删**——哪怕它不在任何桶里；
  2. **最新三份一律不删**：任何时候至少嘴上还留着三个可回滚的点。

还有一条「不用重复打」：`due` 子命令比对源目录的廉价指纹（文件数 + 总字节 + 最大 mtime），
没变化就返回 1，backup.ps1 据此跳过整次打包。

用法：
  python persona_backup_retention.py plan                 # 只读：会删哪些、留哪些、为什么
  python persona_backup_retention.py apply                # 真删（受两条底线约束）
  python persona_backup_retention.py due                  # 0=该打新备份  1=没变化，可以不打
  python persona_backup_retention.py fingerprint          # 看当前指纹（排查用）
"""

import argparse
import datetime
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BK = os.path.join(ROOT, "备份")
OPS = os.path.join(ROOT, "ops")
STATE = os.path.join(OPS, "backup-fingerprint.json")
LOG = os.path.join(OPS, "backup-log.jsonl")

STAMP = re.compile(r"记忆库-(\d{8})-(\d{4})\.zip$")
# 指纹只看"会被打进包"的目录，别把 index/ 的 2.8 GB 也算进指纹（那不是备份内容）
SRC = ("sessions", "archive", "memory", "整理")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(rec):
    os.makedirs(OPS, exist_ok=True)
    rec = dict(rec)
    rec["ts"] = now()
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def snapshots():
    out = []
    for p in glob.glob(os.path.join(BK, "记忆库-*.zip")):
        m = STAMP.search(os.path.basename(p))
        st = os.stat(p)
        when = None
        if m:
            try:
                when = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
            except ValueError:
                when = None
        out.append({"path": p, "name": os.path.basename(p),
                    "size": st.st_size,
                    "mtime": datetime.datetime.fromtimestamp(st.st_mtime),
                    "when": when})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def fingerprint():
    """源目录的廉价指纹：文件数 + 总字节 + 最大 mtime。不读内容，1 秒内出结果。"""
    n = 0
    total = 0
    newest = 0.0
    for d in SRC:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dp, _dn, fs in os.walk(base):
            for fn in fs:
                if not fn.endswith((".md", ".json", ".jsonl", ".txt")):
                    continue
                try:
                    st = os.stat(os.path.join(dp, fn))
                except OSError:
                    continue
                n += 1
                total += st.st_size
                newest = max(newest, st.st_mtime)
    return {"files": n, "bytes": total, "newest": round(newest, 3)}


def plan(daily=7, weekly=4, monthly=3, min_age_days=7):
    snaps = snapshots()
    keep = {}
    # 最新三份先保底
    for s in snaps[:3]:
        keep[s["path"]] = "最新三份之一"

    def bucket(s, key, label, limit, seen):
        if not s["when"]:
            return
        k = key(s["when"])
        if k in seen:
            return
        if len(seen) >= limit:
            return
        seen.add(k)
        keep.setdefault(s["path"], label)

    days, weeks, months = set(), set(), set()
    for s in snaps:
        bucket(s, lambda d: d.strftime("%Y-%m-%d"), "日档", daily, days)
        bucket(s, lambda d: d.strftime("%G-W%V"), "周档", weekly, weeks)
        bucket(s, lambda d: d.strftime("%Y-%m"), "月档", monthly, months)

    cut = datetime.datetime.now() - datetime.timedelta(days=min_age_days)
    deletes, keeps = [], []
    for s in snaps:
        if s["path"] in keep:
            keeps.append((s, keep[s["path"]]))
        elif s["mtime"] > cut:
            keeps.append((s, "太新（< %d 天），不删" % min_age_days))
        else:
            deletes.append(s)
    return snaps, keeps, deletes


def cmd_plan(a):
    snaps, keeps, deletes = plan(a.daily, a.weekly, a.monthly, a.min_age_days)
    tot = sum(s["size"] for s in snaps)
    print("现有快照 %d 份 / %.1f MB" % (len(snaps), tot / 1048576.0))
    print("\n保留 %d 份：" % len(keeps))
    for s, why in keeps:
        print("  %-26s %7.1f MB  %s" % (s["name"], s["size"] / 1048576.0, why))
    print("\n将删除 %d 份 / %.1f MB：" % (len(deletes), sum(s["size"] for s in deletes) / 1048576.0))
    for s in deletes:
        print("  %-26s %7.1f MB  %s" % (s["name"], s["size"] / 1048576.0,
                                        s["mtime"].strftime("%Y-%m-%d %H:%M")))
    if not deletes:
        print("  （没有可删的：都还在年龄底线内，或在保留档里）")
    return 0


def cmd_apply(a):
    snaps, keeps, deletes = plan(a.daily, a.weekly, a.monthly, a.min_age_days)
    freed = 0
    for s in deletes:
        if not a.apply:
            continue
        try:
            os.remove(s["path"])
            freed += s["size"]
            log({"act": "delete", "name": s["name"], "size": s["size"]})
            print("删：%s" % s["name"])
        except OSError as e:
            print("删不掉：%s —— %s" % (s["name"], e))
    print("%s：保留 %d 份 · 删除 %d 份 · 回收 %.1f MB"
          % ("[dry]" if not a.apply else "已执行", len(keeps),
             len(deletes) if a.apply else 0, freed / 1048576.0))
    return 0


def cmd_due(a):
    cur = fingerprint()
    try:
        with io.open(STATE, encoding="utf-8-sig") as f:
            last = json.load(f) or {}
    except (OSError, ValueError):
        last = {}
    changed = not last or any(cur.get(k) != last.get(k) for k in ("files", "bytes", "newest"))
    print(json.dumps({"now": cur, "last": last, "changed": changed}, ensure_ascii=False))
    if not changed:
        print("没变化：这次不用再打一份（要强制打，backup.ps1 -Force）")
        return 1
    return 0


def cmd_fingerprint(a):
    print(json.dumps(fingerprint(), ensure_ascii=False, indent=1))
    return 0


def cmd_record(a):
    """打完备份之后调用：把这次的指纹记下来，下次好判断"有没有变化"。"""
    cur = fingerprint()
    os.makedirs(OPS, exist_ok=True)
    with io.open(STATE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    print("指纹已记录：%s" % json.dumps(cur, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="备份轮转（日/周/月三档 + 年龄底线）")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("plan", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument("--daily", type=int, default=7)
        sp.add_argument("--weekly", type=int, default=4)
        sp.add_argument("--monthly", type=int, default=3)
        sp.add_argument("--min-age-days", type=float, default=7, dest="min_age_days")
        sp.add_argument("--apply", action="store_true")
    sub.add_parser("due").set_defaults(func=cmd_due)
    sub.add_parser("fingerprint").set_defaults(func=cmd_fingerprint)
    sub.add_parser("record").set_defaults(func=cmd_record)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        return cmd_plan(a)
    if a.cmd == "apply":
        if a.min_age_days < 3:
            print("拒绝：--min-age-days 不能小于 3（防误删底线）", file=sys.stderr)
            return 2
        return cmd_apply(a)
    if a.cmd in ("due", "fingerprint", "record"):
        return a.func(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
