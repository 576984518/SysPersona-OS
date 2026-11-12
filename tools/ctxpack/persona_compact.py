# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_compact.py — 水位到线就自己压缩（用注入通道发 /compact）
r"""persona_compact.py — 水位到线就自己压缩（用注入通道发 /compact）

链路（2026-11-10 定）：
  量水位（persona_gauge） → 到线了吗？
     ├─ 没到 → 什么都不做，只记一笔
     └─ 到了 → 冷却过了吗？
              ├─ 没冷却 → 跳过（防抖，不连着压）
              └─ 冷却过 → persona_inject 把 `/compact` 打进壳的输入框

为什么这么干：壳自己有 `/compact`，但只在"人空闲时手动敲"才生效；
我们已经有注入通道，就不用再实现一套压缩，直接替人敲一下。

还负责**记账**（2026-11-10 补）：
  壳自己到线也会压（实测水位 136K → 17K → 39K，一条 fired 都没有）。
  每次检查都拿本次读数跟上次比：掉 20% 以上且少 50K 以上 = 一次「壳自压」，
  记 `action: "shell"`；若这次骤降落在我们自己 fired 之后 10 分钟内，那是我们那条
  /compact 生效了，仍记 `idle` 并附 `shell_after_ours: true`，不重复计数。

记账字段（2026-11-10 二次补，只加账、不改压缩策略）：
  - 每行都有 `source`：`cron`（计划任务/守护调，默认 pythonw 入口）、
    `tray`（托盘「立即压缩一次」）、`cli`（人工命令行）。
  - `fired` 行带 `handoff`（交接包相对路径）与 `sha256`（前 12 位），
    用来把「这次发了 /compact」和「当时镜像/交接的是哪一份」对上。
  - `fired` 后的**下一次** check 把当时读数补成上一行的 `after_ctx`，
    并算 `drop`（百分比）：≥15% → 上一行 `result: "ok"`；否则 `result: "weak"`。
  - 连续 2 次 `fired` 都没 15% 下降、且两次开火间隔 ≤20 分钟 → 记一行
    `action: "ineffective"`，写 `ops/compact-alert.json` 并往
    `ops/alerts.jsonl` 追加一条 warn（B93：`/compact` 是实验功能，掉了要账本自己说）。

用法：
  python persona_compact.py check            # 到线就压（python.exe 入口默认 source=cli）
  python persona_compact.py check --dry-run  # 只报告会不会压（不动账本）
  python persona_compact.py check --force    # 人工点：跳过 350K 阈值，硬闸一条不少
  python persona_compact.py check --source tray   # 调用方标明来源（托盘用）
  python persona_compact.py stats            # 今日/本周 fired / shell / idle + 逐条时刻
  python persona_compact.py check --threshold 350000 --cooldown 900
  python persona_compact.py check --simulate-fired 400000   # 调试：只记一笔 fired，不注入
"""
import argparse
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time

try:
    import persona_win as _pw  # 无窗口
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # realpath：C:\SysPersona 是 junction，解析成 D 盘真路径（自动审查按路径判，C 盘容易被拦）
OPS = os.path.join(ROOT, "ops")
STATE = os.path.join(OPS, "compact-state.json")
LOG = os.path.join(OPS, "compact.jsonl")
ALERT = os.path.join(OPS, "compact-alert.json")
ALERTS = os.path.join(OPS, "alerts.jsonl")
PY = sys.executable
GAUGE = os.path.join(ROOT, "脚本/persona_gauge.py")
HOME = os.path.expanduser("~")
# 仓库版/新布局用统一解析器；本机运行目录的 persona_root 是老版（没有 node()），
# 导入不到就退回旧默认——两边的 persona_compact.py 因此可以保持逐字一致。
try:
    from persona_root import node as _node
    NODE = _node()
except Exception:
    NODE = os.environ.get("PERSONA_NODE") or r_loc.node()
EXPORTER = os.path.join(ROOT, "tools/js/dsh_export.js")
MIRROR = os.path.join(ROOT, "脚本/persona_mirror.py")
HANDOFF_DIR = os.path.join(ROOT, "续接")
HANDOFF_LATEST = os.path.join(HANDOFF_DIR, "接续包-最新.md")
DEFAULT_THRESHOLD = 350_000      # 到 350K 就该压了（壳的自动线是 ~367K，这里提前一点）
DEFAULT_COOLDOWN = 900           # 15 分钟内不连压
FRESH_EXPORT = 60                # 1 分钟内导过就不再导（同一分钟重试别重复落盘）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 2026-11-11 15:50 人两次纠正后定下来（我先把归属搞反了，记在这里别再翻）：
#   **DSH（deepseek） 的压缩命令是 `/compact`** —— 就是"添加 → 指令 → 压缩 compact"那一条；
#   **Hermes 的压缩命令是 `/compress`** —— 它斜杠命令面板里写的是"/compress 压缩上文，
#   可追加聚焦主题（如 /compress 保留鉴权讨论）"。
# 两个客户端两套命令，谁也不能拿对方的去发。上一版我把两边写反过，别再照着改。
COMPACT_CMD = os.environ.get("PERSONA_COMPACT_CMD") or "/compact"

# 壳自压判定（2026-11-10 加）：水位突然被"换掉"的样子，和"到线"是两件事。
SHELL_DROP_RATIO = 0.80          # 本次读数 < 上次的 80%（跌超 20%）
SHELL_DROP_TOKENS = 50_000       # 且绝对少 50K 以上
SHELL_AFTER_OURS = 600           # fired 后 10 分钟内的骤降归因我们那条 /compact
SHELL_MAX_GAP = 6 * 3600         # 两次读数隔了 6 小时以上（关机/任务停了）：基准过期，不判

