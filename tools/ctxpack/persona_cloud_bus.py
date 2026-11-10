# -*- coding: utf-8 -*-
r"""persona_cloud_bus.py — 简易网络同步（写分片 / 读总索引 / 断网降级）

设计（2026-11-10，来源见 `docs/CLOUD_BUS_SPEC.md`）：

- 只假设「上层是一个普通可读写目录」；盘符/挂载/凭据由部署决定（环境变量 PERSONA_CLOUD）；
- 两个区：`Hot_Bus\`（各进程独占写自己的分片，客户端不同步它）+ `Cold_Backup\`（压缩冷备）；
- 写一律 **tmp → rename**（原子），删改一律移进 `Archive\`，不真删；
- 总索引 `MASTER_INDEX.md` 只在**持锁**时由主节点改写，其余节点只读；
- 网盘不可用（断网 / 没挂载）时，**写入自动降级成本地排队**，不挂死、不丢；恢复后 `--flush` 补交。

本机约定：
  $env:PERSONA_CLOUD      网盘根：一个普通可读写目录；不设则一律降级排队
  本地排队                ops\cloud-queue.jsonl

用法：
  python persona_cloud_bus.py --status          # 网盘可用？排队多少？
  python persona_cloud_bus.py --flush           # 把本地排队写进网盘（幂等）
  python persona_cloud_bus.py --read            # 打印总索引（读端只读这个）
  python persona_cloud_bus.py --shard "会话总结" --body-file x.md   # 写一片
"""
import argparse
import io
import json
import os
import sys
import time

import persona_win  # noqa: F401  （GUI 进程里 spawn 不弹窗）

ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
QUEUE = os.path.join(OPS, "cloud-queue.jsonl")
STATE = os.path.join(OPS, "cloud-bus.json")
CLOUD = os.environ.get("PERSONA_CLOUD", "").strip()

HOT = "Hot_Bus"
COLD = "Cold_Backup"
ARCHIVE = "Archive"
MASTER = "MASTER_INDEX.md"
LOCK = ".master.lock"


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def node_id():
    """节点名：机器名-角色。写进分片名，便于多机区分。"""
    host = (os.environ.get("COMPUTERNAME") or "pc").lower()
    role = os.environ.get("PERSONA_NODE") or "dsh"
    return "%s-%s" % (host, role)


# ── 可用性 ───────────────────────────────────────────────────────────
def cloud_available():
    try:
        return os.path.isdir(CLOUD)
    except OSError:
        return False


def ensure_dirs():
    for sub in (HOT, COLD, ARCHIVE):
        os.makedirs(os.path.join(CLOUD, sub), exist_ok=True)


