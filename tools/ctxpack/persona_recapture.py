# -*- coding: utf-8 -*-
r"""persona_recapture.py — 心跳驱动的随机重抓 + 本次增量摘要（B40）

为什么这么做：新内容不会自己走到待办里，必须有人在抓完之后把它翻译成任务。

分工（心跳只做判断，抓取永远在后台）：
  * `decide()` 只回答「这次要不要抓」，读状态、抽下一次的 3–10 分钟随机间隔；
    心跳每 30 秒问一次，它必须在一秒内返回，**绝不在这里抓**。
  * `run_once()` 复用 `persona_readnew` 的 CDP 路径（`force_reload` + `capture`），
    只负责：抓 → 判断「有没有真变」→ 写 `ops/recapture-summary.json` →
    往 `续接\待办备忘.md` 末尾追加一句值得跟进的话。

为什么不能只看「有没有新文件」：
  `collect7.js` 每次都会落一份新的 `capture-*.md`，页面没变也照样写。
  所以要比**尾部指纹**；同一个会话条数**大幅**变少（不到上次一半）时当没抓全，
  防止只挂载了一两条的残缺快照冒充新内容。少量抖动（虚拟列表挂载数不同）
  但尾部指纹变了，仍按新内容处理。

状态（`ops/recapture.json`，tmp→rename 原子写，失败也不丢）：
  {last_at 上次成功的 epoch, last_try 上次尝试的 epoch, next_gap 下次间隔,
   last_sig 上次尾部指纹, fails 连续失败, last_result, last_todo_sig}

用法：
  python persona_recapture.py --decide          # 只判断；exit 0=该抓 1=不该抓
  python persona_recapture.py --once            # 真抓一次（没到间隔会跳过）
  python persona_recapture.py --once --force    # 无视间隔，强抓
  python persona_recapture.py --status          # 看状态与上次摘要
"""
import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
import time

try:
    import persona_win as _pw  # noqa: F401  被 import 就生效：子进程不弹黑窗
except Exception:
    pass

ROOT = os.environ.get("PERSONA_ROOT") or os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
STATE = os.path.join(OPS, "recapture.json")
LOG = os.path.join(OPS, "recapture.jsonl")
ALERTS = os.path.join(OPS, "alerts.jsonl")
SUMMARY = os.path.join(OPS, "recapture-summary.json")
TODO = os.path.join(ROOT, "续接", "待办备忘.md")
LOCK = os.path.join(OPS, "recapture.lock")
ANCHOR = os.path.join(OPS, "anchor.txt")
SESSIONS = os.path.join(ROOT, "sessions")

MIN_GAP = 1200         # 20 分钟（2026-11-10 改：3–10 分钟太密，怕被站点风控）
MAX_GAP = 2700         # 45 分钟
MAX_BACKOFF = 3600     # 失败退避上限 1 小时
LOCK_STALE = 900       # 抓取任务自身超时 900 秒，超过就算锁烂了


# ── 小工具 ────────────────────────────────────────────────────────────

