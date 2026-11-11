# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：§7 容器格式：头部 + 包裹 + 载荷；§9 内核签名。
"""§7 容器格式：头部 + 包裹 + 载荷；§9 内核签名。

本模块只处理「字节与密钥」，不碰工作目录（那是 home.py / api.py 的事）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import bip39
from .canonical import canonical
from .errors import VaultError, unlock_failed
from .provider import DEFAULT_PROFILE, KDF_PROFILES, Provider, get_provider, hkdf_sha256, validate_params

MAGIC = b"SPVAULT1"
FORMAT = "syspersona-vault/1"
VERSION = 1
AAD_PREFIX = MAGIC + VERSION.to_bytes(2, "big")
HEADER_MAX = 65535
PAYLOAD_MAX = 1 << 30
WRAP_ALG = "aes-256-gcm"
CONTENT_ALG = "aes-256-gcm"
SIG_ALG = "hmac-sha256"

KERNEL_KEYS = ("schema", "id", "version", "kernel")


@dataclass
class SealResult:
    blob: bytes
    header: dict
    manifest: dict
    mnemonic: str | None
    ks: bytes


@dataclass
class OpenResult:
    files: dict[str, bytes]
    header: dict
    manifest: dict
    ks: bytes
    raw_header: bytes


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def kernel_subset(obj: dict) -> dict:
    return {key: obj[key] for key in KERNEL_KEYS if key in obj}


def wrap_aad(wrap_id: str) -> bytes:
    return AAD_PREFIX + b"|wrap|" + wrap_id.encode("ascii")


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


def header_mac(header: dict, ks: bytes, content_salt: bytes, provider: Provider) -> bytes:
    body = {key: value for key, value in header.items() if key != "mac"}
    key = hkdf_sha256(ks, content_salt, "syspersona/v1/header", 32)
    return provider.mac(SIG_ALG, key, canonical(body))


def _b64e(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    import base64
    import binascii

    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError, TypeError):
        raise VaultError("E_FORMAT")


# ---------------------------------------------------------------- 封装

def seal_container(
    files: dict[str, bytes],
    kernel: dict,
    persona: dict,
    passphrase: str,
    *,
    profile: str = DEFAULT_PROFILE,
    recovery: bool = False,
    binding_mode: str = "none",
    binding_material: bytes | None = None,
    ks: bytes | None = None,
    sealed_at: str | None = None,
    provider: Provider | None = None,
    _rng=os.urandom,
) -> SealResult:
    if profile not in KDF_PROFILES:
        raise VaultError("E_UNSUPPORTED", f"未知 KDF 档位：{profile}")
    provider = provider or get_provider()
    body = canonical(kernel_subset(kernel))
    if len(body) == 0:
        raise VaultError("E_FORMAT", "缺少人格内核")
    if not persona.get("id") or not persona.get("version"):
        raise VaultError("E_FORMAT", "缺少人格 id / version")

    ks = ks or _rng(32)
    content_salt = _rng(32)
    payload_nonce = _rng(12)
    stamp = sealed_at or now_iso()

    manifest = {
        "schema": "syspersona/manifest@1",
        "persona_id": persona["id"],
        "persona_version": persona["version"],
        "sealed_at": stamp,
        "files": [
            {"path": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
            for name, blob in sorted(files.items())
        ],
        "kernel": {
            "sha256": hashlib.sha256(body).hexdigest(),
            "sig": {
                "alg": SIG_ALG,
                "value": _b64e(
                    provider.mac(SIG_ALG, _kernel_key(ks, content_salt), body)
                ),
            },
        },
    }
    payload_plain = _zip_payload(manifest, files)
    if len(payload_plain) > PAYLOAD_MAX:
        raise VaultError("E_POLICY", "载荷超过 1 GiB；分块容器留给 v2")

    kdf = KDF_PROFILES[profile]
    header = {
        "format": FORMAT,
        "container_id": str(uuid.uuid4()),
        "created_at": stamp,
        "persona": {
            "id": persona["id"],
            "version": persona["version"],
            "kernel_sha256": hashlib.sha256(body).hexdigest(),
        },
        "kdf": {
            "id": kdf["id"],
            "profile": profile,
            "salt": _b64e(_rng(16)),
            "params": dict(kdf["params"]),
        },
        "binding": {"mode": binding_mode},
        "content": {
            "alg": CONTENT_ALG,
            "salt": _b64e(content_salt),
            "nonce": _b64e(payload_nonce),
        },
        "wraps": [],
    }

    factor = binding_material if binding_material is not None else _binding(binding_mode)
    material = passphrase.encode("utf-8") + (b"\x00" + factor if factor else b"")
    kek = provider.derive_key(header["kdf"]["id"], material, _b64d(header["kdf"]["salt"]), header["kdf"]["params"])
    header["wraps"].append(_wrap("passphrase", kek, ks, provider, _rng))

    mnemonic = None
    if recovery:
        seed = _rng(32)
        mnemonic = bip39.mnemonic_from_entropy(seed)
        rk = hkdf_sha256(seed, b"", "syspersona/v1/recovery", 32)
        header["wraps"].append(_wrap("recovery", rk, ks, provider, _rng))
        header["recovery"] = {"scheme": "bip39", "words": 24, "wrap": "recovery"}

    _ensure_unique_nonces(header)
    header["mac"] = _b64e(header_mac(header, ks, content_salt, provider))
    raw_header = canonical(header)
    if len(raw_header) > HEADER_MAX:
        raise VaultError("E_FORMAT", "头部过大")

    payload_ct = provider.aead_seal(
        CONTENT_ALG,
        _vk(ks, content_salt),
        payload_nonce,
        payload_aad(header),
        payload_plain,
    )
    blob = (
        MAGIC
        + VERSION.to_bytes(2, "big")
        + len(raw_header).to_bytes(2, "big")
        + raw_header
        + len(payload_ct).to_bytes(4, "big")
        + payload_ct
    )
    return SealResult(blob=blob, header=header, manifest=manifest, mnemonic=mnemonic, ks=ks)


def _wrap(wrap_id: str, key: bytes, ks: bytes, provider: Provider, rng) -> dict:
    nonce = rng(12)
    return {
        "id": wrap_id,
        "alg": WRAP_ALG,
        "nonce": _b64e(nonce),
        "ct": _b64e(provider.aead_seal(WRAP_ALG, key, nonce, wrap_aad(wrap_id), ks)),
    }


def _ensure_unique_nonces(header: dict) -> None:
    nonces = [wrap["nonce"] for wrap in header["wraps"]] + [header["content"]["nonce"]]
    if len(nonces) != len(set(nonces)):
        raise VaultError("E_CRYPTO", "nonce 重复；拒绝写出容器")


def _zip_payload(manifest: dict, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, blob in files.items():
            archive.writestr(name, blob)
    return buffer.getvalue()


def _kernel_key(ks: bytes, salt: bytes) -> bytes:
    return hkdf_sha256(ks, salt, "syspersona/v1/kernel", 32)


def _vk(ks: bytes, salt: bytes) -> bytes:
    return hkdf_sha256(ks, salt, "syspersona/v1/content", 32)


def _binding(mode: str) -> bytes:
    from . import binding

    return binding.factor(mode)


# ---------------------------------------------------------------- 读取

def parse_bytes(raw: bytes) -> tuple[bytes, dict, bytes]:
    if len(raw) < 20 or raw[:8] != MAGIC:
        raise VaultError("E_FORMAT")
    version = int.from_bytes(raw[8:10], "big")
    if version != VERSION:
        raise VaultError("E_UNSUPPORTED", f"容器版本 {version} 不受支持")
    header_len = int.from_bytes(raw[10:12], "big")
    if header_len > HEADER_MAX or len(raw) < 16 + header_len + 4:
        raise VaultError("E_FORMAT")
    raw_header = raw[12 : 12 + header_len]
    payload_len = int.from_bytes(raw[12 + header_len : 16 + header_len], "big")
    payload_ct = raw[16 + header_len :]
    if payload_len != len(payload_ct) or payload_len > PAYLOAD_MAX:
        raise VaultError("E_FORMAT")
    try:
        header = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VaultError("E_FORMAT")
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise VaultError("E_FORMAT")
    return raw_header, header, payload_ct


def parse_file(path: Path) -> tuple[bytes, dict, bytes]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise VaultError("E_IO", "无法读取容器", str(exc))
    return parse_bytes(raw)


def read_header(path: Path) -> dict:
    return parse_file(path)[1]


def _check_algs(header: dict, provider: Provider) -> None:
    for key in header:
        if key not in _KNOWN_HEADER_KEYS and not key.startswith("x-"):
            raise VaultError("E_UNSUPPORTED", f"未知头部字段：{key}")
    for key in ("container_id", "created_at", "persona", "kdf", "binding", "content", "wraps", "mac"):
        if key not in header:
            raise VaultError("E_FORMAT")
    persona = header["persona"]
    if not isinstance(persona, dict) or not persona.get("id") or not persona.get("version"):
        raise VaultError("E_FORMAT")
    kdf = header.get("kdf") or {}
    kdf_id = kdf.get("id")
    if kdf_id not in provider.algorithms:
        raise VaultError("E_UNSUPPORTED", f"不支持的 KDF：{kdf_id}")
    validate_params(kdf_id, kdf.get("params") or {})
    if not _b64d(str(kdf.get("salt", ""))):
        raise VaultError("E_FORMAT")
    content = header.get("content") or {}
    if content.get("alg") not in provider.algorithms:
        raise VaultError("E_UNSUPPORTED", f"不支持的 AEAD：{content.get('alg')}")
    _b64d(str(content.get("salt", "")))
    _b64d(str(content.get("nonce", "")))
    wraps = header.get("wraps")
    if not isinstance(wraps, list) or not wraps:
        raise VaultError("E_FORMAT")
    for wrap in wraps:
        if wrap.get("alg") != WRAP_ALG:
            raise VaultError("E_UNSUPPORTED", f"不支持的包裹算法：{wrap.get('alg')}")
        if wrap.get("id") not in ("passphrase", "recovery", "device", "escrow"):
            raise VaultError("E_UNSUPPORTED", f"未知 wrap：{wrap.get('id')}")
        _b64d(str(wrap.get("nonce", "")))
        _b64d(str(wrap.get("ct", "")))
    if (header.get("binding") or {}).get("mode") not in ("none", "device", "device-key", "account"):
        raise VaultError("E_UNSUPPORTED", "未知绑定模式")
    _b64d(str(header.get("mac", "")))


_KNOWN_HEADER_KEYS = {
    "format",
    "container_id",
    "created_at",
    "persona",
    "kdf",
    "binding",
    "content",
    "wraps",
    "recovery",
    "mac",
}


def open_container(
    path: Path,
    passphrase: str | None = None,
    *,
    mnemonic: str | None = None,
    binding_material: bytes | None = None,
    provider: Provider | None = None,
) -> OpenResult:
    provider = provider or get_provider()
    raw_header, header, payload_ct = parse_file(path)
    _check_algs(header, provider)

    if mnemonic:
        seed = bip39.entropy_from_mnemonic(mnemonic)
        from .provider import hkdf_sha256

        ks = _unwrap(header, "recovery", hkdf_sha256(seed, b"", "syspersona/v1/recovery", 32), provider)
    else:
        if passphrase is None:
            raise unlock_failed()
        factor = binding_material if binding_material is not None else _binding(header["binding"]["mode"])
        material = passphrase.encode("utf-8") + (b"\x00" + factor if factor else b"")
        kek = provider.derive_key(header["kdf"]["id"], material, _b64d(header["kdf"]["salt"]), header["kdf"]["params"])
        ks = _unwrap(header, "passphrase", kek, provider)

    content_salt = _b64d(header["content"]["salt"])
    if not _mac_equal(header_mac(header, ks, content_salt, provider), _b64d(header["mac"])):
        raise VaultError("E_INTEGRITY")

    try:
        payload_plain = provider.aead_open(
            CONTENT_ALG,
            _vk(ks, content_salt),
            _b64d(header["content"]["nonce"]),
            payload_aad(header),
            payload_ct,
        )
    except VaultError as exc:
        if exc.code == "E_INTEGRITY":
            raise VaultError("E_INTEGRITY")
        raise
    files, manifest = _unzip_payload(payload_plain)
    verify_manifest(files, manifest, header, ks, content_salt, provider)
    return OpenResult(files=files, header=header, manifest=manifest, ks=ks, raw_header=raw_header)


def _unwrap(header: dict, wrap_id: str, key: bytes, provider: Provider) -> bytes:
    for wrap in header.get("wraps") or []:
        if wrap.get("id") != wrap_id:
            continue
        try:
            return provider.aead_open(WRAP_ALG, key, _b64d(wrap["nonce"]), wrap_aad(wrap_id), _b64d(wrap["ct"]))
        except VaultError:
            raise unlock_failed()
    raise unlock_failed()


def _mac_equal(left: bytes, right: bytes) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(left, right)


def _unzip_payload(plain: bytes) -> tuple[dict[str, bytes], dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(plain)) as archive:
            names = archive.namelist()
            if "manifest.json" not in names:
                raise VaultError("E_INTEGRITY")
            manifest = json.loads(archive.read("manifest.json"))
            files = {name: archive.read(name) for name in names if name != "manifest.json"}
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError):
        raise VaultError("E_INTEGRITY")
    if not isinstance(manifest, dict) or manifest.get("schema") != "syspersona/manifest@1":
        raise VaultError("E_INTEGRITY")
    return files, manifest


def verify_manifest(
    files: dict[str, bytes],
    manifest: dict,
    header: dict,
    ks: bytes,
    content_salt: bytes,
    provider: Provider,
) -> None:
    if manifest.get("persona_id") != header["persona"]["id"]:
        raise VaultError("E_INTEGRITY")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise VaultError("E_INTEGRITY")
    listed = set()
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            raise VaultError("E_INTEGRITY")
        listed.add(path)
        blob = files.get(path)
        if blob is None or hashlib.sha256(blob).hexdigest() != entry.get("sha256"):
            raise VaultError("E_INTEGRITY")
    if set(files) != listed:
        raise VaultError("E_INTEGRITY")
    raw = files.get("persona/kernel.json") or files.get("persona.json")
    if raw is None:
        raise VaultError("E_INTEGRITY")
    try:
        kernel = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VaultError("E_INTEGRITY")
    body = canonical(kernel_subset(kernel))
    kernel_meta = manifest.get("kernel")
    if not isinstance(kernel_meta, dict) or not isinstance(kernel_meta.get("sig"), dict):
        raise VaultError("E_INTEGRITY")
    if hashlib.sha256(body).hexdigest() != kernel_meta.get("sha256"):
        raise VaultError("E_SIGNATURE")
    signature = kernel_meta["sig"]
    if signature.get("alg") != SIG_ALG:
        raise VaultError("E_UNSUPPORTED")
    expected = provider.mac(SIG_ALG, _kernel_key(ks, content_salt), body)
    if not _mac_equal(expected, _b64d(str(signature.get("value", "")))):
        raise VaultError("E_SIGNATURE")


# ---------------------------------------------------------------- 轮换

def rotate_passphrase(
    path: Path,
    old_passphrase: str,
    new_passphrase: str,
    *,
    binding_material: bytes | None = None,
    provider: Provider | None = None,
) -> dict:
    """只重包 KS：载荷逐字节不变。"""
    provider = provider or get_provider()
    raw_header, header, payload_ct = parse_file(path)
    _check_algs(header, provider)
    factor = binding_material if binding_material is not None else _binding(header["binding"]["mode"])
    old_material = old_passphrase.encode("utf-8") + (b"\x00" + factor if factor else b"")
    kek = provider.derive_key(header["kdf"]["id"], old_material, _b64d(header["kdf"]["salt"]), header["kdf"]["params"])
    ks = _unwrap(header, "passphrase", kek, provider)
    if not _mac_equal(header_mac(header, ks, _b64d(header["content"]["salt"]), provider), _b64d(header["mac"])):
        raise VaultError("E_INTEGRITY")

    new_salt = os.urandom(16)
    header["kdf"]["salt"] = _b64e(new_salt)
    new_material = new_passphrase.encode("utf-8") + (b"\x00" + factor if factor else b"")
    new_kek = provider.derive_key(header["kdf"]["id"], new_material, new_salt, header["kdf"]["params"])
    for index, wrap in enumerate(header["wraps"]):
        if wrap["id"] == "passphrase":
            header["wraps"][index] = _wrap("passphrase", new_kek, ks, provider, os.urandom)
    _ensure_unique_nonces(header)
    header["mac"] = _b64e(header_mac(header, ks, _b64d(header["content"]["salt"]), provider))
    new_header = canonical(header)
    if len(new_header) > HEADER_MAX:
        raise VaultError("E_FORMAT")
    blob = (
        MAGIC
        + VERSION.to_bytes(2, "big")
        + len(new_header).to_bytes(2, "big")
        + new_header
        + len(payload_ct).to_bytes(4, "big")
        + payload_ct
    )
    _write_bytes(path, blob)
    open_container(path, new_passphrase, binding_material=binding_material, provider=provider)
    return {"rotated": True, "payload_unchanged": parse_file(path)[2] == payload_ct}


def _write_bytes(path: Path, blob: bytes) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    fd = os.open(tmp, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
