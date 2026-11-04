# -*- coding: utf-8 -*-
"""M2 向量检索测试（零依赖，使用 hash / fake provider）。

运行：python -m unittest discover -s impl/m2/tests -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from array import array
from pathlib import Path

IMPL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(IMPL))
sys.path.insert(0, str(IMPL / "m1"))

from m2.embed import index as embed_index  # noqa: E402
from m2.embed import search as embed_search  # noqa: E402
from m2.embed import store as embed_store  # noqa: E402
from m2.embed.providers import HashProvider, _unit  # noqa: E402


class FakeSemanticProvider:
    """把所有文本映射到同一向量：用来验证「向量通道确实接上了」。"""

    id = "fake"
    model = "fake-v1"
    dim = 4
    is_semantic = True

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class ProviderTest(unittest.TestCase):
    def test_hash_deterministic_and_unit(self):
        provider = HashProvider()
        a = provider.embed(["NAS 调优项目"])[0]
        b = provider.embed(["NAS 调优项目"])[0]
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(x * x for x in a), 1.0, places=5)

    def test_hash_partial_overlap(self):
        provider = HashProvider()
        base = provider.embed(["NAS 调优：改注册表解决休眠"])[0]
        near = provider.embed(["NAS 调优 休眠"])[0]
        far = provider.embed(["今天天气不错"])[0]
        dot = lambda x, y: sum(i * j for i, j in zip(x, y))
        self.assertGreater(dot(base, near), dot(base, far))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        embed_store.ensure_schema(self.conn)
        self.provider = HashProvider()

    def tearDown(self):
        self.conn.close()

    def test_upsert_skips_unchanged(self):
        vec = self.provider.embed(["一句话"])[0]
        blob = array("f", _unit(vec)).tobytes()
        self.assertTrue(embed_store.upsert(self.conn, "episode", 1, "hash-v1", "一句话", blob))
        self.assertFalse(embed_store.upsert(self.conn, "episode", 1, "hash-v1", "一句话", blob))
        self.assertTrue(embed_store.upsert(self.conn, "episode", 1, "hash-v1", "另一句话", blob))

    def test_coverage_and_prune(self):
        self.conn.execute("CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY, content TEXT)")
        self.conn.execute("INSERT INTO episodes (id, content) VALUES (1, 'a')")
        self.conn.commit()
        vec = array("f", _unit(self.provider.embed(["a"])[0])).tobytes()
        embed_store.upsert(self.conn, "episode", 1, "hash-v1", "a", vec)
        embed_store.upsert(self.conn, "episode", 2, "hash-v1", "b", vec)
        info = embed_store.coverage(self.conn, "hash-v1")
        self.assertEqual(info["indexed"], 2)
        self.assertEqual(info["episodes"], 1)
        self.assertEqual(embed_store.prune(self.conn), 1)


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        embed_store.ensure_schema(self.conn)
        self.provider = HashProvider()
        for target_id, text in enumerate(["NAS 调优 休眠", "周五交稿", "机械键盘"], 1):
            vec = array("f", _unit(self.provider.embed([text])[0])).tobytes()
            embed_store.upsert(self.conn, "episode", target_id, "hash-v1", text, vec)

    def tearDown(self):
        self.conn.close()

    def test_candidates_ranked_and_floored(self):
        query = self.provider.embed(["NAS 休眠"])[0]
        hits = embed_search.cosine_candidates(self.conn, query, "hash-v1", floor=0.1, limit=3)
        self.assertEqual(hits[0][0], 1)
        self.assertTrue(all(score >= 0.1 for _, score in hits))

    def test_rrf_prefers_both_sides(self):
        fused = embed_search.fuse({1: 1, 2: 2}, {2: 1, 3: 2})
        self.assertGreater(fused[2], fused[1])
        self.assertGreater(fused[1], fused[3])
        self.assertAlmostEqual(max(fused.values()), 1.0)

    def test_index_items_idempotent(self):
        conn = sqlite3.connect(":memory:")
        embed_store.ensure_schema(conn)
        items = [("episode", 10, "一句话")]
        self.assertEqual(embed_index.index_items(conn, items, self.provider), 1)
        self.assertEqual(embed_index.index_items(conn, items, self.provider), 0)
        conn.close()


class RetrieveIntegrationTest(unittest.TestCase):
    """验证 M1.retrieve 的 hybrid 通道：词面零重叠也能被向量召回。"""

    def setUp(self):
        import syspersona as sp

        self.sp = sp
        self.root = Path(tempfile.mkdtemp(prefix="sp_m2_"))
        home = self.root / "data"
        (home / "persona").mkdir(parents=True)
        kernel = {
            "schema": "syspersona/persona@1",
            "id": "11111111-2222-4333-8444-555555555555",
            "version": "1.0.0",
            "kernel": {"identity": {"name": "阿岚"}},
        }
        (home / "persona" / "kernel.json").write_text(json.dumps(kernel, ensure_ascii=False), encoding="utf-8")
        (home / "persona" / "traits.json").write_text('{"traits": {}}', encoding="utf-8")
        self.store = sp.Store(home)
        cfg = self.store.load_config()
        cfg["retrieval"] = {"mode": "hybrid", "provider": "fake"}
        self.store.save_config(cfg)
        self.episode_id = self.store.add_episode("s", "user", "毫不相关的旧句子")
        provider = FakeSemanticProvider()
        embed_index.index_items(
            self.store.db, [("episode", self.episode_id, "毫不相关的旧句子")], provider
        )
        self._saved = dict(sp._M2_STATE)
        sp._M2_STATE.update({"ready": True, "provider": provider, "warned": False})

    def tearDown(self):
        self.sp._M2_STATE.clear()
        self.sp._M2_STATE.update(self._saved)
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_vector_channel_recalls_without_lexical_overlap(self):
        hybrid = self.sp.retrieve(self.store, "xyz", k=3, mode="hybrid")
        self.assertIn(self.episode_id, [row["id"] for _, row in hybrid])
        lexical = self.sp.retrieve(self.store, "xyz", k=3, mode="lexical")
        self.assertNotIn(self.episode_id, [row["id"] for _, row in lexical])

    def test_auto_falls_back_when_provider_not_semantic(self):
        self.sp._M2_STATE.update({"provider": HashProvider(), "ready": True})
        self.assertEqual(self.sp._retrieval_mode(self.store.load_config(), "auto"), "lexical")


if __name__ == "__main__":
    unittest.main()
