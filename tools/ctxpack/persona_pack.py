#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
persona_pack.py — 上下文提取与归档（MVP，v2.5.0）

只处理"变过的文件"：按 mtime + size 记录水位，提取过的自动跳过；
提取完先归档打包，再记水位，下次不会重复处理。

目录约定（脚本同级放记录目录；记录目录里按 日期/来源 两层分桶，来源互不干扰）：
  sessions/2026-11-07/电脑会话/xxx.md     ← 输入（另一台电脑的 DSH 会话）
  sessions/2026-11-08/deepseek网页/xxx.md     ← 输入（网页版对话）
  sessions/2026-11-08/网页文章/xxx.md     ← 输入（网页文章正文）
  archive/2026-11-07/电脑会话/            ← 归档自动跟随输入的 日期/来源
来源名 = 路径里日期段(YMD)的下一段；日期段不存在则视为无来源。

  archive  归档一段对话（可读 .md + .gz + index）
  digest   提取「状态 / 定下的 / 否决的 / 下一步」；可传目录，只处理变更文件
  search   检索归档片段，可按时间或频次排序，可按来源过滤
  status   看哪些文件还没被处理

用法：
  python persona_pack.py digest ./notes -o HANDOFF.md --dry-run
  python persona_pack.py digest . -o HANDOFF.md
  python persona_pack.py search 分身 --sort time --source deepseek网页
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
INDEX = os.path.join(ARCHIVE, "index.jsonl")
STATE = os.path.join(ROOT, "state.json")

DECIDE = ("必须", "决定", "采用", "规则", "不能", "MUST", "应当")
REJECT = ("否决", "不要", "放弃", "不再", "错了", "删掉", "驳回", "反例")
NEXT = ("下一步", "待办", "TODO", "还要", "接下来", "未做", "计划")
STATUS = ("版本", "当前", "状态", "已完成", "发布")
EXTS = (".md", ".txt", ".markdown")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def source_of(rel):
    """从相对路径取 来源名：找到日期段(YMD)，其下一段即来源；找不到返回空。"""
    parts = rel.replace("\\", "/").split("/")
    for i, p in enumerate(parts):
        if DATE_RE.match(p):
            return parts[i + 1] if i + 1 < len(parts) else ""
    return ""


def day_of(rel):
    """从相对路径取 记录日期段(YMD)，用于归档分桶；找不到返回 None。"""
    for p in rel.replace("\\", "/").split("/"):
        if DATE_RE.match(p):
            return p
    return None


def _slug(s):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", s).strip("-")
    return (s[:40] or "note")


def _read(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def _lines(text):
    return [l.strip() for l in text.splitlines() if l.strip()]


def _pick(lines, keys, limit):
    seen, picked = set(), []
    for line in lines:
        if any(k in line for k in keys):
            key = line[:60]
            if key in seen:
                continue
            seen.add(key)
            picked.append(line if len(line) <= 200 else line[:200] + "…")
            if len(picked) >= limit:
                break
    return picked


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"processed": {}}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def sig(path):
    st = os.stat(path)
    return {"mtime": int(st.st_mtime), "size": st.st_size}


def scan(root, st):
    """返回 (changed, skipped)。跳过归档目录、状态文件与 HANDOFF 摘要。"""
    changed, skipped = [], []
    files = []
    if os.path.isfile(root):
        files = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("archive", ".git", "__pycache__")]
            for fn in filenames:
                if fn.lower().startswith("handoff"):
                    continue
                if fn.lower().endswith(EXTS):
                    files.append(os.path.join(dirpath, fn))
    for p in sorted(files):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        cur = sig(p)
        old = st["processed"].get(rel)
        if old and old.get("mtime") == cur["mtime"] and old.get("size") == cur["size"]:
            skipped.append(rel)
        else:
            changed.append((p, rel))
    return changed, skipped


