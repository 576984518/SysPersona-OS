# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_sched.py — 闹钟 / 守护的优先级仲裁（租约，v1）
r"""persona_sched.py — 闹钟 / 守护的优先级仲裁（租约，v1）

同一件事（job）两边都想干时，不是「先到先得」，而是**按优先级让位**：

  * 优先级数字**越小越高**；
  * 闹钟（alarm）高、守护（daemon）低；compact-check 默认 alarm=1、daemon=2；
  * 只有低优先级的一方会跳过；高优先级的一方不看租约，天然不被挡
    （所以「守护跑完写租约」不会反过来挡住闹钟）；
  * 未登记的 job，两边都按 100 算 → 互不让位（等价于没有仲裁）。

`should_skip()` 只有三条同时成立才返回 True：
  ① 有租约；② 租约未过期；③ 租约 owner ≠ 自己，且 owner 的优先级数字更小。

存储（都在 <root>\ops\）：
  sched-priority.json  优先级表；首次运行自动生成默认表
  sched-lease.json     租约 / 执行态；临时文件 + os.replace 原子替换
  sched-runs.jsonl     可抢占执行账本（job / priority / rc / ms / preempted_by / out）

可抢占执行（v2）：
  `run_preemptible()` 开跑前写 running 租约，运行期每 0.5 秒看有没有优先级
  数字更小的新来者；有就 terminate（Windows 先 taskkill /T），结果记
  `preempted_by`。`request_preempt()` 由高优先级任务开跑前调用，把 pending
  写进低优先级任务的租约。persona_compact / persona_mirror / git / 写
  sessions、archive、续接 的命令标 `nonpreemptible`，不杀，排队等下次。

用法：
  python persona_sched.py status [--root <库根>]
  python persona_sched.py release compact-check --owner daemon [--root <库根>]

库根优先级：--root > 环境变量 SYSPERSONA_ROOT > PERSONA_ROOT > 本文件所在目录。

接入点（2026-11-10）：
  * 闹钟：persona_wake.fire_due() 跑成功（rc==0）且闹钟带 job 字段时
    `claim(job, "alarm", ttl=every 或 300, next_due=下一次 due)`；
  * 守护：persona_daemon 的 worker 动手前 `should_skip(job, "daemon")`，
    为 True 直接记「让位给闹钟：…」并跳过；跑完 `claim(job, "daemon", ttl=任务间隔)`
    只供 status 观察，不阻断闹钟。
"""
import argparse
import io
import json
import locale
import os
import re
import subprocess
import time

DEFAULT_PRIORITY = {"jobs": {"compact-check": {"alarm": 1, "daemon": 2}}}
DEFAULT_PRIO = 100          # 未登记的 job / 未登记的一侧
PRIORITY_FILE = "sched-priority.json"
LEASE_FILE = "sched-lease.json"
RUNS_FILE = "sched-runs.jsonl"
PREEMPT_POLL = 0.5         # 运行期轮询租约的间隔
KILL_GRACE = 3             # terminate 后等多久再 kill
RUN_LEASE_TTL = 130        # running 租约没有 until 时的兜底有效期（> 120 秒超时）

# 守护任务名 → 仲裁 job：两边叫法不一样时在这里登记；同名任务不必登记。
TASK_JOB = {"reload": "compact-check"}

OWNER_LABEL = {"alarm": "闹钟", "daemon": "守护"}


def root_of(root=None):
    """库根：显式参数 > SYSPERSONA_ROOT > PERSONA_ROOT > 本文件目录。"""
    return os.path.abspath(root or os.environ.get("SYSPERSONA_ROOT")
                           or os.environ.get("PERSONA_ROOT")
                           or os.path.dirname(os.path.abspath(__file__)))


def _paths(root=None):
    ops = os.path.join(root_of(root), "ops")
    return ops, os.path.join(ops, PRIORITY_FILE), os.path.join(ops, LEASE_FILE)


