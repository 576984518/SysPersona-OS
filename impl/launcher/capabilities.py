# -*- coding: utf-8 -*-
"""能力面裁决与最小执行器（LAUNCHER_SPEC §11 的最小实现）。

只做两件事，且都可测：
1. 按 kernel.permissions 给出 allow / ask / deny（deny 优先，未列出走 default）；
2. 实现 **唯一一个** 执行能力 `fs.read`（限沙箱根目录、限大小），其余一律 not-implemented。

设计约束：
- 不引入沙箱进程，不做网络与命令执行；`proc.exec` 默认 deny 且无实现；
- 路径穿越用 realpath 比对，符号链接也拦得住；
- 本模块不碰存储：拒绝留痕由调用方用返回的 verdict 自行记录。
"""
from dataclasses import dataclass
import os
import re

DEFAULT = "deny"
MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class Decision:
    capability: str
    verdict: str          # allow / ask / deny
    reason: str = ""


def _hit(items, cap):
    for c in items or ():
        if c == "*" or cap == c or cap.startswith(c + ":"):
            return True
    return False


def decide(permissions, capability):
    """纯函数：不给权限表就按 deny。"""
    p = permissions or {}
    if _hit(p.get("deny"), capability):
        return Decision(capability, "deny", "listed in deny")
    if _hit(p.get("ask"), capability):
        return Decision(capability, "ask", "listed in ask")
    if _hit(p.get("allow"), capability):
        return Decision(capability, "allow", "listed in allow")
    d = p.get("default", DEFAULT)
    if d not in ("allow", "ask", "deny"):
        d = DEFAULT
    return Decision(capability, d, "default")


_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _looks_absolute(p):
    """跨平台绝对路径判断：POSIX 用 os.path.isabs，另加 Windows 盘符与 UNC。"""
    return bool(p) and (os.path.isabs(p) or bool(_WIN_ABS.match(p)))


def resolve_path(root, rel):
    """把 rel 解析到 root 下；越界（含 ../、符号链接、异平台绝对路径）返回 None。

    先把反斜杠统一成 "/"：调用方（模型/用户）可能写 Windows 风格路径，
    而判断不该因为跑在 Windows 还是 Linux 而不同。POSIX 上 "..\\x" 只是普通文件名，
    但在混合输入下它就是穿越——统一按分隔符处理，宁可拒错也不放行。
    """
    if not isinstance(rel, str):
        return None
    rel = rel.replace("\\", "/")
    if _looks_absolute(rel):
        return None
    root_real = os.path.realpath(root)
    cand = os.path.realpath(os.path.join(root_real, rel))
    if cand == root_real:
        return None
    if not cand.startswith(root_real + os.sep):
        return None
    return cand


def read_text(root, rel, max_bytes=MAX_BYTES):
    p = resolve_path(root, rel)
    if p is None:
        return False, "path-escape"
    if not os.path.isfile(p):
        return False, "not-found"
    try:
        if os.path.getsize(p) > max_bytes:
            return False, "too-large"
        with open(p, encoding="utf-8", errors="replace") as f:
            return True, f.read(max_bytes)
    except OSError as e:
        return False, "io-error: %s" % e


def execute(permissions, root, capability, args=None):
    """返回 (verdict, result)。

    ask 与 deny 都不执行；只有 allow 且能力已实现时才真正读。
    """
    d = decide(permissions, capability)
    if d.verdict != "allow":
        return d.verdict, {"refused": True, "capability": capability, "reason": d.reason}
    if capability == "fs.read":
        ok, data = read_text(root, (args or {}).get("path", ""))
        return "allow", {"ok": ok, "data": data if ok else None,
                         "error": None if ok else data}
    return "allow", {"ok": False, "error": "not-implemented", "capability": capability}
