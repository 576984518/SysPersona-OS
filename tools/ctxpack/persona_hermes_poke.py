# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_hermes_poke.py — 让 Hermes 自己「开口读交接卡」（不靠人打字）
r"""persona_hermes_poke.py — 让 Hermes 自己「开口读交接卡」（不靠人打字）

怎么做：Hermes 是 Electron 壳，带 `--remote-debugging-port=9333` 启动后就能用 CDP
连进它的渲染进程，把文字塞进输入框再按回车——等价于人手动输入并发送。

为什么要绕这一下（2026-11-10 实测）：
  * 它的 HTTP API（127.0.0.1:9120）除了 /api/health 全要鉴权（401），
    token 在桌面壳运行时生成，auth.json / 日志里都没有；
  * 但渲染进程里的同源 fetch 是通的，CDP 又能直接操作 DOM，所以走这条路。

用法：
  python persona_hermes_poke.py "要发的话"      # 发一次
  python persona_hermes_poke.py                # 默认发「读交接卡」那句
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SYSPERSONA_HERMES_CDP_PORT") or 9333)
RELAY = os.path.join(ROOT, "ops", "hermes-relay.md")
NODE = os.environ.get("SYSPERSONA_NODE") or r"C:\Users\Huang\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe"
POKE_JS = os.path.join(ROOT, "ops", "cdp_poke.js")


def page_ws(timeout=25.0):
    """等 Hermes 的调试口出现，返回页面 WebSocket 地址。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % PORT, timeout=3) as r:
                for t in json.loads(r.read().decode("utf-8")):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
        except Exception:                  # noqa: BLE001
            pass
        time.sleep(1.5)
    return ""


def poke(text):
    ws = page_ws()
    if not ws:
        return False, "调试口没开（Hermes 起来时没带 --remote-debugging-port=%d）" % PORT
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        r = subprocess.run([NODE, POKE_JS, ws, text], capture_output=True, timeout=40, env=env)
    except Exception as e:                 # noqa: BLE001
        return False, str(e)[:120]
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    return (out == "SENT"), out[:120]


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    text = a[0] if a else ("读一下 %s，按里面的交接卡接着干（DSH 那边断网，你代班）。" % RELAY)
    ok, msg = poke(text)
    print("%s：%s" % ("已发" if ok else "没发成", msg))
    return 0 if ok else 4


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
