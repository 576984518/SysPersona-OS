# -*- coding: utf-8 -*-
import os as _os_shim, sys as _sys_shim
_sys_shim.path.insert(0, _os_shim.path.join(_os_shim.path.dirname(_os_shim.path.abspath(__file__)), "脚本"))
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

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # realpath：C:\SysPersona 是 junction，解析成 D 盘真路径（自动审查按路径判，C 盘容易被拦）
TRAY_CLIENTS = os.path.join(ROOT, "ops", "tray-clients.json")


def _client_installed(cfg):
    """本机到底装没装这个客户端：看 detect 里 exe/home/config 有没有一个真实存在。

    为什么不看进程：进程不在有两种完全不同的原因——「装着但没开」和「这台机器没装」。
    前者要提醒，后者提醒一百次也没用（换机器时最明显）。
    """
    det = (cfg or {}).get("detect") or {}
    for key in ("exe", "home", "config"):
        for p in (det.get(key) or []):
            try:
                if p and os.path.exists(os.path.expandvars(str(p))):
                    return True
            except (OSError, ValueError):
                pass
    return False
LOG = os.path.join(ROOT, "ops", "tray-multi.log")

FONT_CANDS = (r"C:\Windows\Fonts\seguisb.ttf",      # Segoe UI Semibold
              r"C:\Windows\Fonts\segoeuib.ttf",
              r"C:\Windows\Fonts\arialbd.ttf")

WARN_PCT, ERR_PCT = 70, 85
COMPACT_WAIT_MIN = 25      # 水位过留意线后，超过这么久没真正压缩 = 超时（只亮红灯）

