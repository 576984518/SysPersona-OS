# -*- coding: utf-8 -*-
r"""persona_janitor.py — 清道夫：先搬到「清理」筐，过几天再真删（人 2026-11-12 定）

人定的两条口径（原话意思）：
  1.「清理加入日常任务，但是不要实时清理，最少保留 3 天或者一个星期，以免误删」；
  2.「或者清理是先移动到清理文件夹，然后确认没误删后再删除」。

所以是**两段式**，中间隔一个可反悔的窗口：

  第一段 sweep   —— 把「够老」的临时垃圾**搬**进 `<库根>\清理\待删\<批次>\`，原路径不变逻辑，
                    每个文件写进清单（原路径/大小/sha256/为什么算垃圾）。**这一步不删任何东西。**
  第二段 purge   —— 搬进去超过 `--keep-days`（默认 7）天的批次，才真删。
  随时 restore  —— 发现误搬，一条命令整批还原回原位。

默认年龄门槛 `--min-age-days 7`：任何文件都要**老于 7 天**才会被搬，够 3 天/一星期的要求。

只搬这些名字（白名单，宁少不多）：
  *.tmp · *.tmp.<十六进制>（调度租约那种） · *.partial · *.crdownload
永远不碰：`.git\`、`清理\` 自己、以及任何主文件（vectors.npy / meta.jsonl / fts.db / *.zip / *.db）。

用法：
  python persona_janitor.py status                     # 只读：现在有哪些候选、筐里几批、各多大
  python persona_janitor.py sweep --dry                 # 只看会搬什么（默认就是 dry）
  python persona_janitor.py sweep --apply               # 真搬
  python persona_janitor.py purge --dry                 # 看会删哪些批次
  python persona_janitor.py purge --apply --keep-days 7 # 真删（筐里满 7 天的批次）
  python persona_janitor.py restore --batch 20261119 --apply
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "ops")
TRASH = os.path.join(ROOT, "清理")
PENDING = os.path.join(TRASH, "待删")
LISTS = os.path.join(TRASH, "清单")
STATE = os.path.join(OPS, "janitor.json")
LOG = os.path.join(OPS, "janitor-log.jsonl")
STOP = os.path.join(OPS, "janitor-stop.json")

# 白名单：只认这几种名字。加一种要在这里加，别在别处偷偷放宽。
JUNK = (
    re.compile(r"\.tmp$", re.I),
    re.compile(r"\.tmp\.[0-9a-f]{6,}$", re.I),
    re.compile(r"\.partial$", re.I),
    re.compile(r"\.crdownload$", re.I),
)
SKIP_DIRS = {".git", "清理", "node_modules"}
# 这些主文件即使名字像垃圾也不碰（防御性写死）
NEVER = re.compile(r"(vectors\.npy|meta\.jsonl|fts\.db|\.db$|\.zip$|\.safetensors$|\.gguf$)", re.I)


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(rec):
    os.makedirs(OPS, exist_ok=True)
    rec = dict(rec)
    rec["ts"] = now()
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read(p, default):
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def sha256(p, limit=50 * 1024 * 1024):
    """小文件算真哈希；大文件（>50 MB）在 J1900 上要十几秒，只记大小+mtime，并标出来。"""
    st = os.stat(p)
    if st.st_size > limit:
        return "", "size+mtime", st.st_size
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest(), "sha256", st.st_size


def is_junk(name):
    if NEVER.search(name):
        return False
    return any(rx.search(name) for rx in JUNK)


def candidates(root=None, min_age_days=7):
    """够老的白名单垃圾。root 默认库根；带出的每条都带绝对路径与年龄。"""
    root = root or ROOT
    cut = datetime.datetime.now().timestamp() - min_age_days * 86400
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not is_junk(fn):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime > cut:
                continue
            out.append({"path": p, "size": st.st_size,
                        "age_days": round((datetime.datetime.now().timestamp() - st.st_mtime) / 86400, 1)})
    return out


def batches():
    out = []
    if not os.path.isdir(PENDING):
        return out
    for name in sorted(os.listdir(PENDING)):
        d = os.path.join(PENDING, name)
        if not os.path.isdir(d):
            continue
        files = [os.path.join(dp, f) for dp, _dn, fs in os.walk(d) for f in fs]
        size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        age = (datetime.datetime.now() - datetime.datetime.fromtimestamp(os.path.getmtime(d))).total_seconds() / 86400
        out.append({"batch": name, "files": len(files), "size": size, "age_days": round(age, 1), "dir": d})
    return out


def cmd_status(a):
    cand = candidates(min_age_days=a.min_age_days)
    tot = sum(c["size"] for c in cand)
    print("候选（够老 %d 天的临时垃圾，扫的是库根）：%d 个 / %.1f MB"
          % (a.min_age_days, len(cand), tot / 1048576.0))
    for c in sorted(cand, key=lambda x: -x["size"])[:10]:
        print("  %8.1f MB  %5.1f 天  %s" % (c["size"] / 1048576.0, c["age_days"],
                                            os.path.relpath(c["path"], ROOT)))
    bs = batches()
    print("\n筐里（清理\\待删\\）：%d 批" % len(bs))
    for b in bs:
        print("  %s  %4d 个 / %8.1f MB  已放 %.1f 天" % (b["batch"], b["files"],
                                                       b["size"] / 1048576.0, b["age_days"]))
    st = _read(STATE, {})
    print("\n上次清扫：%s（搬 %s 个 / 删 %s 个）"
          % (st.get("last_sweep") or "还没跑过", st.get("moved", 0), st.get("purged", 0)))
    return 0


def cmd_sweep(a):
    cand = candidates(min_age_days=a.min_age_days)
    tot = sum(c["size"] for c in cand)
    if not cand:
        print("没有够老（>%d 天）的临时垃圾，什么都不做。" % a.min_age_days)
        return 0
    # 刹车：一次搬太多就先停，等人看一眼（宁可少搬，不可多搬）
    if not a.force and (len(cand) > a.max_files or tot > a.max_gb * 1024 ** 3):
        rec = {"stopped": True, "files": len(cand), "mb": round(tot / 1048576.0, 1),
               "why": "超过单次上限（%d 个 / %.1f GB）" % (a.max_files, a.max_gb), "at": now()}
        _write(STOP, rec)
        print("已停下：候选 %d 个 / %.1f MB，超过单次上限。看 ops\\janitor-stop.json，"
              "确认后加 --force 再来。" % (len(cand), tot / 1048576.0))
        return 3
    batch = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    dst_root = os.path.join(PENDING, batch)
    print("%s：候选 %d 个 / %.1f MB → %s"
          % ("[dry]" if not a.apply else "执行", len(cand), tot / 1048576.0, dst_root))
    moved = []
    for c in cand:
        rel = os.path.relpath(c["path"], ROOT)
        dst = os.path.join(dst_root, rel)
        digest, how, size = ("", "", c["size"])
        if a.apply:
            try:
                digest, how, size = sha256(c["path"])
            except OSError:
                pass
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(c["path"], dst)
            except OSError as e:
                print("  搬不动（跳过）：%s —— %s" % (rel, e))
                log({"act": "move-fail", "path": c["path"], "err": str(e)[:160]})
                continue
            log({"act": "move", "from": c["path"], "to": dst, "size": size,
                 "digest": digest, "how": how})
        print("  %s  %8.1f MB  %5.1f 天" % (rel, size / 1048576.0, c["age_days"]))
        moved.append({"rel": rel, "from": c["path"], "to": dst, "size": size,
                      "digest": digest, "how": how, "age_days": c["age_days"]})
    if a.apply and moved:
        _write(os.path.join(LISTS, batch + ".json"),
               {"batch": batch, "at": now(), "min_age_days": a.min_age_days, "items": moved})
        st = _read(STATE, {})
        st.update({"last_sweep": now(), "moved": len(moved),
                   "moved_mb": round(sum(m["size"] for m in moved) / 1048576.0, 1), "batch": batch})
        _write(STATE, st)
        print("\n已搬 %d 个 / %.1f MB 进筐。清单：清理\\清单\\%s.json；%d 天后才真删。"
              % (len(moved), sum(m["size"] for m in moved) / 1048576.0, batch, a.keep_days))
    return 0


def cmd_purge(a):
    bs = [b for b in batches() if b["age_days"] >= a.keep_days]
    if not bs:
        print("筐里没有放满 %d 天的批次，不删任何东西。" % a.keep_days)
        return 0
    freed = 0
    for b in bs:
        print("%s：%d 个 / %.1f MB，已放 %.1f 天 → %s"
              % (b["batch"], b["files"], b["size"] / 1048576.0, b["age_days"],
                 "[dry]" if not a.apply else "删除"))
        if a.apply:
            try:
                shutil.rmtree(b["dir"])
                freed += b["size"]
                log({"act": "purge", "batch": b["batch"], "files": b["files"], "size": b["size"]})
            except OSError as e:
                print("  删不掉：%s" % e)
    if a.apply:
        st = _read(STATE, {})
        st.update({"last_purge": now(), "purged": len(bs), "freed_mb": round(freed / 1048576.0, 1)})
        _write(STATE, st)
        print("\n真删 %d 批，回收 %.1f MB（清单还留着，日志在 ops\\janitor-log.jsonl）。"
              % (len(bs), freed / 1048576.0))
    return 0


def cmd_restore(a):
    """把某一批整批还原回原位。**不覆盖**已存在的文件，冲突的逐条报出来。"""
    man = _read(os.path.join(LISTS, a.batch + ".json"), None)
    if not man:
        print("找不到清单：清理\\清单\\%s.json" % a.batch)
        return 2
    ok = bad = miss = 0
    for it in man.get("items") or []:
        src, dst = it.get("to"), it.get("from")
        if not (src and dst):
            continue
        if not os.path.exists(src):
            miss += 1
            continue
        if os.path.exists(dst):
            print("  冲突（原位已有同名文件，不覆盖）：%s" % dst)
            bad += 1
            continue
        if not a.apply:
            ok += 1
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            ok += 1
            log({"act": "restore", "from": src, "to": dst})
        except OSError as e:
            print("  还原失败：%s —— %s" % (dst, e))
            bad += 1
    print("%s：可还原 %d 个 · 冲突 %d · 筐里已没有 %d"
          % ("[dry]" if not a.apply else "已还原", ok, bad, miss))
    return 0


def cmd_daily(a):
    r"""守护每天叫的那个入口：自己管「一天只干一次」，而且只在凌晨 4 点以后动手。

    人 2026-11-12 定：「清理加入日常任务，但是不要实时清理；先移到清理文件夹，
    确认没误删再删」。所以这里两道闸：**日期闸**（今天已扫过就不动）+ **时段闸**（4 点后），
    然后才「搬」和「删」（删只删放满 keep_days 天的批次）。
    """
    st = _read(STATE, {})
    today = datetime.date.today().isoformat()
    if st.get("last_run_date") == today:
        return 0
    if datetime.datetime.now().hour < 4:
        return 0                                   # 还没到点，什么都不说，别刷日志
    class _A(object):
        pass
    a1 = _A()
    a1.min_age_days, a1.keep_days = a.min_age_days, a.keep_days
    a1.apply, a1.max_files, a1.max_gb, a1.force = True, 500, 2.0, False
    rc = cmd_sweep(a1)
    if rc == 3:                                    # 撞上限，今天先不删，等人看
        return 0
    cmd_purge(a1)
    st = _read(STATE, {})
    st["last_run_date"] = today
    _write(STATE, st)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="清道夫：先搬进筐，过几天再删；随时可整批还原")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("status", "sweep", "purge", "daily"):
        sp = sub.add_parser(name)
        sp.add_argument("--min-age-days", type=float, default=7, dest="min_age_days",
                        help="多老才算垃圾（默认 7 天，最少 3 天）")
        sp.add_argument("--keep-days", type=float, default=7, dest="keep_days",
                        help="筐里放几天才真删（默认 7 天）")
        sp.add_argument("--apply", action="store_true", help="真动手（不加就是 dry）")
        sp.add_argument("--dry", action="store_true", help="显式写 dry（默认就是；只为读起来清楚）")
        sp.add_argument("--max-files", type=int, default=500, dest="max_files")
        sp.add_argument("--max-gb", type=float, default=2.0, dest="max_gb")
        sp.add_argument("--force", action="store_true", help="越过单次上限（清完记得看日志）")
    rp = sub.add_parser("restore")
    rp.add_argument("--batch", required=True, help="批次号，如 20261119-0400")
    rp.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "daily":
        return cmd_daily(a)
    if a.cmd == "sweep":
        if a.min_age_days < 3:
            print("拒绝：--min-age-days 不能小于 3（人定的防误删底线）", file=sys.stderr)
            return 2
        return cmd_sweep(a)
    if a.cmd == "purge":
        if a.keep_days < 3:
            print("拒绝：--keep-days 不能小于 3（人定的防误删底线）", file=sys.stderr)
            return 2
        return cmd_purge(a)
    if a.cmd == "restore":
        return cmd_restore(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
