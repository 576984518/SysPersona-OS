# -*- coding: utf-8 -*-
"""索引：写入时增量、睡眠时补缺、命令行全量重建。"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from . import store
from .providers import EmbeddingProvider, vector_bytes

CHUNK = 32


def _chunks(items: list, size: int = CHUNK) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def index_items(
    conn: sqlite3.Connection,
    items: list[tuple[str, int, str]],
    provider: EmbeddingProvider,
    model: str | None = None,
    *,
    force: bool = False,
) -> int:
    """items = [(kind, target_id, text)]；只嵌入内容变化的条目。"""
    store.ensure_schema(conn)
    model = model or provider.model
    pending: list[tuple[str, int, str]] = []
    for kind, target_id, text in items:
        digest = store.text_hash(text)
        row = conn.execute(
            "SELECT content_sha256 FROM embeddings WHERE target_kind=? AND target_id=? AND model=?",
            (kind, target_id, model),
        ).fetchone()
        if row and digest and row[0] == digest and not force:
            continue
        pending.append((kind, target_id, text))
    if not pending:
        return 0

    written = 0
    for chunk in _chunks(pending):
        vectors = provider.embed([text for _, _, text in chunk])
        if len(vectors) != len(chunk):
            raise RuntimeError("embedding 返回数量与输入不一致")
        for (kind, target_id, text), vector in zip(chunk, vectors):
            if store.upsert(conn, kind, target_id, model, text, vector_bytes(vector)):
                written += 1
        conn.commit()
    return written


def index_episodes(conn: sqlite3.Connection, ids: list[int], provider: EmbeddingProvider, model: str | None = None) -> int:
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, content FROM episodes WHERE id IN ({marks})", tuple(ids)
    ).fetchall()
    return index_items(conn, [("episode", row[0], row[1]) for row in rows], provider, model)


def reindex_missing(conn: sqlite3.Connection, provider: EmbeddingProvider, model: str | None = None, limit: int | None = None) -> int:
    """给还没建过向量的 episode 补索引（迁移/旧库）。"""
    store.ensure_schema(conn)
    model = model or provider.model
    sql = (
        "SELECT e.id, e.content FROM episodes e"
        " LEFT JOIN embeddings m ON m.target_kind='episode' AND m.target_id=e.id AND m.model=?"
        " WHERE m.id IS NULL ORDER BY e.id"
    )
    params: tuple = (model,)
    if limit:
        sql += " LIMIT ?"
        params = (model, limit)
    rows = conn.execute(sql, params).fetchall()
    return index_items(conn, [("episode", row[0], row[1]) for row in rows], provider, model)


def reindex_all(conn: sqlite3.Connection, provider: EmbeddingProvider, model: str | None = None) -> int:
    store.ensure_schema(conn)
    model = model or provider.model
    rows = conn.execute("SELECT id, content FROM episodes ORDER BY id").fetchall()
    return index_items(conn, [("episode", row[0], row[1]) for row in rows], provider, model, force=True)
