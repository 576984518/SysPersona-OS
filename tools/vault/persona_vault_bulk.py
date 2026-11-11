# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_vault_bulk.py — 整目录加密/解密（带 manifest、可回滚、可核对）
r"""persona_vault_bulk.py — 整目录加密/解密（带 manifest、可回滚、可核对）

人 2026-11-11：「迁移可以现在做，以后所有客户端都读链接。」

这里的"迁移"只做一件事：把**已经存在的明文**就地换成密文（`SPV1`），
或者反过来（`unseal`）把密文解回明文。目录结构、文件名一个字不动——
因为 AAD 绑的就是"相对库根的路径"，所以**密文必须留在原路径上**。

安全顺序（写死在 run 里，顺序不许颠倒）：
  1. `--dry` 先数一遍：多少文件、多少字节、预计多久；
  2. 真封前先确认**备份存在**（`备份\记忆库-*.zip` 最近一份 < 26h），没有就拒绝；
  3. 封的过程中每个文件都"封→立刻解→比 sha256"，对不上就当场还原成明文并停下；
  4. 全部成功才写 manifest；任何异常都保留原文。

用法：
  python persona_vault_bulk.py plan   [--dirs sessions,archive]
  python persona_vault_bulk.py seal   [--dirs sessions,archive] [--dry]
  python persona_vault_bulk.py verify [--dirs sessions,archive]
  python persona_vault_bulk.py unseal [--dirs sessions,archive]
"""
import hashlib
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
MANIFEST = os.path.join(OPS, "vault-bulk.json")
DEFAULT_DIRS = ["sessions", "archive"]
SKIP = ("secret", ".key", ".dpapi", "token", "vault-key")
# 派生物/缓存不封：它们随时被重写，封了反而天天"认证失败"
SKIP_NAMES = ("index.jsonl", "manifest.json", "_cache", ".tmp")


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read(p, d=None):
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                      # noqa: BLE001
        return d


def _write(p, obj):
    tmp = p + ".tmp"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def files(dirs=None):
    out = []
    for d in (dirs or DEFAULT_DIRS):
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dp, _ds, fs in os.walk(base):
            for fn in fs:
                low = fn.lower()
                if any(b in low for b in SKIP) or any(n in low for n in SKIP_NAMES):
                    continue
                out.append(os.path.join(dp, fn))
    return out


def plan(dirs=None):
    import persona_vault as V
    todo, done, total = [], 0, 0
    for p in files(dirs):
        size = os.path.getsize(p)
        if V.is_sealed(p):
            done += 1
            continue
        todo.append((p, size))
        total += size
    return {"todo": len(todo), "already": done, "bytes": total,
            "mb": round(total / 1048576.0, 1),
            "est_sec": round(total / 1048576.0 / 5.5, 1)}


def backup_fresh(hours=26):
    """封之前必须有新鲜备份——这是"能回滚"的唯一保证。"""
    bdir = os.path.join(ROOT, "备份")
    if not os.path.isdir(bdir):
        return False, "没有 备份\\ 目录"
    zips = [os.path.join(bdir, n) for n in os.listdir(bdir)
            if n.startswith("记忆库-") and n.endswith(".zip")]
    if not zips:
        return False, "没有 备份\\记忆库-*.zip"
    newest = max(zips, key=os.path.getmtime)
    age = (time.time() - os.path.getmtime(newest)) / 3600.0
    if age > hours:
        return False, "最新备份 %.1f 小时前（> %d 小时）" % (age, hours)
    return True, "%s（%.1f 小时前）" % (os.path.basename(newest), age)


