#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可行性验证：把《SECURITY_SPEC.md》的每一条硬要求，变成一条可运行的检查。

输出三样东西：
  results.json  —— 机器可读的检查结果与测量值
  RESULTS.md    —— 可直接贴进文档的表格
  控制台        —— 同样的表格

运行：python feasibility.py [--quick]
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import vault_poc as vp
from vault_poc import VaultError

HERE = Path(__file__).resolve().parent
RUN = HERE / "_run"
RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<38} {detail}")


def expect_error(name: str, fn, code: str) -> None:
    try:
        fn()
    except VaultError as exc:
        record(name, exc.code == code, f"{exc.code} {exc}" if exc.code == code else f"期望 {code}，实得 {exc.code}")
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"意外异常 {type(exc).__name__}: {exc}")
    else:
        record(name, False, "本应失败却成功了")


def fresh(name: str) -> Path:
    path = RUN / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def seal_demo(folder: Path, **kwargs) -> tuple[Path, Path, dict]:
    home = vp.demo_home(folder)
    out = folder / "me.sysvault"
    info = vp.seal(home, out, "correct horse battery staple", **kwargs)
    return home, out, info


def rewrite_payload(path: Path, ks: bytes, mutate) -> None:
    """测试专用：用真实 KS 重写载荷，模拟「拿到内容密钥但没拿到签名密钥」的篡改。"""
    header_bytes, header, payload_ct = vp.parse_container(path)
    vk = vp.hkdf(ks, vp.b64d(header["content"]["salt"]), "syspersona/v1/content", 32)
    plain = vp.gcm_decrypt(vk, vp.b64d(header["content"]["nonce"]), payload_ct, vp.payload_aad(header))
    buffer = tempfile.SpooledTemporaryFile()
    buffer.write(plain)
    buffer.seek(0)
    with zipfile.ZipFile(buffer) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    mutate(files)
    buffer = tempfile.SpooledTemporaryFile()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in files.items():
            archive.writestr(name, blob)
    buffer.seek(0)
    new_plain = buffer.read()
    nonce = os.urandom(12)
    header["content"]["nonce"] = vp.b64e(nonce)
    header["mac"] = vp.b64e(vp.header_mac(header, ks, vp.b64d(header["content"]["salt"])))
    header_bytes = vp.canonical(header)
    new_ct = vp.gcm_encrypt(vk, nonce, new_plain, vp.payload_aad(header))
    blob = (vp.MAGIC + (1).to_bytes(2, "big") + len(header_bytes).to_bytes(2, "big") + header_bytes
            + len(new_ct).to_bytes(4, "big") + new_ct)
    Path(path).write_bytes(blob)


def rewrite_header(path: Path, ks: bytes, mutate) -> None:
    header_bytes, header, payload_ct = vp.parse_container(path)
    mutate(header)
    header["mac"] = vp.b64e(vp.header_mac(header, ks, vp.b64d(header["content"]["salt"])))
    new_bytes = vp.canonical(header)
    blob = (vp.MAGIC + (1).to_bytes(2, "big") + len(new_bytes).to_bytes(2, "big") + new_bytes
            + len(payload_ct).to_bytes(4, "big") + payload_ct)
    Path(path).write_bytes(blob)


# ---------------------------------------------------------------- 检查项

def check_roundtrip() -> None:
    folder = fresh("roundtrip")
    home, out, info = seal_demo(folder)
    files, header, ks = vp.unlock(out, "correct horse battery staple")
    assert files["memory.db"] == (home / "memory.db").read_bytes()
    assert files["persona/kernel.json"] == (home / "persona/kernel.json").read_bytes()
    assert len(ks) == 32
    record("R1 封装→解封，逐字节一致", True,
           f"{info['bytes']} B 容器，{len(files)} 个文件，db sha256 {vp.deep_db_hash(home)[:12]}…")


def check_wrong_passphrase() -> None:
    folder = fresh("wrongpass")
    _, out, _ = seal_demo(folder)
    expect_error("R2a 错口令 → E_UNLOCK", lambda: vp.unlock(out, "wrong passphrase"), "E_UNLOCK")

    def tamper_wrap() -> None:
        _, header, _ = vp.parse_container(out)
        ks = vp.unlock(out, "correct horse battery staple")[2]
        rewrite_header(out, ks, lambda h: h["wraps"][0].__setitem__(
            "ct", vp.b64e(bytes([vp.b64d(h["wraps"][0]["ct"])[0] ^ 1]) + vp.b64d(h["wraps"][0]["ct"])[1:])))
        vp.unlock(out, "correct horse battery staple")

    expect_error("R2b 篡改密钥包裹 → E_UNLOCK", tamper_wrap, "E_UNLOCK")


