#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_gate.py — 物理接入授权（v2.2.0 的最小实现）

规则：
  1. 每台设备有唯一 hardware_device_id（首次启动生成，存 device.json）；
  2. 人格要在本机运行，必须持有设备所有者签发的 grant；
  3. 没有 grant 或 grant 失效 → 拒绝启动，不加载任何人格与记忆。

零第三方依赖；签名用设备本地密钥做 HMAC（PoC 级，真实实现应走 TPM）。

用法：
  python device_gate.py --home ./data init
  python device_gate.py --home ./data grant --persona lin --owner u-me --days 30
  python device_gate.py --home ./data check --persona lin
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _paths(home):
    return {
        "device": os.path.join(home, "device.json"),
        "key": os.path.join(home, "device.key"),
        "grants": os.path.join(home, "grants"),
    }


def ensure_device(home):
    """首次调用时生成设备身份；之后只读。"""
    p = _paths(home)
    os.makedirs(p["grants"], exist_ok=True)
    if os.path.exists(p["device"]):
        with open(p["device"], encoding="utf-8") as f:
            return json.load(f)
    dev = {
        "device_id": str(uuid.uuid4()),
        "owner_uuid": "u-owner",           # 设备所有者；真实实现由用户在初始化时指定
        "created_at": time.time(),
    }
    with open(p["device"], "w", encoding="utf-8") as f:
        json.dump(dev, f, ensure_ascii=False, indent=2)
    if not os.path.exists(p["key"]):
        with open(p["key"], "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    return dev


def _key(home):
    with open(_paths(home)["key"], encoding="utf-8") as f:
        return bytes.fromhex(f.read().strip())


def issue_grant(home, persona_id, owner_uuid, days=30, capabilities=None):
    dev = ensure_device(home)
    body = {
        "device_id": dev["device_id"],
        "persona_id": persona_id,
        "owner_uuid": owner_uuid,
        "capabilities": capabilities or ["fs.read", "net.fetch"],
        "issued_at": time.time(),
        "expires_at": time.time() + days * 86400,
    }
    body["sig"] = hmac.new(_key(home), _canon(body), hashlib.sha256).hexdigest()
    path = os.path.join(_paths(home)["grants"], "%s.json" % persona_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    return path, body


def check_grant(home, persona_id):
    """返回 (ok, reason)。任何一项不满足都不放行。"""
    dev = ensure_device(home)
    path = os.path.join(_paths(home)["grants"], "%s.json" % persona_id)
    if not os.path.exists(path):
        return False, "no_grant"
    with open(path, encoding="utf-8") as f:
        g = json.load(f)
    sig = g.pop("sig", "")
    if not hmac.compare_digest(sig, hmac.new(_key(home), _canon(g), hashlib.sha256).hexdigest()):
        return False, "bad_signature"
    if g.get("device_id") != dev["device_id"]:
        return False, "wrong_device"
    if g.get("persona_id") != persona_id:
        return False, "wrong_persona"
    if g.get("expires_at", 0) < time.time():
        return False, "expired"
    return True, "ok"


def revoke(home, persona_id):
    path = os.path.join(_paths(home)["grants"], "%s.json" % persona_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description="物理接入授权")
    ap.add_argument("--home", default="./data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    g = sub.add_parser("grant")
    g.add_argument("--persona", required=True)
    g.add_argument("--owner", default="u-owner")
    g.add_argument("--days", type=int, default=30)
    c = sub.add_parser("check")
    c.add_argument("--persona", required=True)
    r = sub.add_parser("revoke")
    r.add_argument("--persona", required=True)
    a = ap.parse_args()

    if a.cmd == "init":
        print(json.dumps(ensure_device(a.home), ensure_ascii=False))
    elif a.cmd == "grant":
        path, body = issue_grant(a.home, a.persona, a.owner, a.days)
        print("granted:", path)
    elif a.cmd == "check":
        ok, reason = check_grant(a.home, a.persona)
        print(("OK " if ok else "DENIED ") + reason)
        sys.exit(0 if ok else 3)
    elif a.cmd == "revoke":
        print("revoked" if revoke(a.home, a.persona) else "no such grant")


if __name__ == "__main__":
    main()
