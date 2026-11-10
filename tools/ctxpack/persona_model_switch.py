# -*- coding: utf-8 -*-
r"""persona_model_switch.py — 把客户端的「默认模型」在 收费 / 免费 / 本地 之间切换（只改一个 patch 文件）

背景：DSH 的模型选择不看 UI，而看
  `~\.dsh\profiles\desktop\cordis.patch.yml` 里的两处：
    - `llm-pi-ai` 的 `providers`（第三方/自托管路由，OpenAI 兼容的写法见插件 README）
    - `agent-default-model` 的 `provider` / `model`
所以"空闲降级"落到文件上就是这两处：
    收费 → `deepseek-account` + `deepseek-flash`（内置路由，providers 里不用写）
    免费 → `siliconflow` 路由 + 一个**免费模型**
    本地 → `herdsman-local` 路由 + 本机 Herdsman（127.0.0.1:8080）的对话模型

原则（都是硬约束）：
  * **只动这一个文件**，动之前先备份 `cordis.patch.yml.bak-<时间戳>`；
  * 不写任何密钥进文件：云端用 `apiKeyEnv: SILICONFLOW_API_KEY`（按请求从凭据 seam 取）；
  * 本地无鉴权，但 pi-ai 的 OpenAI 兼容实现仍要求一个 API key 占位符，
    所以本地路由用 `apiKeyEnv: HERDSMAN_API_KEY`，值是一个**非密钥占位串**
    `local-placeholder`（由本脚本写入 HKCU\Environment，函数 ensure_local_key 说明）；
  * 免费模型名来自日更缓存 `ops\sf-free.json`（不写死），给了名单外的模型就拒绝；
  * 文本外科手术式替换（保留注释与原格式），改完回读校验两处都对上，否则回滚。

用法：
  python persona_model_switch.py show
  python persona_model_switch.py free [--model Qwen/Qwen2.5-7B-Instruct]
  python persona_model_switch.py paid
  python persona_model_switch.py local [--model Qwen3:8B]
  python persona_model_switch.py revert                 # 回到最近一次备份
注意：改完要**重启客户端（或新开会话）**才吃到新默认；重启交给 persona_watchdog.py 做。
"""
import argparse
import io
import json
import os
import shutil
import sys
import time
import urllib.request

PATCH = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop",
                     "cordis.patch.yml")
ROOT = os.path.dirname(os.path.abspath(__file__))
SF_FREE = os.path.join(ROOT, "ops", "sf-free.json")
PROFILE_DIR = os.path.join(ROOT, "ops", "profiles")   # 固定版本，切档=整份复制

FREE_PROVIDER = "siliconflow"
FREE_MODEL_FALLBACKS = ["Qwen/Qwen2.5-7B-Instruct", "THUDM/glm-4-9b-chat"]
PAID_PROVIDER = "deepseek-account"
PAID_MODEL = "deepseek-flash"

LOCAL_PROVIDER = "herdsman-local"
LOCAL_BASE = os.environ.get("SYSPERSONA_LOCAL_BASE", "http://127.0.0.1:8080/v1")
LOCAL_KEY_ENV = "HERDSMAN_API_KEY"          # 占位凭据的环境变量名（值不是密钥）
LOCAL_KEY_VALUE = "local-placeholder"
LOCAL_MODEL_FALLBACKS = ["Qwen3:8B", "Qwen3.5_9b", "Qwen3.5_4b"]

# 三套固定版本：名字 → (provider, 指定模型或 None=自动挑, 说明)
VERSIONS = {
    "paid": (PAID_PROVIDER, PAID_MODEL, "云端主 · DeepSeek 收费"),
    "cloud": (FREE_PROVIDER, None, "云端备 · 硅基免费档"),
    "local": (LOCAL_PROVIDER, None, "本地 · Herdsman"),
}
LOCAL_MAX_MODELS = 3


def read():
    with io.open(PATCH, encoding="utf-8") as f:
        return f.read()


def write(text):
    tmp = PATCH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PATCH)


def backup():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = PATCH + ".bak-" + stamp
    shutil.copy2(PATCH, dst)
    return dst


