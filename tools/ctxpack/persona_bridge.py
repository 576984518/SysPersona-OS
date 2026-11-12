# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_bridge.py — 把 Hermes 的对话从 state.db 导出成记忆库格式
r"""persona_bridge.py — 把 Hermes 的对话从 state.db 导出成记忆库格式

迁移后日常在 Hermes 里聊，那些对话存在它自己的 SQLite 里；
这个脚本把它们导出成 `sessions\<日期>\电脑会话\hermes-<会话>.md`，
再归档、进索引——记忆库才不会断流。

用法：
  python persona_bridge.py              # 导出今天的会话
  python persona_bridge.py --days 3     # 最近 3 天
  python persona_bridge.py --dry-run    # 只看有几条
"""
import argparse, io, json, os, re, sqlite3, sys, time
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）

ROOT = _loc.vault()
DB = os.path.join(_loc.hermes_home(), "state.db")

sys.path.insert(0, ROOT)


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


SKIP_FILE = os.path.join(ROOT, "ops", "skip-sessions.txt")


def skipped():
    """弃用的会话 id（一行一个）。归档掉的测试会话每次导出都会被重新写回来，
    所以要有名单，否则"归档"五分钟就复活一次。"""
    try:
        return {l.strip() for l in io.open(SKIP_FILE, encoding="utf-8-sig") if l.strip()
                and not l.startswith("#")}
    except OSError:
        return set()


def export(days, dry):
    t0 = time.time() - days * 86400
    skip = skipped()
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row

    sessions = con.execute(
        "select id, model, title, started_at from sessions where started_at >= ? order by started_at",
        (t0,),
    ).fetchall()

    out_paths = []
    for s in sessions:
        if s["id"] in skip:
            continue
        msgs = con.execute(
            "select role, content from messages where session_id=? and role in ('user','assistant') order by id",
            (s["id"],),
        ).fetchall()
        msgs = [(m["role"], norm(m["content"])) for m in msgs if norm(m["content"])]
        if len(msgs) < 2:
            continue

        day = time.strftime("%Y-%m-%d", time.localtime(s["started_at"] or time.time()))
        d = os.path.join(ROOT, "sessions", day, "电脑会话")
        fname = "hermes-%s.md" % s["id"]
        path = os.path.join(d, fname)

        head = "\n".join([
            "# Hermes 会话 %s" % s["id"],
            "",
            "- 会话：%s" % s["id"],
            "- 来源：Hermes Agent（本地 state.db）",
            "- 模型：%s" % (s["model"] or ""),
            "- 标题：%s" % (s["title"] or ""),
            "- 抓取：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "- 消息数：%d" % len(msgs),
            "",
            "---",
            "",
        ])
        body = "".join(
            "\n\n---\n\n## %s\n\n%s" % ("用户" if r == "user" else "助手", c) for r, c in msgs
        )
        if dry:
            print("  [dry] %s  %d 条  %s" % (fname, len(msgs), day))
            continue
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(head + body + "\n")
        out_paths.append(path)
        got = harvest_observations(msgs, day)
        print("  导出 %s（%d 条）%s" % (fname, len(msgs), "，抓取观察 %d 条" % got if got else ""))

    con.close()
    return out_paths



OBS_RE = re.compile(r"记(?:一?条)?观察[:：]\s*(.+)")


def _with_lock(path, fn):
    """跨进程互斥：多个分身（DSH / Hermes / 手机）可能同时写同一份记忆。

    做法是最土的 O_CREAT|O_EXCL 锁文件 + 50 次重试（5 秒）。
    拿不到锁也照写——宁可冒一次覆盖风险，也不丢这条观察。"""
    lock = path + ".lock"
    fd = None
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.1)
    try:
        return fn()
    finally:
        if fd is not None:
            try:
                os.close(fd)
                os.unlink(lock)
            except OSError:
                pass


def harvest_observations(msgs, day):
    """从本轮消息里抓「记一条观察：…」，追加进 memory/USER.md。"""
    dst = os.path.join(ROOT, "memory", "USER.md")
    if not os.path.exists(dst):
        return 0
    body = open(dst, encoding="utf-8").read()
    marker = "<!-- OBSERVATIONS: append below this line -->"
    add = []
    for role, content in msgs:
        if role != "user":
            continue
        for line in content.splitlines():
            m = OBS_RE.search(line)
            if not m:
                continue
            item = "- [%s] %s → 证据：Hermes 会话" % (day, m.group(1).strip())
            if item not in body and item not in add:
                add.append(item)
    if not add:
        return 0
    if marker in body:
        body = body.replace(marker, marker + "\n" + "\n".join(add), 1)
    else:
        body = body.rstrip() + "\n\n" + "\n".join(add) + "\n"
    def _write():
        open(dst, "w", encoding="utf-8", newline="").write(body)
    _with_lock(dst, _write)
    return len(add)


def archive_all(paths):
    import persona_pack
    st = persona_pack.load_state()
    n = 0
    for p in paths:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        meta = st["processed"].get(rel) or {}
        if meta.get("archived_as"):
            persona_pack._drop_archived(meta["archived_as"])
        day = rel.split("/")[1]
        full = persona_pack.archive(p, title=os.path.basename(p), source="电脑会话", day=day)
        st["processed"][rel] = dict(persona_pack.sig(p),
                                    archived_as=os.path.relpath(full, ROOT).replace("\\", "/"),
                                    at=time.time())
        n += 1
    if n:
        persona_pack.save_state(st)
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-archive", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(DB):
        raise SystemExit("找不到 state.db: %s" % DB)

    print("== 导出 Hermes 会话（最近 %d 天）==" % a.days)
    paths = export(a.days, a.dry_run)
    print("共 %d 个会话" % len(paths))

    if paths and not a.no_archive:
        print("== 归档 ==")
        print("归档 %d 个" % archive_all(paths))
