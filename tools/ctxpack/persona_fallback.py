# -*- coding: utf-8 -*-
r"""persona_fallback.py — 保底判定：欠费或断网时，唤醒自动降级

只做判定，不改任何东西。三路输入，**命中任一即降级**：

1. **人工**（最可靠）：`ops\fallback.json` 的 `manual` 非空（如 `"欠费"`），
   人在客户端看到"余额不足"时一句话钉住。`manual="off"` 表示人确认已恢复。
2. **余额快照**：`balance_file` 指向一个 JSON（`{"balance": 3.2}` 之类），
   读到的数字 < `balance_threshold`（默认 1.0）就降级。**读不到不算降级**
   ——DSH 的余额没有可读接口（早期结论），所以这里只认人给的快照，绝不瞎猜。
3. **断网**：对 `offline_targets` 里的地址做 TCP 握手（**不带任何请求数据**），
   全连不上才算断网；默认只探一个口，1 秒超时，避免拖慢 30 秒心跳。

降级后谁用它：
  * `persona_wake.py` 简讯第一行给「⛑ 保底」；
  * `persona_autonomy.py` 档位直接走免费档（本地模型那条路，只花电费）；
  * 联网/付费类动作（抓取、联网检索、API 调用）在保底期一律跳过——
    判断入口就本文件，别处不再各写一份"是不是断网了"。

缓存：`ops\fallback-state.json`，60 秒内复用（`--force` 强制重探），
免得每次 tick 都握手。

命令行：
  python persona_fallback.py state [--json] [--force]
  python persona_fallback.py set --manual "欠费"      # 钉住降级
  python persona_fallback.py set --manual off         # 人确认恢复
  python persona_fallback.py set --threshold 2 --balance-file "…\balance.json"
库根：--root > SYSPERSONA_ROOT > PERSONA_ROOT > 本文件目录。
"""
import argparse
import io
import json
import os
import socket
import time

TTL = 60.0                       # 判定缓存秒数（心跳 30 秒，两拍之内复用同一结论）

DEFAULT_CFG = {
    "probe": True,
    "offline_targets": ["223.5.5.5:443"],   # 公共 DNS，只握手
    "balance_file": "",
    "balance_threshold": 1.0,
    "manual": "",
    "note": "保底判定：欠费/断网时唤醒自动降级；manual 非空即降级，off=人工确认恢复",
}


def root_of(root=None):
    return os.path.abspath(root or os.environ.get("SYSPERSONA_ROOT")
                           or os.environ.get("PERSONA_ROOT")
                           or os.path.dirname(os.path.abspath(__file__)))


def config_path(root=None):
    return os.path.join(root_of(root), "ops", "fallback.json")


def state_path(root=None):
    return os.path.join(root_of(root), "ops", "fallback-state.json")


def _read_json(path, default):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def load(root=None):
    path = config_path(root)
    cfg = _read_json(path, None)
    if not isinstance(cfg, dict):
        _write_json(path, dict(DEFAULT_CFG))
        return dict(DEFAULT_CFG)
    for k, v in DEFAULT_CFG.items():
        cfg.setdefault(k, v)
    return cfg


def save(cfg, root=None):
    _write_json(config_path(root), cfg)


def _tcp_ok(ep, timeout=1.0):
    host, _, port = str(ep).partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 443)),
                                      timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _resolve(path):
    """相对路径按库根解析（配置里写 `ops/sf-balance.json` 就够，不写死机器路径）。"""
    p = str(path or "").strip()
    if not p:
        return ""
    return p if os.path.isabs(p) else os.path.join(root_of(), p)