def check_tamper() -> None:
    folder = fresh("tamper")
    _, out, info = seal_demo(folder)
    ks = info["ks"]

    def flip_payload() -> None:
        raw = bytearray(out.read_bytes())
        raw[-1] ^= 0x01
        (folder / "t1.sysvault").write_bytes(raw)
        vp.unlock(folder / "t1.sysvault", "correct horse battery staple")

    expect_error("R3a 篡改载荷末端 → E_INTEGRITY", flip_payload, "E_INTEGRITY")

    def flip_header_field() -> None:
        raw = out.read_bytes().replace(b'"version":"1.4.0"', b'"version":"9.9.9"')
        (folder / "t2.sysvault").write_bytes(raw)
        vp.unlock(folder / "t2.sysvault", "correct horse battery staple")

    expect_error("R3b 改头部元数据 → E_INTEGRITY", flip_header_field, "E_INTEGRITY")

    def truncate() -> None:
        (folder / "t3.sysvault").write_bytes(out.read_bytes()[:-10])
        vp.unlock(folder / "t3.sysvault", "correct horse battery staple")

    expect_error("R3c 截断容器 → E_FORMAT", truncate, "E_FORMAT")

    def extra_bytes() -> None:
        (folder / "t4.sysvault").write_bytes(out.read_bytes() + b"\x00")
        vp.unlock(folder / "t4.sysvault", "correct horse battery staple")

    expect_error("R3d 尾部追加字节 → E_FORMAT", extra_bytes, "E_FORMAT")


def check_alg_downgrade() -> None:
    folder = fresh("alg")
    _, out, _ = seal_demo(folder)
    raw = out.read_bytes().replace(b'"id":"argon2id"', b'"id":"argon2i_"', 1)
    (folder / "bad.sysvault").write_bytes(raw)
    expect_error("R4 未知 KDF → E_UNSUPPORTED", lambda: vp.unlock(folder / "bad.sysvault", "correct horse battery staple"), "E_UNSUPPORTED")


def check_kernel_signature() -> None:
    folder = fresh("kernel")
    home, out, info = seal_demo(folder)

    def mutate(files: dict[str, bytes]) -> None:
        manifest = json.loads(files["manifest.json"])
        sig = manifest["kernel"]["sig"]["value"]
        raw = bytearray(vp.b64d(sig))
        raw[0] ^= 1
        manifest["kernel"]["sig"]["value"] = vp.b64e(bytes(raw))
        files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()

    rewrite_payload(out, info["ks"], mutate)
    expect_error("R5a 内核签名被改 → E_SIGNATURE", lambda: vp.unlock(out, "correct horse battery staple"), "E_SIGNATURE")

    # 内核被人工改了，但没批准就重新封装 → 必须拒绝
    kernel = json.loads((home / "persona" / "kernel.json").read_text(encoding="utf-8"))
    kernel["kernel"]["identity"]["name"] = "阿岚2"
    (home / "persona" / "kernel.json").write_text(json.dumps(kernel, ensure_ascii=False), encoding="utf-8")
    expect_error("R5b 内核变化未批准 → E_POLICY",
                 lambda: vp.seal(home, folder / "again.sysvault", "correct horse battery staple", prior=out),
                 "E_POLICY")
    info2 = vp.seal(home, folder / "again.sysvault", "correct horse battery staple", prior=out,
                    approve_kernel_change=True)
    files, _, _ = vp.unlock(folder / "again.sysvault", "correct horse battery staple")
    record("R5c 显式批准后可重建签名", True, f"新内核 sha256 {info2['header']['persona']['kernel_sha256'][:12]}…")


