# -*- coding: utf-8 -*-
r"""persona_io.py — 读写小工具的唯一实现（v6.11.0 收 B85 的第一批）

为什么要有它：`_read_json` / `_write_json` 这两个名字在十几个脚本里各有一份，但**契约并不相同**：

| 谁 | 读失败返回 | 编码 | 是否要求 dict |
|---|---|---|---|
| 有的脚本 | `{}` | `utf-8-sig` | 是 |
| 有的脚本 | `None` | `utf-8` | 否 |
| 有的脚本 | 抛异常 | `utf-8-sig` | 否 |

所以**不能按名字批量合并**——那样一定会改掉某处的错误语义。这里的规矩是：
**按契约分批收，不按名字收**。这个模块只提供三种明确的契约，谁符合谁来用：

    read_json(path, default={})     读不到/坏掉 → 返回 default（默认空 dict）
    read_json_or_none(path)         读不到/坏掉 → None
    read_text(path, default="")     读不到 → default
    write_json(path, obj)           原子写：同目录临时文件 → os.replace（Windows 上读句柄占着会重试）

原子写为什么重要：读的人只会看到"完整的旧版"或"完整的新版"，不会读到写了一半的 JSON——
后台进程和前台同时读同一个状态文件是常态。
"""

import io
import json
import os
import time

DEFAULT_RETRIES = 5


def _mkdirs(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def read_json(path, default=None, encoding="utf-8-sig"):
    r"""读 JSON；读不到、坏掉、或顶层不是 dict → 返回 `default`（默认 `None`）。

    顶层必须是 dict：这些文件都是"状态/配置"，顶层是 list 说明文件被别的程序写坏了，
    这时**返回默认值比返回半个结构好**——调用处按 dict 用，不会在下一行炸。
    """
    try:
        with io.open(path, encoding=encoding) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default
    if not isinstance(data, dict):
        return {} if default is None else default
    return data


def read_json_any(path, default=None, encoding="utf-8-sig"):
    r"""读 JSON，**不要求顶层是 dict**（给存 list 的文件用）。"""
    try:
        with io.open(path, encoding=encoding) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_text(path, default="", encoding="utf-8", errors="replace"):
    try:
        with io.open(path, encoding=encoding, errors=errors) as f:
            return f.read()
    except OSError:
        return default


def write_json(path, obj, retries=DEFAULT_RETRIES, indent=1):
    r"""原子写 JSON。

    Windows 上 `os.replace` 会撞"另一个进程正持有目标文件"（杀软/索引器/前台读取），
    所以失败就**退避重试**，不是一次就放弃——一次就放弃等于随机丢状态。
    """
    _mkdirs(path)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), time.time_ns())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    last = None
    for i in range(max(1, retries)):
        try:
            os.replace(tmp, path)
            return True
        except OSError as e:
            last = e
            time.sleep(0.05 * (i + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise OSError("原子写失败（%d 次）：%s（%s）" % (retries, path, last))


def append_jsonl(path, rec):
    """一行一条追加；失败不抛（日志写不进不该把主流程弄死）。"""
    _mkdirs(path)
    try:
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


if __name__ == "__main__":
    import sys
    import tempfile

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = tempfile.mkdtemp(prefix="pio-")
    p = os.path.join(d, "a.json")
    print("读不存在的：", read_json(p))
    print("读不存在的（有默认）：", read_json(p, {"x": 1}))
    write_json(p, {"ok": True})
    print("写后读回：", read_json(p))
    print("原子写临时文件清理：", [x for x in os.listdir(d) if x.endswith(".tmp")] or "无残留")
