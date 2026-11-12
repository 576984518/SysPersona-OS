# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_vision.py — 看图：默认走本机 Windows OCR（免费、约 1 秒、不出网）
r"""persona_vision.py — 看图：默认走本机 Windows OCR（免费、约 1 秒、不出网）

背景：Hermes 主模型是纯文本；壳自带 vision_analyze 慢且会卡在工具返回后。
本机 Windows OCR 对截图（界面、表格、报错）又快又准，还不花钱。

用法：
  python persona_vision.py <图片>            # 默认：本机 OCR，直接出文字
  python persona_vision.py --latest          # 读 Hermes 最近上传的那张
  python persona_vision.py --ds <图片> [问题]    # **直接用 DeepSeek 的 API 看图**（推荐；花 token，但准）
  python persona_vision.py --vl <图片> "问题"  # 走云端 VL（默认免费档 PaddleOCR-VL；慢、一般）
  python persona_vision.py --vl <图片> --model Qwen/Qwen3-VL-8B-Instruct "问题"   # 收费档，慎用

云端 key 来源：环境变量 SILICONFLOW_API_KEY → hermes-home\.env → config.yaml（不写死）
"""
import base64
import persona_locate as _loc  # 本机位置统一解析（v6.11.0）
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

HOME = r_loc.hermes_home()
API = "https://api.siliconflow.cn/v1/chat/completions"
MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"
OCR_PS1 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "winocr.ps1")
NO_WINDOW = 0x08000000
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def find_key():
    k = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if k:
        return k
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
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
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


def deepseek_vl(path, prompt, model="deepseek-flash", max_tokens=1500):
    r"""直接用 DeepSeek 的 API 看图（2026-11-10 实测：它的 API 收 image_url，HTTP 200）。

    好处：不用 OCR、不用第三方 VL，模型本来就会读界面/表格。
    坑：推理类模型先想再答，max_tokens 给小了会全被 reasoning 吃掉、content 为空——
    所以这里给足预算，并在 content 为空时取 reasoning 的最后一段兜底。
    """
    key = _deepseek_key()
    if not key:
        return "（没有 DEEPSEEK_API_KEY，看不了）"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
        "max_tokens": max_tokens}
    req = urllib.request.Request("https://api.deepseek.com/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())["choices"][0]["message"]
    except Exception as e:                 # noqa: BLE001
        # 只发一次、不自动重试：排队慢的时候重试 = 反复扣钱（人 2026-11-10 提醒）
        return ("（DeepSeek 看图失败，未重试：%s）\n"
                "如果是排队/超时：等一会儿再来一次，别脚本里反复重试。" % str(e)[:120])
    out = (d.get("content") or "").strip()
    if not out:
        out = (d.get("reasoning_content") or "").strip()
    return out or "（模型没给内容）"


def _deepseek_key():
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            return winreg.QueryValueEx(reg, "DEEPSEEK_API_KEY")[0]
    except Exception:                      # noqa: BLE001
        return ""


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    mode = "local"
    if args[0] == "--vl":
        mode = "vl"
        args = args[1:]
    elif args[0] == "--ds":
        mode = "ds"
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
    if mode == "ds":
        prompt = " ".join(rest).strip() or "把这张图里的内容说清楚：有什么字、什么界面、在讲什么。"
        print(deepseek_vl(path, prompt))
    elif mode == "vl":
        prompt = " ".join(rest).strip() or "识别并整理这张图里的文字与数字，按原文结构输出。"
        print(cloud_vl(path, prompt, model))
    else:
        print(local_ocr(path))


if __name__ == "__main__":
    main()
