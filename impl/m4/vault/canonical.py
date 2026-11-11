# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：§8 规范化 JSON：签名与 MAC 依赖字节级一致。
"""§8 规范化 JSON：签名与 MAC 依赖字节级一致。"""

from __future__ import annotations

import json
import unicodedata

from .errors import VaultError


def _check(value, depth: int = 0) -> None:
    if depth > 32:
        raise VaultError("E_FORMAT", "JSON 嵌套过深")
    if isinstance(value, float):
        raise VaultError("E_FORMAT", "规范化 JSON 不允许浮点数")
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise VaultError("E_FORMAT", "字符串必须是 NFC")
        return
    if isinstance(value, list):
        for item in value:
            _check(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise VaultError("E_FORMAT", "对象键必须是字符串")
            _check(key, depth + 1)
            _check(item, depth + 1)
        return
    if value is not None and not isinstance(value, (bool, int)):
        raise VaultError("E_FORMAT", f"不支持的类型：{type(value).__name__}")


def canonical(obj) -> bytes:
    """键排序、无空白、UTF-8、NFC、禁止浮点。"""
    _check(obj)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
