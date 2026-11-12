# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_hermes.py — 换 Hermes 客户端的模型（它是文件驱动的，不跟 DSH 一样把选择藏在数据库里）
r"""persona_hermes.py — 换 Hermes 客户端的模型（它是文件驱动的，不跟 DSH 一样把选择藏在数据库里）

背景（2026-11-10 实测）：
  * DSH 客户端把「当前会话用哪个模型」存在自己的 Local Storage（leveldb，值带压缩），
    改 `cordis.patch.yml` 只影响**新会话**；改会话缓存会被它启动时覆盖回去。
  * Hermes 不一样：`<Hermes>\data\hermes-home\config.yaml` 里明明白白写着
      model: {provider, default, base_url, api_mode, api_key}
    换模型就是改这几行，重启窗口即生效。

用法：
  python persona_hermes.py show
  python persona_hermes.py use paid | cloud | local     # 云端收费 / 云端免费 / 本机 Herdsman
  python persona_hermes.py path                         # 打印配置文件路径

不含备份：Hermes 自己已经堆了一堆 .bak，我们按原子替换写，写完回读校验，失败零写入。
"""
import io
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）
import json
import os
import sys
import time

CONF = os.environ.get("SYSPERSONA_HERMES_CONF") or os.path.join(
    os.environ.get("SYSPERSONA_HERMES_HOME")
    or r_loc.hermes_home(),
    "config.yaml")

PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "profiles-hermes")
DB = os.path.join(os.path.dirname(CONF), "desktop-ui.sqlite")
UI_KEY = os.environ.get("SYSPERSONA_HERMES_UI_KEY") or \
    "hermes:last-used-model:managed:http%3A%2F%2F127.0.0.1%3A9120:default"

PROFILES = {
    "paid": {"provider": "deepseek", "default": "deepseek-flash",
             "base_url": "https://api.deepseek.com", "api_mode": "chat_completions"},
    "cloud": {"provider": "siliconflow", "default": "Qwen/Qwen2.5-7B-Instruct",
              "base_url": "https://api.siliconflow.cn/v1", "api_mode": "chat_completions"},
    "local": {"provider": "custom:127-0-0-1-8080", "default": "Qwen3.5_4b",
              "base_url": "http://127.0.0.1:8080/v1", "api_mode": "chat_completions"},
}


def read():
    with io.open(CONF, encoding="utf-8") as f:
        return f.read()


def write(text):
    tmp = CONF + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONF)


def get_model(text):
    """从 `model:` 块里取出 provider / default / base_url（只看缩进两格的那几行）。"""
    out = {}
    inb = False
    for ln in text.splitlines():
        if ln.rstrip() == "model:":
            inb = True
            continue
        if inb:
            if ln and not ln.startswith("  "):
                break
            s = ln.strip()
            for k in ("provider", "default", "base_url", "api_mode"):
                if s.startswith(k + ":"):
                    out[k] = s.split(":", 1)[1].strip()
    return out


def set_model(text, prof):
    lines = text.splitlines(True)
    start = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == "model:":
            start = i
            break
    if start is None:
        raise SystemExit("config.yaml 里找不到 `model:` 块")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip() and not lines[j].startswith("  "):
            end = j
            break
    block = ["model:\n"]
    for k in ("provider", "default", "base_url", "api_mode"):
        block.append("  %s: %s\n" % (k, prof[k]))
    # 保留原块里其它的键（context_length / api_key 等），只替换这四个
    for ln in lines[start + 1:end]:
        s = ln.strip()
        if any(s.startswith(k + ":") for k in ("provider", "default", "base_url", "api_mode")):
            continue
        block.append(ln)
    return "".join(lines[:start]) + "".join(block) + "".join(lines[end:])