# 效果核对（2026-11-10 加）：只判账，不改压缩策略。
EFFECT_DROP_PCT = 15.0           # 一次 /compact 至少该把水位压掉 15%
INEFFECTIVE_GAP = 20 * 60        # 连续两笔 fired 的间隔上限；超过就不配对（可能中间停过任务）
# 2026-11-12 10:29 误报踩到：fired 后 **41 秒**就去量效果，读到 406K → 判 weak → 报「连压不降」，
# 可那时壳的 pressureTokens 还没重算（它要等下一轮才更新），实际压完是 32K（干掉 259K）。
# 所以：**刚开火不到 5 分钟一律不判效果**，把 pending_fire 留着等下一轮；只有真降了才提前结案。
VERIFY_GRACE = 300

# 单实例闸（2026-11-12 人定）：「执行前判断当前是否已经在执行」——
# 托盘手点 + 守护定时两个触发撞在一起时，会同时等空闲、同时弹进度窗、同时往输入框打字，
# 表现就是"一下弹出两个窗口"。先把锁拿到手，拿不到就这一轮什么也不做。
LOCK = os.path.join(OPS, "compact.lock")
LOCK_STALE = 900                  # 锁 15 分钟没更新就按死的处理（进程被强杀会留残锁）


def _pid_alive(pid):
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))       # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259            # STILL_ACTIVE
    except Exception:                                    # noqa: BLE001
        return False


def _write_lock(source=""):
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(LOCK, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "ts": time.time(), "at": now(),
                       "source": source or ""}, f, ensure_ascii=False)
    except OSError:
        pass


