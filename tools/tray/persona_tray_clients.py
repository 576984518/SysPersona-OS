# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：persona_tray_multi.py — 托盘：一个我们自己的图标 + **按客户端动态生成**的菜单
r"""persona_tray_multi.py — 托盘：一个我们自己的图标 + **按客户端动态生成**的菜单

人 2026-11-11 定的三条（这一版就是照这个写的）：
  1.「图标不需要一直换，一直显示我们图标，只需要异常时变色，然后点击手动显示异常」
     → 图标固定是我们那块 S 牌，只有异常才变绿/琥珀/红；菜单有「只显示异常」。
  2.「菜单一个是动态的，对应的客户端运行才有选项」
     → 菜单按**当前在跑的客户端**生成：DSH 没开就没有 DSH 那几项，Hermes 没开就没有 Hermes 那几项。
  3.「每个客户端单独的配置，因为客户端方法不通用，是我们现在没有定下主客户端」
     → 每个客户端的动作和"怎么执行"写在 `ops\tray-clients.json` 里，代码不写死：
       * DSH 没有命令入口，只能界面注入（`inject`）；
       * Hermes 更开放（CLI / CDP 调试口 / 本地 HTTP），可以走 `cdp`、`kernel`、`keeper`。
     所以这里不假设谁是主客户端；谁是主，只影响你点哪个图标、不看菜单结构。

配置形状（`ops\tray-clients.json`，没有就写一份默认的）：

    {
      "gateway_label": "微信网关",
      "refresh_seconds": 5,
      "clients": {
        "DSH":    {"on": true, "label": "DSH",
                   "actions": {"status": true, "compact": "inject",
                               "reload": "inject", "restart": "watchdog:enact:restart"}},
        "Hermes": {"on": true, "label": "Hermes",
                   "actions": {"status": true, "compact": "cdp", "reload": "cdp",
                               "kernel": "restart-kernel",
                               "restart": "watchdog:enact:restart-hermes",
                               "gateway": "keeper"}}
      }
    }

`actions` 的取值（方法）：
    "inject"          走界面注入（DSH 那条路）
    "cdp"             走 Hermes 的调试口（9333）
    "restart-kernel"  只重启 Hermes 内核（:9120）
    "keeper"          拉起微信网关（gatekeeper）
    "watchdog:a:b"    跑 `persona_watchdog.py a b`
"""

import functools
import io
import json
import os
import time

import pystray
from PIL import Image, ImageDraw

GREEN = (46, 125, 50, 255)
AMBER = (249, 168, 37, 255)
RED = (198, 40, 40, 255)
GRAY = (120, 120, 120, 255)

SIZE = 64                     # 画 64 再让系统缩到 16/24/32，边缘才不糊
TICK = 5.0                    # 秒：多久重算一次颜色/菜单

TRAY_CLIENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "tray-clients.json")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "tray-multi.log")
ROOT = os.path.dirname(os.path.abspath(__file__))

FONT_CANDS = (r"C:\Windows\Fonts\seguisb.ttf",      # Segoe UI Semibold
              r"C:\Windows\Fonts\segoeuib.ttf",
              r"C:\Windows\Fonts\arialbd.ttf")

WARN_PCT, ERR_PCT = 70, 85

DEFAULT_SPEC = {
    "_note": "托盘只显示【本机】任务池（ops\\tasks.jsonl，谁领谁干）；服务端/远端任务池不进托盘，"
             "要看得单独请求。会话/日志按客户端分：open.sessions。",
    "gateway_label": "微信网关",
    "refresh_seconds": 5,
    "clients": {
        "DSH": {
            "on": True, "label": "DSH",
            "img": ["DeepSeek Harness.exe"],
            "wm": "dsh",
            "open": {"sessions": "sessions"},
            "actions": {"status": True, "compact": "inject",
                        "reload": "inject", "restart": "watchdog:enact:restart"},
        },
        "Hermes": {
            "on": True, "label": "Hermes",
            "img": ["hermes-agent-cn-desktop.exe",
                    "hermes-agent-cn-runtime-win32-x64.exe"],
            "wm": "hermes",
            "open": {"sessions": ""},        # 空 = 按 hermes-keeper.json 的 home 推出来
            "actions": {"status": True, "compact": "cdp", "reload": "cdp",
                        "kernel": "restart-kernel",
                        "restart": "watchdog:enact:restart-hermes",
                        "gateway": "keeper"},
        },
    },
}


