# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：embeddings 表：与 examples/schema.sql 对齐，补 content_sha256 / norm。
"""embeddings 表：与 examples/schema.sql 对齐，补 content_sha256 / norm。"""

from __future__ import annotations

import hashlib
import sqlite3
from array import array
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    id             INTEGER PRIMARY KEY,
    target_kind    TEXT NOT NULL,
    target_id      INTEGER NOT NULL,
    model          TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    norm           REAL NOT NULL,
    vector         BLOB NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(target_kind, target_id, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model, target_kind);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upsert(
    conn: sqlite3.Connection,
    kind: str,
    target_id: int,
    model: str,
    text: str,
    vector: bytes,
    norm: float = 1.0,
    force: bool = False,
) -> bool:
    """内容没变就跳过；返回是否写入。"""
    digest = text_hash(text)
    row = conn.execute(
        "SELECT content_sha256 FROM embeddings WHERE target_kind=? AND target_id=? AND model=?",
        (kind, target_id, model),
    ).fetchone()
    if row and row[0] == digest and not force:
        return False
    dim = len(vector) // 4
    conn.execute(
        "INSERT INTO embeddings (target_kind, target_id, model, dim, content_sha256, norm, vector, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(target_kind, target_id, model) DO UPDATE SET"
        " dim=excluded.dim, content_sha256=excluded.content_sha256, norm=excluded.norm,"
        " vector=excluded.vector, created_at=excluded.created_at",
        (kind, target_id, model, dim, digest, norm, vector, now()),
    )
    return True


def load(conn: sqlite3.Connection, model: str, kind: str = "episode"):
    """返回 [(target_id, array('f'), norm)]，供暴力余弦。"""
    rows = conn.execute(
        "SELECT target_id, vector, norm FROM embeddings WHERE model=? AND target_kind=?",
        (model, kind),
    ).fetchall()
    return [(row[0], array("f", row[1]), float(row[2])) for row in rows]


def coverage(conn: sqlite3.Connection, model: str) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    indexed = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE model=? AND target_kind='episode'", (model,)
    ).fetchone()[0]
    models = [
        {"model": row[0], "count": row[1], "dim": row[2]}
        for row in conn.execute(
            "SELECT model, COUNT(*), MAX(dim) FROM embeddings GROUP BY model ORDER BY model"
        ).fetchall()
    ]
    return {"episodes": total, "indexed": indexed, "coverage": (indexed / total) if total else 0.0, "models": models}


def prune(conn: sqlite3.Connection) -> int:
    """删除指向已不存在 episode 的向量。"""
    cur = conn.execute(
        "DELETE FROM embeddings WHERE target_kind='episode'"
        " AND target_id NOT IN (SELECT id FROM episodes)"
    )
    conn.commit()
    return cur.rowcount


def drop_model(conn: sqlite3.Connection, model: str) -> int:
    cur = conn.execute("DELETE FROM embeddings WHERE model=?", (model,))
    conn.commit()
    return cur.rowcount