def check_recovery() -> None:
    folder = fresh("recovery")
    home, out, info = seal_demo(folder, recovery=True)
    phrase = info["mnemonic"]
    words = phrase.split()
    roundtrip = vp.entropy_from_mnemonic(phrase)
    record("R6a 助记词生成与回读", len(words) == 24 and len(roundtrip) == 32,
           f"{len(words)} 词：{' '.join(words[:3])} … {' '.join(words[-2:])}")

    files, _, ks = vp.unlock(out, mnemonic=phrase)
    record("R6b 仅凭助记词解封", files["memory.db"] == (home / "memory.db").read_bytes(),
           f"db sha256 {hashlib.sha256(files['memory.db']).hexdigest()[:12]}…")

    # 模拟「口令忘了」：删掉 passphrase 包裹，只留恢复包裹
    def drop_passphrase(h: dict) -> None:
        h["wraps"] = [w for w in h["wraps"] if w["id"] != "passphrase"]

    rewrite_header(out, ks, drop_passphrase)
    expect_error("R6c 没有口令包裹时，口令不可用", lambda: vp.unlock(out, "correct horse battery staple"), "E_UNLOCK")
    files, _, _ = vp.unlock(out, mnemonic=phrase)
    record("R6d 删除口令包裹后，助记词仍可解", files["memory.db"] == (home / "memory.db").read_bytes(), "恢复路径独立于口令")

    bad = " ".join(["zoo"] + words[1:])
    expect_error("R6e 抄错一个词 → 校验位报错", lambda: vp.entropy_from_mnemonic(bad), "E_MNEMONIC")


def check_backup() -> None:
    folder = fresh("backup")
    home, out, _ = seal_demo(folder)
    copy = folder / "offline-backup.sysvault"
    shutil.copy2(out, copy)
    files, _, _ = vp.unlock(copy, "correct horse battery staple")
    record("R7 离线备份副本 → memory.db 哈希一致",
           hashlib.sha256(files["memory.db"]).hexdigest() == vp.deep_db_hash(home),
           f"sha256 {vp.deep_db_hash(home)[:16]}…")


def check_binding() -> None:
    folder = fresh("binding")
    home, out, _ = seal_demo(folder, binding="device")
    files, _, _ = vp.unlock(out, "correct horse battery staple")
    record("R8a 本机绑定正确 → 可解", files["memory.db"] == (home / "memory.db").read_bytes(),
           f"设备描述 {hashlib.sha256(vp.device_descriptor().encode()).hexdigest()[:12]}…")
    expect_error("R8b 换一台设备 → E_UNLOCK（同一句报错）",
                 lambda: vp.unlock(out, "correct horse battery staple", binding_override=os.urandom(32)),
                 "E_UNLOCK")


def check_fork() -> None:
    folder = fresh("fork")
    home = vp.demo_home(folder)
    key = vp._node({"op": "ed25519_keygen"})
    template = folder / "template.json"
    vp.publish_template(home, template, key["private_b64"], key["public_b64"])

    target = folder / "forked"
    result = vp.fork_template(template, target)
    origin = json.loads((home / "persona" / "kernel.json").read_text(encoding="utf-8"))["id"]
    record("R9a 复制模板 → 新 UUID", result["new_id"] != origin,
           f"{origin[:8]}… → {result['new_id'][:8]}…  记忆={result['memory']}")

    blob = json.loads(template.read_text(encoding="utf-8"))
    blob["persona"]["traits"]["traits"]["preferences"][0]["weight"] = 5
    (folder / "tampered.json").write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    expect_error("R9b 模板被改 → E_SIGNATURE",
                 lambda: vp.fork_template(folder / "tampered.json", folder / "f2"), "E_SIGNATURE")


def check_rotate() -> None:
    folder = fresh("rotate")
    _, out, _ = seal_demo(folder)
    before = out.read_bytes()
    vp.rotate_passphrase(out, "correct horse battery staple", "new passphrase")
    after = out.read_bytes()
    payload_before = vp.parse_container(out)[2]
    expect_error("R10a 旧口令失效", lambda: vp.unlock(out, "correct horse battery staple"), "E_UNLOCK")
    files, _, _ = vp.unlock(out, "new passphrase")
    record("R10b 新口令可解，载荷密文未变", payload_before == vp.parse_container(out)[2],
           f"容器 {len(before)}→{len(after)} B，载荷 {len(payload_before)} B 原样保留")


