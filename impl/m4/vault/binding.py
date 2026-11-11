# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：§6.4 绑定因子：把「这台机器」混进 KDF 输入，而不是解密后再比较。
"""§6.4 绑定因子：把「这台机器」混进 KDF 输入，而不是解密后再比较。

强度说明：fingerprint 模式只防随手拷贝；真正的高强度需要 device-key（OS 密钥库），
留给 M4.2。这里先实现 none / device 两种。
"""

from __future__ import annotations

import hashlib
import platform
import uuid
from pathlib import Path

from .errors import VaultError

MODES = ("none", "device")


def device_descriptor() -> str:
    parts = [platform.node()]
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            parts.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
    except (ImportError, OSError):
        machine_id = Path("/etc/machine-id")
        if machine_id.exists():
            try:
                parts.append(machine_id.read_text(encoding="utf-8").strip())
            except OSError:
                parts.append(str(uuid.getnode()))
        else:
            parts.append(str(uuid.getnode()))
    return "|".join(parts)


def factor(mode: str, *, descriptor: str | None = None) -> bytes:
    """返回绑定因子；none 返回空字节。"""
    if mode in (None, "none"):
        return b""
    if mode != "device":
        raise VaultError("E_UNSUPPORTED", f"未知绑定模式：{mode}")
    from .provider import hkdf_sha256

    material = hashlib.sha256((descriptor or device_descriptor()).encode("utf-8")).digest()
    return hkdf_sha256(material, b"", "syspersona/v1/device", 32)
