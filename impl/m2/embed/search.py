# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：余弦检索与 RRF 融合。
"""余弦检索与 RRF 融合。

性能：
  - 优先 math.sumprod（3.12+，C 实现）；
  - 可选 numpy（已安装时）；
  - 都是兜底时限制候选数，避免纯 Python 循环拖慢对话。
"""

from __future__ import annotations

import math
import sqlite3
from array import array

from . import store

RRF_K = 60


def _dot(a: array, b: array) -> float:
    if hasattr(math, "sumprod"):
        return math.sumprod(a, b)
    try:
        import numpy as np  # type: ignore

        return float(np.dot(np.frombuffer(a, dtype="f"), np.frombuffer(b, dtype="f")))
    except ImportError:
        return sum(x * y for x, y in zip(a, b))


def cosine_candidates(
    conn: sqlite3.Connection,
    query: list[float],
    model: str,
    *,
    kind: str = "episode",
    floor: float = 0.25,
    limit: int = 50,
) -> list[tuple[int, float]]:
    q = array("f", query)
    qnorm = math.sqrt(sum(v * v for v in q)) or 1.0
    scored: list[tuple[int, float]] = []
    for target_id, vec, norm in store.load(conn, model, kind):
        if len(vec) != len(q):
            continue
        sim = _dot(q, vec) / (qnorm * (norm or 1.0))
        if sim >= floor:
            scored.append((target_id, sim))
    scored.sort(key=lambda item: -item[1])
    return scored[:limit]


def fuse(lexical_rank: dict[int, int], vector_rank: dict[int, int], k: int = RRF_K) -> dict[int, float]:
    """倒数排名融合；缺失的一侧按 0 计。"""
    fused: dict[int, float] = {}
    for target_id in set(lexical_rank) | set(vector_rank):
        score = 0.0
        if target_id in lexical_rank:
            score += 1.0 / (k + lexical_rank[target_id])
        if target_id in vector_rank:
            score += 1.0 / (k + vector_rank[target_id])
        fused[target_id] = score
    peak = max(fused.values(), default=0.0)
    if peak:
        fused = {key: value / peak for key, value in fused.items()}
    return fused
