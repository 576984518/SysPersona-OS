# -*- coding: utf-8 -*-
"""多模型评审（任务 B）：让另一个免费模型独立总结同一份材料，再和我这份对比，只报差异。

为什么：单模型的总结会漏东西，换一个模型独立总结，再机器比一比，
比"再问一遍同一个模型"更能看出遗漏。对比**不用模型**，只用集合差，省一次调用也更可靠。

纪律：
  - 模型只从 ops/sf-free.json 的 free_ids 里挑，**名单为空就 --local 降级，绝不硬调外部**；
  - 下架模型（deprecated）排最后，只有没有别的免费模型时才用；
  - 只调免费模型，**失败绝不换收费模型兜底**；
  - 超时 60 秒；只对 5xx/超时重试 1 次；4xx 直接失败；
  - key 只从环境变量 SILICONFLOW_API_KEY 取（进程里没有就读用户环境变量），
    不写文件、不打印；错误信息里也不带 key；
  - 评审器**只读缓存**，绝不为了评审去刷新模型页（刷新是 persona_sf_models.py 的事）。

输出：
  ops/review.jsonl   每次一条 {at, model, file, ok, secs, chars}
  ops/review-last.json {at, model, theirs, mine, diff:{only_theirs, only_mine, numbers_mismatch}}

对比规则：抽出「数字」和「专有名词」两个集合做差，再对两边都出现的名词比对邻近数字。
  - 数字：\\d[\\d,\\.]*[KMG%]?
  - 专有名词：ASCII 标识符（模型名/文件名/路径/版本号）+ 中文书名号/引号里的词
  - 不比较语义、不比较措辞，只看"对方有我无 / 我有对方无 / 同名不同数"

CLI：--file <md> [--mine <md>] [--local]
"""
import argparse
import io
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
CACHE = os.path.join(OPS, "sf-free.json")
REVIEW_LOG = os.path.join(OPS, "review.jsonl")
REVIEW_LAST = os.path.join(OPS, "review-last.json")
API = "https://api.siliconflow.cn/v1/chat/completions"
TIMEOUT = 60          # 单次请求上限：60 秒不回就放弃
MAX_INPUT = 20000     # 喂进去的字符上限（7B 模型 32K 上下文，留余量）
MAX_TOKENS = 800

NUM_RE = re.compile(r"\d[\d,\.]*[KMG%]?")
NOUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-./]{2,}")
QUOTED_RE = re.compile(r"[「『《“\"]([^」』》”\"]{2,20})[」』》”\"]")
MSG_RE = re.compile(r"(?ms)^## (用户|助手)\s*$\s*(.*?)(?=^## |\Z)")

STOP = {
    "the", "and", "for", "with", "that", "this", "from", "you", "are", "not", "but",
    "can", "will", "have", "has", "was", "were", "one", "two", "all", "any", "our",
    "out", "use", "use", "new", "see", "per", "via", "http", "https", "www", "com",
    "md", "json", "py", "js", "txt", "html", "true", "false", "null", "none",
}

NON_CHAT = ("image", "tts", "audio", "video", "embedding", "rerank", "whisper",
            "diffusion", "flux", "kolors", "ocr", "sd3", "bge", "m3e")