def free_models():
    """免费且未下线的对话模型 id（下线的不要，避免 404）。"""
    try:
        with io.open(SF_FREE, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    ids = [str(x) for x in (d.get("free_ids") or [])]
    rows = d.get("models") or []
    dead = {str(m.get("id")) for m in rows if m.get("deprecated")}
    alive = [i for i in ids if i not in dead]
    # 优先「instruct / chat」这类通用对话模型，其余排后面
    pref = [i for i in alive if any(k in i.lower() for k in ("instruct", "-chat"))]
    return pref + [i for i in alive if i not in pref]


def local_models():
    """本机 Herdsman 的对话模型（排除向量/重排/语音/OCR/画图）。"""
    try:
        with urllib.request.urlopen(LOCAL_BASE.rstrip("/") + "/models", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        ids = [str(m.get("id")) for m in (data.get("data") or [])]
    except Exception:                      # noqa: BLE001
        return list(LOCAL_MODEL_FALLBACKS)
    bad = ("embed", "rerank", "tts", "ocr", "asr", "voice", "image", "mineru",
           "sensevoice", "voxcpm", "zimage", "paddle")
    keep = [i for i in ids if i and not any(b in i.lower() for b in bad)]
    if not keep:
        return list(LOCAL_MODEL_FALLBACKS)
    # 优先用 LOCAL_MODEL_FALLBACKS 里点名的（按大小/能力排过序），其余按服务端顺序接在后面
    pref = [m for m in LOCAL_MODEL_FALLBACKS if m in keep]
    return (pref + [m for m in keep if m not in pref])[:LOCAL_MAX_MODELS]


def providers_block(free_model):
    """`providers:` 的规范内容：云端免费档 + 本地档（两条都在，切换只改默认模型）。"""
    free = [free_model] + [m for m in free_models() if m != free_model]
    free = free[:4]
    out = ["    providers:\n",
           "      %s:\n" % FREE_PROVIDER,
           "        displayName: 硅基流动（免费档）\n",
           "        apiKeyEnv: SILICONFLOW_API_KEY\n",
           "        api: openai-completions\n",
           "        baseURL: https://api.siliconflow.cn/v1\n",
           "        models:\n"]
    for m in free:
        out.append("          - id: %s\n" % m)
        out.append("            name: %s（免费）\n" % m.split("/")[-1])
        out.append("            contextWindow: 32768\n")
    out += ["      %s:\n" % LOCAL_PROVIDER,
            "        displayName: 本地（Herdsman）\n",
            "        apiKeyEnv: %s\n" % LOCAL_KEY_ENV,
            "        api: openai-completions\n",
            "        baseURL: %s\n" % LOCAL_BASE,
            "        models:\n"]
    for m in local_models():
        out.append("          - id: %s\n" % m)
        out.append("            name: %s（本地）\n" % m)
        out.append("            contextWindow: 32768\n")
    return "".join(out)


def set_providers(text, free_model):
    """把整段 `providers:` 换成规范内容（先删旧块，再插新块）。"""
    lines = text.splitlines(True)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("    providers:"):
            start = i
            break
    if start is None:
        raise SystemExit("patch 里找不到 `    providers:` —— 结构变了，先看文件再动")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and not ln.startswith("      ") and not ln.startswith("    "):
            end = j
            break
    return "".join(lines[:start]) + providers_block(free_model) + "".join(lines[end:])


def set_default(text, provider, model):
    out, in_block, done_p, done_m = [], False, False, False
    for ln in text.splitlines(True):
        if ln.startswith("- id: agent-default-model"):
            in_block = True
            out.append(ln)
            continue
        if in_block:
            if ln.startswith("- id:") and not ln.startswith("- id: agent-default-model"):
                in_block = False
            elif ln.strip().startswith("provider:"):
                out.append("    provider: %s\n" % provider)
                done_p = True
                continue
            elif ln.strip().startswith("model:"):
                out.append("    model: %s\n" % model)
                done_m = True
                continue
        out.append(ln)
    if not (done_p and done_m):
        raise SystemExit("没在 agent-default-model 里同时找到 provider / model，停下")
    return "".join(out)


def current(text):
    """(provider, model, 免费路由是否已配, 本地路由是否已配)。"""
    prov = model = ""
    in_block = False
    for ln in text.splitlines():
        if ln.startswith("- id: agent-default-model"):
            in_block = True
            continue
        if in_block:
            if ln.startswith("- id:"):
                in_block = False
            elif ln.strip().startswith("provider:"):
                prov = ln.split(":", 1)[1].strip()
            elif ln.strip().startswith("model:"):
                model = ln.split(":", 1)[1].strip()
    return (prov, model,
            ("      %s:" % FREE_PROVIDER) in text,
            ("      %s:" % LOCAL_PROVIDER) in text)


def ensure_local_key():
    r"""本地路由的占位凭据：非密钥，写进 HKCU\Environment 的 HERDSMAN_API_KEY。

    为什么需要：pi-ai 的 OpenAI 兼容实现要求 key 或 Authorization 头，无鉴权的本地
    服务器也得给个占位符（见 llm-pi-ai README 的「未认证路由取决于其协议」）。
    这里刻意不用任何真密钥——占位串本身没有价值，写进配置也不违规；
    写成环境变量是为了「配置文件里永远只有引用、没有值」这条硬规矩。
    """
    if os.environ.get(LOCAL_KEY_ENV):
        return "已存在（环境变量）"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ) as k:
        # 读一下有没有人已经设过
            try:
                val, _ = winreg.QueryValueEx(k, LOCAL_KEY_ENV)
                if val:
                    os.environ[LOCAL_KEY_ENV] = str(val)
                    return "已存在（用户环境变量）"
            except FileNotFoundError:
                pass
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, LOCAL_KEY_ENV, 0, winreg.REG_SZ, LOCAL_KEY_VALUE)
        os.environ[LOCAL_KEY_ENV] = LOCAL_KEY_VALUE
        return "已写入 local-placeholder（非密钥）"
    except Exception as e:                 # noqa: BLE001
        os.environ[LOCAL_KEY_ENV] = LOCAL_KEY_VALUE
        return "写环境变量失败（%s），仅本次进程内生效" % e


