# -*- coding: utf-8 -*-
"""凭据 → 权限 的门口实测：不同密码不同权限，手机令牌只读，本机自动放行。"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = r"C:\SysPersona"
sys.path.insert(0, ROOT)
import persona_auth as A  # noqa: E402
import persona_vault as V  # noqa: E402

BASE = "http://127.0.0.1:8738"


def call(path, body=None, bearer=None, ticket=None):
    h = {}
    if bearer:
        h["Authorization"] = "Bearer " + bearer
    if ticket:
        h["X-SysPersona-Ticket"] = ticket
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
        h["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        h["Content-Type"] = "application/json"
    else:
        data = None
    r = urllib.request.Request(BASE + path, data=data, headers=h,
                               method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def creds():
    return A.creds()


def find(kind, label_part=""):
    for cid, rec in creds().items():
        if rec["kind"] == kind and label_part in (rec.get("label") or ""):
            return cid, rec
    return None, None


out = []
mem = "memory/shell/MEMORY.md"

# ① 本机不带任何凭据：device 凭据自动放行
st, raw = call("/open", {"path": mem})
out.append("本机无凭据 open：%s %s" % (st, "放行" if st == 200 else raw[:80]))

# ② 只读密码：读可以、写不行
st, raw = call("/open", {"path": mem}, bearer="Str0ng-Pass!2026")
body = json.loads(raw) if st == 200 else {}
out.append("只读密码 open(r)：%s" % st)
st2, raw2 = call("/open", {"path": mem, "mode": "rw"}, bearer="Str0ng-Pass!2026")
out.append("只读密码 open(rw)：%s %s" % (st2, json.loads(raw2).get("why", "")))

# ③ 客户端令牌：读写都行
cid, rec = find("token")
tok_plain = os.environ.get("SPR_TOKEN", "")
if not tok_plain:
    # 令牌哈希不可逆，这里现建一个专用令牌
    ok, msg = A.add("测试令牌", "token", ["vault:read", "vault:write"])
    tok_plain = msg.split("：")[-1].strip()
    cid = msg.split("（")[1].split("）")[0]
out.append("新令牌：%s" % tok_plain[:12] + "…")
st, raw = call("/open", {"path": mem, "mode": "rw"}, bearer=tok_plain)
out.append("令牌 open(rw)：%s" % st)
if st == 200:
    h = json.loads(raw)
    st, raw = call("/write?id=" + h["id"], b"x", bearer=tok_plain)
    out.append("令牌 write：%s %s" % (st, raw[:60]))

# ④ 错凭据：直接拒，不降级
st, raw = call("/open", {"path": mem}, bearer="wrong-pass")
out.append("错凭据 open：%s %s" % (st, json.loads(raw).get("why", "")))

# ⑤ 手机令牌（只读）：读行、写不行
cid, rec = find("totp", "手机")
code = A.totp(A.totp_secret(rec))
st, raw = call("/open", {"path": mem}, bearer=code)
out.append("手机令牌 open(r)：%s" % st)
st2, raw2 = call("/open", {"path": mem, "mode": "rw"}, bearer=code)
out.append("手机令牌 open(rw)：%s %s" % (st2, json.loads(raw2).get("why", "")))

# 清理测试令牌
if cid:
    A.revoke(cid)
    out.append("测试令牌已吊销：%s" % cid)

print("\n".join(out))
