# -*- coding: utf-8 -*-
"""漂移记录（v5.6）：性格面不建，用模型出厂性格；只记录，不修正。

他 2026-11-09 定：「按默认模型性格来吧，也就是什么都不做，只记录漂移。」

三条纪律：
1. **不建性格面**：没有 `persona.json`，人格 = 底座原生的 神格（PHILOSOPHY 〇）；
2. **不压制**：差异不判"跑偏"，不写回、不改权重、不加提示词；
3. **只记录**：把可数的变化写进 `ops/drift.jsonl`——平均句长、问句比例、决定词密度，
   与**它自己**过去的窗口比（不复用别人的基线，也不跨分身比）。

用法：
  python persona_drift.py scan [--days 7] [--avatar X]    # 跑一次，写记录
  python persona_drift.py show [--n 10]                    # 看最近记录
"""
import argparse
import io
import json
import os
import re
import sys
import time
from collections import defaultdict

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
OPS = os.path.join(ROOT, "ops")
STORE = os.path.join(OPS, "drift.jsonl")

DECIDE = ("决定", "必须", "不要", "以后都", "记住", "禁止", "优先", "定下来")
HEAD_DEV = re.compile(r"(?m)^-\s*(?:设备|来源)：\s*(.+?)\s*$")
HEAD_MOD = re.compile(r"(?m)^-\s*(?:底座|模型)：\s*(.+?)\s*$")
BLOCK = re.compile(r"(?ms)^## 助手\s*$\s*(.*?)(?=^## |\Z)")


def _avatar(head, src):
    try:
        import persona_avatar as av
        return av.from_head(head, src)[2]
    except Exception:
        return "unknown"


def _key(avatar, model=""):
    """漂移统计的身份键：模型为准（同模型不同壳算同一个性格）。"""
    try:
        import persona_avatar as av
        return av.persona_key("", model or avatar.split("-", 1)[-1])
    except Exception:
        return avatar


def _metrics(texts):
    if not texts:
        return None
    n = len(texts)
    chars = sum(len(t) for t in texts) / float(n)
    q = sum(1 for t in texts if "？" in t or "?" in t) / float(n)
    dec = sum(1 for t in texts if any(w in t for w in DECIDE)) / float(n)
    return {"n": n, "chars": round(chars, 1), "q": round(q, 3), "decide": round(dec, 3)}


BASELINE = os.path.join(OPS, "drift-baseline.json")


def _baseline():
    try:
        return json.load(io.open(BASELINE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_baseline(b):
    os.makedirs(OPS, exist_ok=True)
    tmp = BASELINE + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(b, ensure_ascii=False, indent=1))
    os.replace(tmp, BASELINE)


def _collect(days):
    """最近 N 天里，每个 avatar 的助手发言。"""
    now, since = time.time(), time.time() - days * 86400
    out = defaultdict(list)
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in fns:
            if not fn.endswith(".md"):
                continue
            p = os.path.join(dp, fn)
            try:
                if os.path.getmtime(p) < since:
                    continue
                text = io.open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"(?m)^\d{4}-\d{2}-\d{2}$", os.path.abspath(p))
            src = ""
            if m:
                parts = os.path.abspath(p).split(os.sep)
                i = parts.index(m.group(0))
                src = parts[i + 1] if len(parts) > i + 1 else ""
            head = text.partition("\n---\n")[0]
            who = _key(_avatar(head, src), "\n".join(head.splitlines()[:8]))
            out[who].extend(b.group(1).strip()[:2000] for b in BLOCK.finditer(text))
    return out


def scan(days=7, reset=False):
    """拿最近 N 天与**它自己的出厂基线**比；变化超 30% 就记一条（只记，不改）。"""
    cur = _collect(days)
    base = {} if reset else _baseline()
    rows, fresh = [], 0
    for who in sorted(cur):
        m_now = _metrics(cur[who])
        if not m_now:
            continue
        if who not in base:
            base[who] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "metrics": m_now, "note": "首次观测即出厂基线"}
            fresh += 1
            continue
        old = base[who]["metrics"]
        for key in ("chars", "q", "decide"):
            b, n = old.get(key) or 0.0, m_now[key] or 0.0
            if b == 0 and n == 0:
                continue                      # 两边都是 0：没有变化，别记 -100% 的假漂移
            if b == 0:
                rows.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "avatar": who,
                             "metric": key, "before": 0, "now": n, "delta": None,
                             "window": "%dd" % days, "verdict": "记录",
                             "note": "基线为 0，本次出现"})
                continue
            delta = (n - b) / b
            if abs(delta) >= 0.30:
                rows.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "avatar": who,
                             "metric": key, "before": old.get(key), "now": m_now[key],
                             "delta": round(delta, 2), "window": "%dd" % days,
                             "verdict": "记录", "note": "只记录，不修正"})
    _save_baseline(base)
    if rows:
        os.makedirs(OPS, exist_ok=True)
        with io.open(STORE, "a", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows, fresh


def show(n=10):
    if not os.path.exists(STORE):
        print("（无记录）")
        return 0
    lines = [json.loads(l) for l in io.open(STORE, encoding="utf-8") if l.strip()]
    for r in lines[-n:]:
        print("%s  %-26s %-7s %8s → %-8s %+5.0f%%" % (
            r["at"], r["avatar"][:26], r["metric"], r["before"], r["now"], r["delta"] * 100))
    print("—— 共 %d 条" % len(lines))
    return 0


def main():
    ap = argparse.ArgumentParser(description="漂移记录（只记不改）")
    ap.add_argument("cmd", nargs="?", default="scan", choices=["scan", "show"])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    if a.cmd == "show":
        return show(a.n)
    rows, fresh = scan(a.days, a.reset)
    print("本次记录 %d 条；新立基线 %d 个（写 %s）"
          % (len(rows), fresh, os.path.relpath(STORE, ROOT)))
    for r in rows[:10]:
        print("  %-24s %-6s %s → %s（%+.0f%%）" % (
            r["avatar"][:24], r["metric"], r["before"], r["now"], r["delta"] * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