def procs_by_img():
    """{exe 小写: [pid, ...]}：toolhelp 快照直读，不起子进程、不闪窗。"""
    try:
        import persona_winwatch as W
        snap = W.snapshot()
    except Exception as e:                 # noqa: BLE001
        _p("快照失败：%s" % e)
        return {}
    out = {}
    for pid, (_pp, name) in snap.items():
        out.setdefault((name or "").lower(), []).append(pid)
    return out


# ── 版本：客户端更新了要能看出来（人 2026-11-11「客户端还要看版本，如果更新，可能也需要同步」）──
VERSIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "client-versions.json")


def exe_version(path):
    """读 exe 的 FileVersion（ctypes 直读，不起子进程、不弹窗）；读不到返回 ""。"""
    try:
        import ctypes
        from ctypes import wintypes
        import struct
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        r = ctypes.c_void_p()
        n = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(r),
                                                   ctypes.byref(n)):
            return ""
        data = ctypes.string_at(r, 16)
        ms = struct.unpack_from("<I", data, 8)[0]
        ls = struct.unpack_from("<I", data, 12)[0]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:                      # noqa: BLE001
        return ""


def _load(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                      # noqa: BLE001
        return default


def _find_versions_dir(path):
    """从 exe 路径往上找 `data\\versions`（Hermes 的内核版本都在那儿）。"""
    d = os.path.dirname(path or "")
    for _ in range(6):
        if not d:
            break
        for cand in (os.path.join(d, "data", "versions"), os.path.join(d, "versions")):
            if os.path.isdir(cand):
                return cand
        d = os.path.dirname(d)
    return ""


def _save(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:                      # noqa: BLE001
        pass


def _p(msg):
    """pythonw 没有 stdout，一律写文件。"""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:                      # noqa: BLE001
        pass


# ── 画我们自己的图标 ─────────────────────────────────────────────────
def _font(px):
    from PIL import ImageFont
    for p in FONT_CANDS:
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, px)
        except Exception:                  # noqa: BLE001
            pass
    return ImageFont.load_default()


def main_art(color, size=SIZE):
    """主程序图标：深色圆角牌 + 白 S，外圈按状态上色（绿/琥珀/红/灰）。

    4 倍绘制再缩回来，边缘才有抗锯齿；不抽任何客户端 exe 的图标——
    人 2026-11-11 说的「一直显示我们图标」。
    """
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(big * 0.22)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=r, fill=(26, 28, 34, 255))
    pad = max(1, big // 40)
    d.rounded_rectangle([pad, pad, big - 1 - pad, big - 1 - pad], radius=max(2, r - pad),
                        outline=(80, 84, 96, 255), width=max(1, big // 80))
    f = _font(int(big * 0.62))
    d.text((big // 2, big // 2 - int(big * 0.02)), "S", font=f,
           fill=(238, 240, 246, 255), anchor="mm")
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=r, outline=color,
                        width=max(2, int(big * 0.09)))
    return img.resize((size, size), Image.LANCZOS)


# ── 配置 ─────────────────────────────────────────────────────────────
def load_spec():
    """读 `ops\\tray-clients.json`；老格式（{"DSH": true, ...}）自动迁移，不丢设置。"""
    spec = json.loads(json.dumps(DEFAULT_SPEC))       # deep copy
    try:
        if not os.path.exists(TRAY_CLIENTS):
            os.makedirs(os.path.dirname(TRAY_CLIENTS), exist_ok=True)
            with io.open(TRAY_CLIENTS, "w", encoding="utf-8", newline="\n") as f:
                json.dump(spec, f, ensure_ascii=False, indent=1)
            return spec
        with io.open(TRAY_CLIENTS, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:                 # noqa: BLE001
        _p("读 tray-clients.json 失败，用默认：%s" % e)
        return spec

    if isinstance(raw.get("clients"), dict):          # 新格式
        for name, cfg in raw["clients"].items():
            if not isinstance(cfg, dict):
                continue
            cur = spec["clients"].setdefault(name, {"on": True, "label": name, "actions": {}})
            cur.update({k: v for k, v in cfg.items() if k in ("on", "label", "img", "wm", "actions")})
        for k in ("gateway_label", "refresh_seconds"):
            if k in raw:
                spec[k] = raw[k]
        if raw != spec:                      # 老文件缺 img/wm 时补回去，免得以后看不懂
            try:
                with io.open(TRAY_CLIENTS, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(spec, f, ensure_ascii=False, indent=1)
            except Exception:                # noqa: BLE001
                pass
        return spec

    # 老格式：{"DSH": true, "Hermes": true, "rotate_seconds": 4}
    for name, val in raw.items():
        if name in spec["clients"] and isinstance(val, bool):
            spec["clients"][name]["on"] = val
    if isinstance(raw.get("rotate_seconds"), (int, float)):
        spec["refresh_seconds"] = raw["rotate_seconds"]
    try:                                  # 就地迁移成新格式，下次直接读到结构
        with io.open(TRAY_CLIENTS, "w", encoding="utf-8", newline="\n") as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
        _p("tray-clients.json 已迁移成新格式（clients/actions）")
    except Exception:                     # noqa: BLE001
        pass
    return spec


# ── 托盘本体 ─────────────────────────────────────────────────────────
class Tray(object):
    def __init__(self, tray_module):
        self.T = tray_module
        self.icon = None
        self.stop_flag = False
        self._img = {}                   # 颜色 → 成品图标
        self._title = ""
        self._sig = None                 # 上次的"在跑哪些客户端"，变了就重建菜单
        self.spec = load_spec()
        self.period = float(self.spec.get("refresh_seconds") or TICK)

    # ── 读数 ──
    def _rows(self):
        """按配置里的 `img`（进程名）自己算谁在跑。

        人 2026-11-11：「比如别人拿出配合他自己的客户端，就需要匹配他自己的」——
        所以**新客户端 = 加一段配置**（img + actions + 可选 wm），不用改代码：
        它按自己的 exe 名被认出来，按自己配的方法被操作；没有水位源就只报"在跑"，不算异常。
        """
        procs = procs_by_img()
        out = {}
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            imgs = [str(x).lower() for x in (cfg.get("img") or [])]
            n = sum(len(procs.get(i, [])) for i in imgs)
            if not n:
                continue
            wm = cfg.get("wm") or ""
            d = {}
            try:
                if wm == "dsh":
                    d = self.T.dsh_watermark() or {}
                elif wm == "hermes":
                    d = self.T.hermes_watermark() or {}
            except Exception:              # noqa: BLE001
                d = {}
            out[name] = {"name": name, "label": cfg.get("label") or name, "alive": True,
                         "procs": n, "wm": bool(d),
                         "ctx": d.get("ctx"), "max": d.get("max"),
                         "pct": d.get("pct"), "note": d.get("note") or ""}
        return out

    def _running(self, name):
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        if cfg.get("on") is False:
            return False
        return name in self._rows()

    # ── 版本与同步检查 ──
    def _facts(self):
        """每个客户端现在的版本/路径（只算在跑的）。"""
        procs = procs_by_img()
        try:
            import persona_winwatch as W
        except Exception:                  # noqa: BLE001
            W = None
        out = {}
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            path = ""
            for img in [str(x).lower() for x in (cfg.get("img") or [])]:
                for pid in procs.get(img) or []:
                    path = (W.exe_path(pid) if W else "") or ""
                    if path:
                        break
                if path:
                    break
            if not path:
                continue
            out[name] = {"version": exe_version(path), "path": path}
            # Hermes 的内核是它自己那个 runtime：真版本号在 data\versions\<ver>\manifest.json 里
            if (cfg.get("wm") or "") == "hermes":
                try:
                    vd = _find_versions_dir(path)
                    if vd:
                        dirs = sorted(d for d in os.listdir(vd)
                                      if os.path.isdir(os.path.join(vd, d)))
                        if dirs:
                            man = _load(os.path.join(vd, dirs[-1], "manifest.json"), {})
                            out[name]["runtime"] = man.get("runtimeVersion") or dirs[-1]
                            out[name]["kernel"] = man.get("kernelVersion") or ""
                except Exception:          # noqa: BLE001
                    pass
        return out

    @staticmethod
    def _vkey(f):
        """一次"所见版本"的指纹：exe 版本 + 内核/runtime 版本（有就带）。"""
        return "%s|%s" % (f.get("version") or "", f.get("runtime") or "")

    def _version_changes(self):
        """[(说明)]：这次所见版本和上次记录不一样，且人还没确认过。"""
        facts = self._facts()
        st = _load(VERSIONS, {})
        acked = st.get("_acked") or {}
        changes = []
        dirty = False
        for name, f in facts.items():
            cfg = (self.spec.get("clients") or {}).get(name) or {}
            label = cfg.get("label") or name
            prev = (st.get(name) or {}).get("key") or ""
            key = self._vkey(f)
            if prev and key != prev and acked.get(name) != key:
                changes.append("%s 更新了：%s → %s（可能要同步：路径/方法/会话格式）"
                               % (label, prev.replace("|", " "), key.replace("|", " ")))
            if (not prev) or key != prev or (st.get(name) or {}).get("path") != f.get("path"):
                st[name] = {"key": key, "version": f.get("version") or "",
                            "runtime": f.get("runtime") or "",
                            "kernel": f.get("kernel") or "",
                            "path": f.get("path"),
                            "seen_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                dirty = True
        if dirty:
            _save(VERSIONS, st)
        return changes

    def _show_versions(self, icon=None, item=None):
        """「客户端版本 / 同步检查」：逐个列出名字、版本、路径、上次记录。"""
        facts = self._facts()
        st = _load(VERSIONS, {})
        changes = self._version_changes()
        lines = []
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            label = cfg.get("label") or name
            f = facts.get(name)
            if not f:
                rec = st.get(name) or {}
                lines.append("%s：没在运行（上次记录 %s%s）"
                             % (label, rec.get("version") or "无",
                                ("，内核 %s" % rec["runtime"]) if rec.get("runtime") else ""))
                continue
            head = "%s：%s" % (label, f.get("version") or "版本读不到")
            if f.get("runtime"):
                head += "（内核 %s%s）" % (f["runtime"],
                                        (" / 基础版 %s" % f["kernel"]) if f.get("kernel") else "")
            lines.append(head)
            lines.append("   %s" % f.get("path"))
            rec = st.get(name) or {}
            if rec.get("key") and rec.get("key") != self._vkey(f):
                lines.append("   上次记录：%s" % (rec.get("key") or "").replace("|", " "))
        body = ["【客户端版本】", ""] + lines
        if changes:
            body += ["", "【需要同步】"] + ["· " + c for c in changes]
            body += ["", "确认已经适配过，点菜单「版本已确认（清提醒）」"]
        else:
            body += ["", "版本和人确认过的一致。"]
        self.T._dialog("\n".join(body), "SysPersona · 客户端版本")

    def _ack_versions(self, icon=None, item=None):
        """「版本已确认」：把当前版本记成"看过了"，异常提示消失。"""
        facts = self._facts()
        st = _load(VERSIONS, {})
        acked = st.get("_acked") or {}
        for name, f in facts.items():
            if f.get("version"):
                acked[name] = self._vkey(f)
        st["_acked"] = acked
        _save(VERSIONS, st)
        self.T._dialog("已记下当前版本，之后它们再更新还会提醒。", "SysPersona · 客户端版本")


    @staticmethod
    def _worse(a, b):
        """红 > 琥珀 > 绿 > 灰：取更严重那个。"""
        rank = {GRAY: 0, GREEN: 1, AMBER: 2, RED: 3}
        return a if rank.get(a, 0) >= rank.get(b, 0) else b

    def _status(self):
        """算一次状态：返回 (颜色, 异常行, rows, tooltip)。"""
        rows = self._rows()
        clients = self.spec.get("clients") or {}
        lines = []
        color = GREEN

        # 1) 守护（心跳/任务）
        try:
            ok, msg, _tasks = self.T.daemon_health()
        except Exception as e:             # noqa: BLE001
            ok, msg = False, "读不到守护状态：%s" % str(e)[:40]
        if not ok:
            # 「任务失败：xxx」多半只是某条采集任务没取到数（比如余额页面），
            # 不该把整个托盘刷成红色；只有守护本身停了/状态文件过期才是真告警。
            level = AMBER if str(msg).startswith("任务失败") else RED
            lines.append("守护：%s" % msg)
            color = self._worse(color, level)

        # 2) 注入通道
        inj = None
        try:
            inj = self.T.INJ.evaluate_state() if getattr(self.T, "INJ", None) else None
        except Exception:                  # noqa: BLE001
            inj = None
        if inj and inj.get("level") != "ok":
            lines.append("注入：%s" % (inj.get("text") or ""))
            color = self._worse(color, RED if inj.get("level") == "error" else AMBER)

        # 3) 今日成本
        try:
            over = self.T.cost_over(self.T.cost_state())
        except Exception:                  # noqa: BLE001
            over = False
        if over:
            lines.append("成本：今日已超线")
            color = self._worse(color, AMBER)

        # 4) 各客户端
        parts = []
        for name, cfg in clients.items():
            if cfg.get("on") is False:
                continue
            label = cfg.get("label") or name
            r = rows.get(name)
            if not r:
                lines.append("%s：没在运行" % label)
                color = self._worse(color, AMBER)
                parts.append("%s 没在跑" % label)
                continue
            parts.append(self.T.wm_part(r))
            if not r.get("wm"):
                continue                   # 没有水位源的客户端（别人带进来的那种）：只报"在跑"，不算异常
            pct = r.get("pct")
            if pct is None:
                lines.append("%s：水位读数不可用" % label)
                color = self._worse(color, AMBER)
            elif pct >= ERR_PCT:
                lines.append("%s：水位 %.0f%%（告警线 %d%%）" % (label, pct, ERR_PCT))
                color = self._worse(color, RED)
            elif pct >= WARN_PCT:
                lines.append("%s：水位 %.0f%%（留意线 %d%%）" % (label, pct, WARN_PCT))
                color = self._worse(color, AMBER)

        # 5) 客户端版本：更新了但还没人确认过（人 2026-11-11：客户端还要看版本，更新可能要同步）
        try:
            for c in self._version_changes():
                lines.append(c)
                color = self._worse(color, AMBER)
        except Exception as e:             # noqa: BLE001
            _p("版本检查失败：%s" % e)

        head = "SysPersona · " + (" · ".join(parts) if parts else "没有客户端在跑")
        tail = (" · 异常 %d 项" % len(lines)) if lines else " · 正常"
        return color, lines, rows, head + tail

    def _image(self, color):
        if color not in self._img:
            self._img[color] = main_art(color)
        return self._img[color]

    # ── 动作：统一按配置里的"方法"分发 ──
    def _run_watchdog_sync(self, *args):
        """同步跑一个 watchdog 动作，拿回 (ok, 输出尾巴)，供进度窗显示结果。"""
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            r = subprocess.run([sys.executable, os.path.join(here, "persona_watchdog.py")] + list(args),
                               cwd=here, capture_output=True, timeout=300,
                               creationflags=0x08000000,
                               encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode == 0, (out[-600:] or ("exit %d" % r.returncode))
        except Exception as e:             # noqa: BLE001
            return False, "跑不动：%s" % str(e)[:200]

    def _reload_via(self, target):
        """重读设定（第 8 步）。target: dsh / hermes。"""
        def work():
            import sys as _sys
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in _sys.path:
                _sys.path.insert(0, here)
            from steps import reload_settings as R
            try:
                ok, msg = R.run({}, target, force=True)
            except Exception as e:         # noqa: BLE001
                ok, msg = False, "跑不动：%s" % str(e)[:160]
            return bool(ok), ("✔ " if ok else "✘ ") + str(msg)
        return work

    def _compress_cdp(self):
        def work():
            import persona_hermes_poke as poke
            ok, msg = poke.poke("/compact")
            return bool(ok), ("已把 /compact 发给 Hermes：" + str(msg)) if ok else ("发送失败：" + str(msg))
        return work

    def _restart_kernel(self):
        def work():
            import persona_hermes_kernel as k
            return k.restart(90)
        return work

    def _start_gateway(self):
        def work():
            import persona_hermes_keeper as k
            conf = {}
            try:
                with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "ops", "hermes-keeper.json"), encoding="utf-8") as f:
                    conf = json.load(f)
            except Exception:              # noqa: BLE001
                pass
            ok, _txt = k.status(conf)
            if ok:
                return True, "微信网关本来就在跑"
            ok2, msg = k.start(conf)
            if not ok2:
                return False, msg
            for _ in range(6):
                time.sleep(2)
                ok3, _t = k.status(conf)
                if ok3:
                    return True, "微信网关已拉起并恢复"
            return False, "已拉起但 12 秒内没连上"
        return work

    def _do(self, name, action):
        """按配置执行一个动作；返回要开进度窗的 worker，或 None（已就地处理）。"""
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        method = (cfg.get("actions") or {}).get(action) or ""
        label = cfg.get("label") or name
        T = self.T
        if action == "status":
            self._show(name)
            return None
        if action == "compact":
            if method == "cdp":
                return ("压缩 %s 上下文" % label, self._compress_cdp())
            T.compress_once()              # DSH：确认框 / busy 图标都在它里面
            return None
        if action == "reload":
            target = "hermes" if method == "cdp" else "dsh"
            return ("重读设定（%s）" % label, self._reload_via(target))
        if action == "kernel" or method == "restart-kernel":
            return ("重启 %s 内核" % label, self._restart_kernel())
        if action == "gateway" or method == "keeper":
            return (self.spec.get("gateway_label") or "微信网关", self._start_gateway())
        if action == "restart":
            if not T._confirm("重启 %s？\n\n会关掉它再拉起（正在跑的一轮会断，微信网关会跟着断一下再重连）。"
                              % label, "SysPersona · 重启 %s" % label):
                return None
            parts = method.split(":")
            args = parts[1:] if parts and parts[0] == "watchdog" else ["enact", "restart"]
            return ("重启 %s" % label, lambda: self._run_watchdog_sync(*args))
        return None

    # ── 菜单：一级只放"给谁操作"，每个客户端自己一个二级菜单 ──
    def _client_menu(self, name, cfg):
        """某个客户端的二级菜单：状态 / 压缩 / 重读 / 重启…（按它配置里有的动作生成）。"""
        acts = cfg.get("actions") or {}
        sub = []
        if acts.get("status"):
            sub.append(pystray.MenuItem("看状态",
                                        functools.partial(self._dispatch_worker, name, "status")))
        if acts.get("compact"):
            sub.append(pystray.MenuItem("立即压缩上下文",
                                        functools.partial(self._dispatch_worker, name, "compact")))
        if acts.get("reload"):
            sub.append(pystray.MenuItem("重读设定（让它读 MEMORY）",
                                        functools.partial(self._dispatch_worker, name, "reload")))
        if acts.get("kernel") or acts.get("restart") or acts.get("gateway"):
            sub.append(pystray.Menu.SEPARATOR)
        if acts.get("kernel"):
            sub.append(pystray.MenuItem("重启内核（最快，不动窗口）",
                                        functools.partial(self._dispatch_worker, name, "kernel")))
        if acts.get("restart"):
            sub.append(pystray.MenuItem("重启窗口",
                                        functools.partial(self._dispatch_worker, name, "restart")))
        if acts.get("gateway"):
            sub.append(pystray.MenuItem("%s：重拉" % (self.spec.get("gateway_label") or "微信网关"),
                                        functools.partial(self._dispatch_worker, name, "gateway")))
        # 会话/日志是"这个客户端自己的"，所以也放它自己的二级菜单里（人 2026-11-11 要求）
        opens = cfg.get("open") or {}
        if opens.get("sessions") is not None:
            sub.append(pystray.Menu.SEPARATOR)
            sub.append(pystray.MenuItem("打开这个客户的会话存档",
                                        functools.partial(self._open_client_dir, name, "sessions")))
        if opens.get("log"):
            sub.append(pystray.MenuItem("打开这个客户的日志",
                                        functools.partial(self._open_client_dir, name, "log")))
        return pystray.Menu(*sub)

    def _client_dir(self, name, kind):
        """某个客户端要打开的目录：相对路径按仓库根解析；空值按客户端类型推。"""
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        raw = ((cfg.get("open") or {}).get(kind) or "").strip()
        if raw:
            return raw if os.path.isabs(raw) else os.path.join(ROOT, raw)
        if kind == "sessions" and (cfg.get("wm") or "") == "hermes":
            try:
                conf = _load(os.path.join(ROOT, "ops", "hermes-keeper.json"), {})
                home = conf.get("home") or ""
            except Exception:              # noqa: BLE001
                home = ""
            if home:
                p = os.path.join(home, "sessions")
                if os.path.isdir(p):
                    return p
        return os.path.join(ROOT, "sessions")

    def _open_client_dir(self, name, kind, icon=None, item=None):
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        label = cfg.get("label") or name
        p = self._client_dir(name, kind)
        if not os.path.isdir(p):
            self.T._dialog("%s 的%s目录还没生成：\n%s" % (
                label, "会话存档" if kind == "sessions" else "日志", p), "SysPersona · %s" % label)
            return
        try:
            os.startfile(p)
        except Exception as e:             # noqa: BLE001
            self.T._dialog("打不开：%s" % e, "SysPersona · %s" % label)

    def _menu(self):
        T = self.T
        items = [
            pystray.MenuItem("看状态（总览）", T.show_status, default=True),
            pystray.MenuItem("只显示异常", self._show_anomalies),
            pystray.Menu.SEPARATOR,
        ]
        # 每个客户端 = 一个二级菜单；没在跑 → 这一项整个不出现（"运行才有选项"）
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            label = cfg.get("label") or name
            sub = self._client_menu(name, cfg)
            if not sub:
                continue
            items.append(pystray.MenuItem(label, sub,
                                          visible=(lambda item, n=name: self._running(n))))
        items += [
            pystray.Menu.SEPARATOR,
            # 一级只留"客户端"和固定几项；剩下的按"属于谁"归到各自的二级菜单
            pystray.MenuItem("本机", pystray.Menu(
                pystray.MenuItem("任务池（本机全局，谁领谁干）", T.show_pool),
                pystray.MenuItem("派一条…（进上面这个池）", T.submit_task),
                pystray.MenuItem("打开托盘/守护日志", T.open_log))),
            pystray.MenuItem("客户端版本", pystray.Menu(
                pystray.MenuItem("看版本 / 同步检查", self._show_versions),
                pystray.MenuItem("版本已确认（清提醒）", self._ack_versions))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出托盘", self.quit),
        ]
        return pystray.Menu(*items)

    def _dispatch_worker(self, name, action, icon=None, item=None):
        """菜单点击的统一入口（进度窗版本）。"""
        try:
            res = self._do(name, action)
            if isinstance(res, tuple):
                title, worker = res
                self.T.run_with_progress(title, worker, countdown=8)
        except Exception as e:             # noqa: BLE001
            self.T._dialog("动作失败：%s" % str(e)[:200], "SysPersona")

    def _fmt(self, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "?"
        return "%.1fM" % (v / 1e6) if v >= 1e6 else "%.0fK" % (v / 1e3)

    def _show(self, name, icon=None, item=None):
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        label = cfg.get("label") or name
        r = self._rows().get(name)
        if not r:
            self.T._dialog("%s 没在运行。" % label, "SysPersona · %s" % label)
            return
        lines = ["%s：在跑（%d 进程）" % (label, r.get("procs") or 0)]
        if r.get("pct") is None:
            lines.append("水位：读数不可用")
        else:
            lines.append("水位：%s / %s  %.0f%%"
                         % (self._fmt(r.get("ctx")), self._fmt(r.get("max")), r["pct"]))
        if r.get("note"):
            lines.append("备注：%s" % r["note"])
        self.T._dialog("\n".join(lines), "SysPersona · %s" % label)

    def _show_anomalies(self, icon=None, item=None):
        """菜单「只显示异常」：没有异常就明说没有。"""
        try:
            _color, lines, _rows, title = self._status()
        except Exception as e:             # noqa: BLE001
            self.T._dialog("算状态时出错：%s" % str(e)[:200], "SysPersona · 异常")
            return
        if not lines:
            body = "当前没有异常。\n\n" + title
        else:
            body = "当前 %d 项异常：\n\n" % len(lines)
            body += "\n".join("· " + x for x in lines)
            body += "\n\n——\n" + title
        self.T._dialog(body, "SysPersona · 异常")

    # ── 起 & 刷新 ──
    def build(self):
        try:
            color, lines, _rows, title = self._status()
        except Exception as e:             # noqa: BLE001
            _p("算状态失败：%s" % e)
            color, lines, title = GRAY, [], "SysPersona · 状态读不到"
        self._sig = tuple(sorted(self._rows()))
        self.icon = pystray.Icon("SysPersona", icon=self._image(color),
                                 title=title[:120], menu=self._menu())
        self._title = title[:120]
        _p("建托盘（单图标，%s；在跑：%s；%d 项异常）" % (
            {GREEN: "绿", AMBER: "琥珀", RED: "红", GRAY: "灰"}.get(color, "?"),
            ",".join(self._sig) or "无", len(lines)))

    def refresh_icon(self):
        """重算一次：压缩中就整块黄，否则按状态上色；在跑的客户端变了就重建菜单。"""
        try:
            on, _note = self.T.busy_state()
        except Exception:                  # noqa: BLE001
            on = False
        if on:
            icon, title = self.T.make_busy_icon(), self.T.BUSY_TITLE[:120]
        else:
            color, _lines, _rows, title = self._status()
            icon, title = self._image(color), title[:120]
        if title != self._title:
            self.icon.title = title
            self._title = title
        self.icon.icon = icon
        sig = tuple(sorted(self._rows()))
        if sig != self._sig:               # 谁开/关 → 菜单跟着变
            self._sig = sig
            try:
                self.icon.menu = self._menu()      # setter 会 update_menu()
                _p("菜单重建：在跑 %s" % (",".join(sig) or "无"))
            except Exception as e:         # noqa: BLE001
                _p("重建菜单失败：%s" % e)

    def loop(self):
        while not self.stop_flag:
            slept = 0.0
            while slept < max(1.0, self.period) and not self.stop_flag:
                time.sleep(0.25)
                slept += 0.25
            if self.stop_flag:
                break
            try:
                self.refresh_icon()
            except Exception as e:         # noqa: BLE001
                _p("刷新失败：%s" % e)

    def quit(self, icon=None, item=None):
        self.stop_flag = True
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:                  # noqa: BLE001
            pass


def run_all(tray_module=None):
    if tray_module is None:
        import persona_tray as tray_module
    t = Tray(tray_module)
    try:
        t.build()
    except Exception as e:                 # noqa: BLE001
        _p("build 失败：%s" % e)
    try:
        t.icon.run_detached()
    except Exception as e:                 # noqa: BLE001
        _p("图标起不来：%s" % e)
    t.loop()
    return 0