def _version_path(name):
    return os.path.join(PROFILE_DIR, name + ".yml")


def cmd_build():
    """以当前文件为底，派生三套**完整**版本；三套的 providers 完全一致，只有默认模型不同。"""
    base = read()
    free0 = (free_models() or FREE_MODEL_FALLBACKS)[0]
    rows = []
    for name, (prov, model, label) in VERSIONS.items():
        if name == "paid":
            keep, text_v = PAID_MODEL, set_default(base, PAID_PROVIDER, PAID_MODEL)
        elif name == "cloud":
            keep = model or free0
            text_v = set_default(set_providers(base, keep), FREE_PROVIDER, keep)
        else:
            keep = model or (local_models() or LOCAL_MODEL_FALLBACKS)[0]
            text_v = set_default(set_providers(base, free0), LOCAL_PROVIDER, keep)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with io.open(_version_path(name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text_v)
        got_p, got_m, _, _ = current(text_v)
        rows.append((name, label, got_p, got_m, (got_p, got_m) == (prov, keep)))
    for name, label, p, m, ok in rows:
        print("%s %-6s %-24s → %s / %s" % ("✔" if ok else "✘", name, label, p, m))
    print("版本目录：%s" % PROFILE_DIR)
    return 0 if all(r[4] for r in rows) else 1


def cmd_versions():
    if not os.path.isdir(PROFILE_DIR):
        print("还没有版本（先跑 build）")
        return 2
    for name in sorted(VERSIONS):
        p = _version_path(name)
        if not os.path.exists(p):
            print("%-6s %-24s 缺失" % (name, VERSIONS[name][2]))
            continue
        prov, model, _, _ = current(io.open(p, encoding="utf-8").read())
        print("%-6s %-24s %s / %s  （%.1f KB，%s）"
              % (name, VERSIONS[name][2], prov, model, os.path.getsize(p) / 1024.0,
                 time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))))
    prov, model, _, _ = current(read())
    print("当前生效：%s / %s" % (prov, model))
    return 0


