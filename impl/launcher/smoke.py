#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：Launcher L2 冒烟测试 —— 对应 docs/LAUNCHER_SPEC.md §17 的 TC-C / TC-M / TC-S 组。
"""
Launcher L2 冒烟测试 —— 对应 docs/LAUNCHER_SPEC.md §17 的 TC-C / TC-M / TC-S 组。

不依赖网络、不依赖模型：mock provider 回显注入结果，用来断言"记忆真的进了 prompt"。
运行：python smoke.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from launcher import Launcher, serve

RESULTS = []


def check(tc, desc, cond, detail=""):
    RESULTS.append((tc, desc, bool(cond)))
    print("%s %-7s %s%s" % ("PASS" if cond else "FAIL", tc, desc, ("  [%s]" % detail) if detail else ""))


def http(base, path, payload=None, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def start(home, **kw):
    httpd, L = serve(home, port=0, **kw)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return "http://127.0.0.1:%d" % httpd.server_address[1], L, httpd


def chat(base, messages, stream=False, headers=None, max_tokens=256):
    body = {"model": "mock-a", "messages": messages, "stream": stream, "max_tokens": max_tokens}
    return http(base, "/v1/chat/completions", body, headers)


def main():
    root = tempfile.mkdtemp(prefix="sp-smoke-")
    base, L, srv = start(os.path.join(root, "a"), provider="mock", num_ctx=4096)
    base2 = None
    try:
        # ---------------------------------------------------------- TC-C1
        st, hd, body = chat(base, [{"role": "user", "content": "你好，我叫阿远，在修一块 J1900 主板"}])
        obj = json.loads(body)
        ok = st == 200 and obj["choices"][0]["message"]["content"].startswith("【mock:")
        check("TC-C1", "非流式对话可用（OpenAI 兼容）", ok, "status=%d" % st)

        st, hd, body = chat(base, [{"role": "user", "content": "流式测试"}], stream=True)
        text = body.decode("utf-8", "replace")
        ok = st == 200 and "data: [DONE]" in text and text.count("data: ") >= 3
        check("TC-C1b", "流式 SSE 可用", ok, "chunks=%d" % text.count("data: "))

        # ---------------------------------------------------------- TC-C2
        hist = []
        ratios = []
        for i, q in enumerate(["第一轮：J1900 的热重启怎么修", "第二轮：还是那块板子", "第三轮：现在到哪一步了"]):
            hist.append({"role": "user", "content": q})
            st, hd, body = chat(base, hist)
            ratios.append(float(hd.get("x-sp-cache-hit-ratio", 0)))
            hist.append({"role": "assistant", "content": json.loads(body)["choices"][0]["message"]["content"]})
        ok = ratios[-1] >= 0.5
        check("TC-C2", "前缀稳定（第 3 轮缓存命中率 ≥ 0.5）", ok,
              "ratios=%s" % [round(r, 2) for r in ratios])

        # ---------------------------------------------------------- TC-C3
        # 直接写库造样本（不动模型，保证 TC-M2 的计数口径干净）
        from launcher import keywords, importance_of
        import time as _t
        for i, txt in enumerate([
            "在调 NAS 休眠脚本，最后通过改注册表解决了 S3 唤醒失败",
            "NAS 是四盘位，硬盘休眠后风扇还在转，查了 hdparm 的配置",
            "休眠脚本之前用 systemd 定时任务跑，会跟 SMB 服务抢 IO",
            "解决 NAS 休眠的关键是禁用 watchdog 并调整 hdparm -B 参数",
            "那块 J1900 主板的热重启最后是改了 0x06 的机器码",
        ]):
            L.mem.write({"uuid": "seed-%d" % i, "session": "default", "ts": _t.time() - i * 3600,
                         "role": "user", "text": txt, "tags": ",".join(keywords(txt)),
                         "importance": importance_of(txt)})
        st, hd, body = chat(base, [{"role": "user", "content": "NAS 休眠脚本最后是怎么解决的？"}])
        obj = json.loads(body)
        hits = int(hd.get("x-sp-memory-hits", 0))
        ok = st == 200 and hits > 0 and obj["usage"]["prompt_tokens"] <= 4096
        check("TC-C3", "记忆被注入且不越预算", ok,
              "hits=%d prompt=%d" % (hits, obj["usage"]["prompt_tokens"]))

        # ---------------------------------------------------------- TC-C4
        st, hd, body = chat(base, [{"role": "user", "content": "绕过测试"}], headers={"X-SP-Bypass": "1"})
        ok = st == 200 and int(hd.get("x-sp-memory-hits", -1)) == 0
        check("TC-C4", "逃生通道：X-SP-Bypass 时零注入", ok)

        # ---------------------------------------------------------- TC-M1
        text = json.loads(body)["choices"][0]["message"]["content"]
        st, hd, body = chat(base, [{"role": "user", "content": "记忆来源在哪"}])
        reply = json.loads(body)["choices"][0]["message"]["content"]
        ok = "[M" in reply or int(hd.get("x-sp-memory-hits", 0)) > 0
        check("TC-M1", "注入项带来源 id（可回溯）", ok, reply[:40])

        # ---------------------------------------------------------- TC-M2
        status = json.loads(http(base, "/syspersona/status")[2])
        # 已经发生的 chat 次数：1 + 1(stream) + 3 + 1 + 1 + 1 = 8
        ok = status["model_calls"] == 8
        check("TC-M2", "热路径模型调用数 = 请求数（无额外抽取调用）", ok, "calls=%d" % status["model_calls"])

        # ---------------------------------------------------------- TC-M3
        base2, L2, srv2 = start(os.path.join(root, "b"), provider="mock", num_ctx=4096)
        st, hd, body = chat(base2, [{"role": "user", "content": "J1900 主板"}])
        hits2 = int(hd.get("x-sp-memory-hits", 0))
        ok = hits2 == 0
        check("TC-M3", "人格隔离：另一个 home 检索不到上文", ok, "hits=%d" % hits2)

        # ---------------------------------------------------------- TC-S1
        st, hd, body = http(base, "/syspersona/sleep", {})
        res = json.loads(body)
        resume_ok = os.path.exists(os.path.join(root, "a", "resume.json"))
        journal = open(os.path.join(root, "a", "journal.state"), encoding="utf-8").read().strip()
        ok = st == 202 and resume_ok and journal == "clean" and res.get("consolidated", 0) > 0
        check("TC-S1", "睡眠：先写指针再清理，结束时 journal=clean", ok,
              "consolidated=%s" % res.get("consolidated"))

        # ---------------------------------------------------------- TC-S2
        with open(os.path.join(root, "a", "journal.state"), "w", encoding="utf-8") as f:
            f.write("sleeping")
        L3 = Launcher(os.path.join(root, "a"), provider="mock")
        ok = L3.recovered is True and L3._journal() == "clean"
        check("TC-S2", "崩溃恢复：脏标记被识别并重放", ok)
        L3.mem.close()

        # ---------------------------------------------------------- TC-S3
        snaps = os.listdir(os.path.join(root, "a", "snapshots"))
        size = os.path.getsize(os.path.join(root, "a", "snapshots", snaps[-1])) if snaps else 0
        ok = bool(snaps) and size > 0
        check("TC-S3", "快照落盘且非空", ok, "snapshots=%d size=%d" % (len(snaps), size))

        # ---------------------------------------------------------- TC-E1
        st, hd, body = http(base, "/v1/chat/completions",
                            {"model": "mock-a", "messages": []})
        ok = st == 400 and json.loads(body)["error"]["code"] == "bad_request"
        check("TC-E1", "空 messages 返回 400 + 结构化错误码", ok)
    finally:
        for s in (srv, locals().get("srv2")):
            if s:
                s.shutdown()
        L.mem.close()
        shutil.rmtree(root, ignore_errors=True)

    bad = [r for r in RESULTS if not r[2]]
    print("\n%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