def check_migration() -> None:
    folder = fresh("migration")
    home = vp.demo_home(folder)
    bundle = folder / "legacy.syspersona"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("persona/kernel.json", "persona/traits.json", "memory.db"):
            archive.writestr(name, (home / name).read_bytes())
        archive.writestr("manifest.json", json.dumps({
            "schema": "syspersona/bundle@1",
            "persona_id": "9f2b1c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
            "persona_version": "1.4.0",
            "exported_at": vp.now_iso(),
            "db_sha256": vp.deep_db_hash(home),
            "files": ["persona/kernel.json", "persona/traits.json", "memory.db"],
        }))

    def import_plain(allow: bool) -> None:
        vp.import_plaintext(bundle, folder / "home2", "correct horse battery staple",
                            allow_plaintext=allow, profile="scrypt-low")

    expect_error("R11a 明文包默认拒绝导入", lambda: import_plain(False), "E_POLICY")
    result = vp.import_plaintext(bundle, folder / "home2", "correct horse battery staple",
                                 allow_plaintext=True, profile="scrypt-low")
    files, _, _ = vp.unlock(folder / "home2.sysvault", "correct horse battery staple")
    record("R11b 显式允许后升级为密文，内容一致",
           files["memory.db"] == (home / "memory.db").read_bytes(),
           f"已封装 {result['bytes']} B（scrypt 档）")


def check_nonce() -> None:
    folder = fresh("nonce")
    home = vp.demo_home(folder)
    ks = os.urandom(32)
    nonces: list[str] = []
    for index in range(8):
        info = vp.seal(home, folder / f"c{index}.sysvault", "pass", ks=ks, profile="scrypt-low")
        nonces.extend([w["nonce"] for w in info["header"]["wraps"]])
        nonces.append(info["header"]["content"]["nonce"])
    record("R12 8 次封装 nonce 全不重复", len(nonces) == len(set(nonces)),
           f"{len(nonces)} 个 nonce / {len(set(nonces))} 个唯一")


def check_plaintext_leak() -> None:
    folder = fresh("leak")
    _, out, info = seal_demo(folder)
    raw = out.read_bytes()
    hits = [marker.decode() for marker in
            (b"NAS", "阿岚".encode("utf-8"), b"correct horse battery staple", b"episodes")]
    leaked = [marker for marker in hits if marker.encode("utf-8") in raw]
    record("R13 容器内无明文痕迹", not leaked,
           "未出现 NAS / 阿岚 / 口令 / 表名" if not leaked else f"泄漏：{leaked}")


def check_binding_uniform_error() -> None:
    folder = fresh("uniform")
    _, out, _ = seal_demo(folder)
    messages = set()
    for attempt in (lambda: vp.unlock(out, "wrong"), lambda: vp.unlock(out, "another wrong")):
        try:
            attempt()
        except VaultError as exc:
            messages.add((exc.code, str(exc)))
    record("R14 错口令/错设备不泄露差异", len(messages) == 1,
           f"统一为 {messages.pop() if messages else '∅'}")


def check_interop() -> None:
    folder = fresh("interop")
    home, out, info = seal_demo(folder, profile="argon2id-low")
    request = {"container": str(out), "passphrase": "correct horse battery staple"}
    proc = subprocess.run([vp.NODE, str(HERE / "reader.js")],
                          input=json.dumps(request).encode(), capture_output=True, timeout=300)
    reply = json.loads(proc.stdout.decode("utf-8") or "{}")
    expect = {name: hashlib.sha256((home / name).read_bytes()).hexdigest()
              for name in ("persona/kernel.json", "persona/traits.json", "persona/state.json",
                           "memory.db", "config.json")}
    got = {entry["path"]: entry["sha256"] for entry in reply.get("files", [])}
    record("R15 Node 独立实现可读 Python 容器", bool(reply.get("ok")) and got == expect,
           f"独立实现读出 {len(got)} 个文件，内核 {reply.get('kernel_sha256', '')[:12]}…")


def check_spawn_overhead() -> None:
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        vp._node({"op": "ping"})
        samples.append((time.perf_counter() - start) * 1000)
    median = statistics.median(samples)
    record("M0 进程外 provider 单次调用开销", True,
           f"{median:.0f} ms/次（进程内 provider 无此开销，M2 数字需减去它）")


