# -*- coding: utf-8 -*-
"""I/O 门客户端冒烟：真发请求，比对哈希。"""
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = r"C:\SysPersona"
sys.path.insert(0, ROOT)
import persona_vault as V  # noqa: E402


def tok():
    with io.open(os.path.join(V.key_dir(), "lan-token.txt"), encoding="utf-8") as f:
        return f.read().strip()


def req(url, method="GET", body=None, headers=None, raw=False):
    h = {"Authorization": "Bearer " + tok()}
    h.update(headers or {})
    r = urllib.request.Request(url, method=method, data=body, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            data = resp.read()
            return resp.status, (data if raw else json.loads(data or b"{}")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers or {})


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "8738"
    base = "http://127.0.0.1:%s" % port
    out = []
    st, h, _ = req(base + "/health")
    out.append("health %s ok=%s key=%s" % (st, h.get("ok"), h.get("key")))

    # ① 明文文件：门 → 与磁盘哈希一致
    p = os.path.join(ROOT, "memory", "shell", "MEMORY.md")
    st, r, _ = req(base + "/open", "POST", json.dumps({"path": "memory/shell/MEMORY.md"}).encode())
    st2, data, hd = req(r["url"], raw=True)
    same = sha(data) == sha(io.open(p, "rb").read())
    out.append("明文: open=%s read=%s 一致=%s 字节=%d" % (st, st2, same, len(data)))

    # ② 密文文件：门自动解密，哈希仍一致
    sealed = os.path.join(ROOT, "ops", "_vaulttest", "sealed.md")
    io.open(sealed, "wb").write(V.seal(b"vault-me " * 500, V.load_key(), V.rel_aad(sealed)))
    st, r2, _ = req(base + "/open", "POST", json.dumps({"path": "ops/_vaulttest/sealed.md"}).encode())
    st3, data2, _ = req(r2["url"], raw=True)
    out.append("密文: open=%s read=%s 解开正确=%s 字节=%d"
               % (st, st3, data2 == b"vault-me " * 500, len(data2)))

    # ③ 分块读
    st4, part, _ = req(r2["url"] + "&off=3&n=5", raw=True)
    out.append("分块: off=3,n=5 → %r" % part)

    # ④ 只读句柄写（应 403）
    st5, j5, _ = req(base + "/write?id=" + r2["id"], "POST", b"nope")
    out.append("只读句柄写: %s why=%s" % (st5, j5.get("why")))

    # ⑤ 无令牌（应 403）
    rq = urllib.request.Request(r2["url"])
    try:
        urllib.request.urlopen(rq, timeout=10)
        out.append("无令牌读: **没拦住**")
    except urllib.error.HTTPError as e:
        out.append("无令牌读: 拒绝 %s" % e.code)

    # ⑥ 库外路径（应 404）
    st6, j6, _ = req(base + "/open", "POST", json.dumps({"path": "C:/Windows/win.ini"}).encode())
    out.append("库外路径: %s why=%s" % (st6, j6.get("why")))

    print("\n".join(out))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    main()