def digest_text(path, budget=2400):
    lines = _lines(_read(path))
    out = []
    for title, keys, limit in (
        ("状态", STATUS, 4),
        ("定下的", DECIDE, 8),
        ("否决的", REJECT, 6),
        ("下一步", NEXT, 6),
    ):
        items = _pick(lines, keys, limit)
        if items:
            out.append("### " + title)
            out += ["- " + it.lstrip("-* ").strip() for it in items]
            out.append("")
    body = "\n".join(out)
    return body if len(body) <= budget else body[:budget] + "\n…（截断）\n"


def _indexed_files():
    """索引里已记录过的归档路径集合（用于幂等判断）。"""
    out = set()
    if not os.path.exists(INDEX):
        return out
    try:
        with open(INDEX, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.add(json.loads(line).get("file", ""))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _drop_archived(old_rel):
    """删掉某个来源的旧归档（.md 与 .gz）并移除其索引行，保证一个来源只留最新快照。"""
    if not old_rel:
        return
    full = os.path.join(ROOT, old_rel.replace("/", os.sep))
    for p in (full, full + ".gz"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    if not os.path.exists(INDEX):
        return
    keep = []
    with open(INDEX, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except ValueError:
                continue
            if rec.get("file") == old_rel:
                continue
            keep.append(s)
    with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))


def archive(path, title=None, tags=None, source=None, day=None):
    os.makedirs(ARCHIVE, exist_ok=True)
    text = _read(path)
    day = day or time.strftime("%Y-%m-%d")
    d = os.path.join(ARCHIVE, day)
    if source:
        d = os.path.join(d, _slug(source))
    os.makedirs(d, exist_ok=True)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    name = "%s-%s.md" % (_slug(title or os.path.splitext(os.path.basename(path))[0]), h)
    full = os.path.join(d, name)
    rel_full = os.path.relpath(full, ROOT).replace("\\", "/")
    # 幂等：同路径同 sha256 已归档过就不再写、不再追加索引
    if os.path.exists(full) and os.path.exists(full + ".gz") and rel_full in _indexed_files():
        return full
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    with gzip.open(full + ".gz", "wb") as f:
        f.write(text.encode("utf-8"))
    with open(INDEX, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "file": os.path.relpath(full, ROOT).replace("\\", "/"),
            "day": day,
            "source": _slug(source) if source else "",
            "title": title or os.path.basename(path),
            "tags": tags or [],
            "chars": len(text),
            "sha256": h,
            "saved_at": time.time(),
        }, ensure_ascii=False) + "\n")
    return full


