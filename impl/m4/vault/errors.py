# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：§14 失败语义：错误码与统一文案。
"""§14 失败语义：错误码与统一文案。

设计约束：
  - 所有「打不开」的场景共用 E_UNLOCK 与同一句文案，不泄露差异；
  - 错误码可区分「格式 / 算法 / 解锁 / 完整性 / 签名 / 策略」，但不区分「差一点就对」。
"""

from __future__ import annotations

CODES = {
    "E_FORMAT": "文件格式不正确",
    "E_UNSUPPORTED": "此版本无法打开（算法不支持）",
    "E_UNLOCK": "保险库无法打开",
    "E_INTEGRITY": "数据完整性校验失败",
    "E_SIGNATURE": "内核签名无效，拒绝启动",
    "E_POLICY": "操作被策略拒绝",
    "E_MNEMONIC": "助记词不正确",
    "E_BUSY": "保险库正被另一个进程使用",
    "E_IO": "文件读写失败",
    "E_CRYPTO": "密码学 Provider 执行失败",
}

UNLOCK_MESSAGE = CODES["E_UNLOCK"]


class VaultError(Exception):
    """带错误码的失败。文案对用户，detail 只进日志。"""

    def __init__(self, code: str, message: str | None = None, detail: str = ""):
        self.code = code if code in CODES else "E_IO"
        self.message = message or CODES[self.code]
        self.detail = detail
        super().__init__(self.message)

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message}


def unlock_failed() -> VaultError:
    """错口令 / 错设备 / 缺少 wrap 的唯一出口。"""
    return VaultError("E_UNLOCK")