# ── 原子写 ───────────────────────────────────────────────────────────
def atomic_write(path, text):
    """tmp → rename：客户端不会看到半截文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── 文件锁：总索引只有一个写手 ────────────────────────────────────────
class FileLock:
    def __init__(self, path, timeout=10.0):
        self.path = path
        self.timeout = timeout
        self.fh = None

    def __enter__(self):
        import msvcrt
        deadline = time.time() + self.timeout
        self.fh = open(self.path, "a+")
        while True:
            try:
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    raise TimeoutError("总索引被占用：%s" % self.path)
                time.sleep(0.1)

    def __exit__(self, *exc):
        import msvcrt
        try:
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()
        return False


# ── 本地排队（断网降级） ──────────────────────────────────────────────
def queue_append(item):
    os.makedirs(OPS, exist_ok=True)
    with io.open(QUEUE, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def queue_load():
    if not os.path.exists(QUEUE):
        return []
    out = []
    for line in io.open(QUEUE, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def queue_clear():
    if os.path.exists(QUEUE):
        os.replace(QUEUE, QUEUE + ".done-%s" % time.strftime("%Y%m%d-%H%M%S"))


# ── 写一片分片 ───────────────────────────────────────────────────────
def shard_name(title):
    safe = "".join(c for c in title if c not in '\\/:*?"<>|').strip()[:40] or "shard"
    return "%s__%s__%s.md" % (node_id(), safe, time.strftime("%Y%m%d-%H%M%S"))


def write_shard(title, body, kind="会话总结"):
    """写一片：能上云就上云（tmp→rename），上不去就本地排队。"""
    item = {"at": now(), "node": node_id(), "kind": kind,
            "title": title, "body": body, "shard": shard_name(title)}
    if not cloud_available():
        queue_append(item)
        return False, "网盘不可用 → 已排队（%d 条待补交）" % len(queue_load())
    try:
        ensure_dirs()
        atomic_write(os.path.join(CLOUD, HOT, item["shard"]), body)
        update_master(item)
        return True, "已写入 %s\\%s" % (HOT, item["shard"])
    except Exception as e:                     # 盘掉线/权限/空间满，一律降级
        queue_append(item)
        return False, "写入失败已排队：%s" % e


def flush():
    """把本地排队补交到网盘（幂等：同名分片覆盖 + 总索引重算）。"""
    items = queue_load()
    if not items:
        return True, "队列为空"
    if not cloud_available():
        return False, "网盘仍不可用，%d 条继续排队" % len(items)
    ok = 0
    try:
        ensure_dirs()
        for it in items:
            atomic_write(os.path.join(CLOUD, HOT, it["shard"]), it.get("body") or "")
            update_master(it)
            ok += 1
    except Exception as e:
        return False, "补交中断（已提交 %d/%d）：%s" % (ok, len(items), e)
    queue_clear()
    return True, "已补交 %d 条" % ok


# ── 总索引：单写手 + 文件锁 ──────────────────────────────────────────
def _read_master_rows():
    p = os.path.join(CLOUD, MASTER)
    rows = []
    if not os.path.exists(p):
        return rows
    for line in io.open(p, encoding="utf-8", errors="replace"):
        if not line.startswith("| ") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("分片", "节点"):   # 表头不算数据
            continue
        rows.append(cells)
    return rows


def update_master(item):
    """持锁重写 MASTER_INDEX.md：分片清单 + 各节点最近一次上报。"""
    path = os.path.join(CLOUD, MASTER)
    lock = os.path.join(CLOUD, LOCK)
    with FileLock(lock):
        rows = _read_master_rows()
        rows = [r for r in rows if r[0] != item["shard"]]
        rows.append([item["shard"], item.get("node", ""), item.get("kind", ""), item.get("at", "")])
        rows.sort(key=lambda r: r[3], reverse=True)
        lines = ["# 网盘总索引（MASTER_INDEX）", "",
                 "> 只由持有 `%s` 的节点改写；其余节点**只读**。" % LOCK, "",
                 "| 分片 | 节点 | 类型 | 写入时间 |", "|---|---|---|---|"]
        lines += ["| " + " | ".join(r[:4]) + " |" for r in rows[:500]]
        atomic_write(path, "\n".join(lines) + "\n")


def read_master():
    p = os.path.join(CLOUD, MASTER)
    if not cloud_available():
        return False, "网盘不可用（读端降级：用本地缓存）", ""
    if not os.path.exists(p):
        return True, "总索引还没建立", ""
    return True, "ok", io.open(p, encoding="utf-8", errors="replace").read()


# ── 状态 ─────────────────────────────────────────────────────────────
def status():
    q = len(queue_load())
    st = {"at": now(), "cloud": CLOUD, "available": cloud_available(),
          "queued": q, "master": os.path.join(CLOUD, MASTER)}
    try:
        os.makedirs(OPS, exist_ok=True)
        atomic_write(STATE, json.dumps(st, ensure_ascii=False, indent=1))
    except OSError:
        pass
    return st


def main():
    ap = argparse.ArgumentParser(description="网盘索引总线")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--flush", action="store_true")
    ap.add_argument("--read", action="store_true")
    ap.add_argument("--shard", default="")
    ap.add_argument("--body-file", default="")
    ap.add_argument("--kind", default="会话总结")
    a = ap.parse_args()

    if a.status or not any([a.flush, a.read, a.shard]):
        st = status()
        print("网盘      : %s（%s）" % (st["cloud"], "可用" if st["available"] else "不可用"))
        print("本地排队  : %d 条" % st["queued"])
        print("总索引    : %s" % st["master"])
        return
    if a.flush:
        ok, msg = flush()
        print(("OK  " if ok else "FAIL ") + msg)
        return
    if a.read:
        ok, msg, text = read_master()
        if text:
            print(text)
        else:
            print(("OK  " if ok else "FAIL ") + msg)
        return
    if a.shard:
        body = ""
        if a.body_file and os.path.exists(a.body_file):
            body = io.open(a.body_file, encoding="utf-8").read()
        ok, msg = write_shard(a.shard, body, a.kind)
        print(("OK  " if ok else "QUEUED ") + msg)


if __name__ == "__main__":
    main()