def lock_take(source=""):
    """拿本轮执行锁：返回 (拿到没, 说明)。"""
    d = {}
    try:
        with io.open(LOCK, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    try:
        pid = int(d.get("pid") or 0)
        at = float(d.get("ts") or 0)
    except (TypeError, ValueError):
        pid, at = 0, 0.0
    if pid and pid != os.getpid() and _pid_alive(pid) and (time.time() - at) < LOCK_STALE:
        return False, ("已有一轮在跑（pid %d，%.0f 秒前开始，来源 %s），这一轮不重复执行"
                       % (pid, max(0.0, time.time() - at), d.get("source") or "?"))
    _write_lock(source)
    return True, ""


def lock_drop():
    try:
        with io.open(LOCK, encoding="utf-8") as f:
            if int((json.load(f) or {}).get("pid") or 0) != os.getpid():
                return
    except (OSError, ValueError):
        return
    try:
        os.remove(LOCK)
    except OSError:
        pass



def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def measure():
    """跑 gauge 拿当前水位（K 为单位换算成 token）。

    编码坑（2026-11-10 踩）：gauge 子进程在计划任务里按 **GBK** 吐中文，
    按 utf-8 硬解会得到一串 U+FFFD，`[上下文]` 就对不上、误报「没有输出水位」。
    所以：**收字节，两种编码都试**；正则也不依赖中文，认第一个方括号后的数字。
    """
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([PY, GAUGE], capture_output=True, timeout=180,
                           cwd=ROOT, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = r.stdout or b""
        out = ""
        for enc in ("utf-8", "gbk"):
            try:
                out = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not out:
            out = raw.decode("utf-8", "replace")
        m = re.search(r"\[[^\]\r\n]*\]\s*~?([\d.]+)\s*([KM]?)", out)
        if not m:
            return 0, "gauge 没有输出水位（原始输出 %d 字节）" % len(raw)
        val = float(m.group(1))
        unit = m.group(2)
        tok = val * (1_000_000 if unit == "M" else 1000 if unit == "K" else 1)
        return int(tok), ""
    except Exception as e:
        return 0, "gauge 调用失败：%s" % e


# 压缩 + 重读合成一条链（2026-11-12 人定）：
#   「等压缩完成、然后重读了，再关闭弹窗」——所以这两步在本进程里前后脚跑完，
#   进度窗一路显示到重读结果出来才收。
WAIT_COMPACT = 240              # 发完 /compact 最多等 4 分钟看壳压下来（实测 30–90 秒）
BORROW_WAIT = 5.0               # 借键盘前先弹窗，等这么多秒让他停手（2026-11-12 人定：5 秒）
RELOADER_TIMEOUT = 300          # 重载器自己的耐心（内含最多 90 秒等你停手）


def wait_compacted(before, timeout=WAIT_COMPACT):
    """等壳把水位压下来：返回 (压下来了没, 一句话说明)。"""
    t0 = time.time()
    ctx = 0
    while time.time() - t0 < timeout:
        time.sleep(10)
        try:
            ctx, err = measure()
        except Exception:                 # noqa: BLE001
            ctx, err = 0, "err"
        if not err and ctx > 0 and before > 0:
            drop = (before - ctx) * 100.0 / before
            if drop >= EFFECT_DROP_PCT:
                return True, ("水位 %dK → %dK，降了 %.0f%%"
                              % (before // 1000, ctx // 1000, drop))
    tail = ("%dK → %dK" % (before // 1000, ctx // 1000)) if ctx else "量不到水位"
    return False, "%d 秒内没看到 ≥%.0f%% 的下降（%s）" % (int(timeout), EFFECT_DROP_PCT, tail)


def run_reloader(timeout=RELOADER_TIMEOUT, manual=True):
    """在当前进程里跑一次重载 —— 不再甩成独立进程，因为弹窗要等它的结果。

    manual=True（默认）：压缩链里叫的，已经弹过窗请人停手，按"借键盘"走
    `--manual`，跳过"你刚动过键鼠"的闸门（2026-11-12 修：否则人一边打字一边问
    「怎么没开始压缩」，就把重读挡成欠账）。

    返回 (成没成, 一句话, 动作名)。结果读 ops/reloader-last.json（重载器自己写）。
    """
    exe = os.path.join(ROOT, "脚本/persona_constitution_reloader.py")
    if not os.path.exists(exe):
        return False, "找不到重载器脚本", ""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [PY, exe, "--reason", "compact"] + (["--manual"] if manual else [])
    for attempt in (0, 1):
        try:
            subprocess.run(cmd, cwd=ROOT, env=env,
                           capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        except Exception as e:            # noqa: BLE001
            return False, "重载器没跑起来：%s" % str(e)[:80], ""
        try:
            with io.open(os.path.join(OPS, "reloader-last.json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return False, "重载器没留结果（看 ops/reloader.jsonl）", ""
        action = str(d.get("action") or "?")
        if action == "busy-lock" and not attempt:
            # 守护巡检那一轮正好在跑：等它收工再来一次，别把"撞车"记成失败
            time.sleep(15)
            continue
        if d.get("ok"):
            return True, "已重读 %s 字" % (d.get("chars") or "?"), action
        if action == "queued":
            return False, "被拦住了：你在打字，已排队（托盘「补发被拦的设定重载」可立刻补）", action
        if action == "busy-lock":
            return False, "另一次重载正在跑，这次没插进去（欠账还挂着）", action
        return False, "没发出去（%s）：%s" % (action, str(d.get("msg") or "")[:80]), action
    return False, "两次都没插进去", "busy-lock"


def load_state():
    try:
        return json.load(io.open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(d):
    try:
        os.makedirs(OPS, exist_ok=True)
        tmp = STATE + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=1))
        os.replace(tmp, STATE)
    except OSError:
        pass


def log(rec):
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── 来源 / 交接包指纹（2026-11-10 加）────────────────────────────────
SOURCE_KINDS = ("cron", "tray", "cli")


def resolve_source(src=None):
    """调用方没显式给 source 时按入口猜：计划任务用 pythonw.exe，人工用 python.exe。"""
    if src in SOURCE_KINDS:
        return src
    exe = os.path.basename(sys.executable or "").lower()
    return "cron" if exe.startswith("pythonw") else "cli"


def _rel(path):
    try:
        return os.path.relpath(path, ROOT).replace("\\", "/")
    except ValueError:
        return os.path.abspath(path).replace("\\", "/")


def handoff_fingerprint():
    """当时那份交接包（优先 canonical 的 接续包-最新.md）→ {handoff, sha256[:12]}。

    哈希按**文件字节**算（不是按正文），这样任何人拿同一份文件都能复算。
    """
    path = ""
    if os.path.exists(HANDOFF_LATEST):
        path = HANDOFF_LATEST
    else:
        cands = [p for p in glob.glob(os.path.join(HANDOFF_DIR, "接续包-*.md"))
                 if os.path.basename(p) != "接续包-最新.md"]
        if cands:
            path = max(cands, key=lambda p: os.path.getmtime(p))
    if not path:
        return {}
    try:
        with io.open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:12]
        return {"handoff": _rel(path), "sha256": digest}
    except OSError:
        return {}


def _read_lines(path):
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return None


def patch_row(match_at, fields, action="fired"):
    """给账本里**最后一笔** `at == match_at` 且 action 匹配的行补字段。

    只补不改别的行、一行都不删。按 action 过滤 + 取最后一笔，是为了避免同一秒内
    的 idle / fired 撞上（调试模拟时会出现；生产里 fired 至少隔 15 分钟）。
    """
    lines = _read_lines(LOG)
    if lines is None:
        return False
    idx = -1
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and rec.get("at") == match_at
                and (action is None or rec.get("action") == action)):
            idx = i
    if idx < 0:
        return False
    out = list(lines)
    rec = json.loads(out[idx])
    rec.update(fields)
    out[idx] = json.dumps(rec, ensure_ascii=False)
    # 极窄竞态：读完之后定时任务/托盘可能又追加了行；replace 前再读一遍，把尾巴接上。
    newer = _read_lines(LOG) or []
    if len(newer) > len(lines) and newer[:len(lines)] == lines:
        out.extend(newer[len(lines):])
    try:
        tmp = "%s.%d.tmp" % (LOG, os.getpid())
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, LOG)
    except OSError:
        return False
    return True


def _write_json(path, obj):
    """临时文件 + os.replace：读的人只会看到完整旧版/新版。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _append_alert(rec):
    """与 persona_daemon/persona_dispatch 写 ops/alerts.jsonl 的格式保持一致。"""
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(ALERTS, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def report_ineffective(fire_at, ctx_before, prev_ctx, ctx_now, drop, gap, source):
    """连压不降：一行 ineffective + compact-alert.json + alerts.jsonl 一条 warn。"""
    reason = ("连续 2 次 fired（间隔 %d 分钟）都没有 ≥%.0f%% 的下降："
              "%dK → %dK → %dK（最近一次仅降 %.1f%%）"
              % (int(gap // 60), EFFECT_DROP_PCT, prev_ctx // 1000,
                 ctx_before // 1000, ctx_now // 1000, drop))
    at = now()
    alert = {"at": at, "last_fire": fire_at, "ctx_before": ctx_before,
             "ctx_now": ctx_now, "reason": reason}
    wrote = _write_json(ALERT, alert)
    log({"at": at, "ctx": ctx_now, "action": "ineffective", "source": source,
         "last_fire": fire_at, "ctx_before": ctx_before, "ctx_now": ctx_now,
         "drop": drop, "msg": reason})
    _append_alert({"at": at, "level": "warn", "kind": "compact_ineffective",
                   "src": "compact", "last_fire": fire_at,
                   "ctx_before": ctx_before, "ctx_now": ctx_now, "note": reason})
    print("ALERT " + reason)
    if not wrote:
        print("WARN compact-alert.json 没写进去（账本与 alerts.jsonl 已记）")
    return alert


def verify_fired(st, ctx, ts, source="cron"):
    """把「上一笔 fired 之后第一次 check」的读数补进那一行，并判效果。

    返回核对结果 dict；没有待核对 fired 时返回 None。**只读水位，不改压缩策略。**
    """
    pend = st.get("pending_fire") or {}
    at = str(pend.get("at") or "")
    try:
        before = int(pend.get("ctx") or 0)
        fired_ts = float(pend.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if not at or before <= 0 or ctx <= 0:
        return None
    drop = round((before - ctx) * 100.0 / before, 1)
    ok = drop >= EFFECT_DROP_PCT
    # 宽限：刚开火 δ 秒内壳还没重算水位，这时候判"没降"必然误报（2026-11-12 10:29）。
    # 不结案＝不改 st，pending_fire 原样留着，下一轮再量。
    age_fire = ts - fired_ts
    if not ok and fired_ts and 0 <= age_fire < VERIFY_GRACE:
        print("WAIT fired 才 %d 秒（< 宽限 %d 秒），壳还没重算水位，这轮不判效果"
              % (int(age_fire), VERIFY_GRACE))
        return None
    patched = patch_row(at, {"after_ctx": ctx, "drop": drop,
                             "result": "ok" if ok else "weak"})
    # 配对：这一笔和上一笔都是 fired、都没降 15%，且两次开火间隔 ≤20 分钟。
    prev = st.get("weak_fire") or {}
    try:
        prev_ts = float(prev.get("ts") or 0)
        prev_ctx = int(prev.get("ctx") or 0)
    except (TypeError, ValueError):
        prev_ts, prev_ctx = 0.0, 0
    ineffective = bool(not ok and prev_ts and 0 < fired_ts - prev_ts <= INEFFECTIVE_GAP)
    st.pop("pending_fire", None)
    if ok:
        st.pop("weak_fire", None)
    else:
        st["weak_fire"] = {"at": at, "ts": fired_ts, "ctx": before}
    if ineffective:
        report_ineffective(at, before, prev_ctx or before, ctx, drop,
                           fired_ts - prev_ts, source)
        st.pop("weak_fire", None)     # 一对只报一次；再坏要再攒两笔
    print("EFFECT %s：%dK → %dK，drop=%.1f%%（%s）"
          % (at, before // 1000, ctx // 1000, drop, "ok" if ok else "weak"))
    if not patched:
        print("WARN 账本里找不到 fired 行 %s，after_ctx 没补上" % at)
    return {"at": at, "before": before, "after": ctx, "drop": drop,
            "ok": ok, "ineffective": ineffective}


# ── 账本：壳自压也算一次（2026-11-10 加）──────────────────────────────
def shell_drop(st, ctx, ts=None):
    """这次读数和上次比，是不是"被换掉了"。

    判据（缺一不可）：比上次低 20% 以上、绝对少 50K 以上。
    返回 `(is_drop, prev_ctx, after_ours)`；`after_ours=True` 表示骤降落在我们自己
    `last_fire` 之后 10 分钟内 —— 那是我们那条 `/compact` 生效，不能再记一笔壳自压。

    读数间隔超过 `SHELL_MAX_GAP`（关机、计划任务停过）时只更新基准、不判：
    否则十天前的 300K 和今天的 40K 会被算成"壳压了一次"。
    """
    try:
        prev = int(st.get("last_ctx") or 0)
    except (TypeError, ValueError):
        return False, 0, False
    if prev <= 0 or ctx <= 0 or ctx >= prev:
        return False, prev, False
    if ctx >= prev * SHELL_DROP_RATIO or (prev - ctx) <= SHELL_DROP_TOKENS:
        return False, prev, False
    ts = time.time() if ts is None else ts
    try:
        seen_at = float(st.get("last_check_at") or 0)
    except (TypeError, ValueError):
        seen_at = 0.0
    if seen_at and ts - seen_at > SHELL_MAX_GAP:
        return False, prev, False
    try:
        fired_at = float(st.get("last_fire") or 0)
    except (TypeError, ValueError):
        fired_at = 0.0
    since = ts - fired_at
    return True, prev, (0 <= since <= SHELL_AFTER_OURS)


def _row_at(r):
    """记录里的 `at`（本地时间字符串）→ epoch；坏行返回 None。"""
    try:
        return time.mktime(time.strptime(r.get("at") or "", "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def _monday0(ts):
    """本周一 00:00 的 epoch（周一起算）。"""
    lt = time.localtime(ts)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return day0 - lt.tm_wday * 86400


def _stats_block(title, rows, ts_from, detail_until=None):
    sel = []
    for r in rows:
        t = _row_at(r)
        if t is not None and t >= ts_from:
            sel.append((t, r))
    sel.sort(key=lambda x: x[0])
    shown = [(t, r) for t, r in sel if detail_until is None or t < detail_until]
    cnt = {"fired": 0, "shell": 0, "idle": 0, "ineffective": 0}
    other = {}
    for _t, r in sel:
        a = r.get("action") or "?"
        if a in cnt:
            cnt[a] += 1
        else:
            other[a] = other.get(a, 0) + 1
    print(title)
    print("  fired: %d 次（我们触发） / shell: %d 次（壳自压） / idle: %d 次 / "
          "ineffective: %d 次（连压不降）"
          % (cnt["fired"], cnt["shell"], cnt["idle"], cnt["ineffective"]))
    if other:
        print("  其他：" + " / ".join("%s %d 次" % (k, v) for k, v in sorted(other.items())))
    for t, r in shown:
        extra = ""
        if r.get("prev"):
            extra += " prev=%dK" % (int(r["prev"]) // 1000)
        if r.get("ctx"):
            extra += " ctx=%dK" % (int(r["ctx"]) // 1000)
        if r.get("after_ctx"):
            extra += " after=%dK" % (int(r["after_ctx"]) // 1000)
        if r.get("drop") is not None:
            try:
                extra += " drop=%.1f%%" % float(r["drop"])
            except (TypeError, ValueError):
                pass
        if r.get("result"):
            extra += " " + str(r["result"])
        if r.get("source"):
            extra += " src=%s" % r["source"]
        if r.get("shell_after_ours"):
            extra += " shell_after_ours"
        if r.get("manual"):
            extra += " manual"
        msg = (r.get("msg") or "").replace("\n", " ")[:46]
        print("  %s  %-5s%s%s"
              % (time.strftime("%m-%d %H:%M:%S", time.localtime(t)),
                 r.get("action") or "?", extra, ("  " + msg) if msg else ""))
    if not shown:
        print("  （没有更早的记录；今日明细见上）" if detail_until
              else "  （这段时间没有记录）")


def stats():
    """今日 / 本周：fired 几次（我们）、shell 几次（壳自己）、idle 几次，逐条列时刻。"""
    rows = []
    try:
        with io.open(LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError as e:
        print("读不到账本 %s：%s" % (LOG, e))
        return 1
    ts = time.time()
    today0 = time.mktime((time.localtime(ts).tm_year, time.localtime(ts).tm_mon,
                          time.localtime(ts).tm_mday, 0, 0, 0, 0, 0, -1))
    print("账本：%s（共 %d 条）" % (LOG, len(rows)))
    st = load_state()
    if st.get("last_ctx"):
        seen = st.get("last_check_at")
        if seen:
            when = "检查于 " + time.strftime("%m-%d %H:%M:%S", time.localtime(float(seen)))
        else:
            when = "老版本没记检查时刻"
        print("上次读数：%dK（%s）" % (int(st["last_ctx"]) // 1000, when))
    print("")
    _stats_block("今日 %s" % time.strftime("%Y-%m-%d", time.localtime(ts)), rows, today0)
    print("")
    w0 = _monday0(ts)
    _stats_block("本周（%s 起；计数含今日，明细只列今日之前）"
                 % time.strftime("%m-%d", time.localtime(w0)), rows, w0, today0)
    return 0


def latest_session_dir():
    """最近在写的 DSH 会话文件（导出入口）。"""
    import glob
    best, best_t = "", 0.0
    for f in glob.glob(os.path.join(HOME, ".dsh", "sessions", "*", "*", "session.v4.jsonl.zstd")):
        try:
            t = os.path.getmtime(f)
        except OSError:
            continue
        if t > best_t:
            best, best_t = f, t
    return best


def _decode(raw):
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def ensure_mirror(trigger=""):
    """**压之前先把原文落盘**（`BACKGROUND_SPEC` §七 唯一不可降级的一条）。

    顺序：导出当前 DSH 会话 → 镜像进 `archive\\` 并校验 sha256。
    返回 (ok, msg)。ok=False 时调用方**不许压缩**。

    为什么每次开火都现导，而不是复用半小时前那份：
    差的这几十分钟，正好是刚刚聊出来的内容——把它压掉，等于把最该留的丢了。
    """
    import glob
    sess = latest_session_dir()
    if not sess:
        return False, "找不到 DSH 会话，无法落盘"
    srcs = glob.glob(os.path.join(ROOT, "sessions", "*", "电脑会话", "续接前-*.md"))
    srcs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    src = ""
    if srcs and (time.time() - os.path.getmtime(srcs[0])) < FRESH_EXPORT:
        src = srcs[0]
    if not src:
        stamp = time.strftime("%Y%m%d-%H%M")
        src = os.path.join(ROOT, "sessions", time.strftime("%Y-%m-%d"), "电脑会话",
                           "续接前-%s.md" % stamp)
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            r = subprocess.run([NODE, EXPORTER, os.path.dirname(sess), src],
                               capture_output=True, timeout=300, cwd=ROOT,
                               creationflags=NO_WINDOW)
            if r.returncode != 0 or not os.path.exists(src):
                return False, "导出失败（rc=%s）：%s" % (r.returncode, _decode(r.stderr)[:160])
        except Exception as e:
            return False, "导出异常：%s" % e
    try:
        r = subprocess.run([PY, MIRROR, src, "--trigger", trigger or "compact"],
                           capture_output=True, timeout=180, cwd=ROOT,
                           creationflags=NO_WINDOW)
        out = _decode(r.stdout).strip()
        if r.returncode != 0:
            return False, "镜像校验失败：%s" % (out[:200] or _decode(r.stderr)[:200])
        return True, out.splitlines()[-1] if out else "已镜像"
    except Exception as e:
        return False, "镜像异常：%s" % e


def ask_reloader():
    """（已停用，2026-11-11 人定）

    人原话：「两个是同一件事，而且只重读，频率太高」——判水位和判重读本来就是一件事，
    所以只留一条链：**到线才压，压完才重读**。这里不再单独去问重载器，
    那些"没压也重读"的调用就是频率高的来源，已经删掉。
    """
    return


def check(threshold=DEFAULT_THRESHOLD, cooldown=DEFAULT_COOLDOWN, dry_run=False,
          do_mirror=True, force_ctx=0, timeout=600, force=False, source=None):
    source = resolve_source(source)
    ctx, err = measure()
    if force_ctx:                      # 极限实测用：不改真实文件，只假装量到了这个数
        ctx, err = int(force_ctx), ""
        print("TEST 强制水位 %dK（真实值不参与判定）" % (ctx // 1000))
    if err:
        print("FAIL " + err)
        return 2
    # 幂等闸：已经在跑就不进（人 2026-11-12：「执行前判断当前是否已经在执行」）
    got, why = lock_take(source)
    if not got:
        print("SKIP " + why)
        log({"at": now(), "ctx": ctx, "action": "busy-lock", "source": source, "msg": why})
        return 0
    try:
        return _check_locked(threshold, cooldown, dry_run, do_mirror, ctx, timeout,
                             force, source)
    finally:
        lock_drop()


def _check_locked(threshold, cooldown, dry_run, do_mirror, ctx, timeout, force, source):
    st = load_state()
    ts = time.time()
    last = float(st.get("last_fire", 0) or 0)
    age = ts - last
    dropped, prev_ctx, after_ours = shell_drop(st, ctx, ts)
    # 骤降归我们那次时，标记挂到本轮所有记账上（idle 不会重复计成 shell）
    extra = {"shell_after_ours": True} if (dropped and after_ours) else {}

    def _log(action, msg=None, **fields):
        rec = {"at": now(), "ctx": ctx, "action": action, "source": source}
        rec.update(extra)
        rec.update(fields)
        if msg:
            rec["msg"] = msg
        log(rec)

    def _seen():
        """**每次量到数都记基准**：上次读数/上次检查时刻。"""
        st["last_ctx"] = ctx
        st["last_check_at"] = ts
        save_state(st)

    try:
        if not dry_run:                # 干跑只走位：不补 after_ctx、不判 ineffective
            verify_fired(st, ctx, ts, source)
        if dropped:
            if after_ours:
                print("SHELL 读数 %dK → %dK 骤降，落在我们 fired 后 %d 分钟内，"
                      "归我们那条 /compact（记 idle + shell_after_ours，不重复计）"
                      % (prev_ctx // 1000, ctx // 1000, int((ts - last) // 60)))
            else:
                log({"at": now(), "ctx": ctx, "prev": prev_ctx, "action": "shell",
                     "source": source,
                     "msg": "壳自行压缩（读 %dK → %dK）" % (prev_ctx // 1000, ctx // 1000)})
                print("SHELL 检测到壳自行压缩：%dK → %dK（已记一笔 shell）"
                      % (prev_ctx // 1000, ctx // 1000))
        if not force and ctx < threshold:
            print("SKIP 水位 %dK < 线 %dK，不压" % (ctx / 1000, threshold / 1000))
            _log("idle")
            # 2026-11-11 人定：「两个是同一件事，而且只重读，频率太高」——
            # 没到压缩线就**什么都不做**，不再顺手叫重载器（那正是"只重读"的来源）。
            # 重读只挂在压缩成功之后，另加每天一次 / 手动。
            return 0
        if age < cooldown and not force:
            print("SKIP 水位 %dK%s，%d 秒前刚压过（冷却 %d 秒）"
                  % (ctx / 1000,
                     "（--force 不跳冷却，防连点）" if force else "到线了",
                     int(age), cooldown))
            _log("cooldown")
            return 0
        if dry_run:
            if force:
                print("DRY  --force 跳过 %dK 门槛（当前 %dK）：会先导出并镜像原文、校验 sha256，"
                      "再等壳空闲后发 /compact（冷却已过）"
                      % (threshold // 1000, ctx // 1000))
            else:
                print("DRY  水位 %dK ≥ 线 %dK，会先导出并镜像原文，再发 /compact（冷却已过）"
                      % (ctx / 1000, threshold / 1000))
            return 0
        if do_mirror:
            mok, mmsg = ensure_mirror(trigger="force" if force else "")
            if not mok:
                print("FAIL 镜像没落盘，按规矩不许压：%s" % mmsg)
                _log("no-mirror", mmsg)
                return 1
            print("MIRROR " + mmsg)
        try:
            import persona_inject as pi
        except Exception as e:
            print("FAIL 导入注入模块失败：%s" % e)
            return 2
        # ── 先把壳叫出来（2026-11-12 人定）─────────────────────────────
        # 人原话：「我在其他窗户或者网页，或者这个客户端界面关闭了、只显示了托盘图标，
        #   你能不能弹窗后，自己切换窗口，或者双击托盘图标打开。」
        # 所以压缩链的第一步不是"找窗口"，是"把窗口弄出来"：藏起来就恢复，没了就重新拉起。
        # 同时记住他原本在看哪个窗口，收尾时还回去（别让他干别的干到一半窗口被换走）。
        prev_fg = 0
        try:
            prev_fg = pi.foreground_hwnd()
            wok, wmsg = pi.ensure_window()
            print("WAKE %s" % wmsg)
            if not wok:
                _log("busy", wmsg)
                print("FAIL " + wmsg)
                return 1
        except Exception as e:            # noqa: BLE001
            print("WAKE 跳过了：%s" % str(e)[:80])
        # 先等**壳**空下来再敲：10:15 那次撞上正在生成的那一轮，回车被吞。
        iok, waited, imsg = pi.wait_shell_idle(stable=15, timeout=timeout)
        print("等待：%s（%.0f 秒）" % (imsg, waited))
        if not iok:
            _log("busy", imsg)
            print("FAIL 壳一直在动，这轮不压（下一轮再来）")
            return 1
        # ── 借键盘协议（2026-11-12 人定）────────────────────────────────
        # 人原话：「就算你影响我打字了，你把我的全选-剪切，然后备份，最后输入你的，
        #   完成后再把我的粘贴回来；我看到弹窗出来就不操作了，你弹出来等 5 秒再做。」
        # 所以顺序是：先弹窗告知 → 等 5 秒（他停手）→ 取走草稿并备份 → 发 /compact
        #   → 等壳压完 → 重读设定 → **把草稿放回输入框**。
        # 这条协议取代了原来的"他动键盘就不发"：打断可以，但不能让他打的东西丢。
        if not force:
            try:
                _win = pi.pick_window()
            except Exception:             # noqa: BLE001
                _win = None
            if not _win:
                _log("busy", "没找到壳窗口")
                print("FAIL 没找到壳窗口，这轮不压")
                return 1
        _res = os.path.join(OPS, "compact-progress.json")

        _seq = {"n": 0}

        def _stage(text, limit=0):
            """写一段进度：段号 + 本段上限 + 本段开始时刻。

            2026-11-12 人报「等了 1 分钟都没开始压缩，应该分段计时」：原来只写一句话，
            窗里看不出"现在是第几段、这段走多久了、还要多久"。现在每段带上 limit，
            窗里就能显示「第 2/6 段 · 已用 12 秒 / 上限 240 秒」，卡住也一眼知道卡在哪段。
            """
            _seq["n"] += 1
            try:
                with io.open(_res, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"ok": None, "msg": str(text),
                                        "stage": _seq["n"], "limit": int(limit),
                                        "st0": time.time()}, ensure_ascii=False))
            except OSError:
                pass

        def _final(flag, text):
            try:
                with io.open(_res, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"ok": bool(flag), "msg": str(text)},
                                       ensure_ascii=False))
            except OSError:
                pass

        # ① 先弹窗（内容是"请你停手"），人看到就不打了
        _stage("借用键盘：请停手 5 秒（正在保存你输入框里的草稿）…", BORROW_WAIT + 10)
        try:
            _pyw = os.path.join(os.path.dirname(PY), "pythonw.exe")
            subprocess.Popen([_pyw if os.path.exists(_pyw) else PY,
                              os.path.join(ROOT, "脚本/persona_progress.py"),
                              _res, "压缩 + 重读设定（借键盘）", "8"],
                             cwd=ROOT, creationflags=NO_WINDOW)
        except Exception:                 # noqa: BLE001
            pass
        # ② 等 5 秒让他停手
        time.sleep(max(5.0, BORROW_WAIT))
        # ③ 取走草稿 + 备份（丢了也能从 ops\composer-draft.md 找回）
        draft_ok, draft = pi.capture_composer()
        draft_backup = os.path.join(OPS, "composer-draft.md")
        if draft_ok and draft.strip():
            try:
                with io.open(draft_backup, "w", encoding="utf-8") as f:
                    f.write("# 借用前输入框里的草稿（%s，%d 字）\n\n%s\n"
                            % (now(), len(draft), draft))
            except OSError:
                pass
            print("草稿已备份：%d 字 → %s" % (len(draft), _rel(draft_backup)))
            _stage("草稿已备份（%d 字），正在发 /compact…" % len(draft), 15)
            try:
                pi.clear_input()           # 全选 → 删除（内容已在草稿里）
            except Exception:              # noqa: BLE001
                pass
        else:
            draft = ""
            print("输入框是空的，没有草稿要备份")
            _stage("正在发 /compact…", 15)
        ok, msg = pi.inject(COMPACT_CMD, wait_idle=0, manual=True)   # 借键盘：跳过闸门
        if ok:
            # 2026-11-11 修：注入被吞回车时命令会留在人的输入框里
            import time as _t
            _t.sleep(1.2)
            # 2026-11-12 改：**不再事后 clear_input()**。原来"回车被吞 → 文字留在输入框 →
            # 紧接着被 clear_input 抹掉"，结果是看着像发成功、实际什么都没发生（水位 392K→394K
            # 就是那次）。现在：再补一次回车；回车若仍被吞，文字留着——留在框里能看见、能重发，
            # 抹掉就成了一次假成功。
            try:
                pi.press_enter()
                _t.sleep(1.0)
                pi.press_enter()          # 第二次：已发出时等于空回车（无害）
            except Exception:
                pass
            pi.log({"at": now(), "len": len(COMPACT_CMD), "dry": False, "ok": True,
                    "msg": "已按回车两次；若输入框仍有文字，说明回车被吞（文字故意保留）",
                    "src": "compact", "cmd": COMPACT_CMD})
        # ── 链的第二、三段：等壳压完 → 重读设定（都写在账上）────────────────
        how, reload_ok, reload_msg, reload_action = "", False, "", ""
        if ok:
            _stage("压缩已发出，等壳压完（最多 %d 秒）…" % WAIT_COMPACT, WAIT_COMPACT)
            done, how = wait_compacted(ctx)
            print(("RENEW %s" if done else "RENEW? %s") % how)
            _stage(("壳已压完（%s），正在重读设定…" % how) if done
                   else ("没看到水位下降（%s），先重读设定…" % how),
                   RELOADER_TIMEOUT + 30)
            # 2026-11-12 修「压缩成了、重读被挡」：重读原来照常走闸门，人只要在打字
            # （他当然在打字——他在问我"怎么没开始压缩"）就被判 busy → 欠账。
            # 既然整条链已经弹窗请他停手，重读这一下也按"借键盘"算：
            # 先尽量等 3 秒静默，等不到也照发（草稿在下一段会还给他）。
            try:
                iok, iwhy = pi.wait_shell_idle(stable=3, timeout=25)
                print("IDLE %s %s" % ("OK" if iok else "SKIP", str(iwhy)[:60]))
            except Exception:              # noqa: BLE001
                pass
            reload_ok, reload_msg, reload_action = run_reloader(manual=True)
            print("RELOAD %s（%s）" % ("OK" if reload_ok else "FAIL", reload_msg))
        # ── 借键盘协议收尾：把他的草稿放回输入框（不回车）────────────────
        draft_msg = ""
        if draft:
            _stage("把草稿放回输入框（%d 字）…" % len(draft), 20)
            try:
                d_ok, d_msg = pi.restore_composer(draft)
                draft_msg = ("草稿已放回" if d_ok else "草稿放回失败：%s" % d_msg)
            except Exception as e:        # noqa: BLE001
                draft_msg = "草稿放回出错：%s" % str(e)[:80]
            print("DRAFT %s（%d 字）" % (draft_msg, len(draft)))
            if "失败" in draft_msg or "出错" in draft_msg:
                print("  草稿还在 %s，可以手动粘回" % _rel(draft_backup))
        # ── 借键盘收尾②：把他原来在看的窗口还回去（原来是浏览器/别的窗口就别占着）──
        try:
            _cur = (pi.pick_window() or (0,))[0]
            if prev_fg and prev_fg != _cur and pi.restore_foreground(prev_fg):
                print("FG 前台已还给原来的窗口（借键盘结束）")
        except Exception:                 # noqa: BLE001
            pass
        # 只有**真的发出去了**才记冷却：失败就进冷却会让一次误判拖满 900 秒，
        # 表现就是"明明没压，却说刚压过"（2026-11-12 10:19 踩到）。
        if ok:
            st["last_fire"] = time.time()
        save_state(st)
        pi.log({"at": now(), "len": len(COMPACT_CMD), "dry": False, "ok": ok,
                "msg": msg, "src": "compact", "cmd": COMPACT_CMD})
        rec = {"at": now(), "ctx": ctx, "action": "fired" if ok else "fail",
               "msg": msg, "source": source}
        rec.update(extra)
        if force:
            rec["manual"] = True
        if ok:
            rec.update(handoff_fingerprint())   # 这一笔 /compact 对应当时哪份交接包
            rec["compact_result"] = how
            rec["reload"] = {"ok": reload_ok, "action": reload_action, "msg": reload_msg}
            # 等下一次 check 来补 after_ctx / drop / result
            st["pending_fire"] = {"at": rec["at"], "ctx": ctx,
                                  "ts": st["last_fire"], "source": source}
            save_state(st)
        log(rec)
        # 终局：两段都成，倒计时自动关；有任何一段没成，窗留着要人看
        if ok and reload_ok:
            _final(True, "压缩 → 重读都走完\n%s\n%s" % (how, reload_msg))
        elif ok:
            _final(False, "压缩已发，但设定没重读回来\n%s\n%s" % (how, reload_msg))
        print(("OK   " if (ok and reload_ok) else "FAIL ")
              + "水位 %dK → /compact：%s｜重读：%s" % (ctx / 1000, msg, reload_msg or "未做"))
        return 0 if (ok and reload_ok) else 1
    finally:
        _seen()


def simulate_fired(ctx, source=None):
    """调试用：只记一笔 fired（不导出/不镜像/不注入），供核对 after_ctx / ineffective。"""
    source = resolve_source(source)
    ts = time.time()
    rec = {"at": now(), "ctx": int(ctx), "action": "fired", "source": source,
           "msg": "调试模拟（未实际注入）"}
    rec.update(handoff_fingerprint())
    log(rec)
    st = load_state()
    st["last_fire"] = ts
    st["last_ctx"] = int(ctx)
    st["last_check_at"] = ts
    st["pending_fire"] = {"at": rec["at"], "ctx": int(ctx), "ts": ts,
                          "source": source}
    save_state(st)
    print("SIM  已记一笔 fired：%dK（source=%s，handoff=%s）"
          % (int(ctx) // 1000, source, rec.get("handoff") or "无"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    c.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--ctx", type=int, default=0, help="极限实测：假装水位是这个数")
    c.add_argument("--force", action="store_true",
                   help="人工「立即压缩一次」：跳过水位阈值；镜像/等空闲/四道注入闸门一条不少")
    c.add_argument("--source", choices=SOURCE_KINDS, default=None,
                   help="记账来源：cron（定时/守护，pythonw 默认）/ tray（托盘）/ cli（人工）")
    c.add_argument("--simulate-fired", type=int, default=0, metavar="CTX",
                   help="调试：直接记一笔 fired（不注入），验证 after_ctx/drop/ineffective")
    c.add_argument("--no-mirror", action="store_true",
                   help="跳过镜像（只用于测试；生产环境不许用——没落盘就压等于拿记忆赌运气）")
    sub.add_parser("stats", help="今日/本周 fired（我们）/ shell（壳自压）/ idle 次数与逐条时刻")
    a = ap.parse_args()
    if a.cmd == "stats":
        return stats()
    if a.cmd != "check":
        ap.print_help()
        return 0
    if a.simulate_fired:
        return simulate_fired(a.simulate_fired, a.source)
    return check(a.threshold, a.cooldown, a.dry_run, not a.no_mirror, a.ctx,
                 force=a.force, source=a.source)


if __name__ == "__main__":
    sys.exit(main())