def _mtime(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0


def cmd_digest(root, out_path, dry_run, st, max_chars=0, all_files=False, includes=None):
    changed, skipped = scan(root, st)
    if all_files:
        extra = [(os.path.join(ROOT, rel.replace("/", os.sep)), rel) for rel in skipped]
        changed = changed + extra
        # 新会话优先：日期倒序 → 同一天内按修改时间倒序（最近在动的先展开）
        changed.sort(key=lambda x: (day_of(x[1]) or "0000-00-00", _mtime(x[0])), reverse=True)
        skipped = []
    print("待处理 %d 个，已跳过 %d 个（未变更）" % (len(changed), len(skipped)))
    if not changed:
        print("没有新内容，不生成摘要。")
        return 1
    for p, rel in changed:
        print("  + %s" % rel)
    if dry_run:
        return 0

    parts = ["# 交接摘要", "", "> 由 `persona_pack.py digest` 生成 · %s" % time.strftime("%Y-%m-%d %H:%M"), ""]
    for inc in (includes or []):
        try:
            body = _read(inc)
        except OSError:
            continue
        parts.append("## 附：%s（全文）" % os.path.basename(inc))
        parts.append("")
        parts.append(body)
        parts.append("")
    used, cut = 0, []
    for p, rel in changed:
        block = digest_text(p) or "（无可抽取内容）"
        if max_chars and used + len(block) > max_chars:
            cut.append(rel)
            continue
        parts.append("## 来源：`%s`" % rel)
        parts.append("")
        parts.append(block)
        parts.append("")
        used += len(block)
    if cut:
        parts.append("> 其余 %d 个文件因超出 %d 字上限未展开（归档已完整保留，用 search 查）：" % (len(cut), max_chars))
        parts += ["- `%s`" % r for r in cut]

    text = "\n".join(parts)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    archived = 0
    for p, rel in changed:
        meta = st["processed"].get(rel) or {}
        cur = sig(p)
        fresh = not (meta.get("mtime") == cur["mtime"] and meta.get("size") == cur["size"])
        if not fresh:
            continue  # --all 重出摘要时，未变的不重复归档
        old = meta.get("archived_as") or ""
        new_h = hashlib.sha256(_read(p).encode("utf-8")).hexdigest()[:8]
        if old.endswith("-%s.md" % new_h) and os.path.exists(os.path.join(ROOT, old.replace("/", os.sep))):
            st["processed"][rel] = dict(meta, mtime=cur["mtime"], size=cur["size"], at=time.time())
            continue  # 内容哈希没变（只是 mtime 变了）：视为同一份，不动归档
        if old:
            _drop_archived(old)  # 内容变了：旧版本作废，只留最新
        full = archive(p, title=os.path.basename(p), source=source_of(rel), day=day_of(rel))
        st["processed"][rel] = sig(p)
        st["processed"][rel]["archived_as"] = os.path.relpath(full, ROOT).replace("\\", "/")
        st["processed"][rel]["at"] = time.time()
        archived += 1
    save_state(st)
    print("已写 %s（%d 字），归档 %d 个文件；水位已更新。" % (out_path, len(text), archived))
    return 0


def cmd_status(root, st):
    changed, skipped = scan(root, st)
    print("未处理：%d" % len(changed))
    for _, rel in changed:
        print("  + " + rel)
    print("已处理：%d" % len(skipped))
    for rel in skipped[:10]:
        print("  - " + rel)
    return 0


QSTOP = set("""的 了 是 要 在 有 和 与 或 什么 怎么 为什么 哪 哪个 多少 多久 吗 呢 吧 会 能 可以 叫
一个 这个 那个 里 中 上 下 前 后 时 及 我 你 他 它 我们 你们 他们 现在 已经 还是 就是 一次 应该 时候""".split())


def _terms(q):
    """把查询拆成词元：先按标点，再按中文虚词切，去掉虚词与单字。"""
    q = q.strip()
    parts = re.split(r"[\s,，。！？?、;；:：\"'“”‘’（）()\[\]【】{}/\\|]+", q)
    pat = "|".join(sorted(QSTOP, key=len, reverse=True))
    out = []
    for p in parts:
        if not p:
            continue
        for seg in re.split(pat, p) if pat else [p]:
            seg = seg.strip()
            if not seg or seg in QSTOP:
                continue
            if len(seg) == 1 and not seg.isdigit():
                continue
            out.append(seg)
    # 去重保序
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def search(q, limit=5, ctx=1, sort="freq", source=None):
    if not os.path.isdir(ARCHIVE):
        return []
    terms = _terms(q)
    multi = len(terms) > 1
    if not terms:
        terms = [q]
    hits = []
    for dirpath, dirnames, filenames in os.walk(ARCHIVE):
        dirnames.sort(reverse=True)
        rel_dir = os.path.relpath(dirpath, ARCHIVE).replace("\\", "/")
        parts = [] if rel_dir == "." else rel_dir.split("/")
        day = parts[0] if parts else ""
        src = parts[1] if len(parts) >= 2 else ""
        if not DATE_RE.match(day):
            continue
        if source and src != _slug(source) and src != source:
            continue
        for fn in sorted(filenames):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                lines = _read(p).splitlines()
            except OSError:
                continue
            if not multi:
                idxs = [i for i, l in enumerate(lines) if q.lower() in l.lower()]
                if not idxs:
                    continue
                i = idxs[0]
                hits.append({"file": os.path.relpath(p, ROOT).replace("\\", "/"),
                             "day": day, "source": src, "score": len(idxs),
                             "snippet": "\n".join(lines[max(0, i - ctx):i + ctx + 1]).strip()})
                continue
            # 多词：按"命中的词长度之和"打分，长词更具体；返回前 3 个高分行，别只给一行
            scored = []
            for i, l in enumerate(lines):
                low = l.lower()
                matched = [t for t in terms if t.lower() in low]
                if not matched:
                    continue
                s = sum(len(t) for t in matched) + 2 * len(matched)
                scored.append((s, i))
            if not scored:
                continue
            scored.sort(key=lambda x: (-x[0], x[1]))
            top = scored[:3]
            best_s = top[0][0]
            picks = []
            for _, i in top:
                seg = lines[max(0, i - ctx):i + ctx + 1]
                picks.append("\n".join(seg).strip())
            hits.append({"file": os.path.relpath(p, ROOT).replace("\\", "/"),
                         "day": day, "source": src, "score": best_s,
                         "snippet": "\n…\n".join(picks)})
    if sort == "time":
        hits.sort(key=lambda x: (x["day"], x["file"]), reverse=True)
    else:
        hits.sort(key=lambda x: (-x["score"], x["day"]), reverse=False)
    return hits[:limit]


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="上下文提取与归档")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("archive")
    a.add_argument("path")
    a.add_argument("--title", default=None)
    a.add_argument("--source", default=None)
    a.add_argument("--day", default=None)
    a.add_argument("--tags", default="")

    d = sub.add_parser("digest")
    d.add_argument("path")
    d.add_argument("-o", "--out", default="HANDOFF-session.md")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--max-chars", type=int, default=0, help="摘要总字数上限，0 为不限")
    d.add_argument("--all", action="store_true", help="忽略水位，把所有文件重新出一份摘要")
    d.add_argument("--include", action="append", default=None, help="额外并入摘要的文件（如 DECISIONS.md），可重复")

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--sort", choices=["freq", "time"], default="freq")
    s.add_argument("--source", default=None)
    s.add_argument("--hybrid", action="store_true", help="（已默认开启，保留兼容）")
    s.add_argument("--no-vector", action="store_true",
                   help="只走关键词：零依赖、不连模型、结果可预期")

    st = sub.add_parser("status")
    st.add_argument("path", default=".")

    args = ap.parse_args()
    state = load_state()

    if args.cmd == "archive":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        full = archive(args.path, args.title, tags, args.source, args.day)
        print("archived:", full)
        print("gz:      ", full + ".gz")
    elif args.cmd == "digest":
        return cmd_digest(args.path, args.out, args.dry_run, state, args.max_chars, args.all, args.include)
    elif args.cmd == "status":
        return cmd_status(args.path, state)
    else:
        # 默认走混合（记忆优先：宁慢勿漏），失败静默退回关键词。
        # 要纯关键词（零依赖、可预期）用 --no-vector。
        if not getattr(args, 'no_vector', False):
            # 可选增强：向量路只做加法，不改变"关键词永远可用"的底线。
            # 模型没起、索引缺失、依赖没装——一律静默退回关键词，绝不返回"没找到"了事。
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import persona_search
                persona_search.ROOT = ROOT
                persona_search.ARCHIVE = ARCHIVE
                persona_search.INDEX = os.path.join(ROOT, 'index')
                rows = persona_search.merged(args.query, k=args.limit)
                if rows:
                    for r in rows:
                        print("== %s  (rrf=%.4f, %s, source=%s)  [%s]"
                              % (r['file'], r['score'], r['day'], r['source'] or '-', r['why']))
                        print(r['text'][:400].strip())
                        print()
                    return 0
                print("（混合路无结果，退回关键词）", file=sys.stderr)
            except Exception as e:
                print("（混合检索不可用，退回关键词：%s）" % e, file=sys.stderr)
        hits = search(args.query, args.limit, sort=args.sort, source=args.source)
        if not hits:
            print("no hits")
            return 1
        for h in hits:
            print("== %s  (hits=%d, %s, source=%s)" % (h["file"], h["score"], h["day"], h["source"] or "-"))
            print(h["snippet"])
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