def _read_json(path, default):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else default
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    """原子写：同目录临时文件 → os.replace；Windows 上读句柄占着时重试几次。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), time.time_ns())
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        for _ in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def load_priority(root=None):
    """读优先级表；文件不存在时自动落一份默认表。坏文件不覆盖，按默认表用。"""
    _, ppath, _ = _paths(root)
    if not os.path.exists(ppath):
        _write_json(ppath, DEFAULT_PRIORITY)
        return json.loads(json.dumps(DEFAULT_PRIORITY))
    data = _read_json(ppath, {})
    if not isinstance(data.get("jobs"), dict):
        return json.loads(json.dumps(DEFAULT_PRIORITY))
    return data


def _prio(table, job, owner, default=DEFAULT_PRIO):
    cfg = (table.get("jobs") or {}).get(job) or {}
    try:
        return int(cfg.get(owner, default))
    except (TypeError, ValueError):
        return default


def priority(job, owner, root=None, default=DEFAULT_PRIO):
    return _prio(load_priority(root), job, owner, default)


def _runs_path(root=None):
    return os.path.join(root_of(root), "ops", RUNS_FILE)


def job_for_task(task, root=None):
    """守护任务名 → 优先级表里的 job；没登记返回 None。

    依次匹配：同名 job → 表里可选的 "task" 字段 → TASK_JOB 别名。
    """
    jobs = load_priority(root).get("jobs") or {}
    if task in jobs:
        return task
    for job, cfg in jobs.items():
        if isinstance(cfg, dict) and cfg.get("task") == task:
            return job
    alias = TASK_JOB.get(task)
    return alias if alias in jobs else None


def _lease_active(lease, now):
    """租约是否还算「活着」：有 until 看 until；running 没 until 看 at+兜底。"""
    if not isinstance(lease, dict):
        return False
    if float(lease.get("until") or 0) > now:
        return True
    return (lease.get("state") == "running"
            and float(lease.get("at") or 0) + RUN_LEASE_TTL > now)


def claim(job, owner, ttl=300, next_due=None, note="", root=None):
    """占位：写一条 job 租约（原子替换）。

    低优先级的一方**不覆盖**仍在有效期内的更高优先级租约——守护跑完回头
    记账时，不能把刚被闹钟占上的位子抹掉。返回实际生效的那条租约（dict）。
    """
    _, _, lpath = _paths(root)
    now = time.time()
    leases = _read_json(lpath, {})
    table = load_priority(root)          # 首次 claim 就把默认优先级表落下来
    cur = leases.get(job)
    if isinstance(cur, dict) and cur.get("owner") and cur.get("owner") != owner:
        cur_prio = _prio(table, job, cur["owner"])
        my_prio = _prio(table, job, owner)
        if _lease_active(cur, now) and cur_prio < my_prio:
            return cur
    entry = {"job": job, "owner": owner, "at": now, "until": now + float(ttl),
             "next_due": next_due, "note": note}
    # run_preemptible 写的执行态字段要留着，claim 只补「占位到什么时候」。
    if isinstance(cur, dict):
        for k in ("priority", "pid", "state", "nonpreemptible", "preempted_by"):
            if k in cur:
                entry[k] = cur[k]
    leases[job] = entry
    _write_json(lpath, leases)
    return entry


def release(job, owner, root=None):
    """释放租约；只删自己（owner）的那条。返回是否真的删了。"""
    _, _, lpath = _paths(root)
    leases = _read_json(lpath, {})
    cur = leases.get(job)
    if not isinstance(cur, dict) or cur.get("owner") != owner:
        return False
    del leases[job]
    _write_json(lpath, leases)
    return True


def _label(owner):
    return OWNER_LABEL.get(owner, owner or "?")


NONPREEMPTIBLE_MARKERS = ("脚本/persona_compact.py", "脚本/persona_mirror.py")
NONPREEMPTIBLE_DIRS = ("sessions/", "sessions\\", "archive/", "archive\\",
                       "续接/", "续接\\")


def _nonpreemptible(cmd):
    """白名单：压缩/镜像/git/写会话归档交接的命令，谁都不许 terminate。"""
    low = (cmd or "").lower()
    if any(m in low for m in NONPREEMPTIBLE_MARKERS):
        return True
    if re.search(r"(^|[\s\"'\\/])git(\.exe)?(\s|$)", low):
        return True
    if re.search(r"\b(sessions|archive)\b|续接", low):
        return True
    return any(p.lower() in low for p in NONPREEMPTIBLE_DIRS)


def _decode(raw):
    """子进程可能是 UTF-8，也可能是本机 ANSI（cp936）；别把中文记成乱码。"""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", locale.getpreferredencoding(False) or "gbk", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _append_runs(rec, root=None):
    path = _runs_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _kill_tree(proc):
    """先 taskkill /T 清子进程（Windows），再 terminate；最多等 3 秒就 kill。"""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=2,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=KILL_GRACE)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _preempt_signal(job, priority, started, root=None):
    """看有没有「优先级数字更小」的新来者。返回对方 job 或 ""。"""
    _, _, lpath = _paths(root)
    leases = _read_json(lpath, {})
    own = leases.get(job) if isinstance(leases.get(job), dict) else {}
    pending = own.get("pending")
    if isinstance(pending, dict) and pending.get("job") != job:
        try:
            pp = int(pending.get("priority", DEFAULT_PRIO))
        except (TypeError, ValueError):
            pp = DEFAULT_PRIO
        if pp < int(priority):
            return pending.get("job") or "?"
    for key, val in leases.items():
        if key == job or not isinstance(val, dict):
            continue
        if val.get("state") != "running":
            continue
        try:
            vp = int(val.get("priority", DEFAULT_PRIO))
        except (TypeError, ValueError):
            vp = DEFAULT_PRIO
        if vp < int(priority) and float(val.get("at") or 0) >= started - 1:
            return key
    return ""


def request_preempt(job, priority, owner="daemon", root=None):
    """高优先级开跑前调：把 pending 写进正在跑的低优先级租约。

    带 nonpreemptible 标志的任务不写 pending（不杀，排队等下次）。
    返回 {"signaled": [...], "blocked": [...]}。
    """
    _, _, lpath = _paths(root)
    leases = _read_json(lpath, {})
    now = time.time()
    signaled, blocked = [], []
    for key, val in list(leases.items()):
        if key == job or not isinstance(val, dict) or val.get("state") != "running":
            continue
        try:
            vp = int(val.get("priority", DEFAULT_PRIO))
        except (TypeError, ValueError):
            vp = DEFAULT_PRIO
        if vp <= int(priority):
            continue
        if val.get("nonpreemptible"):
            blocked.append(key)
            continue
        val["pending"] = {"job": job, "priority": int(priority),
                          "owner": owner, "at": now}
        signaled.append(key)
    if signaled:
        _write_json(lpath, leases)
    return {"signaled": signaled, "blocked": blocked}


def run_preemptible(job, priority, cmd, timeout=120, owner="alarm", root=None, env=None):
    """可抢占执行：先占租约 → 每 0.5 秒看有没有更高优先级来抢 → 记账。

    白名单命令标 nonpreemptible，不 terminate；别人看到只会排队等下次。
    返回 dict（job / priority / rc / ms / preempted_by / state / out / timeout / started）。
    """
    root = root_of(root)
    job = str(job or "job")
    priority = int(priority)
    started = time.time()
    cmd = (cmd or "").strip()
    nonpre = _nonpreemptible(cmd)
    request_preempt(job, priority, owner=owner, root=root)

    _, _, lpath = _paths(root)
    leases = _read_json(lpath, {})
    cur = leases.get(job) if isinstance(leases.get(job), dict) else {}
    entry = dict(cur)
    entry.update({"job": job, "owner": owner, "priority": priority,
                  "pid": os.getpid(), "at": started, "state": "running",
                  "nonpreemptible": bool(nonpre),
                  "preempted_by": cur.get("preempted_by") or ""})
    entry.pop("pending", None)
    leases[job] = entry
    _write_json(lpath, leases)

    rc, summary, preempted_by, timed_out, state = 0, "", "", False, "idle"
    if cmd:
        child_env = dict(os.environ if env is None else env)
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=root, env=child_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            rc, summary = -1, ("启动失败：%s" % exc)[:400]
        else:
            raw = b""
            while True:
                try:
                    raw, _ = proc.communicate(timeout=PREEMPT_POLL)
                    break
                except subprocess.TimeoutExpired:
                    if not nonpre:
                        who = _preempt_signal(job, priority, started, root)
                        if who:
                            preempted_by = who
                            _kill_tree(proc)
                            break
                    if time.time() - started >= timeout:
                        timed_out = True
                        _kill_tree(proc)
                        break
            text = _decode(raw)
            summary = " ".join(text.split())[:400]
            if preempted_by:
                rc, state = -15, "preempted"
            elif timed_out:
                rc, summary = -9, "超时（%d 秒）" % timeout
            else:
                rc = proc.returncode if proc.returncode is not None else -1
    ms = int((time.time() - started) * 1000)

    leases = _read_json(lpath, {})
    cur = leases.get(job) if isinstance(leases.get(job), dict) else {}
    cur.update({"job": job, "owner": owner, "priority": priority, "pid": 0,
                "at": started, "state": state, "nonpreemptible": bool(nonpre),
                "preempted_by": preempted_by or cur.get("preempted_by") or "",
                "rc": rc, "ms": ms})
    cur.pop("pending", None)
    leases[job] = cur
    _write_json(lpath, leases)

    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "job": job,
           "priority": priority, "rc": rc, "ms": ms,
           "preempted_by": preempted_by,
           "status": ("interrupted" if preempted_by else
                      ("timeout" if timed_out else "done")),
           "out": summary}
    _append_runs(rec, root)
    return {"job": job, "priority": priority, "rc": rc, "ms": ms,
            "preempted_by": preempted_by, "state": state,
            "out": summary, "timeout": timed_out,
            "nonpreemptible": bool(nonpre), "started": started}


def _hms(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "?"


def _next_label(next_due):
    if next_due is None or next_due == "":
        return "—"
    try:
        return time.strftime("%H:%M", time.localtime(float(next_due)))
    except (TypeError, ValueError, OSError):
        return "?"


def should_skip(job, owner, root=None):
    """低优先级的一方要不要让位。返回 (skip, reason)，reason 是一句人话。"""
    _, _, lpath = _paths(root)
    now = time.time()
    lease = (_read_json(lpath, {}) or {}).get(job)
    if not isinstance(lease, dict):
        return False, "无租约，可以跑"
    until = float(lease.get("until") or 0)
    other = lease.get("owner") or ""
    if not _lease_active(lease, now):
        if until:
            return False, "租约已过期（到 %s），可以跑" % _hms(until)
        return False, "无有效租约，可以跑"
    if other == owner:
        return False, "自己持有的租约（到 %s），不跳过" % _hms(until)
    table = load_priority(root)
    other_prio = _prio(table, job, other)
    my_prio = _prio(table, job, owner)
    if other_prio < my_prio:
        reason = "让位给%s：%s 已跑 %s（%s优先级 %d < %s %d），下次 %s" % (
            _label(other), _hms(lease.get("at")), job,
            _label(other), other_prio, _label(owner), my_prio,
            _next_label(lease.get("next_due")))
        return True, reason
    return False, "%s优先级 %d 不高于%s %d，不跳过" % (
        _label(other), other_prio, _label(owner), my_prio)


def status(root=None):
    """小表：job / 租约 owner / 到期 / state / 被谁打断 / 优先级。"""
    table = load_priority(root)
    _, _, lpath = _paths(root)
    leases = _read_json(lpath, {})
    jobs = set((table.get("jobs") or {}).keys()) | set(leases.keys())
    now = time.time()
    rows = []
    for job in sorted(jobs):
        lease = leases.get(job) if isinstance(leases.get(job), dict) else {}
        owner = lease.get("owner") or "—"
        until = float(lease.get("until") or 0)
        state = lease.get("state") or "idle"
        if state == "running" and until <= now:
            expiry = "运行中"
        elif until:
            remain = int(until - now)
            expiry = "%s（%s）" % (_hms(until),
                                  "剩 %ds" % remain if remain > 0 else "已过期")
        else:
            expiry = "—"
        preempted_by = lease.get("preempted_by") or "—"
        pa = _prio(table, job, "alarm")
        pd = _prio(table, job, "daemon")
        pri = "alarm %d / daemon %d" % (pa, pd)
        if lease.get("priority") is not None:
            pri = "%s（alarm %d / daemon %d）" % (lease["priority"], pa, pd)
        rows.append((job, owner, expiry, state, preempted_by, pri))
    return rows


def _cmd_status(a):
    root = getattr(a, "root", "") or None
    _, ppath, lpath = _paths(root)
    rows = status(root)
    print("job            租约 owner  到期                    状态       被谁打断       优先级")
    print("-" * 96)
    if not rows:
        print("（没有登记 job，也没有租约）")
    for job, owner, expiry, state, by, pri in rows:
        print("%-14s %-10s %-22s %-9s %-14s %s" % (job, owner, expiry, state, by, pri))
    print("")
    print("优先级表：%s" % ppath)
    print("租约文件：%s" % lpath)
    return 0


def _cmd_release(a):
    if not a.owner:
        print("release 需要 --owner（只释放自己的租约）")
        return 2
    root = getattr(a, "root", "") or None
    if release(a.job, a.owner, root=root):
        print("已释放 %s（owner=%s）" % (a.job, a.owner))
        return 0
    print("没释放：%s 没有 %s 的租约" % (a.job, a.owner))
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="闹钟 / 守护优先级仲裁")
    ap.add_argument("--root", default="", help="库根（默认 SYSPERSONA_ROOT / 本文件目录）")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("status", help="打印 job / 租约 / 优先级小表")
    sp.add_argument("--root", default=argparse.SUPPRESS)
    sp = sub.add_parser("release", help="释放自己持有的租约")
    sp.add_argument("job")
    sp.add_argument("--owner", default="")
    sp.add_argument("--root", default=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return _cmd_status(a)
    if a.cmd == "release":
        return _cmd_release(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