def build():
    """以当前 config.yaml 为底，派生三套**整份**配置（只有 model: 块不同）。"""
    base = read()
    rows = []
    for name, prof in PROFILES.items():
        text_v = set_model(base, prof)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with io.open(os.path.join(PROFILE_DIR, name + ".yaml"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(text_v)
        got = get_model(text_v)
        rows.append((name, got.get("provider"), got.get("default"),
                     (got.get("provider"), got.get("default")) == (prof["provider"], prof["default"])))
    for name, prov, model, ok in rows:
        print("%s %-6s %s / %s" % ("OK" if ok else "X", name, prov, model))
    print("版本目录：%s" % PROFILE_DIR)
    return 0 if all(r[3] for r in rows) else 1


def versions():
    if not os.path.isdir(PROFILE_DIR):
        print("还没有版本（先跑 build）")
        return 2
    for name in sorted(PROFILES):
        p = os.path.join(PROFILE_DIR, name + ".yaml")
        if not os.path.exists(p):
            print("%-6s 缺失" % name)
            continue
        got = get_model(io.open(p, encoding="utf-8").read())
        print("%-6s %s / %s" % (name, got.get("provider"), got.get("default")))
    print("当前生效：%s" % get_model(read()))
    return 0


def set_ui_model(provider, model):
    r"""UI 自己也记着「上次用的模型」（desktop-ui.sqlite 的 ui_kv），config.yaml 只是初始值。

    不改它的话：状态栏显示本地档，输入框上的模型选择器还是 deepseek——2026-11-10 实测。
    这是正经 SQLite，一行 update 就够，比 DSH 的 leveldb 好办太多。
    """
    import sqlite3
    if not os.path.exists(DB):
        return False, "没有 desktop-ui.sqlite"
    try:
        con = sqlite3.connect(DB, timeout=10)
        cur = con.cursor()
        row = cur.execute("select value_json from ui_kv where key=?", (UI_KEY,)).fetchone()
        if not row:
            return False, "ui_kv 里没有这条（键名变了？）"
        old = json.loads(row[0]) if row[0] else {}
        old.setdefault("selection", {})
        old["selection"].update({"provider": provider, "model": model})
        old["ts"] = int(time.time() * 1000)
        cur.execute("update ui_kv set value_json=?, updated_at=? where key=?",
                    (json.dumps(old, ensure_ascii=False), int(time.time() * 1000), UI_KEY))
        con.commit()
        con.close()
        return True, "UI 记录已改：%s / %s" % (provider, model)
    except Exception as e:                 # noqa: BLE001
        return False, str(e)[:120]


def use_copy(name):
    """整份替换 config.yaml（和 DSH 一样：改文字会被写回去，整份换最稳）。"""
    src = os.path.join(PROFILE_DIR, name + ".yaml")
    if not os.path.exists(src):
        print("没有这套：%s（先跑 build）" % name)
        return 2
    raw = io.open(src, encoding="utf-8").read()
    want = PROFILES[name]
    got = get_model(raw)
    if (got.get("provider"), got.get("default")) != (want["provider"], want["default"]):
        print("版本文件本身不对，拒绝：%s" % got)
        return 1
    write(raw)
    ok_ui, msg_ui = set_ui_model(want["provider"], want["default"])
    now = get_model(read())
    ok = (now.get("provider"), now.get("default")) == (want["provider"], want["default"])
    print("已套用 Hermes 配置 %-6s → %s / %s%s" % (name, now.get("provider"),
          now.get("default"), "" if ok else "（回读不一致！）"))
    print("UI 记录：%s" % msg_ui)
    return 0 if ok else 1


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cmd = a[0] if a else "show"
    if not os.path.exists(CONF):
        print("找不到 Hermes 配置：%s" % CONF)
        return 2
    if cmd == "path":
        print(CONF)
        return 0
    text = read()
    if cmd == "show":
        print("配置：%s" % CONF)
        print("当前：%s" % get_model(text))
        return 0
    if cmd == "build":
        return build()
    if cmd == "versions":
        return versions()
    if cmd == "use-copy":
        return use_copy(a[1] if len(a) > 1 else "")
    if cmd == "use":
        name = a[1] if len(a) > 1 else ""
        prof = PROFILES.get(name)
        if not prof:
            print("用法：persona_hermes.py use paid | cloud | local")
            return 2
        new = set_model(text, prof)
        got = get_model(new)
        if (got.get("provider"), got.get("default")) != (prof["provider"], prof["default"]):
            print("校验没过，未写入：%s" % got)
            return 1
        write(new)
        print("已切换 Hermes → %s（%s / %s）" % (name, prof["provider"], prof["default"]))
        print("重启 Hermes 窗口后生效。")
        return 0
    print("用法：persona_hermes.py show | use paid|cloud|local | path")
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                      # noqa: BLE001
        pass
    raise SystemExit(main())
