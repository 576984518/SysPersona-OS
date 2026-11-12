# -*- coding: utf-8 -*-
r"""persona_machine.py — 每台机器一份配置：先读默认，再按本机实际生成，最后应用

为什么有这份：路径过去散在三处（用户环境变量、%LOCALAPPDATA%\SysPersona\paths.json、
别人写死的默认值）。**重装系统后环境变量和 paths.json 全没了**，但硬件没变——
所以身份要挂在硬件指纹上，配置要存在库这边（跟着 NAS 走），不能只活在本机。

三件事，顺序固定：

    默认配置（配置\_默认.json）      ← 只有键名和「该找什么」，不含任何本机路径
        ↓  按本机探测
    机器配置（配置\机器\<指纹>.json） ← 每台机器一份，重装后按指纹还认得出来
        ↓  应用
    生效（paths.json + 用户级环境变量）← AI 和脚本真正读的那两个地方

命令：
    python persona_machine.py show                 看本机指纹 + 现在生效的配置
    python persona_machine.py detect               只探测（不写任何文件），--json 给机器看
    python persona_machine.py new [--label 名字]   生成/更新本机的配置，并应用（--dry 只看）
    python persona_machine.py apply [--id 指纹]    把某份配置写进 paths.json + 用户环境变量
    python persona_machine.py list                 已登记过的机器（哪台是哪台）
    python persona_machine.py diff                 配置 vs 现实：哪一项对不上了

身份为什么用硬件：主板 UUID / BIOS 序列号**重装系统不变**；主机名会变、用户目录会变、
盘符也会变。所以 `id` 取 `PC-<UUID 前 8 位>`，`label` 只是给人看的备注。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import persona_root

SCHEMA = 1
ENV_KEYS = [
    "SYSPERSONA_ROOT", "SYSPERSONA_REPO", "SYSPERSONA_RUNTIME",
    "PERSONA_PY", "PERSONA_NODE", "GIT_EXE", "GH_EXE",
    "PERSONA_BACKUP2", "SYSPERSONA_DSH_HOME",
]

# 键 -> 环境变量名（写进用户环境时用这个对照）
KEY_ENV = {
    "root": "SYSPERSONA_ROOT",
    "repo": "SYSPERSONA_REPO",
    "runtime": "SYSPERSONA_RUNTIME",
    "python": "PERSONA_PY",
    "node": "PERSONA_NODE",
    "git": "GIT_EXE",
    "gh": "GH_EXE",
    "backup2": "PERSONA_BACKUP2",
    "dsh_home": "SYSPERSONA_DSH_HOME",
    "hermes_home": "SYSPERSONA_HERMES_HOME",
    "herdsman": "PERSONA_HERDSMAN",
    "local_models": "SYSPERSONA_LOCAL_MODELS",
}

KEYS_DOC = {
    "root": "记忆库根：含 archive/ index/ memory/ 的那一层",
    "repo": "代码仓库根：含 tools/ctxpack/persona_root.py 与 docs/",
    "runtime": "独立运行时：其下有 python\\ 与 node\\",
    "python": "<runtime>\\python\\python.exe",
    "node": "<runtime>\\node\\bin\\node.exe",
    "git": "<runtime>\\git\\cmd\\git.exe",
    "gh": "<runtime>\\gh\\gh.exe",
    "backup2": "第二份备份根：另一块盘，且不在库内",
    "dsh_home": "壳的私有目录（默认 %USERPROFILE%\\.dsh）",
    "hermes_home": "Hermes 家目录；没装就是 null（不是空串）",
    "herdsman": "herdsman.exe；没装就是 null",
    "local_models": "本机模型目录（gguf 的上一层）；没有就是 null。有目录 ≠ 跑得动",
}
# 注意：**clients 不在 KEYS_DOC 里**——它不是「探测出来的路径」，而是人对这台机器的决定
# （clients.<名字>.enabled=false + why =「装了但本机不用」）。build_config 会去复制上一份，
# 不会自己编一个。


# ── 路径 ─────────────────────────────────────────────────────────────
def vault_root():
    r"""记忆库根：优先 persona_root（它已经处理过环境变量与向上找），否则从本文件推。"""
    try:
        r = persona_root.root(strict=False)
        if r:
            return r
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here) if os.path.basename(here) == "脚本" else here


def conf_dir():
    return os.path.join(vault_root(), "配置")


def machines_dir():
    return os.path.join(conf_dir(), "机器")


def default_path():
    return os.path.join(conf_dir(), "_默认.json")


def machine_path(mid):
    return os.path.join(machines_dir(), "%s.json" % mid)


# ── 硬件指纹 ─────────────────────────────────────────────────────────
def _ps(script, timeout=15):
    r"""跑一句 PowerShell 拿字符串；拿不到返回 None（不抛）。"""
    exe = shutil.which("powershell.exe") or r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
    try:
        p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "").strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


# 一次 PowerShell 把该问的都问完，返回一行 JSON。
# 为什么不是一问一条：每条都要重起一个 PowerShell（约 1 秒起），十条就是十秒；
# 更糟的是 `Win32_DiskDrive` 遇到休眠/掉线的盘会卡住——分开问时**整条命令卡死**，
# 而且不知道卡在哪一条。合成一次，超时只丢这一次的读数。
FP_PS = r"""
$e = [ordered]@{}
try { $cs = Get-CimInstance Win32_ComputerSystemProduct -ErrorAction Stop; $e.uuid = "$($cs.UUID)" } catch { $e.uuid = "" }
try { $pr = Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1
      $e.cpu_id = "$($pr.ProcessorId)"; $e.cpu_name = ("$($pr.Name)").Trim() } catch { $e.cpu_id = ""; $e.cpu_name = "" }