def say(msg):
    """pythonw 下没有 stdout，打印失败不能把心跳带崩。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def _iso(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if t is None else t))


def _stamp(t, now=None):
    if not t:
        return "从未"
    now = time.time() if now is None else now
    d = max(0, int(now - t))
    if d < 90:
        ago = "%d 秒前" % d
    elif d < 5400:
        ago = "%d 分钟前" % (d // 60)
    else:
        ago = "%.1f 小时前" % (d / 3600.0)
    return "%s（%s）" % (_iso(t), ago)


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def _read(path):
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def _rel(path):
    try:
        return os.path.relpath(path, ROOT).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


def _append_jsonl(path, rec):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _atomic_json(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ── 状态 ──────────────────────────────────────────────────────────────

def load_state():
    try:
        with io.open(STATE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st):
    """tmp→rename：写到一半断电也不会留下半个 JSON。"""
    st["updated"] = _iso()
    _atomic_json(STATE, st)


def _merge_state(**kw):
    st = load_state()
    st.update(kw)
    save_state(st)
    return st


def _gap(st):
    try:
        g = int(st.get("next_gap") or 0)
    except (TypeError, ValueError):
        g = 0
    if g < MIN_GAP or g > MAX_BACKOFF:
        # 没配过 / 被手改成非法值：给一个随机默认值，不写盘（读状态不该有副作用）
        g = random.randint(MIN_GAP, MAX_GAP)
    return g


def _arm_next():
    return random.randint(MIN_GAP, MAX_GAP)


# ── 抓取档与解析 ──────────────────────────────────────────────────────

def _capture_files():
    """只用 sessions 下的原始抓取档；archive 里的是副本，拿它当基线会重复。"""
    try:
        import persona_readnew as RN
        rows = RN.newest()
    except Exception:
        rows = []
    out = []
    prefix = SESSIONS + os.sep
    for t, p in rows:
        b = os.path.basename(p)
        if p.startswith(prefix) and b.startswith("capture-") and b.endswith(".md"):
            out.append((t, p))
    out.sort()
    return out


def _meta(path):
    """读 capture 文件头：会话 / 条数 / 尾部指纹。"""
    m = {"session": "", "count": 0, "fp": "", "at": ""}
    head = _read(path)[:1800]
    for line in head.splitlines():
        line = line.strip()
        if line.startswith("- 会话"):
            m["session"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith("- 抓取"):
            m["at"] = line.split("：", 1)[-1].strip()
        elif line.startswith("- 消息数"):
            try:
                m["count"] = int(re.sub(r"[^\d]", "", line.split("：", 1)[-1]) or 0)
            except ValueError:
                m["count"] = 0
        elif "尾部指纹" in line:
            m["fp"] = line.split("：", 1)[-1].strip()
    return m


def _sig(meta):
    fp = meta.get("fp") or ""
    return ("%d|%s" % (meta.get("count") or 0, fp)) if fp else ""


NOISE = ("---", "已深度思考", "联网搜索", "内容由 AI 生成，请仔细甄别",
         "继续生成", "停止生成", "复制", "重新生成")


def _user_msgs(path, limit=3):
    """取最后 N 条用户消息，各压成一行；去图片占位与思考引用等工具噪音。"""
    txt = _read(path)
    if not txt:
        return []
    chunks = re.split(r"(?m)^##\s*用户\s*$", txt)
    out = []
    for seg in chunks[1:]:
        seg = re.split(r"(?m)^##\s*助手\s*$", seg, 1)[0]
        kept = []
        for ln in seg.splitlines():
            s = ln.strip()
            if not s or s in NOISE:
                continue
            if s.startswith(">"):                    # 思考引用，不是用户写的
                continue
            if re.match(r"^\[图片[×x]?\d*\]$", s):   # collect7 的图片占位
                continue
            if s.startswith("```"):                  # 代码围栏本身不进摘要
                continue
            kept.append(s)
        if not kept:
            continue
        msg = re.sub(r"\s+", " ", " ".join(kept)).strip()
        if len(msg) >= 2:
            out.append(msg)
    return out[-limit:]


def _head(files):
    """每个文件最后 3 条用户消息，带文件名，供醒来一眼扫。"""
    out = []
    for p in files:
        label = os.path.basename(p)
        for msg in _user_msgs(p, 3):
            out.append("%s ｜ %s" % (label, _clip(msg, 180)))
    return out


# ── 判断：这次要不要抓 ────────────────────────────────────────────────

def decide(now=None):
    """心跳每 30 秒问一次：到点就返回 True，并把下次间隔抽成新的随机值。

    这里只做「读状态 + 抽签 + 原子写」，不碰网络、不碰 CDP。
    `last_try` 是防重入的：一旦判定该抓，就先占位，避免抓取还没跑完、
    30 秒后的下一次心跳又起一个线程。
    """
    now = time.time() if now is None else float(now)
    st = load_state()
    gap = _gap(st)
    last_ok = float(st.get("last_at") or 0)
    last_try = float(st.get("last_try") or 0)
    fails = int(st.get("fails") or 0)
    ref = max(last_ok, last_try)
    wait = int(round(gap - (now - ref))) if ref else 0
    if wait > 0:
        return False, "还差 %d 秒（间隔 %d，失败 %d 次）" % (wait, gap, fails)

    st["last_try"] = now
    st["next_gap"] = _arm_next()
    save_state(st)
    return True, "到点（上次成功 %s，失败 %d 次），下次间隔 %d 秒" % (
        _stamp(last_ok, now), fails, st["next_gap"])


# ── 抓一次 + 摘要 ────────────────────────────────────────────────────

def _judge(meta, bmeta, anchor):
    """返回 (changed, result, why)。result ∈ new / no-change / incomplete。"""
    count = int(meta.get("count") or 0)
    fp = meta.get("fp") or ""
    bcount = int(bmeta.get("count") or 0)
    bfp = bmeta.get("fp") or ""
    same_session = bool(meta.get("session") and bmeta.get("session")
                        and meta["session"] == bmeta["session"])

    if anchor:
        # 有锚点时 capture 只落增量，条数少是正常的
        if count <= 0:
            return False, "no-change", "锚点增量 0 条"
        return True, "new", "锚点增量 %d 条" % count

    if not bmeta:
        if count <= 0:
            return False, "incomplete", "空档（0 条）"
        return True, "new", "首次抓到 %d 条" % count

    if fp and bfp and fp == bfp:
        return False, "no-change", "尾部指纹没变（%s）" % fp

    if same_session and bcount and count < bcount:
        # 条数少一半以上：多半是页面只挂载了尾部几条，宁可这轮不写，等下一轮
        if count < max(20, bcount // 2):
            return False, "incomplete", "同会话条数大幅变少 %d→%d，当没抓全" % (bcount, count)
        return True, "new", "条数略少但尾部指纹变了（%d→%d），按增量处理" % (bcount, count)

    if count <= 0:
        return False, "incomplete", "空档（0 条）"

    return True, "new", "尾部指纹变了（%s → %s，条数 %d→%d）" % (
        bfp or "?", fp or "?", bcount, count)


def _todo_line(at, nfiles, chars, last_msg):
    who = "「%s」" % _clip(last_msg, 100) if last_msg else "（只有助手新内容，没有新的用户诉求）"
    return ("- [ ] %s 心跳重抓：新增 %d 份 / %d 字；最新用户说%s"
            " → 核对是否要转成任务" % (time.strftime("%m-%d %H:%M", time.localtime(at)),
                                     nfiles, chars, who))


def _append_todo(line):
    """只追加，不改写别人已写好的段落；被 persona_todo 重生成后会自动补回标题。"""
    try:
        os.makedirs(os.path.dirname(TODO), exist_ok=True)
        text = _read(TODO)
        add = ""
        if "## 心跳重抓追加" not in text:
            if text and not text.endswith("\n"):
                add += "\n"
            add += "\n## 心跳重抓追加（自动，只追加不改上文）\n\n"
        elif text and not text.endswith("\n"):
            add += "\n"
        add += line + "\n"
        with io.open(TODO, "a", encoding="utf-8", newline="\n") as f:
            f.write(add)
        return True
    except OSError as e:
        say("  待办追加失败：%s" % e)
        return False


def _failure(started, note, result, st=None):
    """失败也保住状态：读最新状态再合并，绝不用几秒前的旧副本覆盖。"""
    now = time.time()
    st = load_state() if st is None else st
    fails = int(st.get("fails") or 0) + 1
    gap = _gap(st)
    st.update(last_try=now, next_gap=gap, fails=fails,
              last_result=result, last_error=_clip(note, 200))
    if fails % 3 == 0:
        gap = min(MAX_BACKOFF, max(MIN_GAP, gap * 2))
        st["next_gap"] = gap
        _append_jsonl(ALERTS, {
            "at": _iso(now), "level": "warn", "kind": "recapture", "fails": fails,
            "next_gap": gap, "note": "连续抓取失败 %d 次：%s" % (fails, _clip(note, 160))})
    save_state(st)
    _append_jsonl(LOG, {
        "at": _iso(now), "result": result, "files": [], "chars": 0,
        "ms": int((now - started) * 1000), "fails": fails,
        "gap": int(st.get("next_gap") or gap), "note": _clip(note, 300)})
    say("FAIL %s（连续 %d 次，下次间隔 %d 秒）" % (_clip(note, 160), fails, gap))
    return 1


def _lock():
    os.makedirs(OPS, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(LOCK)
        except OSError:
            age = LOCK_STALE + 1
        if age < LOCK_STALE:
            return False
        try:
            os.remove(LOCK)
        except OSError:
            return False
        return _lock()
    try:
        os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
    finally:
        os.close(fd)
    return True


def _unlock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def run_once(force=False):
    """真抓一次。force=True 时无视「距上次成功不足 next_gap」的保护。"""
    started = time.time()
    st = load_state()
    gap = _gap(st)
    last_ok = float(st.get("last_at") or 0)
    if not force and last_ok and (started - last_ok) < gap:
        say("SKIP 距上次成功抓取 %d 秒 < 间隔 %d 秒（--force 可强抓）"
            % (int(started - last_ok), gap))
        return 0
    if not _lock():
        say("SKIP 已有一次重抓在跑（%s），本次不叠加" % _rel(LOCK))
        return 0
    try:
        _merge_state(last_try=started)   # 先占位，防止 30 秒心跳再起一个
        return _run_locked(started)
    except Exception as e:
        return _failure(started, "异常：%s" % str(e)[:200], "fail")
    finally:
        _unlock()


def _run_locked(started):
    before = _capture_files()
    before_paths = {p for _, p in before}
    base = before[-1][1] if before else ""
    bmeta = _meta(base) if base else {}

    try:
        import persona_readnew as RN
    except Exception as e:
        return _failure(started, "导入 persona_readnew 失败：%s" % e, "fail")

    say("→ 强制刷新页面（复用 reload_page.js，CDP 9222）…")
    rok, rmsg = RN.force_reload()
    say("  刷新%s：%s" % ("成功" if rok else "失败", rmsg))
    if rok:
        time.sleep(4.0)             # 与 readnew 的 settle 同源，等页面重新渲染

    say("→ 抓取（复用 persona_readnew.capture → collect7.js）…")
    ok, cmsg = RN.capture()
    say("  抓取%s：%s" % ("成功" if ok else "失败", cmsg))
    if not ok:
        return _failure(started, "抓取失败：%s" % cmsg, "fail")

    after = _capture_files()
    new = [(t, p) for t, p in after if p not in before_paths]
    new.sort()
    if not new:
        return _failure(started, "抓取进程成功但没落新档（capture 没写出文件）", "fail")

    rep = new[-1][1]
    meta = _meta(rep)
    if not meta.get("fp") and not meta.get("count"):
        return _failure(started, "抓取文件头读不出：%s" % _rel(rep), "fail")

    changed, result, why = _judge(meta, bmeta, os.path.exists(ANCHOR))
    now = time.time()
    st = load_state()

    if not changed:
        if result == "no-change":
            st.update(last_at=now, last_try=now, fails=0, last_result="no-change",
                      last_sig=_sig(meta), next_gap=_arm_next(), last_error="")
            save_state(st)
            _append_jsonl(LOG, {
                "at": _iso(now), "result": "no-change", "files": [], "chars": 0,
                "file": _rel(rep), "ms": int((now - started) * 1000),
                "fails": 0, "gap": int(st.get("next_gap") or 0), "note": why})
            say("NO-CHANGE %s" % why)
            return 0
        return _failure(started, why, "incomplete", st=st)

    files = [_rel(p) for _, p in new]
    chars = sum(len(_read(p)) for _, p in new)
    msgs = []
    for _, p in new:
        msgs.extend(_user_msgs(p, 3))
    head = _head([p for _, p in new])
    at = _iso(now)
    next_gap = _arm_next()
    summary = {
        "at": at, "files": files, "chars": chars, "head": head,
        "session": meta.get("session", ""), "messages": meta.get("count", 0),
        "sig": _sig(meta), "gap": next_gap,
        "ms": int((now - started) * 1000),
    }
    _atomic_json(SUMMARY, summary)

    last_msg = msgs[-1] if msgs else ""
    if last_msg:
        todo_sig = hashlib.sha1(last_msg.encode("utf-8")).hexdigest()[:12]
    else:
        todo_sig = hashlib.sha1(("assistant-only|" + _sig(meta)).encode("utf-8")).hexdigest()[:12]
    appended = False
    if todo_sig != str(st.get("last_todo_sig") or ""):
        appended = _append_todo(_todo_line(now, len(files), chars, last_msg))

    st = load_state()
    st.update(last_at=now, last_try=now, fails=0, last_result="new",
              last_sig=_sig(meta), next_gap=next_gap, last_error="")
    if appended:
        st["last_todo_sig"] = todo_sig
    save_state(st)
    _append_jsonl(LOG, {
        "at": at, "result": "new", "files": files, "chars": chars,
        "new": meta.get("count", 0), "ms": int((now - started) * 1000),
        "fails": 0, "gap": int(st.get("next_gap") or 0),
        "todo": appended, "note": why})
    say("OK %s；新内容 %d 份 / %d 字" % (why, len(files), chars))
    say("   摘要 → %s" % _rel(SUMMARY))
    say("   待办 → %s（%s）" % (_rel(TODO), "已追加一句" if appended else "同一句已追过，跳过"))
    return 0


# ── 状态展示 ──────────────────────────────────────────────────────────

def print_status():
    st = load_state()
    now = time.time()
    gap = _gap(st) if st.get("next_gap") else 0
    last_ok = float(st.get("last_at") or 0)
    last_try = float(st.get("last_try") or 0)
    ref = max(last_ok, last_try)
    wait = max(0, int(round(ref + gap - now))) if (ref and gap) else 0
    fails = int(st.get("fails") or 0)
    say("状态文件：%s%s" % (_rel(STATE),
                          "" if os.path.exists(STATE) else "（还没有，第一次 decide 时创建）"))
    say("上次成功：%s" % _stamp(last_ok, now))
    say("上次尝试：%s" % _stamp(last_try, now))
    if gap:
        say("间隔：%d 秒（%s）" % (gap, ("还差 %d 秒" % wait) if wait else "已到点"))
    else:
        say("间隔：首次 decide 时抽 180–600 秒")
    say("连续失败：%d" % fails)
    note = st.get("last_result") or "—"
    if st.get("last_error"):
        note += "（%s）" % _clip(st["last_error"], 100)
    say("上次结果：%s" % note)
    say("上次指纹：%s" % (st.get("last_sig") or "—"))
    try:
        s = json.load(io.open(SUMMARY, encoding="utf-8-sig"))
        say("上次摘要：%s，%d 份，%d 字 → %s" % (
            s.get("at", "?"), len(s.get("files") or []), int(s.get("chars") or 0),
            _rel(SUMMARY)))
        for line in (s.get("head") or [])[-3:]:
            say("   · %s" % line)
    except (OSError, ValueError):
        say("上次摘要：还没生成（只有抓到新内容才写）")
    say("待办追加：%s" % ("有" if os.path.exists(TODO)
                       and "## 心跳重抓追加" in _read(TODO) else "还没有"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="心跳驱动的随机重抓 + 本次增量摘要（B40）")
    ap.add_argument("--decide", action="store_true",
                    help="只判断这次要不要抓；exit 0=该抓，1=不该抓")
    ap.add_argument("--once", action="store_true", help="真抓一次")
    ap.add_argument("--force", action="store_true", help="无视间隔（配 --once）")
    ap.add_argument("--status", action="store_true", help="看状态与上次摘要")
    a = ap.parse_args(argv)

    if a.status:
        return print_status()
    if a.decide:
        due, why = decide()
        say(("该抓：" if due else "不该抓：") + why)
        return 0 if due else 1
    if a.once:
        return run_once(force=a.force)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