def cmd_use(name):
    src = _version_path(name)
    if not os.path.exists(src):
        print("没有这套版本：%s（先跑 build，或 versions 看有哪些）" % name)
        return 2
    raw = io.open(src, encoding="utf-8").read()
    prov, model, _, _ = current(raw)
    if not (prov and model):
        print("这套版本里读不到 provider/model，拒绝套用")
        return 1
    backup()
    write(raw)
    got_p, got_m, _, _ = current(read())
    if (got_p, got_m) != (prov, model):
        print("套用后校验没过：%s / %s" % (got_p, got_m))
        return 1
    print("已套用版本 %-6s → %s · %s" % (name, got_p, got_m))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="切换客户端默认模型（收费 / 硅基免费档 / 本地）")
    ap.add_argument("cmd", nargs="?", default="show",
                    choices=["show", "free", "paid", "local", "revert", "build", "versions", "use"])
    ap.add_argument("--model", default="", help="free/local 时指定模型")
    ap.add_argument("name", nargs="?", default="", help="use 时指定版本名（paid/cloud/local）")
    a = ap.parse_args(argv)

    if not os.path.exists(PATCH):
        print("找不到 patch 文件：%s" % PATCH)
        return 2
    text = read()

    if a.cmd == "show":
        prov, model, has_sf, has_local = current(text)
        print("patch: %s" % PATCH)
        print("默认模型：provider=%s · model=%s" % (prov or "?", model or "?"))
        print("硅基免费路由：%s" % ("已配" if has_sf else "没配"))
        print("本地路由：%s" % ("已配" if has_local else "没配"))
        ids = free_models()
        print("免费名单：%d 个%s" % (len(ids), ("，例如 " + "、".join(ids[:3])) if ids else ""))
        print("本地模型：%s" % ("、".join(local_models()) or "（服务没起）"))
        return 0

    if a.cmd == "revert":
        cands = sorted([p for p in os.listdir(os.path.dirname(PATCH))
                        if p.startswith("cordis.patch.yml.bak-")])
        if not cands:
            print("没有备份可回滚")
            return 2
        src = os.path.join(os.path.dirname(PATCH), cands[-1])
        backup()
        shutil.copy2(src, PATCH)
        print("已回滚到 %s（回滚前的版本另存为一份 .bak）" % cands[-1])
        return 0

    if a.cmd == "free":
        ids = free_models()
        model = a.model.strip() or (ids or FREE_MODEL_FALLBACKS)[0]
        if ids and model not in ids:
            print("拒绝：%s 不在免费名单里（%d 个免费：%s）"
                  % (model, len(ids), "、".join(ids[:8])))
            return 2
        if not ids and model not in FREE_MODEL_FALLBACKS:
            print("拒绝：没有免费名单缓存，只能用内置保守项（%s）" % "、".join(FREE_MODEL_FALLBACKS))
            return 2
        bak = backup()
        write(set_default(set_providers(text, model), FREE_PROVIDER, model))
        prov, got, has_sf, _ = current(read())
        if (prov, got, has_sf) != (FREE_PROVIDER, model, True):
            shutil.copy2(bak, PATCH)
            print("校验没过，已回滚：%s / %s / %s" % (prov, got, has_sf))
            return 1
        print("已切到免费档：%s · %s\n备份：%s\n**重启客户端或新开会话才生效**"
              % (prov, got, os.path.basename(bak)))
        return 0

    if a.cmd == "paid":
        bak = backup()
        write(set_default(text, PAID_PROVIDER, PAID_MODEL))
        prov, got, _, _ = current(read())
        if (prov, got) != (PAID_PROVIDER, PAID_MODEL):
            shutil.copy2(bak, PATCH)
            print("校验没过，已回滚：%s / %s" % (prov, got))
            return 1
        print("已切回收费档：%s · %s\n备份：%s\n**重启客户端或新开会话才生效**"
              % (prov, got, os.path.basename(bak)))
        return 0

    if a.cmd == "local":
        cands = local_models()
        model = a.model.strip() or (cands or LOCAL_MODEL_FALLBACKS)[0]
        if a.model.strip() and cands and model not in cands:
            print("拒绝：%s 不在本机模型列表里（%s）" % (model, "、".join(cands)))
            return 2
        key_note = ensure_local_key()
        free0 = free_models() or FREE_MODEL_FALLBACKS
        bak = backup()
        write(set_default(set_providers(text, free0[0]), LOCAL_PROVIDER, model))
        prov, got, _, has_local = current(read())
        if (prov, got, has_local) != (LOCAL_PROVIDER, model, True):
            shutil.copy2(bak, PATCH)
            print("校验没过，已回滚：%s / %s / %s" % (prov, got, has_local))
            return 1
        print("已切到本地档：%s · %s\n占位凭据：%s\n备份：%s\n**重启客户端或新开会话才生效**"
              % (prov, got, key_note, os.path.basename(bak)))
        return 0

    # ── 版本化：把整份文件存成几套，切档 = 整份复制 ─────────────────────
    if a.cmd == "build":
        return cmd_build()

    if a.cmd == "versions":
        return cmd_versions()

    if a.cmd == "use":
        return cmd_use(a.name)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
