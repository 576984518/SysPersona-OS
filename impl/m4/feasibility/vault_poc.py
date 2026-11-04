#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SysPersona 加密容器 —— 可行性验证原型（不是产品代码）。

它只回答《SECURITY_SPEC.md》里那些「能不能做到」的问题：

  1. 定义出来的字节格式，能否被一个独立实现（OpenSSL / Node）正确解开？
  2. 错口令、篡改任意字节、降级、换设备，是否都能在写盘之前失败？
  3. 助记词恢复、离线备份、分工（fork）语义是否自洽？
  4. KDF 与 AEAD 的时间/空间代价，是否落在可接受范围？

密码学原语全部委托给 Node 24 的 OpenSSL（oracle.js），Python 只负责
「格式 + 状态机 + 失败语义」。两边互为对照：格式错，Oracle 不会配合；
Oracle 被换掉，格式测试仍能发现。

运行：python feasibility.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODE = os.environ.get("SYSPERSONA_NODE") or shutil.which("node") or "node"
ORACLE = HERE / "oracle.js"
WORDS = (HERE / "bip39_english.txt").read_text(encoding="utf-8").split()

MAGIC = b"SPVAULT1"
FORMAT = "syspersona-vault/1"
AAD_PREFIX = MAGIC + (1).to_bytes(2, "big")
HEADER_MAX = 64 * 1024
PAYLOAD_MAX = 1 << 30

KDF_PROFILES = {
    "argon2id-low": {"id": "argon2id", "params": {"memory": 19456, "passes": 2, "parallelism": 1}},
    "argon2id-mid": {"id": "argon2id", "params": {"memory": 65536, "passes": 3, "parallelism": 1}},
    "scrypt-low": {"id": "scrypt", "params": {"N": 32768, "r": 8, "p": 1}},
}


class VaultError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 原语

def _node(req: dict) -> dict:
    proc = subprocess.run(
        [NODE, str(ORACLE)],
        input=json.dumps(req).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=600,
    )
    if proc.returncode != 0:
        raise VaultError("E_CRYPTO", proc.stderr.decode("utf-8", "replace")[-400:])
    out = json.loads(proc.stdout.decode("utf-8"))
    if out.get("error"):
        raise VaultError(out["error"], out.get("detail", out["error"]))
    return out


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def kdf(profile_id: str, pass_material: bytes, salt: bytes) -> bytes:
    profile = KDF_PROFILES[profile_id]
    if profile["id"] == "argon2id":
        reply = _node({"op": "argon2id", "pass_b64": b64e(pass_material), "salt_b64": b64e(salt),
                       "tag_length": 32, **profile["params"]})
    elif profile["id"] == "scrypt":
        reply = _node({"op": "scrypt", "pass_b64": b64e(pass_material), "salt_b64": b64e(salt),
                       "key_len": 32, **profile["params"]})
    else:
        raise VaultError("E_UNSUPPORTED", f"未知 KDF：{profile['id']}")
    return b64d(reply["key_b64"])


def hkdf(ikm: bytes, salt: bytes, info, length: int = 32) -> bytes:
    info_bytes = info.encode("utf-8") if isinstance(info, str) else info
    reply = _node({"op": "hkdf", "ikm_b64": b64e(ikm), "salt_b64": b64e(salt),
                   "info_b64": b64e(info_bytes), "length": length})
    return b64d(reply["key_b64"])


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    reply = _node({"op": "hmac", "key_b64": b64e(key), "message_b64": b64e(message)})
    return b64d(reply["mac_b64"])


def gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    reply = _node({"op": "gcm_encrypt", "key_b64": b64e(key), "nonce_b64": b64e(nonce),
                   "aad_b64": b64e(aad), "plaintext_b64": b64e(plaintext)})
    return b64d(reply["ct_b64"])


