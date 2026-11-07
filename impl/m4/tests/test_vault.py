# -*- coding: utf-8 -*-
"""M4 加密层：KAT 与负向矩阵（规范 §20）。

运行：
  python -m unittest discover -s impl/m4/tests -v
需要可选依赖 cryptography + argon2-cffi；未安装时相关用例 skip。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

IMPL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(IMPL))

from m4.vault import api, bip39, container, home as home_mod  # noqa: E402
from m4.vault.canonical import canonical  # noqa: E402
from m4.vault.errors import VaultError  # noqa: E402
from m4.vault.provider import hkdf_sha256, get_provider, available  # noqa: E402

PASSPHRASE = "correct horse battery staple"
HAS_CRYPTO = available()


def build_home(root: Path, rows: int = 2) -> Path:
    home = Path(root) / "data"
    (home / "persona").mkdir(parents=True)
    kernel = {
        "schema": "syspersona/persona@1",
        "id": "9f2b1c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
        "version": "1.4.0",
        "kernel": {"identity": {"name": "阿岚"}, "values": ["诚实 > 顺从"]},
    }
    (home / "persona" / "kernel.json").write_text(json.dumps(kernel, ensure_ascii=False), encoding="utf-8")
    (home / "persona" / "traits.json").write_text(
        json.dumps({"traits": {"preferences": []}}, ensure_ascii=False), encoding="utf-8"
    )
    (home / "persona" / "state.json").write_text(json.dumps({"mood": {}}), encoding="utf-8")
    conn = sqlite3.connect(home / "memory.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, content TEXT)")
    conn.executemany(
        "INSERT INTO episodes (content) VALUES (?)",
        [(f"片段 {i}：我在做 NAS 调优项目",) for i in range(rows)],
    )
    conn.commit()
    conn.close()
    (home / "config.json").write_text('{"model": "mock-a"}', encoding="utf-8")
    return home


class KATTest(unittest.TestCase):
    """§20.1 已知答案测试。"""

    @classmethod
    def setUpClass(cls):
        if not HAS_CRYPTO:
            raise unittest.SkipTest("需要 cryptography/argon2-cffi")

    def test_hkdf_rfc5869(self):
        okm = hkdf_sha256(b"\x0b" * 22, bytes(range(13)), bytes(range(0xF0, 0xFA)), 42)
        self.assertTrue(okm.hex().startswith("3cb25f25faacd57a90434f64d0362f2a"))

    def test_hmac_vector(self):
        provider = get_provider()
        mac = provider.mac("hmac-sha256", b"key", b"The quick brown fox jumps over the lazy dog")
        self.assertEqual(mac.hex(), "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8")

    def test_aes_gcm_vectors(self):
        provider = get_provider()
        key, nonce = bytes(32), bytes(12)
        self.assertEqual(
            provider.aead_seal("aes-256-gcm", key, nonce, b"", b"").hex(),
            "530f8afbc74536b9a963b4f1c4cb738b",
        )
        self.assertEqual(
            provider.aead_seal("aes-256-gcm", key, nonce, b"", bytes(16)).hex(),
            "cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919",
        )

    def test_argon2id_vector(self):
        provider = get_provider()
        key = provider.derive_key(
            "argon2id",
            PASSPHRASE.encode(),
            bytes(range(16)),
            {"memory": 19456, "passes": 2, "parallelism": 1},
        )
        self.assertEqual(
            key.hex(), "818259b6310026a8e0dbac5d2e6927abcfdb07b32258fac4f61b18b80f929085"
        )

    def test_scrypt_vector(self):
        provider = get_provider()
        key = provider.derive_key(
            "scrypt", PASSPHRASE.encode(), bytes(range(16)), {"N": 32768, "r": 8, "p": 1}
        )
        self.assertEqual(
            key.hex(), "7a8e34241db898d59175c696538c417467a975ffe569068425f16188d3159c58"
        )

    def test_bip39_zero_entropy(self):
        phrase = bip39.mnemonic_from_entropy(bytes(32))
        self.assertEqual(phrase.split()[-1], "art")
        self.assertEqual(len(phrase.split()), 24)
        self.assertEqual(bip39.entropy_from_mnemonic(phrase), bytes(32))

    def test_canonical_json(self):
        self.assertEqual(canonical({"b": 1, "a": "×"}).hex(), "7b2261223a22c397222c2262223a317d")
        with self.assertRaises(VaultError):
            canonical({"x": 0.5})
        with self.assertRaises(VaultError):
            canonical({"x": "A\u030A"})


@unittest.skipUnless(HAS_CRYPTO, "需要 cryptography/argon2-cffi")
class ContainerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sp_m4_"))
        self.home = build_home(self.root)
        self.vault = self.root / "me.vault"
        self.info = api.seal_home(self.home, self.vault, PASSPHRASE, profile="scrypt", recovery=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _copy(self, name: str, mutate) -> Path:
        raw = bytearray(self.vault.read_bytes())
        changed = mutate(raw)
        if changed is not None:
            raw = bytearray(changed)
        path = self.root / name
        path.write_bytes(bytes(raw))
        return path

    # R1 往返
    def test_roundtrip(self):
        out = api.unlock_home(self.vault, self.root / "restored", PASSPHRASE)
        self.assertEqual(out["db_sha256"], hashlib.sha256((self.home / "memory.db").read_bytes()).hexdigest())
        restored = self.root / "restored"
        self.assertEqual((restored / "persona" / "kernel.json").read_bytes(),
                         (self.home / "persona" / "kernel.json").read_bytes())

    # N1
    def test_wrong_passphrase(self):
        with self.assertRaises(VaultError) as ctx:
            api.unlock_home(self.vault, self.root / "bad", "wrong passphrase")
        self.assertEqual(ctx.exception.code, "E_UNLOCK")
        self.assertEqual(str(ctx.exception), "保险库无法打开")

    # N2
    def test_tamper_payload(self):
        path = self._copy("t2.vault", lambda raw: raw.__setitem__(-1, raw[-1] ^ 1))
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(path, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_INTEGRITY")

    # N3
    def test_tamper_header_metadata(self):
        path = self._copy("t3.vault", lambda raw: raw.replace(b'"version":"1.4.0"', b'"version":"9.9.9"'))
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(path, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_INTEGRITY")

    # N4
    def test_tamper_wrap(self):
        _, header, _ = container.parse_file(self.vault)
        old = container._b64d(header["wraps"][0]["ct"])
        new = bytes([old[0] ^ 1]) + old[1:]
        path = self._copy("t4.vault", lambda raw: raw.replace(
            header["wraps"][0]["ct"].encode(), container._b64e(new).encode()))
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(path, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_UNLOCK")

    # N5
    def test_truncated_and_appended(self):
        truncated = self._copy("t5a.vault", lambda raw: raw.__delitem__(slice(-5, None)))
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(truncated, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_FORMAT")
        appended = self.root / "t5b.vault"
        appended.write_bytes(self.vault.read_bytes() + b"\x00")
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(appended, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_FORMAT")

    # N6
    def test_unknown_kdf(self):
        path = self._copy("t6.vault", lambda raw: raw.replace(b'"id":"scrypt"', b'"id":"scrypX"', 1))
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(path, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_UNSUPPORTED")

    # N7 内核签名
    def test_kernel_signature(self):
        opened = container.open_container(self.vault, PASSPHRASE)
        header = opened.header
        provider = get_provider()
        salt = container._b64d(header["content"]["salt"])
        plain = zipfile.ZipFile(io.BytesIO(
            provider.aead_open("aes-256-gcm", container._vk(opened.ks, salt),
                               container._b64d(header["content"]["nonce"]),
                               container.payload_aad(header), container.parse_file(self.vault)[2])
        ))
        files = {name: plain.read(name) for name in plain.namelist()}
        manifest = json.loads(files["manifest.json"])
        sig = bytearray(container._b64d(manifest["kernel"]["sig"]["value"]))
        sig[0] ^= 1
        manifest["kernel"]["sig"]["value"] = container._b64e(bytes(sig))
        files["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, blob in files.items():
                archive.writestr(name, blob)
        nonce = os.urandom(12)
        header["content"]["nonce"] = container._b64e(nonce)
        header["mac"] = container._b64e(container.header_mac(header, opened.ks, salt, provider))
        payload = provider.aead_seal("aes-256-gcm", container._vk(opened.ks, salt), nonce,
                                     container.payload_aad(header), buffer.getvalue())
        raw_header = canonical(header)
        blob = (container.MAGIC + (1).to_bytes(2, "big") + len(raw_header).to_bytes(2, "big")
                + raw_header + len(payload).to_bytes(4, "big") + payload)
        path = self.root / "t7.vault"
        path.write_bytes(blob)
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(path, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_SIGNATURE")

    # N8 内核变更需批准
    def test_unapproved_kernel_change(self):
        kernel = json.loads((self.home / "persona" / "kernel.json").read_text(encoding="utf-8"))
        kernel["kernel"]["identity"]["name"] = "阿岚2"
        (self.home / "persona" / "kernel.json").write_text(json.dumps(kernel, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(VaultError) as ctx:
            api.seal_home(self.home, self.vault, PASSPHRASE, profile="scrypt")
        self.assertEqual(ctx.exception.code, "E_POLICY")
        info = api.seal_home(self.home, self.vault, PASSPHRASE, profile="scrypt", approve_kernel_change=True)
        self.assertEqual(info["ok"], True)

    # N9 绑定
    def test_device_binding(self):
        material_a = bytes(range(32))
        material_b = bytes(range(32, 64))
        vault = self.root / "bound.vault"
        api.seal_home(self.home, vault, PASSPHRASE, profile="scrypt", binding="device", binding_material=material_a)
        self.assertEqual(api.verify_file(vault, PASSPHRASE, binding_material=material_a)["ok"], True)
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(vault, PASSPHRASE, binding_material=material_b)
        self.assertEqual(ctx.exception.code, "E_UNLOCK")

    # N10 明文包默认拒绝
    def test_plaintext_import_requires_flag(self):
        bundle = self.root / "legacy.syspersona"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("persona/kernel.json", "persona/traits.json", "memory.db"):
                archive.writestr(name, (self.home / name).read_bytes())
            archive.writestr("manifest.json", json.dumps({
                "schema": "syspersona/bundle@1",
                "persona_id": "9f2b1c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
                "persona_version": "1.4.0",
                "db_sha256": hashlib.sha256((self.home / "memory.db").read_bytes()).hexdigest(),
                "exported_at": "2026-11-04T00:00:00+08:00",
                "files": [],
            }))
        target = self.root / "from_bundle"
        with self.assertRaises(VaultError) as ctx:
            api.import_plaintext(bundle, target, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_POLICY")
        result = api.import_plaintext(bundle, target, PASSPHRASE, allow_plaintext=True, profile="scrypt")
        self.assertEqual(result["ok"], True)
        restored = api.unlock_home(self.root / "from_bundle.vault", self.root / "bundle_out", PASSPHRASE)
        self.assertEqual(restored["db_sha256"], hashlib.sha256((self.home / "memory.db").read_bytes()).hexdigest())

    # N12 助记词
    def test_bad_mnemonic(self):
        words = self.info["mnemonic"].split()
        broken = " ".join(["zoo"] + words[1:])
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(self.vault, mnemonic=broken)
        self.assertEqual(ctx.exception.code, "E_MNEMONIC")

    # N13 轮换
    def test_rotate_passphrase(self):
        before = self.vault.read_bytes()
        result = api.rotate_passphrase(self.vault, PASSPHRASE, "a much better passphrase")
        self.assertTrue(result["payload_unchanged"])
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(self.vault, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_UNLOCK")
        self.assertEqual(api.verify_file(self.vault, "a much better passphrase")["ok"], True)
        self.assertLess(abs(len(self.vault.read_bytes()) - len(before)), 64)

    # N14 容器里没有明文
    def test_no_plaintext_leak(self):
        raw = self.vault.read_bytes()
        for marker in (b"NAS", "阿岚".encode(), PASSPHRASE.encode(), b"episodes"):
            self.assertNotIn(marker, raw)

    # 助记词恢复（删掉口令 wrap 后仍可用）
    def test_recovery_without_passphrase_wrap(self):
        opened = container.open_container(self.vault, PASSPHRASE)
        header = opened.header
        provider = get_provider()
        salt = container._b64d(header["content"]["salt"])
        header["wraps"] = [wrap for wrap in header["wraps"] if wrap["id"] != "passphrase"]
        header["mac"] = container._b64e(container.header_mac(header, opened.ks, salt, provider))
        raw_header = canonical(header)
        payload = container.parse_file(self.vault)[2]
        blob = (container.MAGIC + (1).to_bytes(2, "big") + len(raw_header).to_bytes(2, "big")
                + raw_header + len(payload).to_bytes(4, "big") + payload)
        path = self.root / "recovery_only.vault"
        path.write_bytes(blob)
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(path, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_UNLOCK")
        self.assertEqual(api.verify_file(path, mnemonic=self.info["mnemonic"])["ok"], True)

    # 头 MAC 覆盖元数据：改 created_at 必须失败
    def test_header_mac_covers_metadata(self):
        path = self._copy("t15.vault", lambda raw: raw.replace(b'"created_at":"', b'"created_at":"x'))
        with self.assertRaises(VaultError) as ctx:
            api.inspect_file(path)
        # 长度变了，先按格式失败也可接受；重新构造同长度篡改
        self.assertIn(ctx.exception.code, ("E_FORMAT", "E_INTEGRITY"))
        _, header, _ = container.parse_file(self.vault)
        old = header["created_at"]
        new = old[:-1] + ("0" if old[-1] != "0" else "1")
        mutated = self.vault.read_bytes().replace(old.encode(), new.encode(), 1)
        path2 = self.root / "t16.vault"
        path2.write_bytes(mutated)
        with self.assertRaises(VaultError) as ctx:
            api.verify_file(path2, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_INTEGRITY")

    # 快照随源封装，--purge 一起清除
    def test_snapshots_and_purge(self):
        snapshots = self.home / "snapshots"
        snapshots.mkdir()
        (snapshots / "pre_consolidate_1.json").write_text('{"episodes": []}', encoding="utf-8")
        vault = self.root / "purged.vault"
        info = api.seal_home(self.home, vault, PASSPHRASE, profile="scrypt", purge=True)
        self.assertGreater(len(info["purged"]), 0)
        self.assertFalse((self.home / "memory.db").exists())
        self.assertFalse(snapshots.exists())
        out = self.root / "restored_after_purge"
        api.unlock_home(vault, out, PASSPHRASE)
        self.assertTrue((out / "snapshots" / "pre_consolidate_1.json").exists())

    # 解封到非空目录必须 --force，且保留备份
    def test_refuse_nonempty_and_keep_backup(self):
        target = self.root / "existing"
        target.mkdir()
        (target / "note.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaises(VaultError) as ctx:
            api.unlock_home(self.vault, target, PASSPHRASE)
        self.assertEqual(ctx.exception.code, "E_POLICY")
        result = api.unlock_home(self.vault, target, PASSPHRASE, force=True)
        self.assertTrue(result["backup"] and Path(result["backup"]).exists())
        self.assertEqual((Path(result["backup"]) / "note.txt").read_text(encoding="utf-8"), "keep me")

    # 多次封装 nonce 不重复
    def test_nonce_unique(self):
        nonces = []
        for index in range(4):
            info = api.seal_home(self.home, self.root / f"n{index}.vault", PASSPHRASE, profile="scrypt")
            header = container.read_header(self.root / f"n{index}.vault")
            nonces.extend([wrap["nonce"] for wrap in header["wraps"]] + [header["content"]["nonce"]])
            self.assertEqual(info["ok"], True)
        self.assertEqual(len(nonces), len(set(nonces)))


if __name__ == "__main__":
    unittest.main()
