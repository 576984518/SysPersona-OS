#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中间层自身开销测量（不含模型生成）。运行：python bench.py"""

import json
import os
import statistics
import sys
import tempfile
import threading
import time
import urllib.request

from launcher import serve


def main(n=30):
    root = tempfile.mkdtemp(prefix="sp-bench-")
    httpd, L = serve(os.path.join(root, "a"), port=0, provider="mock", num_ctx=4096)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    hist = []
    totals, retrieves = [], []
    for i in range(n):
        hist.append({"role": "user", "content": "第 %d 轮：J1900 的热重启和 NAS 休眠脚本怎么处理" % i})
        body = json.dumps({"model": "mock-a", "messages": hist, "max_tokens": 256,
                           "stream": False}, ensure_ascii=False).encode()
        req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            timing = json.loads(r.headers.get("X-SP-Timing") or "{}")
        totals.append((time.perf_counter() - t0) * 1000)
        retrieves.append(timing.get("retrieve_ms", 0))
        hist.append({"role": "assistant", "content": data["choices"][0]["message"]["content"]})

    t0 = time.perf_counter()
    req = urllib.request.Request(base + "/syspersona/sleep", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        sleep_ms = (time.perf_counter() - t0) * 1000
        sleep_res = json.loads(r.read())

    def p(v, q):
        return sorted(v)[max(0, int(len(v) * q) - 1)]

    print("requests:      %d" % n)
    print("total ms:      mean %.1f  p50 %.1f  p95 %.1f" %
          (statistics.mean(totals), p(totals, 0.5), p(totals, 0.95)))
    print("retrieve ms:   mean %.2f  p95 %.2f" % (statistics.mean(retrieves), p(retrieves, 0.95)))
    print("sleep ms:      %.1f  (consolidated=%s, snapshot=%s bytes)" %
          (sleep_ms, sleep_res.get("consolidated"), sleep_res.get("snapshot_bytes")))
    print("episodes:      %d" % L.mem.count())
    print("cache ratio:   %.2f" % L.last_ratio)
    httpd.shutdown()
    L.mem.close()
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