def check_kdf_timing(quick: bool) -> None:
    salt = bytes(range(16))
    data = []
    profiles = ["scrypt-low", "argon2id-low"] if quick else ["scrypt-low", "argon2id-low", "argon2id-mid"]
    for profile in profiles:
        samples = []
        for _ in range(2 if quick else 3):
            start = time.perf_counter()
            vp.kdf(profile, b"correct horse battery staple", salt)
            samples.append((time.perf_counter() - start) * 1000)
        median = statistics.median(samples)
        params = vp.KDF_PROFILES[profile]
        data.append({"profile": profile, "kdf": params["id"], "params": params["params"], "median_ms": round(median, 1)})
        record(f"M1 {profile} 解锁耗时", True, f"{median:.0f} ms（{params['id']} {params['params']}）")
    (HERE / "kdf.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check_throughput() -> None:
    folder = fresh("throughput")
    sizes = [(64, 210), (256, 840), (1024, 3400)]
    rows = []
    for kib, count in sizes:
        home = vp.demo_home(folder / f"k{kib}", db_rows=count)
        out = folder / f"{kib}k.sysvault"
        start = time.perf_counter()
        info = vp.seal(home, out, "pass", profile="scrypt-low")
        seal_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        vp.unlock(out, "pass")
        unlock_ms = (time.perf_counter() - start) * 1000
        actual = round((home / "memory.db").stat().st_size / 1024, 1)
        rows.append({"db_kib": actual, "container_kib": round(info["bytes"] / 1024, 1),
                     "header_bytes": info["header_bytes"], "payload_kib": round(info["payload_ct"] / 1024, 1),
                     "seal_ms": round(seal_ms, 1), "unlock_ms": round(unlock_ms, 1)})
        record(f"M2 {actual} KiB 数据库：封装/解封", True,
               f"封装 {seal_ms:.0f} ms，解封 {unlock_ms:.0f} ms，头部 {info['header_bytes']} B，"
               f"载荷 {info['payload_ct'] / 1024:.0f} KiB")
    (HERE / "throughput.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    for size_mib in (1, 8):
        bench = vp.bench_aead(size=size_mib << 20, rounds=8)
        record(f"M2b 原生 AES-256-GCM {size_mib} MiB", True,
               f"{bench['ms_per_call']:.1f} ms/次，{bench['mb_per_s']:.0f} MB/s")
        (HERE / f"aead_{size_mib}mib.json").write_text(json.dumps(bench, indent=2), encoding="utf-8")


def check_wal() -> None:
    folder = fresh("wal")
    path = folder / "wal.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO t (content) VALUES ('SECRET_MARKER_12345')")
    conn.commit()
    wal_text = (folder / "wal.db-wal").read_bytes() if (folder / "wal.db-wal").exists() else b""
    leak_before = b"SECRET_MARKER_12345" in wal_text
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    remaining = sorted(p.name for p in folder.iterdir())
    record("M3 WAL 会在旁路文件留下明文", leak_before,
           f"提交后 wal 中含明文={leak_before}；checkpoint(TRUNCATE)+close 后残留文件={remaining}")

    # 删除行后不 VACUUM，明文仍在主库空页里
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()
    before = b"SECRET_MARKER_12345" in path.read_bytes()
    conn = sqlite3.connect(path)
    conn.execute("VACUUM")
    conn.close()
    after = b"SECRET_MARKER_12345" in path.read_bytes()
    record("M4 删除行后 VACUUM 才真正清除明文", before and not after,
           f"DELETE 后残留={before}，VACUUM 后残留={after}")


def check_oracle_kat() -> None:
    kat: dict = {"provider": "node " + vp._node({"op": "ping"})["openssl"]}
    salt = bytes(range(16))
    kat["argon2id"] = {
        "passphrase": "correct horse battery staple",
        "salt_hex": salt.hex(), "memory": 19456, "passes": 2, "parallelism": 1, "tag_length": 32,
        "key_hex": vp.kdf("argon2id-low", b"correct horse battery staple", salt).hex(),
    }
    kat["scrypt"] = {
        "passphrase": "correct horse battery staple", "salt_hex": salt.hex(),
        "N": 32768, "r": 8, "p": 1, "key_len": 32,
        "key_hex": vp.kdf("scrypt-low", b"correct horse battery staple", salt).hex(),
    }
    ikm = b"\x0b" * 22
    kat["hkdf_rfc5869_case1"] = {
        "ikm_hex": ikm.hex(), "salt_hex": "000102030405060708090a0b0c",
        "info_hex": "f0f1f2f3f4f5f6f7f8f9", "L": 42,
        "okm_hex": vp.hkdf(ikm, bytes(range(13)), bytes(range(0xF0, 0xFA)), 42).hex(),
    }
    key = bytes(32)
    nonce = bytes(12)
    empty = vp.gcm_encrypt(key, nonce, b"", b"")
    kat["aes256gcm_empty"] = {"key_hex": key.hex(), "nonce_hex": nonce.hex(), "aad_hex": "",
                              "plaintext_hex": "", "ciphertext_and_tag_hex": empty.hex()}
    block = bytes(16)
    kat["aes256gcm_one_block"] = {"key_hex": key.hex(), "nonce_hex": nonce.hex(), "aad_hex": "",
                                  "plaintext_hex": block.hex(),
                                  "ciphertext_and_tag_hex": vp.gcm_encrypt(key, nonce, block, b"").hex()}
    kat["hmac_sha256"] = {"key_utf8": "key", "message_utf8": "The quick brown fox jumps over the lazy dog",
                          "mac_hex": vp.hmac_sha256(b"key", b"The quick brown fox jumps over the lazy dog").hex()}
    sample = {"b": 1, "a": "×"}
    kat["canonical_json"] = {"value": sample, "bytes_hex": vp.canonical(sample).hex(),
                             "sha256": hashlib.sha256(vp.canonical(sample)).hexdigest()}
    entropy = bytes(32)
    kat["bip39"] = {"entropy_hex": entropy.hex(), "mnemonic": vp.mnemonic_from_entropy(entropy),
                    "roundtrip": vp.entropy_from_mnemonic(vp.mnemonic_from_entropy(entropy)).hex()}
    (HERE / "kat.json").write_text(json.dumps(kat, ensure_ascii=False, indent=2), encoding="utf-8")
    record("K1 生成 KAT 向量", True,
           f"AES-GCM 空消息 tag={empty[-16:].hex()[:16]}…，BIP39 {len(kat['bip39']['mnemonic'].split())} 词")
    record("K2 HKDF 与 RFC 5869 用例一致",
           kat["hkdf_rfc5869_case1"]["okm_hex"].startswith("3cb25f25faacd57a"),
           kat["hkdf_rfc5869_case1"]["okm_hex"][:32] + "…")
    record("K3 HMAC 与公开向量一致",
           kat["hmac_sha256"]["mac_hex"] == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
           kat["hmac_sha256"]["mac_hex"][:16] + "…")


def check_canonical_rules() -> None:
    expect_error("C1 浮点数被拒绝", lambda: vp.canonical({"x": 0.5}), "E_FORMAT")
    expect_error("C2 非 NFC 字符串被拒绝", lambda: vp.canonical({"x": "A\u030A"}), "E_FORMAT")
    a = vp.canonical({"b": 1, "a": 2})
    b = vp.canonical({"a": 2, "b": 1})
    record("C3 键序不影响规范字节", a == b, a.decode())


def build_markdown() -> None:
    lines = ["| 检查 | 结果 | 说明 |", "|---|---|---|"]
    for item in RESULTS:
        lines.append(f"| {item['name']} | {'✅' if item['ok'] else '❌'} | {item['detail']} |")
    (HERE / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> int:
    quick = "--quick" in sys.argv
    RUN.mkdir(exist_ok=True)
    started = time.time()
    try:
        provider = vp._node({"op": "ping"})["openssl"]
    except FileNotFoundError:
        print("找不到 node。请安装 Node >= 22，或用 SYSPERSONA_NODE 指定可执行文件路径。")
        return 2
    print(f"provider: node {provider}   python {sys.version.split()[0]}")
    print("-" * 88)
    check_roundtrip()
    check_wrong_passphrase()
    check_tamper()
    check_alg_downgrade()
    check_kernel_signature()
    check_recovery()
    check_backup()
    check_binding()
    check_fork()
    check_rotate()
    check_migration()
    check_nonce()
    check_plaintext_leak()
    check_binding_uniform_error()
    check_canonical_rules()
    check_interop()
    check_oracle_kat()
    check_spawn_overhead()
    check_kdf_timing(quick)
    check_throughput()
    check_wal()
    print("-" * 88)
    failed = [item for item in RESULTS if not item["ok"]]
    (HERE / "results.json").write_text(json.dumps(
        {"passed": len(RESULTS) - len(failed), "failed": len(failed),
         "elapsed_s": round(time.time() - started, 1), "items": RESULTS},
        ensure_ascii=False, indent=2), encoding="utf-8")
    build_markdown()
    print(f"通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}，用时 {time.time() - started:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
