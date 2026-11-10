# -*- coding: utf-8 -*-
r"""persona_session_model.py — 改「当前会话记住的模型」（让切档对正在用的对话也生效）

为什么需要它（2026-11-10 实测）：
  * `cordis.patch.yml` 的 `agent-default-model` 只管**新会话**；
  * 正在用的会话在 `~\.dsh\storages\session_projcache\sessions\session-<id>.json` 里
    存着一条 `record.rows.modelSelection.val.lastUsed = {provider, model}`，开窗时恢复的是它；
  * 这条不在会话日志（zstd）里，所以只改配置 = 当前对话照旧用老模型。

产物：把那条记录改成目标 provider/model。**不保留备份**（会话文件每次都不一样，存备份会堆垃圾）：
  * 先在内存里改 → 写 `<file>.tmp` → 回读校验（JSON 能解、字段对得上）→ 原子替换 → 删 tmp；
  * 任何一步不对就零写入，原文件不动；
  * 只在 `ops\session-patch.log` 记一行（时间 / 会话 id / 改什么 / sha256 前后各 16 位）。

用法：
  python persona_session_model.py show [--session <id>]
  python persona_session_model.py set <provider> <model> [--session <id>] [--dry]
  python persona_session_model.py newest           # 打印会话文件的「路径与属主」说明
"""
import argparse
import glob
import hashlib
import io
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".dsh", "storages", "session_projcache", "sessions")
ROOT = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.join(ROOT, "ops")
LOG = os.path.join(OPS, "session-patch.log")


def find(session_id=""):
    """找会话文件：给了 id 就按 id；否则取最近改动的那个（= 现在这个对话）。"""
    if session_id:
        hits = glob.glob(os.path.join(CACHE_DIR, "session-%s*.json" % session_id))
        return hits[0] if hits else ""
    files = glob.glob(os.path.join(CACHE_DIR, "session-*.json"))
    return max(files, key=os.path.getmtime) if files else ""


def load(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def get_last_used(d):
    rows = (d.get("record") or {}).get("rows") or {}
    ms = rows.get("modelSelection") or {}
    return (ms.get("val") or {}).get("lastUsed") or {}


def set_last_used(d, provider, model, drop_reasoning=False):
    rows = (d.get("record") or {}).get("rows") or {}
    ms = rows.setdefault("modelSelection", {})
    val = ms.setdefault("val", {})
    lu = val.get("lastUsed") or {}
    before = dict(lu)
    lu["provider"] = provider
    lu["model"] = model
    if drop_reasoning:
        lu.pop("reasoningEffort", None)   # 本地/免费档没这档思考强度，留着会报错
    val["lastUsed"] = lu
    return before


def sha16(b):
    return hashlib.sha256(b).hexdigest()[:16]


def write_atomic(path, data_bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cmd_show(a):
    p = find(a.session)
    if not p:
        print("找不到会话文件（%s）" % CACHE_DIR)
        return 2
    d = load(p)
    print("会话文件：%s（%.0f KB，改于 %s）"
          % (os.path.basename(p), os.path.getsize(p) / 1024,
             time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(p)))))
    print("记住的模型：%s" % json.dumps(get_last_used(d), ensure_ascii=False))
    return 0


def cmd_set(a):
    p = find(a.session)
    if not p:
        print("找不到会话文件（%s）" % CACHE_DIR)
        return 2
    raw = open(p, "rb").read()
    d = json.loads(raw.decode("utf-8-sig"))
    before = set_last_used(d, a.provider, a.model, a.drop_reasoning)
    after = get_last_used(d)
    text = json.dumps(d, ensure_ascii=False)
    if a.dry:
        print("会话：%s" % os.path.basename(p))
        print("%s → %s" % (json.dumps(before, ensure_ascii=False),
                           json.dumps(after, ensure_ascii=False)))
        print("--dry：未写入")
        return 0
    # 回读校验：把要写的字节再解一遍，确认字段对得上
    new_bytes = text.encode("utf-8")
    check = json.loads(new_bytes.decode("utf-8"))
    got = get_last_used(check)
    if got.get("provider") != a.provider or got.get("model") != a.model:
        print("校验没过，未写入")
        return 1
    write_atomic(p, new_bytes)
    line = "%s %s %s/%s sha %s -> %s bytes %d -> %d\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(p),
        a.provider, a.model, sha16(raw), sha16(new_bytes), len(raw), len(new_bytes))
    try:
        os.makedirs(OPS, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print("已改（原子替换，没留备份）：%s → %s／%s"
          % (os.path.basename(p), a.provider, a.model))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="改当前会话记住的模型")
    ap.add_argument("cmd", nargs="?", default="show", choices=["show", "set", "newest"])
    ap.add_argument("provider", nargs="?", default="")
    ap.add_argument("model", nargs="?", default="")
    ap.add_argument("--session", default="")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--drop-reasoning", action="store_true", dest="drop_reasoning",
                    help="切到本地/免费档时去掉 reasoningEffort（那档没有这个选项）")
    a = ap.parse_args(argv)

    if a.cmd == "newest":
        p = find("")
        print(p or "（没有会话文件）")
        return 0
    if a.cmd == "set":
        if not (a.provider and a.model):
            print("用法：persona_session_model.py set <provider> <model>")
            return 2
        return cmd_set(a)
    return cmd_show(a)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
