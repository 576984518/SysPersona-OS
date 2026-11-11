# -*- coding: utf-8 -*-
"""沙箱：加密开关打开时，写进磁盘的必须是密文，读回来必须一致。"""
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request

T = r"C:\SysPersona\ops\_vaulttest\sb"
sys.path.insert(0, T)
os.environ["SYSPERSONA_VAULT_DIR"] = T
import persona_vault as V  # noqa: E402

BASE = "http://127.0.0.1:8740"


def tok():
    with io.open(os.path.join(T, "lan-token.txt"), encoding="utf-8") as f:
        return f.read().strip()


def req(url, method="GET", body=None, headers=None, raw=False):
    h = {"Authorization": "Bearer " + tok()}
    h.update(headers or {})
    r = urllib.request.Request(url, method=method, data=body, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            d = resp.read()
            return resp.status, (d if raw else json.loads(d or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


out = []
st, h = req(BASE + "/health")
out.append("health %s key=%s" % (st, h.get("key")))

payload = b"# encrypt me\n" * 200
st, r = req(BASE + "/open", "POST",
            json.dumps({"path": "sessions/a.md", "mode": "rw"}).encode())
out.append("open(rw) %s mode=%s" % (st, r.get("mode")))

st, w = req(BASE + "/write?id=" + r["id"], "POST", payload)
out.append("write %s bytes=%s sealed=%s" % (st, w.get("bytes"), w.get("sealed")))

disk = io.open(os.path.join(T, "sessions", "a.md"), "rb").read()
out.append("磁盘上是密文=%s（前 4 字节 %r）" % (disk[:4] == V.MAGIC, disk[:4]))

back = V.open_file(os.path.join(T, "sessions", "a.md"))
out.append("直接解码一致=%s" % (back == payload))

st, r2 = req(BASE + "/open", "POST", json.dumps({"path": "sessions/a.md"}).encode())
st, data = req(r2["url"], raw=True)
out.append("门口读回一致=%s 字节=%d" % (data == payload, len(data)))

# 换把钥匙就解不开（证明真的按钥匙加密，不是混淆）
other = hashlib.sha256(b"other-key").digest()
try:
    V.unseal(disk, other, V.rel_aad(os.path.join(T, "sessions", "a.md")))
    out.append("换钥匙: **没拦住**")
except Exception:  # noqa: BLE001
    out.append("换钥匙: 拒绝")

print("\n".join(out))
