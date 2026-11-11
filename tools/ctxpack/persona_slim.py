# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_slim.py — 滚动上下文头（抽取式，不调用模型）
r"""persona_slim.py — 滚动上下文头（抽取式，不调用模型）

从最新归档里压出一份 ≤2.5KB 的 CONTEXT.md：
  最近 N 条原文截断 + 更早的每条首句 + 事实锚点（版本号/条数/路径/日期）

用法：
  python persona_slim.py                 # 用今天最新的归档
  python persona_slim.py --file x.md     # 指定归档
  python persona_slim.py --recent 6      # 最近几条保留全文

环境变量：SYSPERSONA_ROOT 指记忆库根（旧名 PERSONA_ROOT 也认），产出写到
`<根>\memory\CONTEXT.md`；不设就按脚本所在目录推导，推不出来直接抛错。
"""
import argparse, glob, json, os, re, time, urllib.request

from persona_root import root as _persona_root

ROOT = _persona_root()
OUTDIR = os.path.join(ROOT, "memory")
STATE = os.path.join(OUTDIR, ".persona_slim.state.json")
HERDSMAN = "http://127.0.0.1:8080/v1"
SUMMARY_MODEL = "Qwen3.5_4b"
RECENT = 4
FULL_CAP = 200      # 最近条目的正文上限
HEAD_CAP = 55       # 更早条目的首句上限
TOTAL_CAP = 950

def parse(path):
    t = open(path, encoding="utf-8", errors="replace").read()
    parts = re.split(r"^## (用户|助手)$", t, flags=re.M)
    secs = [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]
    return t, secs

def body_of(s):
    return re.sub(r"(?:\s*-{2,}\s*)+$", "", re.sub(r"^> 思考：.*?\n\n", "", s, flags=re.S).strip()).strip()

def one_line(s, cap):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= cap else s[:cap].rstrip() + "…"

def summarize(secs, n=10, timeout=240):
    """用本地 Herdsman 的小模型把最后几条压成要点（失败就返回空）。"""
    tail = secs[-n:]
    convo = "\n".join("%s：%s" % (r, re.sub(r"\s+", " ", body_of(s))[:400]) for r, s in tail)
    prompt = ("下面是一段对话的最后几条。用不超过 5 条要点，写成「正在做什么 / 已定的事 / 下一步」。"
              "只写对话里明确出现的事实，不要评价、不要推测；没有的就不写，不要编。\n\n" + convo)
    body = json.dumps({"model": SUMMARY_MODEL,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 320, "temperature": 0.2}).encode("utf-8")
    req = urllib.request.Request(HERDSMAN + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    return (d["choices"][0]["message"].get("content") or "").strip()

def latest_archive():
    files = glob.glob(os.path.join(ROOT, "archive", "**", "*.md"), recursive=True)
    files = [f for f in files if not f.endswith(".gz")]
    return max(files, key=os.path.getmtime) if files else None

def latest_live():
    """正在进行的会话：取最近 12 小时内、体积最大的那份（避开刚落的空壳抓取文件）。"""
    cut = time.time() - 12 * 3600
    files = [f for f in glob.glob(os.path.join(ROOT, "sessions", "**", "*.md"), recursive=True)
             if not os.path.basename(f).startswith("capture-") and os.path.getmtime(f) > cut]
    return max(files, key=os.path.getsize) if files else latest_archive()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--recent", type=int, default=RECENT)
    ap.add_argument("--live", action="store_true", help="用最新会话（未归档）而不是归档")
    ap.add_argument("--out", default="", help="输出路径，默认 memory/CONTEXT.md")
    ap.add_argument("--summarize", action="store_true", help="消息数变了就让本地 4B 生成要点")
    a = ap.parse_args()

    path = a.file or (latest_live() if a.live else latest_archive())
    if not path or not os.path.exists(path):
        raise SystemExit("找不到源文件")
    # --live 时若会话文件超过 3 小时没动，退回归档（免得拿着旧稿当现状）
    if a.live and not a.file and (time.time() - os.path.getmtime(path)) > 3 * 3600:
        path = latest_archive() or path

    raw, secs = parse(path)
    if not secs:
        raise SystemExit("归档里没有可解析的消息")

    recent = secs[-a.recent:]
    older = secs[:-a.recent]

    facts = set()
    for pat in (r"v\d+\.\d+\.\d+", r"\d{3,4} 条", r"[A-Z]:\\\\[^\s，。；]+", r"20\d{2}-\d{2}-\d{2}"):
        for m in re.finditer(pat, raw):
            facts.add(m.group(0)[:60])
    versions = sorted(set(re.findall(r"v\d+\.\d+\.\d+", raw)), key=lambda v: raw.rfind(v))
    paths = [f for f in facts if "hermes-mem" in f or re.match(r"^[A-Z]:", f)]

    lines = []
    lines.append("# CONTEXT（滚动头 · 自动生成 %s）" % time.strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    lines.append("> 抽取式压缩，不含推测。要细节去查 archive/ 或 persona_search；本文件只回答「现在在做什么」。")
    lines.append("")
    lines.append("- 来源：`%s`" % os.path.relpath(path, ROOT).replace("\\", "/"))
    lines.append("- 总条数：%d" % len(secs))
    lines.append("")

    if a.summarize:
        prev = 0
        try:
            prev = json.load(open(STATE, encoding="utf-8")).get("n", 0)
        except Exception:
            prev = 0
        if len(secs) != prev:
            try:
                s = summarize(secs)
                if s:
                    lines.append("## 最新摘要（本地 4B 生成，可能不准；以本页原文为准）")
                    lines.append("")
                    for ln in s.splitlines():
                        if ln.strip():
                            lines.append(ln.strip())
                    lines.append("")
                    json.dump({"n": len(secs), "at": time.time()},
                              open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
                    print("摘要已更新（%d 条）" % len(secs))
                else:
                    print("摘要为空，跳过")
            except Exception as e:
                print("摘要失败（跳过）：%s" % str(e)[:80])

    lines.append("## 最近 %d 条（截断原文）" % len(recent))
    lines.append("")
    for role, s in recent:
        lines.append("- **%s**：%s" % (role, one_line(body_of(s), FULL_CAP)))
    lines.append("")

    if older:
        tail_older = older[-12:]
        lines.append("## 更早（最近 %d 条的首句，共 %d 条）" % (len(tail_older), len(older)))
        lines.append("")
        for role, s in tail_older:
            lines.append("- %s：%s" % (role, one_line(body_of(s), HEAD_CAP)))

    text = "\n".join(lines)
    if len(text) > TOTAL_CAP:
        text = text[:TOTAL_CAP].rstrip() + "\n\n…（已截断，见 archive/）\n"

    os.makedirs(OUTDIR, exist_ok=True)
    out = a.out or os.path.join(OUTDIR, "CONTEXT.md")
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(text + "\n")
    print("已写 %s（%d 字节 / 源 %d 条）" % (out, len(text.encode("utf-8")), len(secs)))

if __name__ == "__main__":
    main()