def _balance(cfg):
    """(数值, 说明, 明细)；读不到返回 (None, 原因, {})。绝不猜。

    支持两种写法：
      * 老：`balance_file` + `balance_threshold`（单个来源）；
      * 新：`balance_sources` = [{"path": ..., "threshold": 1.0, "label": "硅基"}, …]
        任一来源低于自己的阈值 → 降级，原因里写清是哪家、多少、阈值多少。
    """
    sources = cfg.get("balance_sources")
    if isinstance(sources, list) and sources:
        lowest = None
        detail = {"sources": []}
        for s in sources:
            if not isinstance(s, dict):
                continue
            path = _resolve(s.get("path"))
            try:
                thr = float(s.get("threshold") or 0)
            except (TypeError, ValueError):
                thr = 0.0
            label = str(s.get("label") or os.path.basename(path) or "余额")
            try:
                warn = float(s.get("warn") or 0)
            except (TypeError, ValueError):
                warn = 0.0
            data = _read_json(path, None) if path else None
            val = None
            if isinstance(data, dict):
                for key in ("balance", "total", "available", "remaining"):
                    try:
                        val = float(data.get(key)); break
                    except (TypeError, ValueError):
                        continue
            detail["sources"].append({"label": label, "value": val,
                                      "threshold": thr, "warn": warn, "path": path})
            if val is None:
                continue
            if lowest is None or val < lowest[0]:
                lowest = (val, label, thr)
        if lowest and lowest[2] > 0 and lowest[0] < lowest[2]:
            return lowest[0], "余额 %.2f < 阈值 %.2f（%s）" % (
                lowest[0], lowest[2], lowest[1]), detail
        return (lowest[0] if lowest else None), "", detail

    path = _resolve(cfg.get("balance_file"))
    if not path:
        return None, "没配余额快照", {}
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return None, "余额文件读不到：%s" % path, {}
    for key in ("balance", "total", "available", "remaining"):
        try:
            return float(data.get(key)), "来自 %s" % os.path.basename(path), {}
        except (TypeError, ValueError):
            continue
    return None, "余额文件里没有可认的数字字段", {}


def _decide(cfg):
    """(是否降级, 原因, 明细)。顺序：人工 > 余额 > 断网。"""
    detail = {}
    manual = str(cfg.get("manual") or "").strip()
    if manual and manual.lower() != "off":
        detail["manual"] = manual
        return True, "人工钉住：%s" % manual, detail

    try:
        thr = float(cfg.get("balance_threshold") or 0)
    except (TypeError, ValueError):
        thr = 0.0
    bal, why, bal_detail = _balance(cfg)
    detail["balance"] = bal
    detail["balance_why"] = why
    detail["balance_threshold"] = thr
    if bal_detail:
        detail["balance_sources"] = bal_detail.get("sources")
    if bal is not None and why:
        return True, why, detail

    targets = [t for t in (cfg.get("offline_targets") or []) if str(t).strip()]
    if cfg.get("probe") and targets:
        alive = [t for t in targets if _tcp_ok(t)]
        detail["probe"] = {"targets": targets, "alive": alive}
        if not alive:
            return True, "断网（%d 个目标全连不上）" % len(targets), detail
    return False, "", detail


def state(root=None, force=False):
    """当前是否保底。带 60 秒缓存；`force` 强制重探。"""
    root = root_of(root)
    cfg = load(root)
    if not force:
        cache = _read_json(state_path(root), None)
        if isinstance(cache, dict) and time.time() - float(cache.get("ts") or 0) < TTL:
            cache["cached"] = True
            return cache
    on, why, detail = _decide(cfg)
    snap = {"on": bool(on), "why": why, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(), "cached": False}
    snap.update(detail)
    try:
        _write_json(state_path(root), snap)
    except OSError:
        pass
    return snap


