#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""persona_bootstrap.py — 最小引导包：给新设备只装"点火"的那点东西（v6.5.0）

为什么要有它（人 2026-11-12 的原话意思）：
  「想部署必须先复制，复制了才能连节点，这是不是一个悖论」
  「把部署说明和需要的最小文件单独拿出来，去部署，而不是现在复制所有的会话」

所以这里只回答一件事：**一个新设备需要哪几个文件才能跑起来**。
会话、归档、索引、备份**一律不进包**——那些等连上节点再用
`persona_lanbus.py sync` 按需拉。

包里必须自足（离线可完成），且**绝不含密钥**：名字像密钥的一律拒收，
内容里扫到 `sk-` 开头的长串也拒收——宁可少打一个文件，也不让包变成泄露源。

用法：
  python persona_bootstrap.py list                     # 只列：会打哪些、多大
  python persona_bootstrap.py pack --out <目录>         # 打成一个 zip + MANIFEST.json
  python persona_bootstrap.py pack --out <目录> --with-runtime   # 连独立运行时一起（约 0.4 GB）
  python persona_bootstrap.py verify <zip>             # 逐个文件校 sha256

退出码：0 成功；2 参数/环境问题；3 扫到密钥或越界目录（拒绝打包）。
"""

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "dev"

# 这几个顶层目录**永远不进包**：它们是数据，不是点火器
NEVER_DIRS = {"sessions", "archive", "index", "备份", "ops", "清理", "续接", "steps"}
# 名字像密钥的，一律拒收。**必须同时满足**：扩展名是"装密钥的容器" + 名字里有密钥词。
# 早先只匹配 `token` 子串，把 Python 自带的 `tokenize.py`、`token.py` 全拒了——库文件不是密钥。
SECRET_NAME = re.compile(r"(secret|credential|token|passwd|password|id_rsa|api[-_]?key)", re.I)
SECRET_EXT = {".txt", ".json", ".yaml", ".yml", ".env", ".ini", ".cfg", ".conf",
              ".dat", ".key", ".pem", ".ps1", ".cmd", ".bat", ".sh"}
SECRET_BODY = re.compile(r"sk-[A-Za-z0-9]{24,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
# 必带的说明文档（有就带，没有不算错）
DOC_FILES = ("先读我.md", "使用说明.md", "接入手册-新客户端.md", "自检清单.md",
             "交接文件规范-v2.md", "DEPLOY_MIN_SPEC.md")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def resolve_root(explicit=""):
    if explicit:
        return os.path.abspath(explicit)
    try:
        sys.path.insert(0, HERE)
        import persona_root as R
        r = R.root()
        if r and os.path.isdir(r):
            return r
    except Exception:                                  # noqa: BLE001
        pass
    return os.path.dirname(HERE)


def collect(root, with_runtime=False):
    """返回 [(绝对路径, 包内相对路径)]。只收白名单目录，别的一律不看。

    包内路径**一律用正斜杠**：zip 里写反斜杠，别的解压工具会当成文件名的一部分。
    另外跳过 `.bak-*` 这类临时备份（它们是修改前的旧版本，不是点火需要的）。
    """
    out, skipped = [], []

    def rel_in(prefix, base, p):
        return "/".join([prefix] + os.path.relpath(p, base).replace("\\", "/").split("/"))

    scripts = os.path.join(root, "脚本")
    if not os.path.isdir(scripts):
        scripts = HERE                                  # 仓库布局：本文件所在目录就是脚本目录
    for dirpath, dirnames, filenames in os.walk(scripts):
        dirnames[:] = [d for d in dirnames if d not in NEVER_DIRS and d != "__pycache__"]
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")) or ".bak-" in fn or fn.startswith("~$"):
                continue
            p = os.path.join(dirpath, fn)
            out.append((p, rel_in("scripts", scripts, p)))
    cfg = os.path.join(root, "配置")
    if os.path.isdir(cfg):
        for dirpath, _dn, filenames in os.walk(cfg):
            for fn in filenames:
                if ".bak-" in fn or fn.startswith("~$"):
                    continue
                p = os.path.join(dirpath, fn)
                out.append((p, rel_in("config", cfg, p)))
    docs = os.path.join(root, "文档")
    for name in DOC_FILES:
        p = os.path.join(docs, name)
        if os.path.isfile(p):
            out.append((p, "docs/" + name))
        else:
            skipped.append("docs/" + name)
    if with_runtime:
        try:
            sys.path.insert(0, HERE)
            import persona_root as R
            rt = R.runtime_dir()
        except Exception:                              # noqa: BLE001
            rt = None
        if not rt or not os.path.isdir(rt):
            skipped.append("runtime/（找不到独立运行时，跳过）")
        else:
            for dirpath, dirnames, filenames in os.walk(rt):
                dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "pip-cache", "tmp")]
                for fn in filenames:
                    if fn.endswith((".pyc", ".pyo")) or ".bak-" in fn:
                        continue
                    p = os.path.join(dirpath, fn)
                    out.append((p, rel_in("runtime", rt, p)))
    return out, skipped


TEXT_EXT = {".py", ".ps1", ".cmd", ".bat", ".json", ".yml", ".yaml", ".toml", ".ini",
            ".cfg", ".conf", ".txt", ".md", ".env", ".key", ".pem", ".crt", ".csv"}


def _is_textish(path, head=None):
    r"""看起来是文本吗？**先看扩展名，再看开头有没有 NUL**。

    为什么必须判：运行时空闲 0.4 GB，里面全是 git / python / node 的二进制。
    早先只按"名字像密钥"筛，结果 `git-credential-manager.exe`、`ssh-agent.exe`
    全被当成密钥拒收——一个能跑的引导包永远打不出来。二进制的**内容**也不该按文本扫。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXT:
        return False
    if head is None:
        try:
            with open(path, "rb") as f:
                head = f.read(1024)
        except OSError:
            return False
    return b"\x00" not in head


