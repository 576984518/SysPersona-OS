# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_crosscheck.py — 交叉核对：两个模型各自总结，只列差异，不判对错
r"""persona_crosscheck.py — 交叉核对：两个模型各自总结，只列差异，不判对错

背景（2026-11-09 定）：单模型总结会漏信息，而且漏掉的会被当成「没有」。
做法：A 模型出第一份要点，B 模型（换一个）独立出一份，然后比对清单：
  - 共有：两份都有
  - 仅 A / 仅 B：只有一方提到 —— 这就是「可能漏掉」的地方
**不仲裁**：差异大也两个都入库，第三方只做补充。

用法：
  python persona_crosscheck.py <会话文件.md> [--tail 24000] [--model-b Qwen/Qwen3-8B-Instruct]

输出：
  ops/crosscheck-<时间>.md   —— 两份要点 + 差异清单
  ops/crosscheck.jsonl       —— 每条一行，只追加
"""
import argparse
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）
import difflib
import io
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = _loc.hermes_home()
API = "https://api.siliconflow.cn/v1/chat/completions"
MODEL_A = "deepseek-ai/DeepSeek-V4-Flash"
MODEL_B = "Qwen/Qwen3-8B-Instruct"

PROMPT = """把下面这段会话整理成要点清单，逐条列，每行一条，以「- 」开头。
要求：只写会话里确实出现的内容；数字、结论、待办、决定必须保留；不要润色成文章。
最多 25 条。不要输出任何解释或标题。

=== 会话 ===
%s
"""


def find_key():
    k = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if k:
        return k
    envf = os.path.join(HOME, ".env")
    if os.path.exists(envf):
        for line in io.open(envf, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*SILICONFLOW_API_KEY\s*=\s*(\S+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    cf = os.path.join(HOME, "config.yaml")
    if os.path.exists(cf):
        for line in io.open(cf, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*api_key:\s*(sk-\S+)", line)
            if m:
                return m.group(1).strip()
    return None


def call(model, text, key, timeout=180):
    body = {"model": model,
            "messages": [{"role": "user", "content": PROMPT % text}],
            "max_tokens": 1600, "temperature": 0.3}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    msg = out["choices"][0]["message"]["content"]
    return msg, out.get("usage") or {}


def bullets(text):
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        s = re.sub(r"^[-*•]\s*", "", s)
        s = re.sub(r"^\d+[.、)]\s*", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) >= 6:
            out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--tail", type=int, default=24000)
    ap.add_argument("--model-b", default=MODEL_B)
    a = ap.parse_args()

    key = find_key()
    if not key:
        raise SystemExit("找不到 SiliconFlow key")

    raw = io.open(a.file, encoding="utf-8", errors="replace").read()
    text = raw[-a.tail:] if len(raw) > a.tail else raw

    t0 = time.time()
    sa, ua = call(MODEL_A, text, key)
    sb, ub = call(a.model_b, text, key)
    cost_s = "%.1fs" % (time.time() - t0)

    ba, bb = bullets(sa), bullets(sb)
    only_a, only_b, shared = [], [], []
    used_b = set()
    for x in ba:
        best, bi = 0.0, -1
        for i, y in enumerate(bb):
            if i in used_b:
                continue
            r = difflib.SequenceMatcher(None, x, y).ratio()
            if r > best:
                best, bi = r, i
        if best >= 0.55:
            shared.append((x, bb[bi]))
            used_b.add(bi)
        else:
            only_a.append(x)
    for i, y in enumerate(bb):
        if i not in used_b:
            only_b.append(y)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(ROOT, "ops", "crosscheck-%s.md" % stamp)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    lines = [
        "# 交叉核对（%s）" % time.strftime("%Y-%m-%d %H:%M:%S"), "",
        "- 源：`%s`（取尾部 %d 字）" % (a.file, len(text)),
        "- A：%s（%d 条，%.0f tok）" % (MODEL_A, len(ba), ua.get("total_tokens", 0)),
        "- B：%s（%d 条，%.0f tok）" % (a.model_b, len(bb), ub.get("total_tokens", 0)),
        "- 耗时：%s" % cost_s, "",
        "## 差异（只报不同，不判对错）", "",
        "### 仅 A 提到（%d）" % len(only_a), "",
    ]
    lines += ["- " + x for x in only_a] or ["（无）"]
    lines += ["", "### 仅 B 提到（%d）" % len(only_b), ""]
    lines += ["- " + x for x in only_b] or ["（无）"]
    lines += ["", "## 两份原文", "", "### A · " + MODEL_A, "", sa, "",
              "### B · " + a.model_b, "", sb, ""]
    io.open(out, "w", encoding="utf-8", newline="\n").write("\n".join(lines))

    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "src": a.file,
           "chars": len(text), "modelA": MODEL_A, "modelB": a.model_b,
           "itemsA": len(ba), "itemsB": len(bb), "shared": len(shared),
           "onlyA": len(only_a), "onlyB": len(only_b), "report": os.path.relpath(out, ROOT)}
    with io.open(os.path.join(ROOT, "ops", "crosscheck.jsonl"), "a",
                 encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
