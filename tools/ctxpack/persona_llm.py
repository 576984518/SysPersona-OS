# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_llm.py — 一个入口，两种底座：本机 Herdsman（免费）与硅基流动（花钱）
r"""persona_llm.py — 一个入口，两种底座：本机 Herdsman（免费）与硅基流动（花钱）

为什么要有它：记忆层自己不产字，字要底座出。底座有两条路，
一条只花电费（本机 Herdsman），一条按 token 花钱（硅基流动）。
两条路的调用姿势还不一样——本机那条**必须走流式**（非流式实测会挂到超时），
所以把这套差异收在这里，别让每个脚本各写一份。

用法：
  python persona_llm.py check                      # 两条底座都探一下（不花钱）
  python persona_llm.py models --provider local    # 本机在跑/停着的模型（只读 /v1/models）
  python persona_llm.py models --provider sf       # 硅基可用对话模型（一次 GET，不花 token）
  python persona_llm.py chat "回两个字：在的" --provider local --max-tokens 16
  python persona_llm.py chat "…" --provider sf --model zai-org/GLM-5.3-Flash

钥匙从哪来（**只读，绝不回显、绝不落盘**）：
  1. 环境变量 SILICONFLOW_API_KEY；
  2. 没有就读 Windows 用户环境变量（注册表，`winreg`）——守护进程是开机时起的，
     拿不到后来新加的环境变量，所以这一层兜底是必要的；
  3. 再没有就报「没配」，绝不猜。
输出里最多出现「长度 51 / 前缀 sk-i…」，不出现完整 key。

钱：`sf` 是**按 token 收费**的，只在你显式 `--provider sf` 时才走；
默认永远 `local`（只花电费）。本模块不做自动切换。
"""
import argparse
import json
import os
import sys
import time
import urllib.request

LOCAL_BASE = os.environ.get("PERSONA_LOCAL_LLM") or "http://127.0.0.1:8080/v1"
SF_BASE = os.environ.get("PERSONA_SF_BASE") or "https://api.siliconflow.cn/v1"
SF_ENV = "SILICONFLOW_API_KEY"
ROOT = os.path.dirname(os.path.abspath(__file__))
SF_FREE = os.path.join(ROOT, "ops", "sf-free.json")      # 日更的免费名单（persona_sf_models.py 抓）

# 任务分级（人 2026-11-10 定）：**日常任务一律降级到免费底座**，重要任务才值得花钱。
# 这里只给「日常」一个默认；「重要」不自动选路——得显式指定 provider/model，免得默认烧钱。
ROUTINE = ("日常", "routine", "daily", "自检", "索引", "index", "观察", "observe",
           "抓取", "capture", "整理", "归档", "summary", "摘要", "翻译", "分类")
IMPORTANT = ("重要", "important", "发布", "release", "审计", "audit", "改代码",
             "重构", "删除", "迁移", "决策")


def route(task):
    """任务名 → (provider, 说明)。日常=local；重要=不自动选（返回空）。"""
    t = str(task or "").strip().lower()
    if any(k in t for k in IMPORTANT):
        return "", "重要任务不自动选底座：显式给 --provider/--model（或走客户端里的贵模型）"
    if any(k in t for k in ROUTINE):
        return "local", "日常任务 → 本机免费底座（失败再试硅基免费档）"
    return "local", "没标级别，按日常处理 → 本机免费底座"

# 铁律（人 2026-11-10 定）：硅基只用**免费模型**。名单来自日更缓存，不写死在代码里。
# 要花钱必须显式 `--allow-paid`，默认一律拒。

# 本机列表里混着 TTS/OCR/ASR/向量/生图，挑对话模型时按名字剔除
NOT_CHAT = ("tts", "ocr", "asr", "sensevoice", "voxcpm", "zimage", "mineru",
            "bge-", "rerank", "embed", "speech", "whisper")