def line(root=None):
    """给唤醒简讯的一行。"""
    s = state(root)
    if s.get("on"):
        return ("⛑ 保底：%s —— 只做本机活（索引/检索/自检/压缩/重载），"
                "抓取·联网检索·付费动作一律跳过；本地模型在线就它上"
                % (s.get("why") or "已降级"))
    srcs = s.get("balance_sources") or []
    if srcs:
        bits, warns = [], []
        for r in srcs:
            v = r.get("value")
            txt = "¥%.2f" % v if isinstance(v, (int, float)) else "不可读"
            bits.append("%s %s" % (r.get("label"), txt))
            w = r.get("warn") or 0
            if isinstance(v, (int, float)) and w and v < w:
                warns.append("%s %s 偏低（警戒 %.2f）" % (r.get("label"), txt, w))
        tail = " · ".join(bits)
        if warns:
            tail += "；⚠ " + "；".join(warns)
        return "⛑ 保底：关（联网正常；%s）" % tail
    bal = s.get("balance")
    tail = "余额 %.2f" % bal if isinstance(bal, (int, float)) else "余额不可读"
    return "⛑ 保底：关（联网正常，%s）" % tail


def ds_balance_path(root=None):
    return os.path.join(root_of(root), "ops", "ds-balance.json")


def set_ds_balance(value, source="", root=None):
    """人工记一笔 DeepSeek 余额（接口读不到，只能人念数字）。

    人 2026-11-10 在客户端余额行看到 `¥2.7306` —— 这类数字程序拿不到，
    所以给一条明路：看见就记一笔，落 `ops\\ds-balance.json`，降级判定照用。
    """
    p = ds_balance_path(root)
    snap = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "balance": float(value),
            "currency": "CNY", "source": source or "人工录入"}
    _write_json(p, snap)
    return snap


def main(argv=None):
    ap = argparse.ArgumentParser(description="保底判定：欠费或断网时唤醒自动降级")
    ap.add_argument("cmd", nargs="?", default="state",
                    choices=["state", "set", "balance"])
    ap.add_argument("--root", default="")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--force", action="store_true", help="state 强制重探，不用缓存")
    ap.add_argument("--set", type=float, default=None, dest="set_value",
                    help="balance 子命令：记一笔 DeepSeek 余额（客户端里看到的数字）")
    ap.add_argument("--source", default="", help="balance 子命令：数字从哪来")
    ap.add_argument("--manual", default=None, help='set 钉住降级（如 "欠费"）/ off 表示恢复')
    ap.add_argument("--threshold", type=float, default=None, help="set 余额阈值")
    ap.add_argument("--balance-file", default=None, dest="balance_file",
                    help="set 余额快照 JSON 路径")
    ap.add_argument("--no-probe", action="store_true", help="set 关掉断网探测")
    a = ap.parse_args(argv)
    root = a.root or None

    if a.cmd == "balance":
        if a.set_value is None:
            snap = _read_json(ds_balance_path(root), None)
            print(json.dumps(snap or {"note": "还没记过"}, ensure_ascii=False, indent=1))
            return 0
        snap = set_ds_balance(a.set_value, a.source, root)
        print("已记：DeepSeek 余额 ¥%.4f（%s）→ %s"
              % (snap["balance"], snap["at"], ds_balance_path(root)))
        print(line(root))
        return 0

    if a.cmd == "set":
        cfg = load(root)
        changed = []
        if a.manual is not None:
            cfg["manual"] = "" if str(a.manual).lower() == "off" else str(a.manual)
            changed.append("manual=%s" % (cfg["manual"] or "off"))
        if a.threshold is not None:
            cfg["balance_threshold"] = float(a.threshold)
            changed.append("balance_threshold=%.2f" % a.threshold)
        if a.balance_file is not None:
            cfg["balance_file"] = a.balance_file
            changed.append("balance_file=%s" % (a.balance_file or "（空）"))
        if a.no_probe:
            cfg["probe"] = False
            changed.append("probe=false")
        save(cfg, root)
        print("已改：%s" % "；".join(changed) if changed else "没改任何东西")
        s = state(root, force=True)
        print("%s" % line(root))
        if a.as_json:
            print(json.dumps(s, ensure_ascii=False))
        return 0

    s = state(root, force=a.force)
    if a.as_json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(line(root))
        print("配置：%s" % config_path(root))
        print("缓存：%s（%.0f 秒内复用；--force 重探）" % (state_path(root), TTL))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
