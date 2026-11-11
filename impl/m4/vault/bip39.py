# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：§12.2 助记词：32 字节恢复种子 ↔ BIP39 24 词。
"""§12.2 助记词：32 字节恢复种子 ↔ BIP39 24 词。"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from .errors import VaultError

WORDLIST_PATH = Path(__file__).with_name("bip39_english.txt")


@lru_cache(maxsize=1)
def wordlist() -> tuple[str, ...]:
    try:
        words = tuple(WORDLIST_PATH.read_text(encoding="utf-8").split())
    except OSError as exc:
        raise VaultError("E_IO", "助记词词表缺失", str(exc))
    if len(words) != 2048:
        raise VaultError("E_IO", "助记词词表损坏")
    return words


def mnemonic_from_entropy(entropy: bytes) -> str:
    if len(entropy) not in (16, 32):
        raise VaultError("E_MNEMONIC", "恢复种子必须是 16 或 32 字节")
    words = wordlist()
    checksum_bits = len(entropy) // 4
    bits = int.from_bytes(entropy + hashlib.sha256(entropy).digest()[:1], "big")
    total = len(entropy) * 8 + checksum_bits
    return " ".join(words[(bits >> (total - 11 * (i + 1))) & 0x7FF] for i in range(total // 11))


def entropy_from_mnemonic(phrase: str) -> bytes:
    words = phrase.split()
    if len(words) not in (12, 24):
        raise VaultError("E_MNEMONIC", "助记词长度必须是 12 或 24 个词")
    table = wordlist()
    index = {word: i for i, word in enumerate(table)}
    try:
        bits = 0
        for word in words:
            bits = (bits << 11) | index[word]
    except KeyError:
        raise VaultError("E_MNEMONIC", "助记词包含词表外的单词")
    total = len(words) * 11
    ent_bits = total * 32 // 33
    entropy = (bits >> (total - ent_bits)).to_bytes(ent_bits // 8, "big")
    checksum = bits & ((1 << (total - ent_bits)) - 1)
    expect = hashlib.sha256(entropy).digest()[0] >> (8 - (total - ent_bits))
    if checksum != expect:
        raise VaultError("E_MNEMONIC", "助记词校验位不匹配（可能抄错了一个词）")
    return entropy
