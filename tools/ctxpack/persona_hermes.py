# -*- coding: utf-8 -*-
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
import os
import sys
import time

CONF = os.environ.get("SYSPERSONA_HERMES_CONF") or os.path.join(
    os.environ.get("SYSPERSONA_HERMES_HOME")
    or r"D:\ProgramData\Hermes Agent CN Desktop\data\hermes-home",
    "config.yaml")

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