def _json(url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def sf_key():
    """(key, 来源)。环境变量 → 用户级注册表。**不打印值**。"""
    k = os.environ.get(SF_ENV) or ""
    if k:
        return k, "环境变量"
    try:
        import winreg                     # 只在 Windows 有；失败就当没配
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
            val, _ = winreg.QueryValueEx(h, SF_ENV)
        if val:
            return str(val), "用户环境变量（注册表）"
    except Exception:                     # noqa: BLE001
        pass
    return "", ""


def mask(k):
    return "%d 位，前缀 %s…" % (len(k), k[:4]) if k else "（未配置）"


def sf_free():
    """(免费模型 id 列表, 缓存时间, 是否过期)。名单来自 ops\\sf-free.json。"""
    ids, at, stale = _read_free()
    return ids, at, stale


def _read_free(max_hours=20.0):
    """读免费名单缓存；(ids, at, 是否过期)。超过 max_hours 就算过期。"""
    try:
        with open(SF_FREE, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return [], "", True
    ids = [str(x) for x in (d.get("free_ids") or [])]
    at = str(d.get("at") or "")
    stale = True
    try:
        import datetime as _dt
        stamp = _dt.datetime.fromisoformat(at)
        stale = (_dt.datetime.now() - stamp).total_seconds() > max_hours * 3600
    except (TypeError, ValueError):
        stale = not at
    return ids, at, stale


def refresh_free(force=False):
    """用之前先抓一次（人 2026-11-10 定）。

    调 `persona_sf_models.py --refresh`，它自己带 20 小时闸门与 24 小时 2 次上限，
    所以这里多叫几次也不会多抓。返回 (ok, 一句话)。
    """
    script = os.path.join(ROOT, "persona_sf_models.py")
    if not os.path.exists(script):
        return False, "找不到 persona_sf_models.py"
    cmd = [sys.executable, script, "--refresh"]
    if force:
        cmd.append("--force")
    try:
        import subprocess
        p = subprocess.run(cmd, capture_output=True, timeout=240,
                           cwd=ROOT)
        out = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        tail = out[-1] if out else ""
        return p.returncode == 0, tail[:160] or ("退出码 %d" % p.returncode)
    except Exception as e:                 # noqa: BLE001
        return False, "抓取失败：%s" % str(e)[:120]


def ensure_free(refresh=True):
    """确保免费名单是新的：(ids, at, 说明)。

    过期就当场抓一次；抓完还是过期 → 返回过期标记，调用方据此**拒绝**花钱的调用。
    """
    ids, at, stale = _read_free()
    if not stale:
        return ids, at, ""
    if not refresh:
        return ids, at, "名单已过期（%s），且本次 --no-refresh 不抓" % (at or "无缓存")
    ok, msg = refresh_free()
    ids2, at2, stale2 = _read_free()
    if not stale2:
        return ids2, at2, "已现抓：%s" % msg
    return ids2, at2, "抓了但仍过期/没抓到（%s）：%s" % (at2 or "无缓存", msg)


def pick_chat(rows):
    """从 /v1/models 里挑一个正在跑的对话模型；没有返回空串。"""
    for rec in rows or []:
        if not isinstance(rec, dict):
            continue
        mid = str(rec.get("id") or "")
        if not mid or str(rec.get("status") or "").lower() != "running":
            continue
        if any(bad in mid.lower() for bad in NOT_CHAT):
            continue
        return mid
    return ""


def local_models():
    d = _json(LOCAL_BASE + "/models", timeout=8)
    return d.get("data") or []


def sf_models():
    k, src = sf_key()
    if not k:
        raise SystemExit("没配 %s：环境变量和用户注册表都没有。%s 可用时用 --provider local。"
                         % (SF_ENV, "本机 Herdsman"))
    d = _json(SF_BASE + "/models?sub_type=chat",
              {"Authorization": "Bearer " + k}, timeout=20)
    return d.get("data") or []


def chat(prompt, provider="local", model="", max_tokens=256, timeout=180,
         allow_paid=False, refresh=True, fallback=True):
    """发一句、按流式收。返回 {provider, model, first_s, total_s, text, usage}。

    硅基一侧**只走免费模型**：不给 `--model` 就取免费名单第一个；
    给了但不在名单里，除非 `allow_paid=True`，否则直接拒（不花一分钱）。
    动硅基之前先确保名单是新的（人 2026-11-10：要用之前最好抓一次）；
    抓不到且名单过期 → **宁可不打**（陈旧名单可能把已收费的模型当免费）。
    """
    if provider == "sf":
        k, _ = sf_key()
        if not k:
            raise SystemExit("--provider sf 需要 %s（环境变量或用户注册表）" % SF_ENV)
        free_ids, at, note = ensure_free(refresh=refresh)
        if note:
            print("· %s" % note, file=sys.stderr)
        if not free_ids:
            raise SystemExit("没有免费名单（%s）；先跑 python persona_sf_models.py --refresh"
                             % SF_FREE)
        if not model:
            # 免费名单里优先挑"纯对话"的（有些免费模型只吐思考过程，content 是空的）
            pref = [m for m in free_ids if any(k in m.lower() for k in
                                               ("instruct", "-chat", "glm-4-9b"))]
            model = (pref or free_ids)[0]
        elif model not in free_ids and not allow_paid:
            raise SystemExit("硅基只用免费模型：%s 不在免费名单里。\n免费的有（%d 个）：%s\n"
                             "确实要花钱就加 --allow-paid"
                             % (model, len(free_ids), "、".join(free_ids[:8]) or "（名单空）"))
        base, key = SF_BASE, k
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    else:
        base = LOCAL_BASE
        if not model:
            try:
                model = pick_chat(local_models())
            except Exception:             # noqa: BLE001
                model = ""
        if not model:
            if fallback:
                print("· 本机没有在跑的对话模型 → 退到硅基免费档", file=sys.stderr)
                return chat(prompt, "sf", "", max_tokens, timeout, allow_paid, refresh)
            raise SystemExit("本机没有在跑的对话模型（只有 TTS/OCR 之类）；"
                             "在 Herdsman 里启动一个再试，或 --provider sf")
        headers = {"Content-Type": "application/json"}
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(max_tokens), "stream": True}
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers=headers)
    t0, first, chunks, usage, reasoning = time.time(), None, [], {}, []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            if d.get("usage"):
                usage = d["usage"]
            delta = ((d.get("choices") or [{}])[0].get("delta") or {})
            txt = delta.get("content") or ""
            think = delta.get("reasoning_content") or ""
            if think:
                reasoning.append(think)
            if txt:
                if first is None:
                    first = time.time() - t0
                chunks.append(txt)
    text = "".join(chunks)
    if not text and reasoning:        # 只有思考过程（有些免费模型就这样），别装作没回
        text = "".join(reasoning)
        usage = dict(usage or {}, note="只有思考内容（无正文）", model=model)
    return {"provider": provider, "model": model,
            "first_s": round(first, 2) if first is not None else None,
            "total_s": round(time.time() - t0, 2),
            "text": text, "usage": usage}


