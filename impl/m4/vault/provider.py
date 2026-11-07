# -*- coding: utf-8 -*-
"""§15 Provider 协议 + 原生实现。

核心格式与流程不依赖第三方库；密码学原语必须由 Provider 提供。
原生实现：cryptography（AES-256-GCM）+ argon2-cffi（Argon2id）+ hashlib（scrypt/HKDF/HMAC）。

未安装依赖时，import 本模块仍然成功；真正调用才会抛 E_UNSUPPORTED，
这样 M1 的零依赖路径完全不受影响。
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from typing import Protocol

from .errors import VaultError

INSTALL_HINT = "加密功能需要可选依赖：pip install -r impl/m4/requirements-m4.txt"

KDF_PROFILES = {
    "low": {"id": "argon2id", "params": {"memory": 19456, "passes": 2, "parallelism": 1}},
    "mid": {"id": "argon2id", "params": {"memory": 65536, "passes": 3, "parallelism": 1}},
    "high": {"id": "argon2id", "params": {"memory": 262144, "passes": 4, "parallelism": 1}},
    "scrypt": {"id": "scrypt", "params": {"N": 32768, "r": 8, "p": 1}},
}
DEFAULT_PROFILE = "mid"

# 防 DoS：头部参数是不可信输入，超出本机上限直接拒绝
LIMITS = {
    "argon2id": {"memory": (8, 1_048_576), "passes": (1, 64), "parallelism": (1, 16)},
    "scrypt": {"N": (1024, 1 << 20), "r": (1, 32), "p": (1, 16)},
}


def hkdf_sha256(ikm: bytes, salt: bytes, info, length: int = 32) -> bytes:
    """RFC 5869，单块足够（length ≤ 32）。"""
    if length > 64:
        raise VaultError("E_CRYPTO", "HKDF 输出过长")
    info_bytes = info.encode("utf-8") if isinstance(info, str) else info
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info_bytes + struct.pack("B", counter), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def validate_params(kdf_id: str, params: dict) -> None:
    rules = LIMITS.get(kdf_id)
    if rules is None:
        raise VaultError("E_UNSUPPORTED", f"不支持的 KDF：{kdf_id}")
    for key, (low, high) in rules.items():
        value = params.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise VaultError("E_UNSUPPORTED", f"KDF 参数 {key} 非法")
        if not low <= value <= high:
            raise VaultError("E_UNSUPPORTED", f"KDF 参数 {key} 超出上限")
    if kdf_id == "scrypt" and params["N"] & (params["N"] - 1):
        raise VaultError("E_UNSUPPORTED", "scrypt N 必须是 2 的幂")


class Provider(Protocol):
    id: str
    version: str
    algorithms: frozenset[str]

    def derive_key(self, kdf_id: str, material: bytes, salt: bytes, params: dict) -> bytes: ...

    def aead_seal(self, alg: str, key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes: ...

    def aead_open(self, alg: str, key: bytes, nonce: bytes, aad: bytes, blob: bytes) -> bytes: ...

    def mac(self, alg: str, key: bytes, message: bytes) -> bytes: ...


class NativeProvider:
    """基于 cryptography / argon2-cffi 的默认实现。"""

    id = "native"
    version = "1.0"
    algorithms = frozenset({"argon2id", "scrypt", "aes-256-gcm", "hmac-sha256"})

    def __init__(self) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise VaultError("E_UNSUPPORTED", INSTALL_HINT, str(exc))
        self._aesgcm_cls = AESGCM
        try:
            from argon2.low_level import Type, hash_secret_raw  # noqa: F401
        except ImportError:
            self._argon2 = None
        else:
            self._argon2 = (hash_secret_raw, Type.ID)

    def derive_key(self, kdf_id: str, material: bytes, salt: bytes, params: dict) -> bytes:
        validate_params(kdf_id, params)
        if kdf_id == "scrypt":
            try:
                return hashlib.scrypt(
                    material,
                    salt=salt,
                    n=params["N"],
                    r=params["r"],
                    p=params["p"],
                    dklen=32,
                    maxmem=64 * 1024 * 1024,
                )
            except (ValueError, MemoryError) as exc:
                raise VaultError("E_CRYPTO", "scrypt 执行失败", str(exc))
        if kdf_id == "argon2id":
            if self._argon2 is None:
                raise VaultError(
                    "E_UNSUPPORTED",
                    "缺少 argon2-cffi；可改用 --profile scrypt",
                    INSTALL_HINT,
                )
            hash_secret_raw, type_id = self._argon2
            try:
                return hash_secret_raw(
                    secret=material,
                    salt=salt,
                    time_cost=params["passes"],
                    memory_cost=params["memory"],
                    parallelism=params["parallelism"],
                    hash_len=32,
                    type=type_id,
                )
            except Exception as exc:  # noqa: BLE001 - argon2 的异常类型不稳定
                raise VaultError("E_CRYPTO", "Argon2id 执行失败", str(exc))
        raise VaultError("E_UNSUPPORTED", f"不支持的 KDF：{kdf_id}")

    def aead_seal(self, alg: str, key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
        if alg != "aes-256-gcm":
            raise VaultError("E_UNSUPPORTED", f"不支持的 AEAD：{alg}")
        if len(nonce) != 12:
            raise VaultError("E_CRYPTO", "nonce 必须是 12 字节")
        try:
            return self._aesgcm_cls(key).encrypt(nonce, plaintext, aad)
        except Exception as exc:  # noqa: BLE001
            raise VaultError("E_CRYPTO", "AEAD 加密失败", str(exc))

    def aead_open(self, alg: str, key: bytes, nonce: bytes, aad: bytes, blob: bytes) -> bytes:
        if alg != "aes-256-gcm":
            raise VaultError("E_UNSUPPORTED", f"不支持的 AEAD：{alg}")
        if len(nonce) != 12:
            raise VaultError("E_CRYPTO", "nonce 必须是 12 字节")
        try:
            return self._aesgcm_cls(key).decrypt(nonce, blob, aad)
        except Exception:  # noqa: BLE001 - 统一成完整性失败，不泄露细节
            raise VaultError("E_INTEGRITY")

    def mac(self, alg: str, key: bytes, message: bytes) -> bytes:
        if alg != "hmac-sha256":
            raise VaultError("E_UNSUPPORTED", f"不支持的 MAC：{alg}")
        return hmac.new(key, message, hashlib.sha256).digest()


_REGISTRY: dict[str, type] = {"native": NativeProvider}


def register(name: str, factory: type) -> None:
    """第三方 Provider 注册入口。算法选择只来自容器头部，不来自用户输入。"""
    _REGISTRY[name] = factory


def get_provider(name: str = "native") -> Provider:
    factory = _REGISTRY.get(name)
    if factory is None:
        raise VaultError("E_UNSUPPORTED", f"未知 Provider：{name}")
    return factory()


def available() -> bool:
    try:
        get_provider()
    except VaultError:
        return False
    return True