def screen(items):
    """过一遍红线：名字像密钥、内容像密钥、落在禁收目录。有问题就抛。

    只看**文本**文件：二进制（.exe/.dll/.so/.pyd…）既不算"名字像密钥"也不读内容。
    这不是放松，是纠正：把 git 自带的 `ssh-agent.exe` 当成泄密源，只会让包永远打不出来。
    """
    bad = []
    for src, rel in items:
        if any(rel.split("/")[0].lower() == d for d in NEVER_DIRS):
            bad.append(("禁收目录", rel))
            continue
        base = os.path.basename(rel)
        if not _is_textish(src):
            continue
        ext = os.path.splitext(base)[1].lower()
        if ext in SECRET_EXT and SECRET_NAME.search(base):
            bad.append(("名字像密钥", rel))
            continue
        if os.path.getsize(src) <= 2 * 1024 * 1024:
            try:
                with io.open(src, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(256 * 1024)
                if SECRET_BODY.search(head):
                    bad.append(("内容里有密钥", rel))
            except OSError:
                pass
    return bad


def cmd_list(a):
    root = resolve_root(a.root)
    items, skipped = collect(root, a.with_runtime)
    tot = sum(os.path.getsize(p) for p, _ in items)
    print("库根：%s" % root)
    print("会打 %d 个文件 / %.1f MB%s" % (len(items), tot / 1048576.0,
                                         "（含运行时）" if a.with_runtime else ""))
    top = {}
    for p, rel in items:
        k = rel.split("/")[0]
        top[k] = top.get(k, 0) + os.path.getsize(p)
    for k in sorted(top):
        print("  %-10s %8.1f MB" % (k, top[k] / 1048576.0))
    for s in skipped:
        print("  跳过：%s" % s)
    bad = screen(items)
    for why, rel in bad:
        print("  ✗ %s：%s" % (why, rel))
    return 0


def cmd_pack(a):
    root = resolve_root(a.root)
    items, skipped = collect(root, a.with_runtime)
    bad = screen(items)
    if bad:
        print("拒绝打包：其中有 %d 个文件不能进包" % len(bad), file=sys.stderr)
        for why, rel in bad[:10]:
            print("  %s：%s" % (why, rel), file=sys.stderr)
        return 3
    os.makedirs(a.out, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    name = "SysPersona-bootstrap-%s-%s.zip" % (a.version, stamp)
    zpath = os.path.join(a.out, name)
    man = {"version": a.version, "created": now(), "with_runtime": bool(a.with_runtime),
           "items": []}
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for src, rel in items:
            z.write(src, rel)
            man["items"].append({"rel": rel, "size": os.path.getsize(src),
                                 "sha256": sha256_file(src)})
        man["totals"] = {"files": len(man["items"]),
                         "bytes": sum(i["size"] for i in man["items"])}
        z.writestr("MANIFEST.json", json.dumps(man, ensure_ascii=False, indent=1))
    print("已打包：%s" % zpath)
    print("  文件 %d 个 / %.1f MB（含 MANIFEST.json）"
          % (man["totals"]["files"], man["totals"]["bytes"] / 1048576.0))
    print("  包里**没有** sessions / archive / index / 备份 / ops / 续接；也没有任何密钥")
    for s in skipped:
        print("  跳过：%s" % s)
    return 0


def cmd_verify(a):
    zpath = a.zip
    if not os.path.isfile(zpath):
        print("找不到：%s" % zpath, file=sys.stderr)
        return 2
    with zipfile.ZipFile(zpath) as z:
        try:
            man = json.loads(z.read("MANIFEST.json").decode("utf-8"))
        except KeyError:
            print("包里没有 MANIFEST.json，无法校验", file=sys.stderr)
            return 2
        bad = miss = ok = 0
        for it in man.get("items") or []:
            try:
                data = z.read(it["rel"])
            except KeyError:
                miss += 1
                print("  ✗ 缺文件：%s" % it["rel"])
                continue
            if hashlib.sha256(data).hexdigest() != it.get("sha256"):
                bad += 1
                print("  ✗ 哈希不符：%s" % it["rel"])
            else:
                ok += 1
    print("校验 %s：OK %d · 哈希不符 %d · 缺 %d（版本 %s，生成于 %s）"
          % (os.path.basename(zpath), ok, bad, miss, man.get("version"), man.get("created")))
    return 0 if (bad == 0 and miss == 0) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="最小引导包：只装点火的那点东西，不搬会话")
    sub = ap.add_subparsers(dest="cmd")
    lp = sub.add_parser("list", help="只列清单与大小")
    pp = sub.add_parser("pack", help="打包成 zip + MANIFEST.json")
    vp = sub.add_parser("verify", help="按 MANIFEST 校 sha256")
    for sp in (lp, pp):
        sp.add_argument("--root", default="", help="库根（默认自动探测）")
        sp.add_argument("--with-runtime", action="store_true", dest="with_runtime",
                        help="把独立运行时一起打进去（约 0.4 GB）")
    pp.add_argument("--out", required=True, help="输出目录")
    pp.add_argument("--version", default=os.environ.get("SYSPERSONA_VERSION", "dev"))
    vp.add_argument("zip")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        return cmd_list(a)
    if a.cmd == "pack":
        return cmd_pack(a)
    if a.cmd == "verify":
        return cmd_verify(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