def gcm_decrypt(key: bytes, nonce: bytes, blob: bytes, aad: bytes) -> bytes:
    out = _node({"op": "gcm_decrypt", "key_b64": b64e(key), "nonce_b64": b64e(nonce),
                 "aad_b64": b64e(aad), "ct_b64": b64e(blob)})
    if out.get("error"):
        raise VaultError("E_INTEGRITY", "密文完整性校验失败")
    return b64d(out["plaintext_b64"])


def hmac_compare(left: bytes, right: bytes) -> bool:
    if len(left) != len(right):
        return False
    diff = 0
    for a, b in zip(left, right):
        diff |= a ^ b
    return diff == 0


# ---------------------------------------------------------------- 编码

def canonical(obj) -> bytes:
    """规范 §5.1：键排序、无空白、UTF-8、NFC、禁止浮点。"""
    def check(value):
        if isinstance(value, float):
            raise VaultError("E_FORMAT", "规范化 JSON 不允许浮点数")
        if isinstance(value, str):
            if unicodedata.normalize("NFC", value) != value:
                raise VaultError("E_FORMAT", "字符串必须是 NFC")
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise VaultError("E_FORMAT", "对象键必须是字符串")
                check(key)
                check(item)
        elif value is not None and not isinstance(value, (bool, int)):
            raise VaultError("E_FORMAT", f"不支持的类型：{type(value).__name__}")

    check(obj)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def kernel_subset(obj: dict) -> dict:
    return {key: obj[key] for key in ("schema", "id", "version", "kernel") if key in obj}


def wrap_aad(wrap_id: str) -> bytes:
    return AAD_PREFIX + b"|wrap|" + wrap_id.encode("ascii")


# ---------------------------------------------------------------- 助记词