try { $bb = Get-CimInstance Win32_BaseBoard -ErrorAction Stop
      $e.board_serial = "$($bb.SerialNumber)"; $e.board_name = "$($bb.Product)" } catch { $e.board_serial = ""; $e.board_name = "" }
try { $e.macs = (@(Get-CimInstance Win32_NetworkAdapter -Filter 'PhysicalAdapter=True' -ErrorAction Stop |
        ForEach-Object { "$($_.MACAddress)" }) -join ',') } catch { $e.macs = "" }
try { $e.disks = (@(Get-CimInstance Win32_DiskDrive -ErrorAction Stop |
        ForEach-Object { ("$($_.SerialNumber)").Trim() }) -join ',') } catch { $e.disks = "" }
try { $e.machine_guid = "$((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid -ErrorAction Stop).MachineGuid)" } catch { $e.machine_guid = "" }
try { $e.os_install = (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).InstallDate.ToString('yyyy-MM-dd') } catch { $e.os_install = "" }
try { $e.bios_serial = "$((Get-CimInstance Win32_BIOS -ErrorAction Stop).SerialNumber)" } catch { $e.bios_serial = "" }
$e | ConvertTo-Json -Compress
"""

_FP_CACHE = {}


def fingerprint(full=True, timeout=45):
    r"""本机指纹：**多个信号一起收**，不靠单独任何一个。

    为什么不能只用一个（人 2026-11-12 指出的两条都得满足）：
      - 这台机器**两块网卡**（现在读到 3 块物理网卡），拆掉/停用一块 MAC 就少一个，
        单 MAC 当身份就会「换机器」；
      - 主板/BIOS 报的是 `To be filled by O.E.M.`，UUID 是 `03000200-…`（白牌机通用占位值），
        拿它当身份又会跟别人的机器撞号。

    所以收成一整组，判「是不是同一台」时看两组证据（见 match_score）：
        硬件组：CPUID、主板序列号/UUID、全部物理网卡 MAC、全部硬盘序列号
        身份组：系统 MachineGuid、计算机名、账户名

    full=False 时只给「机名 + 账户」这类不用起进程就能拿到的字段（`list` 用，快）。
    """
    host = os.environ.get("COMPUTERNAME") or "?"
    user = os.environ.get("USERNAME") or "?"
    if not full:
        return {"host": host, "user": user, "macs": [], "disks": [], "cpu_id": "",
                "board_uuid": "", "board_serial": "", "machine_guid": ""}

    key = (host, user)
    if key in _FP_CACHE:
        return _FP_CACHE[key]

    raw = _ps(FP_PS, timeout=timeout) or ""
    try:
        j = json.loads(raw[raw.find("{"):]) if "{" in raw else {}
    except ValueError:
        j = {}

    def valid_serial(s):
        s = (s or "").strip()
        if not s:
            return ""
        low = s.lower()
        if "to be filled" in low or low in ("none", "unknown", "default string", "o.e.m."):
            return ""
        if len(set(s)) <= 2:          # 00000000 / FFFFFFFF 这类
            return ""
        return s

    def valid_uuid(u):
        u = (u or "").strip()
        hexonly = re.sub(r"[^0-9A-Fa-f]", "", u)
        if not hexonly or len(set(hexonly)) <= 2:
            return ""
        if u.upper() in ("03000200-0400-0500-0006-000700080009",
                         "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"):
            return ""
        return u

    def split(s):
        return sorted({x.strip().upper() for x in (s or "").split(",") if x.strip()})

    fp = {
        "cpu_id": (j.get("cpu_id") or "").strip().upper(),
        "cpu_name": (j.get("cpu_name") or "").strip(),
        "board_uuid": valid_uuid(j.get("uuid")),
        "board_serial": valid_serial(j.get("board_serial")),
        "board_name": (j.get("board_name") or "").strip(),
        "bios_serial": valid_serial(j.get("bios_serial")),
        "macs": split(j.get("macs")),
        "disks": [d for d in split(j.get("disks")) if len(d) >= 4],
        "machine_guid": (j.get("machine_guid") or "").strip(),
        "os_install": (j.get("os_install") or "").strip(),
        "host": host, "user": user,
    }
    _FP_CACHE[key] = fp
    return fp


def hw_seed(fp):
    r"""算 id 用的种子：只放**硬件**，且放「多台之间最不容易撞、本机最不容易变」的那几个。

    顺序：CPUID + 主板序列号/型号 > 硬盘序列号 > 全部 MAC > 机器名（最后手段）。
    两个网卡都进 MAC 列表；id 一旦定下来就不再随指纹变化（见 cmd_new 的 match_score 逻辑）。
    """
    for parts in (
        [fp.get("cpu_id"), fp.get("board_serial"), fp.get("board_name")],
        fp.get("disks") or [],
        fp.get("macs") or [],
        [fp.get("host")],
    ):
        vals = [str(x).strip() for x in parts if x and str(x).strip()]
        if vals:
            return "|".join(vals)
    return "unknown"


def machine_id(fp=None):
    fp = fp or fingerprint()
    h = hashlib.sha1(hw_seed(fp).encode("utf-8")).hexdigest()[:8].upper()
    return "PC-%s" % h


def match_score(old, new):
    r"""两台/两次之间「是不是同一台」——两组证据，**任一组成立就算同一台**（人 2026-11-12 定）。

    第一组（硬件）：CPUID、主板序列号/UUID、物理网卡 MAC、硬盘序列号——**对上任意一个就算**。
    第二组（系统/身份）：系统 MachineGuid、计算机名、账户名——**对上任意一个也算**。

    为什么这么松：本机两个网卡，拆一个下来还是这台；重装系统后 MachineGuid 会变、
    机器名可能改，但硬件没动——两条路必须各留一条能认出来的。

    为什么还要分组：`DESKTOP-XXXX` 这种默认机名可能撞，但它在第二组；硬件那组照样独立成立，
    所以「名字撞了」不会把两台不相关的机器并成一台——只是不再额外加分而已。

    返回 (同一台, 硬件命中, 身份命中, 证据列表, 硬件是否全变了)。
    """
    old, new = old or {}, new or {}
    hw, ident, why = [], [], []

    def eq(a, b):
        a, b = (a or "").strip().upper(), (b or "").strip().upper()
        return bool(a) and a == b

    if eq(old.get("cpu_id"), new.get("cpu_id")):
        hw.append("CPUID 一致")
    if eq(old.get("board_uuid"), new.get("board_uuid")):
        hw.append("主板 UUID 一致")
    if eq(old.get("board_serial"), new.get("board_serial")):
        hw.append("主板序列号一致")
    om, nm = set(old.get("macs") or []), set(new.get("macs") or [])
    both = len(om & nm)
    if both:
        hw.append("网卡 MAC 对上 %d 个%s" % (both, "（两块都在）" if both >= 2 else ""))
    if set(old.get("disks") or []) & set(new.get("disks") or []):
        hw.append("硬盘序列号一致")

    if eq(old.get("machine_guid"), new.get("machine_guid")):
        ident.append("系统 MachineGuid 一致（没重装）")
    if eq(old.get("host"), new.get("host")):
        ident.append("计算机名一致")
    if eq(old.get("user"), new.get("user")):
        ident.append("账户名一致")

    same = bool(hw) or bool(ident)
    hw_changed = bool(ident) and not hw
    if hw_changed:
        why = ident + ["硬件全变了 → 换硬件了（同一台系统）"]
    else:
        why = hw + ident
    return same, bool(hw), bool(ident), why, hw_changed


def find_machine(fp=None):
    r"""在已登记的机器里找本机。

    返回 (配置 dict, 证据列表, 是否是「换了硬件」)。命中多个时，取证据最多的那份。
    """
    fp = fp or fingerprint()
    best, best_why, best_cfg, best_hw, best_n = None, [], None, False, -1
    d = machines_dir()
    if not os.path.isdir(d):
        return None, [], False
    for fn in os.listdir(d):
        if not fn.endswith(".json") or ".paths.json.bak" in fn:
            continue
        try:
            cfg = json.load(open(os.path.join(d, fn), encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        same, _hw, _ident, why, hw_changed = match_score(cfg.get("fingerprint"), fp)
        if not same:
            continue
        if len(why) > best_n:
            best, best_why, best_cfg, best_hw, best_n = len(why), why, cfg, hw_changed, len(why)
    return best_cfg, best_why, best_hw


# ── 探测 ─────────────────────────────────────────────────────────────
def _first_file(*paths):
    for p in paths:
        if p and os.path.isfile(os.path.expandvars(p)):
            return os.path.normpath(os.path.expandvars(p))
    return None


def _first_dir(*paths):
    for p in paths:
        if p and os.path.isdir(os.path.expandvars(p)):
            return os.path.normpath(os.path.expandvars(p))
    return None


def _drives():
    r"""本机盘符，按 C→D→E… 排；只用来列候选，不假设程序在哪块盘。"""
    out = []
    for letter in "CDEFGH":
        d = letter + ":\\"
        if os.path.isdir(d):
            out.append(d)
    return out


def _runtime_candidates():
    cands = [os.environ.get("SYSPERSONA_RUNTIME"),
             os.environ.get("PERSONA_RUNTIME")]
    for d in _drives():
        cands.append(os.path.join(d, "ProgramData", "SysPersona-Runtime"))
    la = persona_root.localappdata()
    if la:
        cands.append(os.path.join(la, "SysPersona", "Runtime"))
    return [c for c in cands if c]


def detect():
    r"""按默认配置的键逐个探测；探不到就是 None（**不猜一个能跑的路径冒充**）。"""
    out = {}
    out["root"] = vault_root()

    # runtime：先看变量，再看常见盘，再看壳自带的那个（只作兜底，会被标注）
    rt = None
    for c in _runtime_candidates():
        if c and os.path.isdir(c) and (os.path.isdir(os.path.join(c, "python"))
                                       or os.path.isdir(os.path.join(c, "node"))):
            rt = os.path.normpath(c)
            break
    out["runtime"] = rt

    def tool(exe_rel, env, which):
        v = os.environ.get(env)
        if v and os.path.isfile(os.path.expandvars(v)):
            return os.path.normpath(os.path.expandvars(v))
        if rt:
            p = os.path.join(rt, exe_rel)
            if os.path.isfile(p):
                return p
        w = shutil.which(which)
        return os.path.normpath(w) if w else None

    out["python"] = tool(os.path.join("python", "python.exe"), "PERSONA_PY", "python")
    out["node"] = tool(os.path.join("node", "bin", "node.exe"), "PERSONA_NODE", "node")
    out["git"] = tool(os.path.join("git", "cmd", "git.exe"), "GIT_EXE", "git")
    out["gh"] = tool(os.path.join("gh", "gh.exe"), "GH_EXE", "gh")

    # repo：从库根向上、同盘常见位置找「含 tools/ctxpack/persona_root.py」的那一层
    repo = os.environ.get("SYSPERSONA_REPO")
    if not (repo and os.path.isfile(os.path.join(repo, "tools", "ctxpack", "persona_root.py"))):
        repo = None
        here = vault_root()
        for _ in range(3):
            here = os.path.dirname(here)
            if os.path.isfile(os.path.join(here, "tools", "ctxpack", "persona_root.py")):
                repo = here
                break
        if not repo:
            for d in _drives():
                for name in ("SysPersona-OS", os.path.join("文档", "SysPersona-OS"),
                             os.path.join("Documents", "SysPersona-OS")):
                    cand = os.path.join(d, name)
                    if os.path.isfile(os.path.join(cand, "tools", "ctxpack", "persona_root.py")):
                        repo = cand
                        break
                if repo:
                    break
    out["repo"] = repo

    # backup2：只认配置/变量，不给默认值（第二份必须在另一块盘）
    out["backup2"] = persona_root.backup_root()

    # dsh_home / hermes_home / herdsman：配置里可能写着「应该有」，但本机其实没装——
    # 所以这里一律再验一次存在性，不把配置里的期望当成事实。
    out["dsh_home"] = persona_root.dsh_home()
    hh = persona_root.hermes_home()
    out["hermes_home"] = hh if hh and os.path.isdir(hh) else None
    hs = persona_root.herdsman()
    out["herdsman"] = hs if hs and os.path.isfile(hs) else None
    lm = persona_root.local_models()
    out["local_models"] = lm if lm and os.path.isdir(lm) else None
    return out


def user_env(names=None):
    r"""读**用户级**环境变量（注册表里的，不是本进程的）。

    本进程的环境是启动时快照：刚设过的变量在这儿看不到，直接读 os.environ 会误报「没有」。
    """
    names = names or ENV_KEYS
    script = "; ".join(
        "Write-Output ('%s=' + [Environment]::GetEnvironmentVariable('%s','User'))" % (n, n)
        for n in names)
    out = _ps(script) or ""
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip() or None
    return d


# ── 默认配置 ─────────────────────────────────────────────────────────
DEFAULT_DOC = {
    "schema": SCHEMA,
    "_note": "默认配置：只有键名和「该找什么」，不含任何本机路径。新机器照它探测，再生成 机器\\<指纹>.json。",
    "keys": KEYS_DOC,
    "env": ENV_KEYS,
    "order": "默认 -> 探测（detect）-> 生成机器配置（new）-> 应用（apply）",
}


def ensure_default():
    p = default_path()
    if os.path.isfile(p):
        return p
    os.makedirs(conf_dir(), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_DOC, f, ensure_ascii=False, indent=2)
    return p


def build_config(label=None, mid=None, created=None, prev_clients=None):
    r"""探测 + 拼成一份完整配置（字段顺序固定，方便 diff）。

    `mid` 由调用方给：新机器按指纹算，老机器按 find_machine 认出来（id 不能因为
    加了一块网卡就变成另一台）。
    """
    fp = fingerprint()
    det = detect()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    cfg = {
        "schema": SCHEMA,
        "id": mid or machine_id(fp),
        "label": label or fp["host"],
        "created": created or now,
        "updated": now,
        "fingerprint": fp,
    }
    cfg.update({k: det.get(k) for k in KEYS_DOC})
    if isinstance(prev_clients, dict) and prev_clients:
        cfg["clients"] = prev_clients        # 本机策略是人定的，重跑探测不能把它冲掉
    cfg["env"] = {KEY_ENV[k]: det.get(k) for k in KEY_ENV if det.get(k)}
    return cfg


# ── 应用 ─────────────────────────────────────────────────────────────
def _env_paths_json():
    la = persona_root.localappdata()
    return os.path.join(la, "SysPersona", "paths.json") if la else None


def apply_config(cfg, dry=False):
    r"""把一份配置写进两个真正生效的地方：paths.json + 用户级环境变量。

    写之前先备份 paths.json（坏了一键回滚）；环境变量只增改、不删别人的。
    """
    mid = cfg.get("id") or "unknown"
    pj = _env_paths_json()
    done = []
    if pj:
        os.makedirs(os.path.dirname(pj), exist_ok=True)
        if os.path.isfile(pj):
            bak = os.path.join(machines_dir(), "%s.paths.json.bak-%s" % (
                mid, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
            if not dry:
                os.makedirs(machines_dir(), exist_ok=True)
                shutil.copy2(pj, bak)
                done.append("备份旧 paths.json -> %s" % bak)
        payload = {k: v for k, v in cfg.items()
                   if k in KEY_ENV and v}
        # 这两项不是环境变量，但每次 apply 都得跟着进 paths.json：
        # id 用来认出「我是谁」，clients 是本机策略（装了但不用哪些重客户端）。
        if cfg.get("id"):
            payload["id"] = cfg["id"]
        payload["clients"] = cfg.get("clients") or {}
        payload["note"] = "由 persona_machine.py 于 %s 从 %s 生成" % (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), mid)
        if dry:
            done.append("[dry] 写 paths.json：%s" % json.dumps(payload, ensure_ascii=False)[:120])
        else:
            with open(pj, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            done.append("写 paths.json -> %s" % pj)

    # 环境变量：一次 PowerShell 全设完（一个变量起一个进程要 1 秒，九个就是九秒）
    todo = [(env, str(cfg.get(key))) for key, env in KEY_ENV.items() if cfg.get(key)]
    if todo:
        if dry:
            for env, val in todo:
                done.append("[dry] 设 %s=%s" % (env, val))
        else:
            script = "; ".join(
                "[Environment]::SetEnvironmentVariable('%s','%s','User')"
                % (env, val.replace("'", "''")) for env, val in todo)
            try:
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, timeout=60)
                for env, val in todo:
                    old = os.environ.get(env)
                    os.environ[env] = val
                    done.append("环境变量 %s（原值 %s）" % (env, old or "无"))
            except (OSError, subprocess.SubprocessError) as e:
                done.append("环境变量一次写失败：%s" % e)
    return done


# ── 命令 ─────────────────────────────────────────────────────────────
def cmd_show(args):
    fp = fingerprint()
    prev, why, hw_changed = find_machine(fp)
    mid = (prev or {}).get("id") or machine_id(fp)
    print("本机：%s%s" % (mid, "   （认出来的：%s）" % "；".join(why) if prev else ""))
    print("  CPU    %s  [%s]" % (fp["cpu_id"] or "?", fp["cpu_name"] or "?"))
    print("  主板   %s / %s%s" % (fp["board_name"] or "?",
                                  fp["board_serial"] or "（序列号空）",
                                  "" if fp["board_uuid"] else "  · UUID 是占位值，不采信"))
    print("  网卡   %d 块：%s" % (len(fp["macs"]), "、".join(fp["macs"]) or "（没读到）"))
    print("  硬盘   %d 块：%s" % (len(fp["disks"]), "、".join(fp["disks"]) or "（没读到）"))
    print("  系统   MachineGuid %s · 装机 %s" % (fp["machine_guid"] or "?", fp["os_install"] or "?"))
    print("  身份   计算机名 %s · 账户 %s" % (fp["host"], fp["user"]))
    if hw_changed:
        print("  ⚠ 系统没重装，但硬件全变了 —— 记成「换硬件」")
    ue = user_env()
    print("\n生效配置（用户级环境变量 / 本进程）：")
    for k, env in KEY_ENV.items():
        u = ue.get(env)
        p = os.environ.get(env)
        mark = "" if u == p else "  （本进程还是旧值：%s）" % (p or "空")
        print("  %-13s %-22s %s%s" % (k, env, u or "（没有）", mark))
    p = machine_path(mid)
    print("\n本机配置档：%s  %s" % (p, "在" if os.path.isfile(p) else "还没有（跑 new）"))
    if os.path.isfile(p):
        try:
            cfg = json.load(open(p, encoding="utf-8-sig"))
            print("  生成于 %s · 标签 %s" % (cfg.get("created"), cfg.get("label")))
        except (OSError, ValueError):
            pass
    return 0


def cmd_detect(args):
    det = detect()
    if args.json:
        print(json.dumps(det, ensure_ascii=False, indent=2))
        return 0 if det.get("root") else 1
    for k in KEYS_DOC:
        v = det.get(k)
        print("  %-13s %s" % (k, v if v else "（没找到）"))
    return 0


def cmd_new(args):
    ensure_default()
    fp = fingerprint()
    prev, why, hw_changed = find_machine(fp)
    created = (prev or {}).get("created")
    mid = (prev or {}).get("id") or machine_id(fp)
    cfg = build_config(args.label, mid=mid, created=created,
                       prev_clients=(prev or {}).get("clients"))
    if prev and not args.label:
        cfg["label"] = prev.get("label") or cfg["label"]
    cfg["matched_by"] = why if prev else []
    cfg["hardware_changed"] = bool(hw_changed)

    p = machine_path(cfg["id"])
    if args.dry:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        print("\n[dry] 会写到 %s，并应用（paths.json + 用户环境变量）" % p)
        return 0

    if prev:
        print("认出是本机：%s（%s）" % (mid, "；".join(why)))
        if hw_changed:
            print("  注意：系统没重装但硬件对不上——按「换硬件」处理，id 不变。")
    else:
        print("新机器：%s（按硬件算的，没有任何已知机器对得上）" % mid)

    os.makedirs(machines_dir(), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("机器配置已写：%s" % p)
    for line in apply_config(cfg):
        print("  " + line)
    return 0


def cmd_apply(args):
    mid = args.id or ((find_machine()[0] or {}).get('id')) or machine_id()
    p = machine_path(mid)
    if not os.path.isfile(p):
        print("没有这份配置：%s（先跑 new，或 list 看看有哪些）" % p)
        return 2
    cfg = json.load(open(p, encoding="utf-8-sig"))
    print("应用 %s（%s）" % (mid, cfg.get("label")))
    for line in apply_config(cfg):
        print("  " + line)
    return 0


def cmd_list(args):
    d = machines_dir()
    if not os.path.isdir(d):
        print("还没有登记过机器：%s" % d)
        return 0
    me_host = (os.environ.get("COMPUTERNAME") or "?").upper()
    me_user = (os.environ.get("USERNAME") or "?").upper()
    rows = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json") or ".paths.json.bak" in fn:
            continue
        try:
            c = json.load(open(os.path.join(d, fn), encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        fp = c.get("fingerprint") or {}
        # 列个表而已，不必把 CIM 全查一遍：机名+账户对上就标「本机」（要严格判用 show/diff）
        me = (fp.get("host") or "").upper() == me_host and (fp.get("user") or "").upper() == me_user
        rows.append((c.get("id") or fn, c.get("label") or "?", c.get("updated") or c.get("created") or "?", me))
    if not rows:
        print("还没有登记过机器：%s" % d)
        return 0
    print("%-14s %-16s %-20s %s" % ("id", "标签", "最后更新", "本机"))
    for mid, label, up, me in rows:
        print("%-14s %-16s %-20s %s" % (mid, label, up, "← 是" if me else ""))
    return 0


def cmd_diff(args):
    mid = (find_machine()[0] or {}).get('id') or machine_id()
    p = machine_path(mid)
    if not os.path.isfile(p):
        print("本机还没有配置档（%s）；先跑 new。" % p)
        return 2
    cfg = json.load(open(p, encoding="utf-8-sig"))
    now = detect()
    bad = 0
    print("%-13s %-46s %s" % ("键", "配置里", "现在探测到"))
    for k in KEYS_DOC:
        want, got = cfg.get(k), now.get(k)
        same = (want == got) or (not want and not got)
        print("%-13s %-46s %s%s" % (k, want or "（空）", got or "（空）", "" if same else "   ← 不一致"))
        bad += 0 if same else 1
    if bad:
        print("\n%d 项对不上：跑 `new` 重新生成（会保留 created 和 label）。" % bad)
        return 1
    print("\n配置与现实一致。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="每台机器一份配置：默认 -> 探测 -> 生成 -> 应用")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("show", help="本机指纹 + 生效配置")
    p = sub.add_parser("detect", help="只探测")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("new", help="生成/更新本机配置并应用")
    p.add_argument("--label", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("apply", help="应用某份配置")
    p.add_argument("--id", default=None)
    sub.add_parser("list", help="已登记的机器")
    sub.add_parser("diff", help="配置 vs 现实")
    a = ap.parse_args(argv)
    return {"show": cmd_show, "detect": cmd_detect, "new": cmd_new,
            "apply": cmd_apply, "list": cmd_list, "diff": cmd_diff}.get(
        a.cmd or "show", cmd_show)(a)


if __name__ == "__main__":
    sys.exit(main())