def main(argv=None):
    ap = argparse.ArgumentParser(description="两种底座一个入口：本机 Herdsman（免费）/ 硅基流动（花钱）")
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["check", "models", "chat"])
    ap.add_argument("prompt", nargs="?", default="")
    ap.add_argument("--provider", choices=["local", "sf"], default="local")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-tokens", type=int, default=256, dest="max_tokens")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--allow-paid", action="store_true", dest="allow_paid",
                    help="硅基：允许用不在免费名单里的模型（默认拒，一分钱不花）")
    ap.add_argument("--task", default="", help="任务级别：日常/自检/索引… 走免费；重要 不自动选")
    ap.add_argument("--no-refresh", action="store_true", dest="no_refresh",
                    help="硅基：名单过期也不现抓（默认会先抓一次）")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        out = {"local": {}, "sf": {}}
        try:
            rows = local_models()
            run = pick_chat(rows)
            out["local"] = {"base": LOCAL_BASE, "models": len(rows),
                            "chat_running": run,
                            "ok": bool(run)}
        except Exception as e:            # noqa: BLE001
            out["local"] = {"base": LOCAL_BASE, "ok": False, "why": str(e)[:120]}
        k, src = sf_key()
        free_ids, at, stale = sf_free()
        out["sf"] = {"base": SF_BASE, "key": mask(k), "key_from": src or "无",
                     "ok": bool(k), "free_models": len(free_ids),
                     "free_list_at": at, "free_list_stale": stale}
        if a.as_json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            lo = out["local"]
            print("本机底座（只花电费）：%s" % ("在线，对话模型 %s，共 %d 个模型"
                                          % (lo.get("chat_running"), lo.get("models", 0))
                                          if lo.get("ok") else "不可用：%s" % lo.get("why")))
            print("硅基流动（只用免费模型）：key %s（来自 %s）；免费名单 %d 个%s"
                  % (out["sf"]["key"], out["sf"]["key_from"], out["sf"]["free_models"],
                     "，⚠ 已过期（%s）" % out["sf"]["free_list_at"]
                     if out["sf"].get("free_list_stale") else ""))
        return 0

    if a.cmd == "models":
        if a.provider == "sf":
            free_ids, at, stale = sf_free()
            print("免费名单（%s%s）：" % (at or "无缓存", "，可能已过期" if stale else ""))
            for mid in free_ids:
                print("  ★ %s" % mid)
            print("（其余模型一律不列——硅基只用免费的；要全量自己跑 persona_sf_models.py --list）")
            return 0
        try:
            rows = local_models()
        except SystemExit as e:
            print(str(e))
            return 2
        for rec in rows:
            if a.as_json:
                print(json.dumps(rec, ensure_ascii=False))
            else:
                print("%-34s %s" % (str(rec.get("id")), str(rec.get("status") or "")))
        return 0

    if not a.prompt.strip():
        print('chat 要一句话：python persona_llm.py chat "你好" --provider local')
        return 2
    if a.task:
        provider, why = route(a.task)
        if not provider:
            print(why)
            return 2
        print("· %s" % why, file=sys.stderr)
        a.provider = provider

    try:
        r = chat(a.prompt.strip(), a.provider, a.model, a.max_tokens,
                 allow_paid=a.allow_paid, refresh=not a.no_refresh)
    except SystemExit as e:
        print(str(e))
        return 2
    if a.as_json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("[%s · %s] 首字 %s 秒 · 总 %s 秒" % (
            r["provider"], r["model"], r["first_s"], r["total_s"]))
        print(r["text"])
        if r.get("usage"):
            print("用量：%s" % json.dumps(r["usage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
