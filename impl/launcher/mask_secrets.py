# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：mask_secrets.py — 上云前的本地脱敏（DLP）。
"""mask_secrets.py — 上云前的本地脱敏（DLP）。

原则：**先脱敏，再出本地**。命中即打码，保留上下文语义，不删整段。
用法：
    python mask_secrets.py --test        # 自测
    python mask_secrets.py file.md       # 打印脱敏后的文本（不写回）
    from mask_secrets import mask        # mask(text) -> (masked, hits)
"""
import re
import sys

MASK = "****"


def _keep_head(s, head=3, tail=2):
    if len(s) <= head + tail:
        return MASK
    return s[:head] + MASK + s[-tail:]


# 顺序很重要：先整段吃掉 URL / 密钥，再处理里面的邮箱、电话等碎片
RULES = [
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), lambda m: "sk-" + MASK),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.I), lambda m: "Bearer " + MASK),
    ("url_auth", re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@"), lambda m: m.group(1) + MASK + "@"),
    ("kv_secret", re.compile(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"',;]{6,})"),
     lambda m: m.group(1) + "=" + MASK),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"), lambda m: _keep_head(m.group(0), 2)),
    ("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), lambda m: m.group(0)[:3] + MASK + m.group(0)[-2:]),
    ("idcard_cn", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), lambda m: m.group(0)[:4] + MASK + m.group(0)[-2:]),
]


def mask(text):
    """返回 (脱敏文本, [(kind, 命中长度)])。命中列表不含原文，避免二次泄漏。"""
    hits = []
    out = text
    for kind, pat, fn in RULES:
        def _sub(m, _k=kind, _f=fn):
            hits.append((_k, len(m.group(0))))
            return _f(m)
        out = pat.sub(_sub, out)
    return out, hits


def _test():
    cases = [
        ("我的密钥是 sk-abcdefghijklmnop1234 用完删除", "api_key"),
        ("Authorization: Bearer abcdefghijklmnop1234", "bearer"),
        ("联系我 zhangsan@example.com", "email"),
        ("手机号 13812345678", "phone_cn"),
        ("身份证 110101199003071234", "idcard_cn"),
        ("password: hunter2secret", "kv_secret"),
        ("https://user:pass123@example.com/x", "url_auth"),
    ]
    for src, kind in cases:
        out, hits = mask(src)
        assert hits, "未命中: " + src
        assert any(h[0] == kind for h in hits), "类型不对: %s -> %s" % (src, hits)
        print("  ok %-10s %s" % (kind, out[:60]))

    plain = "今天讨论了索引分层和冷热数据分离，B10 决定不做。"
    out, hits = mask(plain)
    assert out == plain and not hits, "误伤: " + out
    print("  ok 不误伤普通文本")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2 or sys.argv[1] == "--test":
        _test()
        print("ALL OK")
    else:
        data = open(sys.argv[1], encoding="utf-8", errors="replace").read()
        masked, hits = mask(data)
        print(masked)
        print("\n[命中 %d 处]" % len(hits), file=sys.stderr)
