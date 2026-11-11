# -*- coding: utf-8 -*-
r"""persona_settings_backup.py — 设定/宪法级文件的多副本定时快照

人 2026-11-11：「每一个设定文件都不能只有一份。前面不是说交接文件按设定时间备份吗？
这些全部都按同样的逻辑备份。」

所以照 `handoff_rotate` 的规矩来（定时 + 分级保留 + 不删干净），但对象换成"设定类文件"：

    每次运行 → 一个快照目录  备份\设定\<stamp>\<原相对路径>
                            备份\设定\<stamp>\manifest.json   (每份的 sha256)
              → 本机一份 + 网盘一份（同一份内容，两个位置）
              → 另存 备份\设定\最新\ 供体检直接比对

分级保留（和交接文件一个思路，只删我们自己造的目录）：
    · 48 小时内全留；
    · 之后每天留第一份，留 60 天；
    · 再之后每周留第一份，留 365 天；
    · 更老的才删。

体检：`verify` 检查"每份文件至少两处、内容一致、最新快照不超过 26 小时"。
"""
import hashlib
import io
import json
import os
import shutil
import sys
import time

ROOT = os.environ.get("SYSPERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
CONF = os.path.join(OPS, "settings-backup.json")
STAMP_FMT = "%Y%m%d-%H%M"
DEFAULT = {
    "sources": ["memory/设定/*.md", "memory/shell/MEMORY.md", "memory/shell/USER.md",
                "memory/MEMORY.md", "续接/接续包-最新.md",
                "DECISIONS.md", "BACKLOG.md", "SKILL.md", "HANDOFF.md", "state.json"],
    "targets": ["备份/设定", r"\\Nas-1900\nas-文件\AI会话检索记录\备份\设定"],
    "skip_names": ["secret", ".key", ".dpapi", "token", "vault-key"],
    "fresh_hours": 26,
    "note": "每个设定文件至少两份（本机+网盘）；快照不可变，保留按分级",
}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read(p, d=None):
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                      # noqa: BLE001
        return d


def _write(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def conf():
    c = _read(CONF, None) or dict(DEFAULT)
    for k, v in DEFAULT.items():
        c.setdefault(k, v)
    return c


def _skip(name):
    low = name.lower()
    return any(b in low for b in conf()["skip_names"])


def sources():
    """把配置里的路径/通配展开成 [(绝对路径, 相对库根的 posix 路径)]。"""
    out = []
    for pat in conf()["sources"]:
        p = pat.replace("\\", "/")
        if "*" in p:
            import glob
            for f in sorted(glob.glob(os.path.join(ROOT, p))):
                if os.path.isfile(f) and not _skip(os.path.basename(f)):
                    out.append((f, os.path.relpath(f, ROOT).replace("\\", "/")))
        else:
            f = os.path.join(ROOT, p)
            if os.path.isfile(f) and not _skip(os.path.basename(f)):
                out.append((f, p))
    return out


def sha(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(stamp=None):
    """打一个快照到所有 target；返回 {target: {files, bytes, ok}}。"""
    c = conf()
    stamp = stamp or time.strftime(STAMP_FMT)
    items = sources()
    if not items:
        return {}, "没有可备份的源文件（配置 sources 不对？）"
    man = {}
    for src, rel in items:
        man[rel] = {"sha256": sha(src), "size": os.path.getsize(src),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(os.path.getmtime(src)))}
    res = {}
    for t in c["targets"]:
        base = t if os.path.isabs(t) or t.startswith("\\\\") else os.path.join(ROOT, t)
        snap = os.path.join(base, stamp)
        try:
            for src, rel in items:
                dst = os.path.join(snap, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            _write(os.path.join(snap, "manifest.json"),
                   {"stamp": stamp, "at": now(), "note": c["note"], "files": man})
            res[base] = {"files": len(items), "bytes": sum(v["size"] for v in man.values()),
                         "ok": True}
        except Exception as e:             # noqa: BLE001
            res[base] = {"ok": False, "why": str(e)[:120]}
    # 最新副本：给体检一个固定路径
    for base in res:
        if not res[base].get("ok"):
            continue
        latest = os.path.join(base, "最新")
        try:
            if os.path.isdir(latest):
                shutil.rmtree(latest)
            shutil.copytree(os.path.join(base, stamp), latest)
        except Exception:                  # noqa: BLE001
            pass
    trim()
    return res, "%d 个设定文件 → %d 处" % (len(items), len(res))


def snapshots(base):
    """列出某 target 下的快照目录（不含 '最新'），按时间升序。"""
    if not os.path.isdir(base):
        return []
    names = [n for n in os.listdir(base)
             if n != "最新" and n[:8].isdigit() and "-" in n]
    return sorted(names)


def trim(keep_hours=48, keep_days=60, keep_weeks=365):
    """分级保留：只动 `<target>/<stamp>/` 这种自己造的目录。"""
    c = conf()
    removed = []
    for t in c["targets"]:
        base = t if os.path.isabs(t) or t.startswith("\\\\") else os.path.join(ROOT, t)
        names = snapshots(base)
        if not names:
            continue
        keep, seen_day, seen_week = set(), set(), set()
        cutoff_h = time.time() - keep_hours * 3600
        cut_d = time.time() - keep_days * 86400
        cut_w = time.time() - keep_weeks * 7 * 86400
        for n in names:
            try:
                ts = time.mktime(time.strptime(n, STAMP_FMT))
            except Exception:              # noqa: BLE001
                continue
            day = n[:8]
            week = time.strftime("%Y-%W", time.localtime(ts))
            if ts >= cutoff_h:
                keep.add(n)
            elif ts >= cut_d and day not in seen_day:
                seen_day.add(day)
                keep.add(n)
            elif ts >= cut_w and week not in seen_week:
                seen_week.add(week)
                keep.add(n)
        for n in names:
            if n in keep:
                continue
            try:
                shutil.rmtree(os.path.join(base, n))
                removed.append(os.path.join(base, n))
            except Exception:              # noqa: BLE001
                pass
    return removed


def verify():
    """体检：每份文件 ≥2 处、哈希一致、最新快照不超龄。"""
    c = conf()
    items = sources()
    msgs, bad = [], 0
    bases = []
    for t in c["targets"]:
        bases.append(t if os.path.isabs(t) or t.startswith("\\\\")
                     else os.path.join(ROOT, t))
    newest = None
    for base in bases:
        latest = os.path.join(base, "最新")
        if not os.path.isdir(latest):
            msgs.append("%s 还没有『最新』快照" % base)
            bad += 1
            continue
        man = _read(os.path.join(latest, "manifest.json"), {}) or {}
        for src, rel in items:
            dst = os.path.join(latest, rel.replace("/", os.sep))
            if not os.path.isfile(dst):
                msgs.append("%s 缺 %s" % (os.path.basename(base), rel))
                bad += 1
                continue
            if sha(dst) != sha(src):
                msgs.append("%s 的 %s 与现文件不一致（快照旧了）" % (os.path.basename(base), rel))
                bad += 1
        stamp = man.get("stamp") or ""
        if stamp:
            try:
                ts = time.mktime(time.strptime(stamp, STAMP_FMT))
                newest = ts if newest is None else max(newest, ts)
            except Exception:              # noqa: BLE001
                pass
    if newest is None:
        return False, "没有可用快照：" + "；".join(msgs[:3])
    age_h = (time.time() - newest) / 3600.0
    if age_h > float(c["fresh_hours"]):
        bad += 1
        msgs.append("最新快照 %.1f 小时前（上限 %s）" % (age_h, c["fresh_hours"]))
    if bad:
        return False, "%d 份设定/快照有问题：%s" % (bad, "；".join(msgs[:4]))
    return True, "%d 份设定文件，%d 处副本齐全（最近快照 %.1f 小时前）" % (
        len(items), len(bases), age_h)


def status():
    c = conf()
    items = sources()
    lines = ["【设定备份】%s" % now(), "源：%d 个文件" % len(items)]
    for t in c["targets"]:
        base = t if os.path.isabs(t) or t.startswith("\\\\") else os.path.join(ROOT, t)
        snaps = snapshots(base)
        lines.append("  %s：%d 个快照%s" % (
            base, len(snaps), ("，最近 " + snaps[-1]) if snaps else "（空）"))
    ok, why = verify()
    lines.append(("体检通过：" if ok else "体检不通过：") + why)
    lines.append("保留：48 小时全留 → 每天 1 份留 60 天 → 每周 1 份留 365 天")
    return "\n".join(lines)


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cmd = a[0] if a else "run"
    if cmd in ("run", "snapshot"):
        res, msg = snapshot()
        print("【设定备份】%s  %s" % (now(), msg))
        for base, r in res.items():
            print("  %s：%s" % (base, ("%d 文件 / %.1f KB" % (r["files"], r["bytes"] / 1024.0))
                                if r.get("ok") else "失败 " + r.get("why", "")))
        return 0 if all(r.get("ok") for r in res.values()) else 1
    if cmd == "verify":
        ok, why = verify()
        print(("OK   " if ok else "FAIL ") + why)
        return 0 if ok else 1
    if cmd == "status":
        print(status())
        return 0
    if cmd == "trim":
        for p in trim():
            print("删旧快照 %s" % p)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