def _iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _log(rec):
    os.makedirs(OPS, exist_ok=True)
    with io.open(REVIEW_LOG, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read(path):
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def get_key():
    """只从环境取，绝不落盘；进程环境没有就读用户环境变量。"""
    k = (os.environ.get("SILICONFLOW_API_KEY") or "").strip()
    if k:
        return k
    try:
        import winreg
    except ImportError:
        return ""
    for hive, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                       (winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, path) as key:
                v, _ = winreg.QueryValueEx(key, "SILICONFLOW_API_KEY")
                if v:
                    return v.strip()
        except OSError:
            continue
    return ""


def load_free():
    try:
        with io.open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def choose_model(data):
    """按优先级挑一个轻量免费对话模型；下架模型垫底。"""
    if not data:
        return ""
    meta = {m.get("id"): m for m in data.get("models") or []}
    scored = []
    for mid in data.get("free_ids") or []:
        low = mid.lower()
        if any(x in low for x in NON_CHAT):
            continue
        score = 0
        if "instruct" in low:
            score += 4
        if "flash" in low:
            score += 4
        if re.search(r"(?<![0-9])(8b|9b)(?![0-9])", low):
            score += 4
        if "chat" in low:
            score += 1
        if re.search(r"(?<![0-9])(7b|6b|4b|3b|2b|1\.5b)(?![0-9])", low):
            score += 2
        for heavy in ("pro", "max", "235b", "397b", "480b", "122b", "110b", "72b", "32b", "30b", "20b"):
            if heavy in low:
                score -= 3
        if (meta.get(mid) or {}).get("deprecated"):
            score -= 100
        scored.append((score, mid))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return scored[0][1]


def build_prompt(material):
    if len(material) > MAX_INPUT:
        head = material[:MAX_INPUT - 4000]
        tail = material[-4000:]
        material = (head + "\n\n……（中间省略 %d 字）……\n\n" % (len(material) - MAX_INPUT) + tail)
    return (
        "你是独立评审员。下面是一份会话/抓取材料。请用中文写一份要点摘要，供另一个人和已有总结做对照：\n"
        "- 写 8-12 条，每条一行，以「- 」开头；\n"
        "- 必须保留材料里出现的所有数字、模型名、文件名、路径、日期、版本号；\n"
        "- 只写材料里有的，不要补充常识、不要寒暄、不要复述标题。\n\n"
        "材料：\n" + material
    )


def call_api(model, prompt, key):
    """一次调用（内部最多重试 1 次，只对 5xx/超时）。返回 (ok, text_or_reason, secs, attempts)。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
    }
    payload = json.dumps(body).encode("utf-8")
    started = time.time()
    reason = ""
    for attempt in (1, 2):
        req = urllib.request.Request(
            API, data=payload, method="POST",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            obj = json.loads(raw.decode("utf-8", "replace"))
            msg = ((obj.get("choices") or [{}])[0].get("message") or {})
            text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            if not text:
                return False, "模型回了个空内容", round(time.time() - started, 1), attempt
            return True, text, round(time.time() - started, 1), attempt
        except urllib.error.HTTPError as e:
            snippet = (e.read() or b"")[:300].decode("utf-8", "replace").replace("\n", " ")
            reason = "HTTP %d：%s" % (e.code, snippet)
            if e.code >= 500 and attempt == 1:
                continue
            return False, reason, round(time.time() - started, 1), attempt
        except (socket.timeout, TimeoutError):
            reason = "超时（%d 秒）" % TIMEOUT
            if attempt == 1:
                continue
            return False, reason, round(time.time() - started, 1), attempt
        except urllib.error.URLError as e:
            reason = "网络错误：%s" % (getattr(e, "reason", e),)
            if attempt == 1 and "timed out" in str(getattr(e, "reason", "")).lower():
                continue
            return False, reason, round(time.time() - started, 1), attempt
        except ValueError:
            return False, "返回不是 JSON", round(time.time() - started, 1), attempt
    return False, reason or "未知错误", round(time.time() - started, 1), 2


def norm_num(s):
    return s.strip().rstrip(".,").upper().replace(",", "")


def nums(text):
    return {norm_num(m) for m in NUM_RE.findall(text) if norm_num(m)}


def nouns(text):
    out = set()
    for t in NOUN_RE.findall(text):
        if t.lower() in STOP:
            continue
        out.add(t)
    for t in QUOTED_RE.findall(text):
        t = t.strip()
        if len(t) >= 2:
            out.add(t)
    return out


def nearest_number(text, term):
    """取离名词最近的数字：优先名词之后 60 字内，其次之前 20 字内；名词自带的数字算 0 距离。"""
    idx = text.find(term)
    if idx < 0:
        return ""
    best = None  # (距离, 数字)
    for m in NUM_RE.finditer(text):
        n = norm_num(m.group(0))
        if not n:
            continue
        if m.start() >= idx + len(term):
            dist = m.start() - (idx + len(term))
            if dist > 60:
                continue
        elif m.end() <= idx:
            dist = (idx - m.end()) + 1000
            if dist > 1020:
                continue
        else:
            dist = 0
        if best is None or dist < best[0]:
            best = (dist, n)
    return best[1] if best else ""


def diff(theirs, mine):
    tn, mn = nums(theirs), nums(mine)
    tno, mno = nouns(theirs), nouns(mine)
    only_theirs = sorted(tno - mno)
    only_mine = sorted(mno - tno)
    mismatch = []
    for term in sorted(tno & mno):
        a = nearest_number(theirs, term)
        b = nearest_number(mine, term)
        if a and b and a != b:
            mismatch.append({"term": term, "theirs": a[:60], "mine": b[:60]})
    return {"only_theirs": only_theirs[:80],
            "only_mine": only_mine[:80],
            "numbers_mismatch": mismatch[:40]}


def local_report(path, material, why):
    print("本次未调用外部模型（%s）。" % why)
    users = []
    for m in MSG_RE.finditer(material):
        if m.group(1) != "用户":
            continue
        body = re.sub(r"(?m)^>.*$\n?", "", m.group(2)).strip()
        if body:
            users.append(body)
    users.sort(key=len, reverse=True)
    print("待人工比对要点清单（文件里 %d 条用户消息，取最长的 5 条）：" % len(users))
    print("文件：%s" % path)
    if not users:
        print("- 没解析到「## 用户」小节，确认一下文件格式")
        return 0
    for i, body in enumerate(users[:5], 1):
        head = body.splitlines()[0][:100]
        ns = sorted(nums(body))[:12]
        print("%d. [%d 字] %s" % (i, len(body), head))
        print("   数字：%s" % ("、".join(ns) if ns else "（没有数字）"))
    print("回头对着这几条看：对方的总结有没有漏掉其中的数字或结论。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="多模型评审：另一个免费模型独立总结 + 机器比对")
    ap.add_argument("--file", required=True, help="要评审的 md")
    ap.add_argument("--mine", default="", help="我这边已有的总结 md（可省略）")
    ap.add_argument("--local", action="store_true", help="不调外部，只出人工比对清单")
    a = ap.parse_args()

    if not os.path.isfile(a.file):
        print("找不到文件：%s" % a.file)
        return 2
    material = _read(a.file)

    data = load_free()
    model = choose_model(data) if data else ""
    why = "指定了 --local" if a.local else ("免费名单为空或没缓存" if not model else "")

    if a.local or not model:
        _log({"at": _iso(), "model": "local", "file": a.file, "ok": False,
              "secs": 0, "chars": 0, "reason": why})
        return local_report(a.file, material, why)

    key = get_key()
    if not key:
        print("环境变量 SILICONFLOW_API_KEY 没读到，不调外部。")
        return 1

    print("模型：%s（来自 ops/sf-free.json 的 free_ids）" % model)
    prompt = build_prompt(material)
    ok, text, secs, attempts = call_api(model, prompt, key)
    _log({"at": _iso(), "model": model, "file": a.file, "ok": ok,
          "secs": secs, "chars": len(text) if ok else 0,
          "reason": "" if ok else text, "attempts": attempts})
    if not ok:
        print("调用失败（%.1f 秒，试了 %d 次）：%s" % (secs, attempts, text))
        print("不换收费模型兜底；本次到此为止。")
        return 1

    print("对方总结：%d 字，耗时 %.1f 秒（试了 %d 次）" % (len(text), secs, attempts))

    mine = ""
    mine_note = ""
    if a.mine:
        if os.path.isfile(a.mine):
            mine = _read(a.mine)
        else:
            mine_note = "找不到 --mine 文件：%s，未做差异比对" % a.mine

    if not mine:
        if not mine_note:
            mine_note = "没给 --mine，本次只留对方总结、不做差异比对"
        d = {"only_theirs": [], "only_mine": [], "numbers_mismatch": []}
    else:
        d = diff(text, mine)

    payload = {"at": _iso(), "model": model, "theirs": text, "mine": mine, "diff": d}
    if mine_note:
        payload["note"] = mine_note
    os.makedirs(OPS, exist_ok=True)
    _write_atomic(REVIEW_LAST, json.dumps(payload, ensure_ascii=False, indent=1))

    print("== 差异（只报不同） ==")
    if mine_note:
        print(mine_note)
    else:
        if d["only_theirs"]:
            print("对方有我无：%s" % "、".join(d["only_theirs"]))
        if d["only_mine"]:
            print("我有对方无：%s" % "、".join(d["only_mine"]))
        if d["numbers_mismatch"]:
            for x in d["numbers_mismatch"]:
                print("数字不一致：%s  对方=%s  我=%s" % (x["term"], x["theirs"], x["mine"]))
        if not (d["only_theirs"] or d["only_mine"] or d["numbers_mismatch"]):
            print("机器可对齐的数字/名词完全一致（措辞差异不在比对范围内）。")
    print("明细：ops/review-last.json；流水：ops/review.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
