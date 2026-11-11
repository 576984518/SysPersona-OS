# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：capabilities.py 的自测。跑法：python impl/launcher/test_capabilities.py
"""capabilities.py 的自测。跑法：python impl/launcher/test_capabilities.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capabilities as C  # noqa: E402

PERM = {
    "default": "deny",
    "allow": ["fs.read"],
    "ask": ["fs.write"],
    "deny": ["proc.exec"],
}


def main():
    # 1. 裁决：deny 优先 / ask 次之 / allow / default
    assert C.decide(PERM, "proc.exec").verdict == "deny"
    assert C.decide(PERM, "proc.exec:bash").verdict == "deny"
    assert C.decide(PERM, "fs.write:workspace").verdict == "ask"
    assert C.decide(PERM, "fs.read").verdict == "allow"
    assert C.decide(PERM, "camera").verdict == "deny"          # default
    assert C.decide({}, "fs.read").verdict == "deny"           # 没有权限表 = 全拒

    # 2. 路径沙箱
    root = tempfile.mkdtemp(prefix="cap-")
    (Path(root) / "ok.txt").write_text("你好", encoding="utf-8")
    (Path(root) / "big.txt").write_text("x" * (C.MAX_BYTES + 1), encoding="utf-8")
    assert C.resolve_path(root, "ok.txt") is not None
    assert C.resolve_path(root, "../escape.txt") is None
    assert C.resolve_path(root, "C:\\Windows\\win.ini") is None
    assert C.resolve_path(root, "/etc/passwd") is None
    assert C.resolve_path(root, "..\\escape.txt") is None
    assert C.resolve_path(root, "") is None

    # 3. 执行：只读能力可用，越权/未实现不执行
    v, r = C.execute(PERM, root, "fs.read", {"path": "ok.txt"})
    assert v == "allow" and r["ok"] and r["data"] == "你好"

    v, r = C.execute(PERM, root, "fs.read", {"path": "../escape.txt"})
    assert v == "allow" and not r["ok"] and r["error"] == "path-escape"

    v, r = C.execute(PERM, root, "fs.read", {"path": "big.txt"})
    assert v == "allow" and not r["ok"] and r["error"] == "too-large"

    v, r = C.execute(PERM, root, "proc.exec", {"cmd": "whoami"})
    assert v == "deny" and r["refused"] is True

    v, r = C.execute(PERM, root, "fs.write", {"path": "x"})
    assert v == "ask" and r["refused"] is True

    v, r = C.execute(PERM, root, "net.fetch", {"url": "https://example.com"})
    assert v == "deny" and r["refused"] is True

    # 4. 行为：allow 但未实现的能力，不执行也不报成功
    perm2 = dict(PERM, allow=["fs.read", "net.fetch"])
    v, r = C.execute(perm2, root, "net.fetch", {"url": "https://example.com"})
    assert v == "allow" and r["error"] == "not-implemented"

    print("ALL OK")


if __name__ == "__main__":
    main()
