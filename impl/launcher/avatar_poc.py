#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avatar_poc.py — 人格分身记忆合并的最小可行性实验

验证 docs/AVATAR_SPEC.md 的 TC-AV1..AV6：
  AV1 分身独立记录
  AV2 跨分支不去重（同一事件两个视角，合并后两条都在）
  AV3 分支内幂等（重复提交不产生第二条）
  AV4 发言权按会话加锁（同会话互斥，不同会话并行）
  AV5 device + concurrent 组合被拒绝
  AV6 对账不覆盖：双方库不变，主库只追加

运行：python impl/launcher/avatar_poc.py
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from launcher import seg  # 复用中文双字 gram，保证检索与主实现一致

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT UNIQUE,
  branch_id TEXT,
  branch_name TEXT,
  ts REAL,
  speaker TEXT,
  text TEXT,
  tags TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS ep_fts USING fts5(text, tags, tokenize='unicode61');
"""


def open_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def write_episode(db, branch_id, branch_name, text, ts=None, speaker="assistant", uuid=None):
    ts = ts if ts is not None else time.time()
    uuid = uuid or "%s-%d-%d" % (branch_id, int(ts * 1000), abs(hash(text)) % 100000)
    tags = ",".join(t for t in seg(text).split() if len(t) == 2)[:200]
    cur = db.execute(
        "INSERT OR IGNORE INTO episodes(uuid,branch_id,branch_name,ts,speaker,text,tags) VALUES(?,?,?,?,?,?,?)",
        (uuid, branch_id, branch_name, ts, speaker, text, tags))
    if cur.rowcount:
        db.execute("INSERT INTO ep_fts(rowid,text,tags) VALUES(?,?,?)",
                   (cur.lastrowid, seg(text), seg(tags)))
    db.commit()
    return (cur.lastrowid if cur.rowcount else None), uuid


def merge(main, branches):
    """并集：按 uuid 幂等，跨分支不去重。"""
    added = 0
    for db, branch_id in branches:
        rows = db.execute(
            "SELECT uuid,branch_id,branch_name,ts,speaker,text,tags FROM episodes").fetchall()
        for uuid, bid, bname, ts, speaker, text, tags in rows:
            cur = main.execute(
                "INSERT OR IGNORE INTO episodes(uuid,branch_id,branch_name,ts,speaker,text,tags) "
                "VALUES(?,?,?,?,?,?,?)", (uuid, bid, bname, ts, speaker, text, tags))
            if cur.rowcount:
                main.execute("INSERT INTO ep_fts(rowid,text,tags) VALUES(?,?,?)",
                             (cur.lastrowid, seg(text), seg(tags)))
                added += 1
    main.commit()
    return added


def query(db, q, k=10):
    toks = [t for t in dict.fromkeys(seg(q).split()) if len(t) >= 1][:16]
    if not toks:
        return []
    match = " OR ".join('"%s"' % t for t in toks)
    try:
        rows = db.execute(
            "SELECT e.branch_name,e.text,e.ts FROM ep_fts JOIN episodes e ON e.id=ep_fts.rowid "
            "WHERE ep_fts MATCH ? ORDER BY e.ts LIMIT ?", (match, k)).fetchall()
    except sqlite3.OperationalError:
        return []
    return rows


def db_hash(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]


OK = []


def check(tc, desc, cond, detail=""):
    OK.append(bool(cond))
    print("%s %-6s %s%s" % ("PASS" if cond else "FAIL", tc, desc, ("  [" + detail + "]") if detail else ""))


def main():
    root = tempfile.mkdtemp(prefix="avatar-poc-")
    a_path = os.path.join(root, "branch_a.db")
    b_path = os.path.join(root, "branch_b.db")
    m_path = os.path.join(root, "main.db")

    A = open_db(a_path)
    B = open_db(b_path)
    M = open_db(m_path)

    now = time.time()
    # 分支 A（电脑端）：自己的经历
    write_episode(A, "pc-01", "电脑端分身", "我在电脑前和老板一起写 SysPersona 的规范", now)
    # 分支 B（手机端）：自己的经历
    write_episode(B, "ph-01", "手机端分身", "我在手机上帮老板查了明天的天气", now + 1)
    # 同一事件，两个视角（AV2 的关键）
    write_episode(A, "pc-01", "电脑端分身", "老板说下周要搬家", now + 2)
    write_episode(B, "ph-01", "手机端分身", "老板说下周要搬家", now + 3)

    check("TC-AV1", "分身各自记录，互不写入",
          A.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2 and
          B.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2)

    h_a, h_b = db_hash(a_path), db_hash(b_path)
    added = merge(M, [(A, "pc-01"), (B, "ph-01")])
    total = M.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    check("TC-AV2", "跨分支不去重：同一事件保留两条视角",
          total == 4 and added == 4,
          "rows=%d added=%d" % (total, added))

    added2 = merge(M, [(A, "pc-01"), (B, "ph-01")])
    total2 = M.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    check("TC-AV3", "分支内幂等：重复合并不新增", added2 == 0 and total2 == 4,
          "added=%d rows=%d" % (added2, total2))

    check("TC-AV6", "对账不覆盖：两个分支库字节未变",
          db_hash(a_path) == h_a and db_hash(b_path) == h_b)

    hits = query(M, "老板今天让我做了什么")
    branches = {r[0] for r in hits}
    check("TC-AV1b", "主脑可检索并区分来源", len(hits) >= 2 and len(branches) >= 2,
          "hits=%d branches=%s" % (len(hits), "/".join(sorted(branches))))

    # 发言权：按会话加锁
    floors = {}

    def acquire(session, branch):
        if session in floors:
            return False
        floors[session] = branch
        return True

    ok_same = acquire("s-user", "pc-01") and not acquire("s-user", "ph-01")
    ok_diff = acquire("s-agent", "ph-01")
    check("TC-AV4", "发言权按会话唯一，跨会话可并行", ok_same and ok_diff)

    def valid_combo(presence, encryption):
        return not (presence == "concurrent" and encryption == "device")

    check("TC-AV5", "device + concurrent 被拒绝",
          valid_combo("concurrent", "device") is False and valid_combo("concurrent", "managed") is True)

    print("\n合并后的主库：")
    for bname, text, ts in M.execute(
            "SELECT branch_name,text,ts FROM episodes ORDER BY ts").fetchall():
        print("  [%s] %s" % (bname, text))

    print("\n问：我今天都干了什么？")
    for bname, text, ts in query(M, "今天都干了什么 代码 天气 搬家"):
        print("  - %s：%s" % (bname, text))

    A.close(); B.close(); M.close()
    bad = OK.count(False)
    print("\n%d/%d passed" % (len(OK) - bad, len(OK)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