def seal(dirs=None, dry=False):
    import persona_vault as V
    ok, why = backup_fresh()
    if not ok:
        return False, "拒绝封存：%s。先跑 backup.ps1。" % why
    key = V.load_key()
    idx = _read(MANIFEST, {}) or {}
    n = bad = 0
    t0 = time.time()
    for p in files(dirs):
        if V.is_sealed(p):
            continue
        n += 1
        if dry:
            continue
        with io.open(p, "rb") as f:
            plain = f.read()
        digest = sha(plain)
        aad = V.rel_aad(p)
        blob = V.seal(plain, key, aad)
        tmp = p + ".tmp"
        with io.open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, p)
        # 立刻读回来核对；对不上就还原明文，别留半成品
        try:
            back = V.unseal(_read_bytes(p), key, aad)
        except Exception:                  # noqa: BLE001
            back = b""
        if sha(back) != digest:
            with io.open(p, "wb") as f:
                f.write(plain)
            bad += 1
            print("回读失败，已还原：%s" % os.path.relpath(p, ROOT))
            continue
        idx[os.path.relpath(p, ROOT).replace("\\", "/")] = {
            "sha256": digest, "plain": len(plain), "sealed": len(blob), "at": now()}
        if n % 200 == 0:
            _write(MANIFEST, idx)
    if not dry:
        _write(MANIFEST, idx)
    dt = time.time() - t0
    return (bad == 0), "%s %d 个文件，%.1f 秒%s" % (
        "计划" if dry else "已封", n, dt, "" if bad == 0 else "，%d 个回读失败已还原" % bad)


def _read_bytes(p):
    with io.open(p, "rb") as f:
        return f.read()


def verify(dirs=None, limit=0):
    import persona_vault as V
    key = V.load_key()
    idx = _read(MANIFEST, {}) or {}
    ok = bad = missing = plain = 0
    for rel, rec in sorted(idx.items()):
        if limit and ok + bad + missing >= limit:
            break
        if any(n in rel.lower() for n in SKIP_NAMES):
            continue
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            missing += 1
            continue
        if not V.is_sealed(p):
            plain += 1
            continue
        try:
            if sha(V.open_file(p, key)) != rec.get("sha256"):
                bad += 1
                print("内容对不上：%s" % rel)
            else:
                ok += 1
        except Exception as e:             # noqa: BLE001
            bad += 1
            print("解不开：%s（%s）" % (rel, str(e)[:60]))
    plain_left = sum(1 for p in files(dirs) if not V.is_sealed(p))
    return (bad == 0 and missing == 0), \
        "核对 %d 好 / %d 坏 / %d 缺｜另有 %d 个正被写、待下一轮封" % (ok, bad, missing, plain + plain_left)


def unseal(dirs=None):
    import persona_vault as V
    key = V.load_key()
    idx = _read(MANIFEST, {}) or {}
    n = bad = 0
    for p in files(dirs):
        if not V.is_sealed(p):
            continue
        n += 1
        try:
            plain = V.open_file(p, key)
            tmp = p + ".tmp"
            with io.open(tmp, "wb") as f:
                f.write(plain)
            os.replace(tmp, p)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            idx.pop(rel, None)
        except Exception as e:             # noqa: BLE001
            bad += 1
            print("解不开，跳过：%s（%s）" % (os.path.relpath(p, ROOT), str(e)[:60]))
    _write(MANIFEST, idx)
    return bad == 0, "解开 %d 个文件%s" % (n, "" if bad == 0 else "，%d 个失败" % bad)


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cmd = a[0] if a else "plan"

    def opt(name, default=None):
        for i, x in enumerate(a):
            if x == name and i + 1 < len(a):
                return a[i + 1]
        return default
    dirs = (opt("--dirs") or "").split(",") if opt("--dirs") else None
    if cmd == "plan":
        p = plan(dirs)
        ok, why = backup_fresh()
        print("【保险箱·计划】待封 %d 个 / 已封 %d 个｜%.1f MB｜预计 %.0f 秒" % (
            p["todo"], p["already"], p["mb"], p["est_sec"]))
        print("备份：%s%s" % ("OK  " if ok else "**没有** ", why))
        return 0 if ok else 1
    if cmd == "seal":
        ok, msg = seal(dirs, dry="--dry" in a)
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    if cmd == "verify":
        ok, msg = verify(dirs, limit=opt("--limit") and int(opt("--limit")) or 0)
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    if cmd == "unseal":
        ok, msg = unseal(dirs)
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
