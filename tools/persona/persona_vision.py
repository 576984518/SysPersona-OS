# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_vision.py — 看图：默认走本机 Windows OCR（免费、约 1 秒、不出网）
r"""persona_vision.py — 看图：默认走本机 Windows OCR（免费、约 1 秒、不出网）

背景：Hermes 主模型是纯文本；壳自带 vision_analyze 慢且会卡在工具返回后。
本机 Windows OCR 对截图（界面、表格、报错）又快又准，还不花钱。

用法：
  python persona_vision.py <图片>            # 默认：本机 OCR，直接出文字
  python persona_vision.py --latest          # 读 Hermes 最近上传的那张
  python persona_vision.py --vl <图片> "问题"  # 走云端 VL（默认免费档 PaddleOCR-VL；慢、一般）
  python persona_vision.py --vl <图片> --model Qwen/Qwen3-VL-8B-Instruct "问题"   # 收费档，慎用

云端 key 来源：环境变量 SILICONFLOW_API_KEY → hermes-home\.env → config.yaml（不写死）

环境变量：PERSONA_HERMES_HOME = Hermes 家目录（.env / config.yaml / images 都在它下面）；
PERSONA_PY 不需要，本脚本不拉别的 Python。没配 PERSONA_HERMES_HOME 时 `--latest` 直接
报错、`--vl` 只认环境变量里的 key——不会退回某台机器的路径。
"""
import base64
import io
import json
import os
import re
import subprocess
try:
    import persona_win as _pw  # 无窗口：GUI 进程里 spawn 控制台程序不弹黑窗
except Exception:
    pass

import sys
import urllib.request

try:
    from persona_root import hermes_home as _hermes_home
except ImportError:   # 仓库布局：tools\persona\ → tools\ctxpack\
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ctxpack"))
    from persona_root import hermes_home as _hermes_home

HOME = _hermes_home()
API = "https://api.siliconflow.cn/v1/chat/completions"
MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"
OCR_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "winocr.ps1")
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def find_key():
    k = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if k:
        return k
    if not HOME:
        return None      # 没配 PERSONA_HERMES_HOME：只认环境变量
    envf = os.path.join(HOME, ".env")
    if os.path.exists(envf):
        for line in io.open(envf, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*SILICONFLOW_API_KEY\s*=\s*(\S+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    cf = os.path.join(HOME, "config.yaml")
    if os.path.exists(cf):
        for line in io.open(cf, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*api_key:\s*(sk-\S+)", line)
            if m:
                return m.group(1).strip()
    return None


def latest_image():
    if not HOME:
        raise SystemExit("没配置 PERSONA_HERMES_HOME（Hermes 家目录），--latest 用不了；"
                         "请设这个变量，本脚本不再猜机器路径。")
    d = os.path.join(HOME, "images")
    files = [os.path.join(d, f) for f in os.listdir(d)
             if os.path.splitext(f)[1].lower() in MIME]
    if not files:
        raise SystemExit("没有找到图片：%s" % d)
    return max(files, key=os.path.getmtime)


def local_ocr(path):
    if not os.path.exists(OCR_PS1):
        raise SystemExit("缺少本机 OCR 脚本：%s" % OCR_PS1)
    p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", OCR_PS1, path],
                       capture_output=True, text=True, timeout=90,
                       encoding="utf-8", errors="replace")
    out = "\n".join(l for l in (p.stdout or "").splitlines() if not l.startswith("LANG:")).strip()
    if not out:
        raise SystemExit("本机 OCR 没认出文字（图片可能是照片/无文字）：%s" % (p.stderr or "")[:200])
    return out


def cloud_vl(path, prompt, model):
    key = find_key()
    if not key:
        raise SystemExit("找不到 SiliconFlow key")
    ext = os.path.splitext(path)[1].lower()
    if ext not in MIME:
        raise SystemExit("不支持的图片格式：%s" % ext)
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (MIME[ext], b64)}},
        {"type": "text", "text": prompt}]}], "max_tokens": 1200}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    return out["choices"][0]["message"]["content"].strip()


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    mode = "local"
    if args[0] == "--vl":
        mode = "vl"
        args = args[1:]
    model = MODEL
    if "--model" in args:
        i = args.index("--model")
        model = args[i + 1]
        del args[i:i + 2]
    path = latest_image() if (not args or args[0] == "--latest") else args[0]
    rest = [a for a in args if a not in ("--latest", path)]
    if not os.path.exists(path):
        raise SystemExit("文件不存在：%s" % path)
    if mode == "vl":
        prompt = " ".join(rest).strip() or "识别并整理这张图里的文字与数字，按原文结构输出。"
        print(cloud_vl(path, prompt, model))
    else:
        print(local_ocr(path))


if __name__ == "__main__":
    main()