def mnemonic_from_entropy(entropy: bytes) -> str:
    checksum_bits = len(entropy) // 4
    bits = int.from_bytes(entropy + hashlib.sha256(entropy).digest()[:1], "big")
    total = len(entropy) * 8 + checksum_bits
    return " ".join(WORDS[(bits >> (total - 11 * (i + 1))) & 0x7FF] for i in range(total // 11))


def entropy_from_mnemonic(phrase: str) -> bytes:
    words = phrase.split()
    if len(words) not in (12, 24):
        raise VaultError("E_MNEMONIC", "助记词长度必须是 12 或 24 个词")
    try:
        bits = 0
        for word in words:
            bits = (bits << 11) | WORDS.index(word)
    except ValueError:
        raise VaultError("E_MNEMONIC", "助记词包含词表外的单词")
    total = len(words) * 11
    ent_bits = total * 32 // 33
    entropy = (bits >> (total - ent_bits)).to_bytes(ent_bits // 8, "big")
    checksum = bits & ((1 << (total - ent_bits)) - 1)
    expect = hashlib.sha256(entropy).digest()[0] >> (8 - (total - ent_bits))
    if checksum != expect:
        raise VaultError("E_MNEMONIC", "助记词校验位不匹配（可能抄错了一个词）")
    return entropy


# ---------------------------------------------------------------- 设备绑定

def device_descriptor() -> str:
    parts = [platform.node()]
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            parts.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
    except Exception:
        parts.append(str(uuid.getnode()))
    return "|".join(parts)


def binding_factor(mode: str) -> bytes:
    if mode in (None, "none"):
        return b""
    if mode == "device":
        material = hashlib.sha256(device_descriptor().encode("utf-8")).digest()
        return hkdf(material, b"", "syspersona/v1/device", 32)
    raise VaultError("E_UNSUPPORTED", f"未知绑定模式：{mode}")


# ---------------------------------------------------------------- 家庭目录

def read_home(home: Path, include_state: bool) -> tuple[dict[str, bytes], dict, dict]:
    files: dict[str, bytes] = {}
    persona_dir = home / "persona"
    if persona_dir.is_dir():
        for name in ("kernel.json", "traits.json", "state.json"):
            path = persona_dir / name
            if path.exists() and (include_state or name != "state.json"):
                files[f"persona/{name}"] = path.read_bytes()
        kernel = json.loads((persona_dir / "kernel.json").read_text(encoding="utf-8-sig"))
    else:
        path = home / "persona.json"
        if not path.exists():
            raise VaultError("E_FORMAT", "找不到人格文件")
        files["persona.json"] = path.read_bytes()
        kernel = json.loads(path.read_text(encoding="utf-8-sig"))

    if (home / "memory.db").exists():
        files["memory.db"] = (home / "memory.db").read_bytes()
    if (home / "config.json").exists():
        files["config.json"] = (home / "config.json").read_bytes()

    subset = kernel_subset(kernel)
    return files, {"id": subset.get("id"), "version": subset.get("version"), "kernel": subset}, subset


def zip_payload(files: dict[str, bytes], manifest: dict, deterministic: bool) -> bytes:
    buffer = tempfile.SpooledTemporaryFile(max_size=8 << 20)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if deterministic:
            info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, blob in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, blob)
        else:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, blob in files.items():
                archive.writestr(name, blob)
    buffer.seek(0)
    return buffer.read()


def build_manifest(files: dict[str, bytes], persona: dict, kernel: dict, ks: bytes, content_salt: bytes, sealed_at: str) -> dict:
    body = canonical(kernel)
    kernel_key = hkdf(ks, content_salt, "syspersona/v1/kernel", 32)
    return {
        "schema": "syspersona/manifest@1",
        "persona_id": persona["id"],
        "persona_version": persona["version"],
        "sealed_at": sealed_at,
        "files": [
            {"path": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
            for name, blob in sorted(files.items())
        ],
        "kernel": {
            "sha256": hashlib.sha256(body).hexdigest(),
            "sig": {"alg": "hmac-sha256", "value": b64e(hmac_sha256(kernel_key, body))},
        },
    }


# ---------------------------------------------------------------- 封装 / 解封

def seal(
    home: Path,
    out: Path,
    passphrase: str,
    *,
    profile: str = "argon2id-low",
    binding: str = "none",
    recovery: bool = False,
    include_state: bool = True,
    ks: bytes | None = None,
    prior: Path | None = None,
    approve_kernel_change: bool = False,
    test: dict | None = None,
) -> dict:
    home, out = Path(home), Path(out)
    files, persona, kernel = read_home(home, include_state)
    rand = test.get("rng", os.urandom) if test else os.urandom
    stamp = test.get("now", now_iso()) if test else now_iso()

    if prior is not None and Path(prior).exists() and not approve_kernel_change:
        old = read_header(prior)
        if old.get("persona", {}).get("kernel_sha256") not in (None, hashlib.sha256(canonical(kernel)).hexdigest()):
            raise VaultError("E_POLICY", "内核自上次封装后已变化；需显式批准")

    ks = ks or rand(32)
    content_salt = rand(32)
    manifest = build_manifest(files, persona, kernel, ks, content_salt, stamp)
    payload_plain = zip_payload(files, manifest, deterministic=bool(test))
    vk = hkdf(ks, content_salt, "syspersona/v1/content", 32)
    payload_nonce = rand(12)
    container_id = test.get("container_id", str(uuid.uuid4())) if test else str(uuid.uuid4())

    header = {
        "format": FORMAT,
        "container_id": container_id,
        "created_at": stamp,
        "persona": {
            "id": persona["id"],
            "version": persona["version"],
            "kernel_sha256": hashlib.sha256(canonical(kernel)).hexdigest(),
        },
        "kdf": {
            "id": KDF_PROFILES[profile]["id"],
            "profile": profile,
            "salt": b64e(rand(16)),
            "params": KDF_PROFILES[profile]["params"],
        },
        "binding": {"mode": binding},
        "content": {"alg": "aes-256-gcm", "salt": b64e(content_salt), "nonce": b64e(payload_nonce)},
        "wraps": [],
    }

    factor = binding_factor(binding)
    material = passphrase.encode("utf-8") + (b"\x00" + factor if factor else b"")
    kek = kdf(profile, material, b64d(header["kdf"]["salt"]))
    header["wraps"].append(wrap_ks("passphrase", kek, ks, rand))

    mnemonic = None
    if recovery:
        rs = rand(32)
        mnemonic = mnemonic_from_entropy(rs)
        header["wraps"].append(wrap_ks("recovery", hkdf(rs, b"", "syspersona/v1/recovery", 32), ks, rand))
        header["recovery"] = {"scheme": "bip39", "words": 24, "wrap": "recovery"}

    header["mac"] = b64e(header_mac(header, ks, content_salt))
    header_bytes = canonical(header)

    payload_ct = gcm_encrypt(vk, payload_nonce, payload_plain, payload_aad(header))
    blob = (
        MAGIC
        + (1).to_bytes(2, "big")
        + len(header_bytes).to_bytes(2, "big")
        + header_bytes
        + len(payload_ct).to_bytes(4, "big")
        + payload_ct
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_bytes(blob)
    fd = os.open(tmp, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, out)
    return {
        "container": str(out),
        "bytes": len(blob),
        "header_bytes": len(header_bytes),
        "payload_plain": len(payload_plain),
        "payload_ct": len(payload_ct),
        "mnemonic": mnemonic,
        "ks": ks,
        "header": header,
        "manifest": manifest,
    }


def wrap_ks(wrap_id: str, key: bytes, ks: bytes, rand) -> dict:
    nonce = rand(12)
    return {"id": wrap_id, "alg": "aes-256-gcm", "nonce": b64e(nonce),
            "ct": b64e(gcm_encrypt(key, nonce, ks, wrap_aad(wrap_id)))}


def header_mac(header: dict, ks: bytes, content_salt: bytes) -> bytes:
    body = {key: value for key, value in header.items() if key != "mac"}
    return hmac_sha256(hkdf(ks, content_salt, "syspersona/v1/header", 32), canonical(body))


def payload_aad(header: dict) -> bytes:
    return (
        AAD_PREFIX
        + b"|payload|"
        + header["container_id"].encode("ascii")
        + b"|"
        + header["content"]["salt"].encode("ascii")
        + b"|"
        + header["persona"]["id"].encode("ascii")
    )


def parse_container(path: Path) -> tuple[bytes, dict, bytes]:
    raw = Path(path).read_bytes()
    if len(raw) < 20 or raw[:8] != MAGIC:
        raise VaultError("E_FORMAT", "不是 SysPersona 加密容器")
    version = int.from_bytes(raw[8:10], "big")
    if version != 1:
        raise VaultError("E_UNSUPPORTED", f"容器版本 {version} 不受支持")
    header_len = int.from_bytes(raw[10:12], "big")
    if header_len > HEADER_MAX or len(raw) < 16 + header_len + 4:
        raise VaultError("E_FORMAT", "容器头部长度非法")
    header_bytes = raw[12 : 12 + header_len]
    payload_len = int.from_bytes(raw[12 + header_len : 16 + header_len], "big")
    payload_ct = raw[16 + header_len :]
    if payload_len != len(payload_ct) or payload_len > PAYLOAD_MAX:
        raise VaultError("E_FORMAT", "容器被截断或尾部有多余字节")
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VaultError("E_FORMAT", "容器头部不是合法 JSON")
    if header.get("format") != FORMAT:
        raise VaultError("E_FORMAT", "容器格式标识不匹配")
    return header_bytes, header, payload_ct


def read_header(path: Path) -> dict:
    return parse_container(path)[1]


def _check_algs(header: dict) -> None:
    kdf_id = header.get("kdf", {}).get("id")
    if kdf_id not in ("argon2id", "scrypt"):
        raise VaultError("E_UNSUPPORTED", f"不支持的 KDF：{kdf_id}")
    for wrap in header.get("wraps", []):
        if wrap.get("alg") != "aes-256-gcm":
            raise VaultError("E_UNSUPPORTED", f"不支持的包裹算法：{wrap.get('alg')}")
    if header.get("content", {}).get("alg") != "aes-256-gcm":
        raise VaultError("E_UNSUPPORTED", "不支持的内容加密算法")


def unlock(path: Path, passphrase: str | None = None, *, mnemonic: str | None = None,
           binding_override: bytes | None = None) -> tuple[dict[str, bytes], dict, bytes]:
    header_bytes, header, payload_ct = parse_container(Path(path))
    _check_algs(header)

    if mnemonic:
        ks = _unwrap(header, "recovery", hkdf(entropy_from_mnemonic(mnemonic), b"", "syspersona/v1/recovery", 32))
    else:
        if passphrase is None:
            raise VaultError("E_POLICY", "需要口令或助记词")
        profile_id = _profile_from_header(header)
        factor = binding_factor(header.get("binding", {}).get("mode", "none")) if binding_override is None else binding_override
        material = passphrase.encode("utf-8") + (b"\x00" + factor if factor else b"")
        ks = _unwrap(header, "passphrase", kdf(profile_id, material, b64d(header["kdf"]["salt"])))

    content_salt = b64d(header["content"]["salt"])
    if not hmac_compare(header_mac(header, ks, content_salt), b64d(header["mac"])):
        raise VaultError("E_INTEGRITY", "头部与签名不一致")
    plain = gcm_decrypt(
        hkdf(ks, content_salt, "syspersona/v1/content", 32),
        b64d(header["content"]["nonce"]),
        payload_ct,
        payload_aad(header),
    )
    files, manifest = unzip_payload(plain)
    verify_manifest(files, manifest, ks, content_salt)
    return files, header, ks


def unzip_payload(plain: bytes) -> tuple[dict[str, bytes], dict]:
    buffer = tempfile.SpooledTemporaryFile(max_size=len(plain) + 1)
    buffer.write(plain)
    buffer.seek(0)
    try:
        with zipfile.ZipFile(buffer) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            files = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        raise VaultError("E_INTEGRITY", "载荷不是合法归档")
    return files, manifest


def verify_manifest(files: dict[str, bytes], manifest: dict, ks: bytes, content_salt: bytes) -> None:
    for entry in manifest.get("files", []):
        blob = files.get(entry["path"])
        if blob is None or hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            raise VaultError("E_INTEGRITY", f"文件哈希不一致：{entry['path']}")
    raw = files.get("persona/kernel.json") or files.get("persona.json")
    if raw is None:
        raise VaultError("E_INTEGRITY", "缺少人格内核")
    try:
        kernel = json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError:
        raise VaultError("E_INTEGRITY", "内核不是合法 JSON")
    body = canonical(kernel_subset(kernel))
    if hashlib.sha256(body).hexdigest() != manifest["kernel"]["sha256"]:
        raise VaultError("E_SIGNATURE", "内核哈希与签名清单不一致")
    kernel_key = hkdf(ks, content_salt, "syspersona/v1/kernel", 32)
    if not hmac_compare(hmac_sha256(kernel_key, body), b64d(manifest["kernel"]["sig"]["value"])):
        raise VaultError("E_SIGNATURE", "内核签名无效")


def _profile_from_header(header: dict) -> str:
    kdf_id = header["kdf"]["id"]
    params = header["kdf"].get("params", {})
    for name, profile in KDF_PROFILES.items():
        if profile["id"] == kdf_id and profile["params"] == params:
            return name
    raise VaultError("E_UNSUPPORTED", f"不支持的 KDF 参数组合：{kdf_id} {params}")


def _unwrap(header: dict, wrap_id: str, key: bytes) -> bytes:
    for wrap in header.get("wraps", []):
        if wrap.get("id") == wrap_id:
            try:
                return gcm_decrypt(key, b64d(wrap["nonce"]), b64d(wrap["ct"]), wrap_aad(wrap_id))
            except VaultError:
                raise VaultError("E_UNLOCK", "保险库无法打开")
    raise VaultError("E_UNLOCK", "保险库无法打开")


def extract(files: dict[str, bytes], home: Path) -> None:
    home = Path(home)
    staging = home.with_name(home.name + ".unseal.tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for name, blob in files.items():
            if name.startswith("/") or ".." in Path(name).parts:
                raise VaultError("E_FORMAT", f"非法路径：{name}")
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        if home.exists():
            shutil.rmtree(home)
        os.replace(staging, home)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def rotate_passphrase(path: Path, old: str, new: str) -> dict:
    header_bytes, header, payload_ct = parse_container(path)
    profile_id = _profile_from_header(header)
    factor = binding_factor(header.get("binding", {}).get("mode", "none"))
    ks = _unwrap(header, "passphrase", kdf(profile_id, old.encode() + (b"\x00" + factor if factor else b""), b64d(header["kdf"]["salt"])))
    new_salt = os.urandom(16)
    new_kek = kdf(profile_id, new.encode() + (b"\x00" + factor if factor else b""), new_salt)
    header["kdf"]["salt"] = b64e(new_salt)
    for index, wrap in enumerate(header["wraps"]):
        if wrap["id"] == "passphrase":
            header["wraps"][index] = wrap_ks("passphrase", new_kek, ks, os.urandom)
    header["mac"] = b64e(header_mac(header, ks, b64d(header["content"]["salt"])))
    new_header_bytes = canonical(header)
    blob = (
        MAGIC
        + (1).to_bytes(2, "big")
        + len(new_header_bytes).to_bytes(2, "big")
        + new_header_bytes
        + len(payload_ct).to_bytes(4, "big")
        + payload_ct
    )
    Path(path).write_bytes(blob)
    return {"rotated": True, "payload_unchanged": payload_ct == parse_container(path)[2]}


def import_plaintext(bundle_path: Path, home: Path, passphrase: str, *, allow_plaintext: bool = False,
                     profile: str = "argon2id-low") -> dict:
    """迁移路径：明文 .syspersona（v0.7 zip 包）→ 加密容器。

    默认拒绝：允许明文导入等于允许把「降级」当成正常路径。
    """
    if not allow_plaintext:
        raise VaultError("E_POLICY", "明文包默认拒绝导入；确需迁移请显式允许")
    bundle_path, home = Path(bundle_path), Path(home)
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            data = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        raise VaultError("E_FORMAT", "不是合法的明文迁移包")
    if hashlib.sha256(data["memory.db"]).hexdigest() != manifest.get("db_sha256"):
        raise VaultError("E_INTEGRITY", "明文包校验失败")
    for name, blob in data.items():
        if name.startswith("/") or ".." in Path(name).parts:
            raise VaultError("E_FORMAT", f"非法路径：{name}")
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return seal(home, home.with_suffix(".sysvault"), passphrase, profile=profile)


# ---------------------------------------------------------------- 公开模板 / 分叉

def publish_template(home: Path, out: Path, private_key_b64: str, public_key_b64: str) -> dict:
    traits = json.loads((home / "persona" / "traits.json").read_text(encoding="utf-8-sig"))
    kernel = json.loads((home / "persona" / "kernel.json").read_text(encoding="utf-8-sig"))
    body = {
        "schema": "syspersona-template/1",
        "publisher": {"persona_id": kernel["id"], "ed25519_public": public_key_b64},
        "persona": {"id": kernel["id"], "version": kernel["version"], "kernel": kernel, "traits": traits},
        "visibility": "public",
        "license": "MIT",
        "created_at": now_iso(),
    }
    signature = b64d(_node({"op": "ed25519_sign", "private_b64": private_key_b64,
                            "message_b64": b64e(canonical(body))})["sig_b64"])
    body["signature"] = {"alg": "ed25519", "value": b64e(signature)}
    out.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return body


def fork_template(template_path: Path, home: Path) -> dict:
    template = json.loads(Path(template_path).read_text(encoding="utf-8"))
    if template.get("schema") != "syspersona-template/1":
        raise VaultError("E_FORMAT", "不是公开人格模板")
    signature = template.pop("signature", None)
    if not signature:
        raise VaultError("E_SIGNATURE", "模板缺少签名")
    ok = _node({"op": "ed25519_verify", "public_b64": template["publisher"]["ed25519_public"],
                "message_b64": b64e(canonical(template)), "sig_b64": signature["value"]})["ok"]
    if not ok:
        raise VaultError("E_SIGNATURE", "模板签名无效")

    new_id = str(uuid.uuid4())
    kernel = dict(template["persona"]["kernel"])
    kernel["id"] = new_id
    kernel["meta"] = {"forked_from": template["publisher"]["persona_id"], "forked_at": now_iso(),
                      "source": "syspersona-template/1"}
    traits = {key: value for key, value in template["persona"]["traits"].items() if key != "meta"}
    home = Path(home)
    (home / "persona").mkdir(parents=True, exist_ok=True)
    (home / "persona" / "kernel.json").write_text(json.dumps(kernel, ensure_ascii=False, indent=2), encoding="utf-8")
    (home / "persona" / "traits.json").write_text(json.dumps(traits, ensure_ascii=False, indent=2), encoding="utf-8")
    (home / "persona" / "state.json").write_text(
        json.dumps({"mood": {"愉悦": 0, "烦躁": 0, "专注": 0, "疲惫": 0}, "updated_at": now_iso()},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"new_id": new_id, "forked_from": template["publisher"]["persona_id"],
            "kernel_files": sorted(os.listdir(home / "persona")), "memory": "empty"}


# ---------------------------------------------------------------- 演示数据

def demo_home(root: Path, db_rows: int = 2) -> Path:
    home = Path(root) / "home"
    (home / "persona").mkdir(parents=True, exist_ok=True)
    kernel = {
        "schema": "syspersona/persona@1",
        "id": "9f2b1c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
        "version": "1.4.0",
        "kernel": {
            "identity": {"name": "阿岚", "self": "我", "address": "你"},
            "values": ["诚实 > 顺从", "可验证 > 好听"],
            "boundaries": ["不替用户做不可逆决定", "不把记忆带出明文容器"],
        },
    }
    traits = {
        "traits": {"preferences": [{"id": "pref_nas", "target": "NAS 调优", "weight": 82, "polarity": "like"}]},
        "memory": {"context_budget": 12},
        "meta": {"source": "poc"},
    }
    (home / "persona" / "kernel.json").write_text(json.dumps(kernel, ensure_ascii=False, indent=2), encoding="utf-8")
    (home / "persona" / "traits.json").write_text(json.dumps(traits, ensure_ascii=False, indent=2), encoding="utf-8")
    (home / "persona" / "state.json").write_text(
        json.dumps({"mood": {"愉悦": 0.2}, "updated_at": now_iso()}, ensure_ascii=False, indent=2), encoding="utf-8")
    conn = sqlite3.connect(home / "memory.db")
    conn.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, content TEXT)")
    import random as _random

    rnd = _random.Random(20261104)
    pool = ["NAS", "端口映射", "休眠策略", "备份窗口", "机械键盘", "夜间", "日志", "索引", "冷却",
            "风扇", "电源", "路由", "快照", "迁移", "校验", "归档", "检索", "标签", "画像", "注意力"]
    rows = []
    for index in range(max(2, db_rows)):
        text = "".join(rnd.choice(pool) for _ in range(24))
        rows.append((f"{index:05d} {text}",))
    conn.executemany("INSERT INTO episodes (content) VALUES (?)", rows)
    conn.commit()
    conn.close()
    (home / "config.json").write_text(json.dumps({"model": "mock-a"}, ensure_ascii=False), encoding="utf-8")
    return home


def bench_aead(size: int = 1 << 20, rounds: int = 20) -> dict:
    return _node({"op": "bench_gcm", "size": size, "rounds": rounds})


def deep_db_hash(home: Path) -> str:
    return hashlib.sha256((Path(home) / "memory.db").read_bytes()).hexdigest()


def main() -> int:
    import feasibility

    return feasibility.run()