# ── 单实例：全局命名互斥量（2026-11-11 13:40） ──
# 为什么不用命令行判重：提权进程的 CommandLine 从非提权进程读出来是空的，
# 于是守护每 120 秒都以为"托盘没在跑"，重复拉起 → 人看到两个、三个图标。
# 命名互斥量跨权限级别有效：第二个实例拿到 ERROR_ALREADY_EXISTS 就直接退出。
MUTEX_NAME = r"Global\SysPersonaTray-v1"
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def mutex_alive():
    """有人占着托盘互斥量 = 已经有一个托盘在跑。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenMutexW(0x00100000, False, MUTEX_NAME)   # SYNCHRONIZE
        if h:
            k32.CloseHandle(h)
            return True
        return ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except Exception:                      # noqa: BLE001
        return False


def claim_mutex():
    """抢占托盘互斥量。返回 True = 我是唯一实例。"""
    global _mutex_handle
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            if h:
                k32.CloseHandle(h)
            return False
        _mutex_handle = h
        return bool(h)
    except Exception:                      # noqa: BLE001
        return True                        # 抢不到锁就当能跑，别把托盘弄没了


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
VERSIONS = os.path.join(ROOT, "ops", "client-versions.json")


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
def clip_tip(title, n=124):
    """tooltip 截断：按**行**截，不在句子中间切。

    2026-11-12 改多行 tooltip 之后，原来直接 `title[:120]` 会把最后一行切成半句；
    Windows 的 szTip 上限是 128 个字符，留 4 个字符余量。
    """
    s = str(title or "")
    if len(s) <= n:
        return s
    out = []
    used = 0
    for line in s.split("\n"):
        if used + len(line) + (1 if out else 0) > n - 1:
            break
        out.append(line)
        used += len(line) + (1 if len(out) > 1 else 0)
    return "\n".join(out) + "…" if out else s[:n - 1] + "…"


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
            # 全量合并：白名单会把新字段直接砍掉（detect / open 就是这么丢的，2026-11-11 踩过）
            cur.update(cfg)
        for k, v in raw.items():
            if k != "clients":
                spec[k] = v
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


    def _compact_overdue(self):
        """压缩超时了吗？返回要说的一行（空=没事）。

        两条判据，任一命中就算：
          ① `ops\\compact-alert.json` 是 6 小时内的"连压不降"告警；
          ② 水位已经在留意线以上，但 `ops\\compact.jsonl` 里最近一次真正 fired
             已经超过 `COMPACT_WAIT_MIN` 分钟（该压没压成：排队、窗口忙、注入失败都算）。
        只报不做：**图标变红就够了**，不弹窗、不抢焦点。
        """
        base = ROOT
        now = time.time()
        # ① 连压不降
        try:
            with io.open(os.path.join(base, "ops", "compact-alert.json"), encoding="utf-8") as f:
                al = json.load(f)
            at = time.mktime(time.strptime(al.get("at") or "", "%Y-%m-%d %H:%M:%S"))
            if now - at < 6 * 3600:
                return "压缩：连压不降（%s，水位没下来）" % (al.get("at") or "?")
        except Exception:                  # noqa: BLE001
            pass
        # ② 该压没压
        try:
            rows = self._rows()
            pct = max([(r.get("pct") or 0) for r in rows.values()] or [0])
        except Exception:                  # noqa: BLE001
            pct = 0
        if pct < WARN_PCT:
            return ""
        last = None
        try:
            with io.open(os.path.join(base, "ops", "compact.jsonl"), encoding="utf-8") as f:
                lines = f.read().strip().splitlines()[-80:]
            for ln in reversed(lines):
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if d.get("action") == "fired":
                    last = time.mktime(time.strptime(d.get("at") or "",
                                                     "%Y-%m-%d %H:%M:%S"))
                    break
        except Exception:                  # noqa: BLE001
            pass
        if last is None:
            return "压缩：水位 %.0f%% 但查不到压缩记录（巡检可能停了）" % pct
        mins = (now - last) / 60.0
        if mins >= COMPACT_WAIT_MIN:
            return "压缩：水位 %.0f%%，上次压缩在 %.0f 分钟前（超时未压）" % (pct, mins)
        return ""

    def _pending_manual(self):
        """有没有"自动连拦 3 次、等着人补发"的欠账（C 方案，读 reloader 写的那份）。"""
        base = ROOT
        try:
            with io.open(os.path.join(base, "ops", "pending-manual.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if d.get("at") else None
        except Exception:                  # noqa: BLE001
            return None

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
                # 2026-11-12 修：本机压根没装（detect 的 exe/home 都不存在）就别说「没在运行」——
                # 那是把「没装」当成「掉线」，新机器上会永远挂着一项假异常。
                if not _client_installed(cfg):
                    # 只写进 tooltip 的客户端行，不进异常列表：没装不是「出问题」。
                    parts.append("%s 未安装" % label)
                    continue
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

        # 6) 压缩超时：该压却没压成，**只把图标变红**，不弹窗、不自己动手
        #    （人 2026-11-11：「超时没有压缩，图标直接显示红色报警就可以了」）
        try:
            why = self._compact_overdue()
        except Exception as e:             # noqa: BLE001
            why = ""
            _p("压缩超时判断失败：%s" % e)
        if why:
            lines.append(why)
            color = self._worse(color, RED)

        # 7) 自动重载连续 3 次没发出去 → 转成待人补的欠账（2026-11-11 人定 C 方案）
        #    只亮红灯 + 菜单里给一条"补发"，绝不自己再抢一次键盘。
        try:
            pm = self._pending_manual()
        except Exception as e:             # noqa: BLE001
            pm = None
            _p("待办判断失败：%s" % e)
        if pm:
            lines.append("待你补发：设定重载连续 %s 次没发出去（%s 起）"
                         % (pm.get("count") or "?", pm.get("at") or "?"))
            color = self._worse(color, RED)

        # 2026-11-12 人定（截图划重点）：tooltip 分三块，**一行一件事**——
        #   第一行 名称（有异常就把"异常 N 项"挂在这行）
        #   第二行 金额（今日花费 · 调用数）
        #   之后   每个客户端各占一行（名称 + 水位）
        # 以前是一整行用 · 串起来，客户端一多就挤成一条，看不出哪个是哪个。
        head = "SysPersona" + ("（异常 %d 项）" % len(lines) if lines else "")
        cost_seg = ""
        try:
            _cd = self.T.cost_state()
            cost_seg = self.T.cost_tip(_cd) if _cd else ""
        except Exception as e:             # noqa: BLE001
            _p("成本行读不到：%s" % e)
        rows_tip = parts if parts else ["没有客户端在跑"]
        title = "\n".join([head] + ([cost_seg] if cost_seg else []) + rows_tip)
        return color, lines, rows, title

    def _image(self, color):
        if color not in self._img:
            self._img[color] = main_art(color)
        return self._img[color]

    # ── 动作：统一按配置里的"方法"分发 ──
    def _run_watchdog_sync(self, *args):
        """同步跑一个 watchdog 动作，拿回 (ok, 输出尾巴)，供进度窗显示结果。"""
        import subprocess
        import sys
        here = ROOT
        try:
            r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_watchdog.py")] + list(args),
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
            here = ROOT
            if here not in _sys.path:
                _sys.path.insert(0, here)
            from steps import reload_settings as R
            try:
                ok, msg = R.run({}, target, force=True)
            except Exception as e:         # noqa: BLE001
                ok, msg = False, "跑不动：%s" % str(e)[:160]
            return bool(ok), ("✔ " if ok else "✘ ") + str(msg)
        return work

    def _reload_forced(self):
        """补发被自动拦下的那次重载（C 方案的手动出口）：走 reloader --manual。

        手动 = 不看任何闸门（他刚点完菜单，30 秒打字那条必然命中），
        发成功就清账，托盘红灯自己灭。
        """
        def work():
            import subprocess
            import sys as _sys
            here = ROOT
            try:
                r = subprocess.run([_sys.executable,
                                    os.path.join(here, "脚本/persona_constitution_reloader.py"),
                                    "--reason", "compact", "--manual"],
                                   cwd=here, capture_output=True, timeout=300,
                                   creationflags=0x08000000,
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "OK " in out
                return bool(ok), (("✔ " if ok else "✘ ") + (out[-500:] or "没有输出"))
            except Exception as e:         # noqa: BLE001
                return False, "跑不动：%s" % str(e)[:200]
        return work

    def _compress_cdp(self):
        """Hermes 的压缩：CDP 打 `/compress`（**它不是 DSH，命令不一样**）。

        2026-11-11 15:50 人纠正：DSH 用 `/compact`，Hermes 用 `/compress`。
        这里原来跟着 DSH 一起打成 `/compact`，等于给 Hermes 发了一条它不认识的话。
        """
        def work():
            import persona_hermes_poke as poke
            cmd = "/compress"
            ok, msg = poke.poke(cmd)
            return bool(ok), ("已把 %s 发给 Hermes：%s" % (cmd, msg)) if ok else ("发送失败：%s" % msg)
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

    def _switch_model(self, model_name):
        """换模型：直接干，**不弹确认框**。

        2026-11-11 16:05 人纠正：「换模型不需要确认，是改权限需要确认」。
        所以这里原来那个 _confirm 已删掉——换模型可逆、不涉权限；
        以后真去动 /permission（权限预设）那类，才照旧先问。
        """
        def work():
            import subprocess
            import sys as _sys
            here = ROOT
            # 壳正在生成时别抢输入框（和压缩/重读共用同一个框）
            try:
                import persona_inject as pi
                idle, waited, imsg = pi.wait_shell_idle(stable=5, timeout=60)
                if not idle:
                    return False, "壳在说话（等了 %.0f 秒：%s），等它说完再切" % (waited, imsg)
            except Exception:              # noqa: BLE001
                pass
            try:
                r = subprocess.run([_sys.executable,
                                    os.path.join(here, "脚本/persona_model.py"),
                                    "--name", model_name, "--force"],
                                   cwd=here, capture_output=True, timeout=120,
                                   creationflags=0x08000000,
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "OK " in out
                return bool(ok), (("✔ " if ok else "✘ ") + (out[-500:] or "没有输出"))
            except Exception as e:         # noqa: BLE001
                return False, "跑不动：%s" % str(e)[:200]
        return ("换模型（%s）" % model_name, work)

    def _switch_effort(self, level):
        """切推理档位（Off/Low/High/Max）：不弹确认（可逆），走 persona_effort.py。

        2026-11-11 17:10 人定：「你加进去，后期可以自动切」——所以这里只是**手工入口**，
        后期自动切档由一个上层判断调用同一个脚本（`persona_effort.py --set X`）即可。
        """
        def work():
            import subprocess
            import sys as _sys
            here = ROOT
            try:
                r = subprocess.run([_sys.executable, os.path.join(here, "脚本/persona_effort.py"),
                                    "--set", level, "--force"],
                                   cwd=here, capture_output=True, timeout=180,
                                   creationflags=0x08000000,
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "OK " in out
                return bool(ok), (("✔ " if ok else "✘ ") + (out[-500:] or "没有输出"))
            except Exception as e:             # noqa: BLE001
                return False, "跑不动：%s" % str(e)[:200]
        return ("切推理档位（%s）" % level, work)

    def _effort_menu(self, name):
        """「推理等级」二级菜单：顶上一行显示现在哪档，下面 Off/Low/High/Max 可选。"""
        items = []
        cur = ""
        try:
            import persona_effort as PE
            cur = (PE.current()[1] or PE.current()[0]) or ""
        except Exception:                      # noqa: BLE001
            pass
        if cur:
            items.append(pystray.MenuItem("现在：%s" % cur, None, enabled=False))
            items.append(pystray.Menu.SEPARATOR)
        # 只给 low/high/max：**off 不上菜单**（2026-11-11 人定：不许把推理等级切到关）
        for lv in ("low", "high", "max"):
            items.append(pystray.MenuItem(lv, functools.partial(
                self._dispatch_worker, name, "effort:" + lv)))
        return pystray.Menu(*items)

    def _model_menu(self, name):
        """「换模型」二级菜单：顶上一行是**现在用的是哪个**，下面才可选。

        当前模型从会话缓存里读（`modelSelection.val.lastUsed`），离线可见，
        不用看屏幕。免费档（硅基流动）不进菜单：要另配 key，历史上欠费直接 402。
        """
        items = []
        cur = ""
        names = ["DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro"]
        try:
            import persona_model as PM
            names = PM.model_names("账号") or names
            c = PM.current_model() or {}
            cur = c.get("model") or ""
        except Exception:                  # noqa: BLE001
            pass
        if cur:
            items.append(pystray.MenuItem("现在：%s" % cur, None, enabled=False))
            items.append(pystray.Menu.SEPARATOR)
        items.extend([
            pystray.MenuItem(n, functools.partial(self._dispatch_worker, name,
                                                  "model:" + n))
            for n in names])
        return pystray.Menu(*items)

    def _do(self, name, action):
        """按配置执行一个动作；返回要开进度窗的 worker，或 None（已就地处理）。"""
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        method = (cfg.get("actions") or {}).get(action) or ""
        label = cfg.get("label") or name
        T = self.T
        if action == "status":
            self._show(name)
            return None
        if action.startswith("model:"):
            return self._switch_model(action.split(":", 1)[1])
        if action.startswith("effort:"):
            return self._switch_effort(action.split(":", 1)[1])
        if action == "compact":
            if method == "cdp":
                return ("压缩 %s 上下文" % label, self._compress_cdp())
            T.compress_once()              # DSH：确认框 / busy 图标都在它里面
            return None
        if action == "reload":
            target = "hermes" if method == "cdp" else "dsh"
            return ("重读设定（%s）" % label, self._reload_via(target))
        if action == "reload-force":
            return ("补发设定重载（手动，不看闸门）", self._reload_forced())
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
        # 2026-11-11 16:06 修：这里原来比的是 `name == "dsh"`，但配置里的键是 **"DSH"**，
        # 大小写对不上 → 这两项在真实菜单里根本没出现过（人报「没有切换模型的选项」才发现）。
        # 现在用配置里的 `wm`（小写）判客户端，别再拿显示名比。
        wm = (cfg.get("wm") or name).lower()
        # C 方案（2026-11-11 人定）：自动连续 3 次被拦下来的那次重载，由人点这里补发。
        # 名字带「补发」两个字，好和上面那条"重读设定"分开：这条专治欠账。
        if acts.get("reload") and wm == "dsh":
            sub.append(pystray.MenuItem("补发被拦的设定重载（自动连续 3 次没发出去时用）",
                                        functools.partial(self._dispatch_worker, name, "reload-force")))
        # 换模型（2026-11-11 加）：DSH 才有 /model；点了直接换，不弹确认（人 16:05 纠正）
        if acts.get("model") and wm == "dsh":
            sub.append(pystray.MenuItem("换模型", self._model_menu(name)))
        # 推理档位（2026-11-11 加）：切档可逆、不涉权限，所以不弹确认
        if acts.get("effort") and wm == "dsh":
            sub.append(pystray.MenuItem("推理等级", self._effort_menu(name)))
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
        if (cfg.get("detect") or {}):
            sub.append(pystray.Menu.SEPARATOR)
            sub.append(pystray.MenuItem("打开这个客户的配置文件",
                                        functools.partial(self._open_client_cfg, name)))
            sub.append(pystray.MenuItem("看它认路的结果（exe/配置/工作目录）",
                                        functools.partial(self._show_client_paths, name)))
        return pystray.Menu(*sub)

    def _client_probe(self, name):
        """客户端认路：exe / 配置 / 工作目录 / 是否在跑（2026-11-11 加）。"""
        try:
            import persona_client_probe as P
            return P.detect(name)
        except Exception as e:             # noqa: BLE001
            _p("客户端探测失败：%s" % e)
            return {}

    def _open_client_cfg(self, name, icon=None, item=None):
        st = self._client_probe(name)
        p = (st.get("config") or "")
        if not p or not os.path.exists(p):
            self.T._dialog("%s 没认出配置文件（detect.config 没配或路径变了）" % name,
                           "SysPersona · %s" % name)
            return
        try:
            subprocess.Popen(["explorer.exe", "/select,", p], creationflags=0x08000000)
        except Exception:                  # noqa: BLE001
            os.startfile(os.path.dirname(p))

    def _show_client_paths(self, name, icon=None, item=None):
        st = self._client_probe(name)
        if not st:
            return
        body = ("exe：%s\n版本：%s\n在跑：%s（pid %s）\n\n配置：%s\n工作目录：%s\nauto_launch：%s"
                % (st.get("exe") or "没认出", st.get("version") or "?",
                   "是" if st.get("running") else "否",
                   ",".join(map(str, (st.get("pids") or [])[:3])) or "-",
                   st.get("config") or "没认出", st.get("workdir") or "没认出",
                   st.get("auto_launch")))
        self.T._dialog(body, "SysPersona · %s 认路" % name)

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

    def _menu_txt(self, menu, ind=0):
        """把当前菜单摊平成文本（查菜单/报问题用，不参与点击）。"""
        out = []
        for it in menu:
            out.append("%s- %s" % (" " * ind, getattr(it, "text", "?")))
            sub = getattr(it, "submenu", None)
            if sub is not None:
                out.append(self._menu_txt(sub, ind + 2))
        return "\n".join(out)

    def _dump_menu(self, menu):
        """菜单一变就落一份 `ops\\tray-menu.txt`。

        2026-11-11 16:05 人报「没有切换模型的选项」：光看屏幕分不清
        「代码没加进去」和「他点的是旧/幽灵图标」。落这份文件，就能直接对证。
        """
        try:
            txt = self._menu_txt(menu)
            if txt == getattr(self, "_menu_txt_last", None):
                return
            self._menu_txt_last = txt
            with io.open(os.path.join(ROOT, "ops", "tray-menu.txt"), "w",
                         encoding="utf-8", newline="\n") as f:
                f.write(txt + "\n")
        except Exception:                  # noqa: BLE001
            pass

    def _launch_client(self, name, icon=None, item=None):
        """拉起一个**没在跑**的客户端（2026-11-11 人：「后台程序还少了个拉起客户端的选项」）。

        走它的 `detect.exe` 第一个存在的路径；工作目录用它 `detect.workdir`。
        拉起属运维动作，不需要人点头（改它的配置/提权才要）。
        """
        import subprocess
        st = self._client_probe(name)
        exe = (st.get("exe") or "")
        cfg = (self.spec.get("clients") or {}).get(name) or {}
        label = cfg.get("label") or name
        if not exe or not os.path.exists(exe):
            self.T._dialog("%s 没认出可执行文件（detect.exe 没配或路径变了）。\n\n"
                           "先点「看它认路的结果」确认。" % label, "SysPersona · 拉起 %s" % label)
            return
        workdir = (st.get("workdir") or "")
        try:
            subprocess.Popen([exe], cwd=(workdir if workdir and os.path.isdir(workdir) else None),
                             creationflags=0x00000008)      # DETACHED_PROCESS：别跟着托盘一起死
        except Exception as e:             # noqa: BLE001
            self.T._dialog("拉起失败：%s" % str(e)[:200], "SysPersona · 拉起 %s" % label)
            return
        self.T._dialog("已发起拉起：%s\n\n路径：%s\n等它起来后，菜单里才会出现它的操作项。"
                       % (label, exe), "SysPersona · 拉起 %s" % label)

    # ── 叫醒客户端（2026-11-12 人定）───────────────────────────────────
    # 人原话：「窗口被毁的拉起，需要托盘有一个显示的开关，比如我手动退出的，你不能无限强拉，
    #   而且有时候崩溃了，还有报错弹窗，需要托盘程序去掉报错，或者强杀进程，在重拉」。
    def _wake_allowed(self):
        try:
            import persona_inject as _pi
            return _pi.wake_allowed()
        except Exception:                  # noqa: BLE001
            return True

    def _wake_toggle(self, icon=None, item=None):
        """开关：允许/不许我自动叫醒客户端。手动退出了就关掉它。"""
        try:
            import persona_inject as _pi
            cur = _pi.wake_allowed()
            ok = _pi.set_wake_allowed(not cur)
            self.T._dialog("自动叫醒客户端：%s%s"
                           % ("已关掉（你手动退出时就这样，我不强拉）" if cur else "已打开（压缩/切档会自己把窗口叫回来）",
                              "" if ok else "\n写配置失败，下次再试"),
                           "SysPersona · 叫醒客户端")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("改不了：%s" % str(e)[:200], "SysPersona · 叫醒客户端")
        self.icon.menu = self._menu()

    def _wake_close_dialogs(self, icon=None, item=None):
        """关掉客户端的报错/崩溃弹窗（先 WM_CLOSE，再点「确定」）。"""
        try:
            import persona_inject as _pi
            n0, left = _pi.close_error_dialogs()
            if not n0:
                self.T._dialog("没看到客户端的报错窗", "SysPersona · 报错窗")
                return
            self.T._dialog("报错窗：处理 %d 个，还剩 %d 个%s"
                           % (n0, left, "" if not left else "\n关不掉就点「强杀并重拉」"),
                           "SysPersona · 报错窗")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("出错：%s" % str(e)[:200], "SysPersona · 报错窗")

    def _wake_force(self, icon=None, item=None):
        """强杀客户端 → 重新拉起（报错卡死 / 窗口毁了时用）。"""
        try:
            import persona_inject as _pi
            kok, kmsg = _pi.kill_client()
            rok, rmsg = _pi.relaunch_client()
            self.T._dialog("强杀：%s\n重拉：%s" % (kmsg, rmsg), "SysPersona · 强杀并重拉")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("出错：%s" % str(e)[:200], "SysPersona · 强杀并重拉")

    def _wake_menu(self):
        return pystray.Menu(
            pystray.MenuItem("允许自动叫醒（关掉=我手动退出了）", self._wake_toggle,
                             checked=lambda item: self._wake_allowed()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("关掉报错窗", self._wake_close_dialogs),
            pystray.MenuItem("强杀并重拉（窗口会关掉，会话不丢）", self._wake_force))

    def _launch_menu(self):
        """「拉起客户端」二级菜单：列所有配置里的客户端（不管在不在跑）。"""
        items = []
        for name, cfg in (self.spec.get("clients") or {}).items():
            if cfg.get("on") is False:
                continue
            label = cfg.get("label") or name
            items.append(pystray.MenuItem("%s（%s）" % (label, "在跑" if self._running(name) else "没在跑"),
                                          functools.partial(self._launch_client, name)))
        return pystray.Menu(*items) if items else None

    def _power(self, args, title):
        """跑 persona_power.py（关机 / 取消），把结果弹给人看。

        2026-11-11 人：「关机也可以添加到后台程序中」。关机有 60 秒延迟，
        响铃期间再点「取消关机」就撤销（`shutdown /a`）。
        """
        import subprocess
        import sys as _sys
        import time as _t
        import uuid as _uuid
        import json as _json
        here = ROOT
        # 人点这一下 = 下单：写一张一次性单子（动作 + 时间 + nonce），关机脚本核对后才动手。
        order = os.path.join(here, "ops", "power-order.json")
        act = "shutdown"
        for i, a in enumerate(args):
            if a in ("--shutdown", "--restart", "--sleep", "--logoff"):
                act = a.lstrip("-")
                break
        try:
            os.makedirs(os.path.dirname(order), exist_ok=True)
            with open(order, "w", encoding="utf-8") as f:
                _json.dump({"action": act, "ts": _t.time(), "nonce": _uuid.uuid4().hex},
                           f, ensure_ascii=False)
            args = args + ["--order", order]
        except Exception:                  # noqa: BLE001
            pass
        try:
            r = subprocess.run([_sys.executable, os.path.join(here, "脚本/persona_power.py")] + args,
                               cwd=here, capture_output=True, timeout=90,
                               creationflags=0x08000000, encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
        except Exception as e:             # noqa: BLE001
            out = "跑不动：%s" % str(e)[:200]
        self.T._dialog(out[-800:] or "没有输出", title)

    def _power_shutdown(self, icon=None, item=None):
        delay = 60
        self._power(["--shutdown", "--delay", str(delay), "--why", "托盘点"],
                    "SysPersona · 关机")
        # 排完再开一个**倒计时窗**：显示还剩多少秒 + 「取消关机」按钮。
        # 起因：人点了关机、把弹出的结果框关掉就以为取消了，60 秒后真关机。
        try:
            import subprocess
            import sys as _sys
            here = ROOT
            subprocess.Popen([_sys.executable, os.path.join(here, "脚本/persona_power.py"),
                              "--countdown", "--delay", str(delay)],
                             cwd=here, creationflags=0x08000000)
        except Exception:                  # noqa: BLE001
            pass

    def _power_cancel(self, icon=None, item=None):
        self._power(["--cancel"], "SysPersona · 取消关机")

    def _menu(self):
        T = self.T
        items = [
            pystray.MenuItem("看状态（总览）", T.show_status, default=True),
            pystray.MenuItem("只显示异常", self._show_anomalies),
            # 2026-11-12 B87：日报只读入口。点它不跑任何任务、不改任何文件，
            # 只把 ops\daily-status.md 摊开给你看——自检本身由 23:50 的计划任务负责。
            pystray.MenuItem("今日自检（只读）", self._show_digest),
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
                pystray.MenuItem("打开托盘/守护日志", T.open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("工具箱（只读，点了就跑）", self._toolbox_menu()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("关机（60 秒后，可取消）", self._power_shutdown),
                pystray.MenuItem("取消关机", self._power_cancel))),
            pystray.MenuItem("拉起客户端", self._launch_menu()),
            pystray.MenuItem("叫醒客户端", self._wake_menu()),
            pystray.MenuItem("客户端版本", pystray.Menu(
                pystray.MenuItem("看版本 / 同步检查", self._show_versions),
                pystray.MenuItem("版本已确认（清提醒）", self._ack_versions))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("加密", self._crypto_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出托盘", self.quit),
        ]
        menu = pystray.Menu(*items)
        self._dump_menu(menu)
        return menu

    # ── 工具箱：把"有工具但没人点"的那些手动脚本放进托盘（人 2026-11-12 定）──────────
    def _toolbox_menu(self):
        r"""「工具箱（只读）」：每项都是**只读**命令，点了就跑，输出摊在一个框里。

        为什么只放只读的：托盘里的点击是"一次误触就执行"，凡是有副作用的
        （加密/解封、改权限档、清道夫真删、备份真打）都不放在这里——
        那些必须先在命令行里看清楚参数。宁可少给几个按钮，也不给"点了就改"的按钮。
        """
        return pystray.Menu(
            pystray.MenuItem("工具链体检", lambda i, it: self._tool(["persona_tools.py", "check"], "工具链体检")),
            pystray.MenuItem("备份轮转计划（只读）", lambda i, it: self._tool(["persona_backup_retention.py", "plan"], "备份轮转计划")),
            pystray.MenuItem("清道夫状态（只读）", lambda i, it: self._tool(["persona_janitor.py", "status"], "清道夫状态")),
            pystray.MenuItem("引导包清单（只读）", lambda i, it: self._tool(["persona_bootstrap.py", "list"], "引导包清单")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("人在不在", lambda i, it: self._tool(["persona_presence.py"], "人在不在")),
            pystray.MenuItem("权限档位（读当前）", lambda i, it: self._tool(["persona_permission.py", "--current"], "权限档位")),
            pystray.MenuItem("漂移记录（看最近 10 条）", lambda i, it: self._tool(["persona_drift.py", "show", "--n", "10"], "漂移记录")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("守护日志（最后 40 行）", lambda i, it: self._tool(["persona_logs.py", "--tail", "40"], "守护日志", 120)),
        )

    def _tool(self, argv, title, timeout=90):
        r"""跑一个**只读**工具，把输出摊在框里。

        三个约束：不抢焦点（NO_WINDOW）、有超时（托盘线程不能被一个卡住的脚本拖死）、
        输出截断（框里塞不下几万字，只留尾部，并说明被截断了）。
        """
        import subprocess
        script = os.path.join(ROOT, "脚本", argv[0])
        if not os.path.isfile(script):
            self.T._dialog("本机没有这个脚本：%s" % argv[0], "SysPersona · %s" % title)
            return
        try:
            r = subprocess.run([self.T.PY, script] + list(argv[1:]),
                               capture_output=True, timeout=timeout,
                               cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (r.stdout or b"").decode("utf-8", "replace").strip()
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired:
            self.T._dialog("超时（%d 秒还没跑完）：%s" % (timeout, " ".join(argv)), "SysPersona · %s" % title)
            return
        except Exception as e:                       # noqa: BLE001
            self.T._dialog("跑不起来：%s" % str(e)[:200], "SysPersona · %s" % title)
            return
        body = out or err or "（没有任何输出：命令正常但没话可说）"
        if len(body) > 6000:
            body = "…（前面省略 %d 字）\n" % (len(body) - 6000) + body[-6000:]
        self.T._dialog(body, "SysPersona · %s" % title)

    # ── 加密入口（人 2026-11-11 13:33「要有加解密入口，而且能输入密码，生成密钥文件」）──
    def _crypto_menu(self):
        return pystray.Menu(
            pystray.MenuItem("设置 / 更换密码（生成密钥文件）", self._crypto_setpw),
            pystray.MenuItem("看加密状态", self._crypto_stat),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即加密（封存明文）", self._crypto_seal),
            pystray.MenuItem("恢复明文（解封）", self._crypto_unseal),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("验证方式（令牌 / 邮箱 / 短信）", self._auth_menu()),
            pystray.MenuItem("验证加密与解密（自检）", self._verify_crypto_menu),
        )

    # ── 验证方式：令牌（能用）+ 邮箱/短信（只有框架） ──
    def _auth_menu(self):
        return pystray.Menu(
            pystray.MenuItem("看凭据 / 令牌", self._auth_status),
            pystray.MenuItem("生成一个新令牌（只显示一次）", self._auth_new_token),
            pystray.MenuItem("用手机令牌算码（TOTP）", self._auth_code),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("邮箱 / 短信：框架状态（未实现）", self._auth_providers),
        )

    def _auth_cli(self, *args, timeout=120):
        import subprocess
        import sys
        here = ROOT
        r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_auth.py")] + list(args),
                           cwd=here, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           encoding="utf-8", errors="replace")
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    def _auth_status(self, icon=None, item=None):
        try:
            _rc, out = self._auth_cli("status")
            self.T._dialog(out[-1500:] or "（没有输出）", "SysPersona · 凭据与令牌")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读不到：%s" % str(e)[:200], "SysPersona · 凭据与令牌")

    def _auth_new_token(self, icon=None, item=None):
        name = self._ask_text("SysPersona · 令牌", "给这个令牌起个名字（例如：笔记本 / NAS 同步）")
        if not name:
            return
        try:
            rc, out = self._auth_cli("add", "--kind", "token", "--label", name.strip(),
                                     "--scopes", "vault:read,vault:write")
            self.T._dialog(out[-1200:] + ("\n\n（只显示这一次，抄走再关窗）" if rc == 0 else ""),
                           "SysPersona · 新令牌")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("生成失败：%s" % str(e)[:200], "SysPersona · 新令牌")

    def _auth_code(self, icon=None, item=None):
        cid = self._ask_text("SysPersona · 手机令牌", "凭据 id（见「看凭据 / 令牌」里的列表）")
        if not cid:
            return
        try:
            _rc, out = self._auth_cli("code", cid.strip())
            self.T._dialog(out[-400:] or "（没有输出）", "SysPersona · 手机令牌")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("算码失败：%s" % str(e)[:200], "SysPersona · 手机令牌")

    def _auth_providers(self, icon=None, item=None):
        try:
            import subprocess
            import sys
            r = subprocess.run([sys.executable, os.path.join(ROOT, "脚本/persona_auth_providers.py"), "status"],
                               cwd=ROOT, capture_output=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
            self.T._dialog(((r.stdout or "") + (r.stderr or "")).strip()[-1200:],
                           "SysPersona · 二次因子框架")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读不到：%s" % str(e)[:200], "SysPersona · 二次因子框架")

    def _ask_via_dialog(self, title, prompt, secret=True, minlen=0):
        """起一个**独立进程**弹输入窗，读它的 stdout。

        2026-11-11 13:45 踩坑：直接在托盘线程里 tk.Tk()，窗口能出来但事件循环
        不在主线程 → **关不掉、点不动**。独立进程就没这问题（Esc / 右上角 / 取消都灵）。
        取消 = 退出码非 0 = 读不到内容 = 返回 None。
        """
        import subprocess
        import sys
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else py
        cmd = [exe, os.path.join(ROOT, "脚本/persona_pw_dialog.py"), title, prompt]
        if not secret:
            cmd.append("--text")
        if minlen:
            cmd += ["--min", str(int(minlen))]
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=300,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
        except Exception:                  # noqa: BLE001
            return None
        if r.returncode != 0:
            return None
        v = (r.stdout or "").strip()
        return v or None

    def _ask_pw(self, title, prompt):
        """遮口输入一行密码。取消返回 None。"""
        return self._ask_via_dialog(title, prompt, secret=True)

    def _ask_text(self, title, prompt):
        """输入一行普通文字（令牌名 / 凭据 id）。取消返回 None。"""
        return self._ask_via_dialog(title, prompt, secret=False)

    def _crypto_setpw(self, icon=None, item=None):
        p1 = self._ask_pw("SysPersona · 加密", "设置加密密码（至少 8 位，丢了就解不开）")
        if p1 is None:
            return
        p1 = p1.strip()
        if len(p1) < 8:
            self.T._dialog("密码太短（至少 8 位）。", "SysPersona · 加密")
            return
        p2 = self._ask_pw("SysPersona · 加密", "再输一次确认")
        if p2 is None:
            return
        if p1 != p2.strip():
            self.T._dialog("两次输入不一致，没改动。", "SysPersona · 加密")
            return

        def work():
            import persona_vault as V
            lines, ok = [], True
            try:
                if not os.path.exists(V.key_path()):
                    made, msg = V.init_key(True)
                    lines.append("生成密钥文件：%s" % msg)
                    if not made:
                        return False, "\n".join(lines)
                else:
                    lines.append("密钥文件已存在，沿用：%s" % V.fingerprint(V.load_key()))
                V.save_pass_copy(p1)
                lines.append("口令副本已更新：%s" % V.pass_path())
                lines.append("记牢这个密码：它是换机器时唯一的开门钥匙。")
            except Exception as e:         # noqa: BLE001
                ok, lines = False, lines + ["失败：%s" % str(e)[:200]]
            return ok, "\n".join(lines)

        self.T.run_with_progress("设置密码 / 生成密钥文件", work, countdown=8)

    def _crypto_stat(self, icon=None, item=None):
        try:
            import persona_vault as V
            self.T._dialog(V.status(), "SysPersona · 加密状态")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("读不到：%s" % str(e)[:200], "SysPersona · 加密状态")

    def _set_switch(self, enabled, mode):
        import json
        p = os.path.join(ROOT, "ops", "vault.json")
        rec = {"enabled": bool(enabled), "mode": mode,
               "note": "2026-11-11 托盘开关：%s / %s" % ("开" if enabled else "关", mode)}
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return rec

    def _crypto_seal(self, icon=None, item=None):
        try:
            import persona_vault as V
            V.load_key()
        except Exception as e:             # noqa: BLE001
            self.T._dialog("还没有密钥文件，先点「设置 / 更换密码」。\n\n%s"
                           % str(e)[:160], "SysPersona · 加密")
            return

        def work():
            import subprocess
            import sys
            here = ROOT
            self._set_switch(True, "local")
            r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_vault_bulk.py"), "seal"],
                               cwd=here, capture_output=True, timeout=3600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode == 0, out[-900:]

        self.T.run_with_progress("立即加密（封存明文）", work, countdown=8)

    def _crypto_unseal(self, icon=None, item=None):
        def work():
            import subprocess
            import sys
            here = ROOT
            self._set_switch(False, "plain")
            r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_vault_bulk.py"), "unseal"],
                               cwd=here, capture_output=True, timeout=3600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               encoding="utf-8", errors="replace")
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            return r.returncode == 0, out[-900:]

        self.T.run_with_progress("恢复明文（解封）", work, countdown=8)

    # ── 加密自检：人 2026-11-11「程序还要留手动验证解密的选项」 ──
    def _verify_crypto_menu(self, icon=None, item=None):
        try:
            title, worker = self._verify_crypto()
            self.T.run_with_progress(title, worker, countdown=8)
        except Exception as e:             # noqa: BLE001
            self.T._dialog("跑不动：%s" % str(e)[:200], "SysPersona · 加密自检")

    def _verify_crypto(self):
        def work():
            import subprocess
            import sys
            here = ROOT
            lines = []
            try:
                import persona_vault as V
                st = V.status()
                lines.append(st)
            except Exception as e:         # noqa: BLE001
                lines.append("读保险箱状态失败：%s" % str(e)[:120])
            try:
                r = subprocess.run([sys.executable, os.path.join(here, "脚本/persona_vault.py"), "selftest"],
                                   cwd=here, capture_output=True, timeout=240,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                   encoding="utf-8", errors="replace")
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0 and "往返正确" in out
                lines.append(out[-700:])
            except Exception as e:         # noqa: BLE001
                ok, lines = False, lines + ["自检跑不动：%s" % str(e)[:160]]
            return ok, "\n".join(lines)
        return ("验证加密与解密（往返自检）", work)

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

    def _show_digest(self, icon=None, item=None):
        r"""菜单「今日自检（只读）」：把已经生成好的日报摊开给你看。

        只读，三个"不"：不跑自检、不刷新、不改任何文件。自检本身由计划任务
        `SysPersona-Digest` 每天 23:50 跑；这里只是让人少开一次命令行。
        日报不在就直说"还没有"，并给出想现在就跑的那一行命令——不替人跑。
        """
        p = os.path.join(self.T.OPS, "daily-status.md")
        try:
            if not os.path.isfile(p):
                self.T._dialog(
                    "还没有日报。\n\n每天 23:50 会自动生成一份。\n"
                    "现在就想看，跑这一句（不是我替你跑）：\n\n"
                    "  python <库>\\脚本\\persona_digest.py --once",
                    "SysPersona · 今日自检")
                return
            txt = io.open(p, encoding="utf-8-sig").read().strip()
            age_h = (time.time() - os.path.getmtime(p)) / 3600.0
            when = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
            head = "生成于 %s（%.1f 小时前）· 只读\n\n" % (when, age_h)
            self.T._dialog(head + txt, "SysPersona · 今日自检")
        except Exception as e:             # noqa: BLE001
            self.T._dialog("日报读不出来：%s" % str(e)[:200], "SysPersona · 今日自检")

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
    def _ensure_vault_gate(self):
        """托盘起来时顺手把 I/O 门拉起（已在听就不动）。

        为什么放托盘：门要"开机就在"，但注册登录触发式计划任务要管理员权限。
        托盘本来就是常驻且开机自启的，让它管这扇门最省事。
        """
        try:
            import socket
            import subprocess
            import sys as _sys
            base = ROOT
            port, bind = 8738, "127.0.0.1"
            try:
                with io.open(os.path.join(base, "ops", "vault-gate.json"), encoding="utf-8") as f:
                    c = json.load(f)
                port, bind = int(c.get("port") or port), c.get("bind") or bind
            except Exception:              # noqa: BLE001
                pass
            s = socket.socket()
            s.settimeout(0.6)
            try:
                s.connect((bind, port))
                return "门已在听 %s:%d" % (bind, port)
            except OSError:
                pass
            finally:
                s.close()
            pyw = os.path.join(os.path.dirname(_sys.executable), "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = _sys.executable
            subprocess.Popen([pyw, os.path.join(base, "脚本/persona_vault_gate.py"), "serve"],
                             cwd=base, creationflags=0x08000000, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return "门已拉起 %s:%d" % (bind, port)
        except Exception as e:             # noqa: BLE001
            return "门没起来：%s" % e

    def build(self):
        _p(self._ensure_vault_gate())
        try:
            color, lines, _rows, title = self._status()
        except Exception as e:             # noqa: BLE001
            _p("算状态失败：%s" % e)
            color, lines, title = GRAY, [], "SysPersona · 状态读不到"
        self._sig = tuple(sorted(self._rows()))
        self.icon = pystray.Icon("SysPersona", icon=self._image(color),
                                 title=clip_tip(title), menu=self._menu())
        self._title = clip_tip(title)
        _p("tooltip：%r" % (self._title,))
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
            icon, title = self.T.make_busy_icon(), clip_tip(self.T.BUSY_TITLE)
        else:
            color, _lines, _rows, title = self._status()
            icon, title = self._image(color), clip_tip(title)
        if title != self._title:
            self.icon.title = title
            self._title = title
            _p("tooltip 变更：%r" % (title,))      # 排查"悬停看到的和代码不一样"就靠这行
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
    if not claim_mutex():
        _p("已有一个托盘在跑（互斥量），本实例退出")
        return 0
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
    _p("图标已就绪（互斥量 %s）" % ("可连" if mutex_alive() else "?"))
    t.loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_all())
