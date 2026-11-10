# -*- coding: utf-8 -*-
r"""persona_root.py — 路径解析的唯一出口（2026-11-10 收口：本机路径全部改成变量/环境变量）

取值优先级（全模块统一，不要另写一套）：
    显式参数 > 环境变量 > 本机可选配置 > 相对脚本自身的推导 > 明确的默认值

需要的环境变量（都可不设；设了优先，见 docs/USAGE.md「路径与环境变量」）：
    SYSPERSONA_ROOT     记忆库根目录（正式名）；PERSONA_ROOT 为旧名，仍认
    SYSPERSONA_REPO     代码仓库根目录（含 tools/ctxpack 与 docs/）
    PERSONA_PY          Python 解释器（python.exe 或 pythonw.exe）
    SYSPERSONA_RUNTIME  运行时根（其下有 python\ 与 node\bin\）；不设就从 sys.executable 推
    PERSONA_NODE        node 可执行文件；不设就找 <runtime>\node\bin\node.exe，再找 PATH
    PERSONA_HERMES_HOME Hermes 家目录（state.db / desktop-ui.sqlite 所在）
    PERSONA_CAPTURE     抓取用的 node 脚本（collect7.js 一类）
    PERSONA_HERDSMAN    herdsman.exe 路径
    PERSONA_BACKUP2     第二备份根目录
    SYSPERSONA_DSH_HOME 壳的私有目录（默认 %USERPROFILE%\.dsh；也认 DSH_HOME）
    SYSPERSONA_DSH_STORAGES 壳的 storages 目录（默认 <DSH_HOME>\storages）

缺了会怎样：root() / repo() 找不到对应目录时抛 PathUnset，消息里直接写该设哪个变量；
node() / hermes_home() / runtime_dir() / herdsman() 找不到就返回 None，由调用处报一句
清楚的话并跳过该任务——**不做静默回退，也不猜第二台机器的路径**。
LOCALAPPDATA / ProgramData 一律交给 os.environ，取不到返回 None（本模块不替系统做主）。

可选的本机配置（只读，仓库里不创建、不提交）：
  %LOCALAPPDATA%\SysPersona\paths.json  键名同上面的变量（小写）
  <repo|本文件目录>\ops\persona.conf.json  键名 capture_js / backup2_root 等
没有这两个文件也能跑；有就等于把变量固化成文件，优先级低于环境变量。
"""
import json
import os
import shutil
import sys

__all__ = [
    "PathUnset", "find_root", "root", "repo", "runtime_dir", "runtime_py", "node",
    "hermes_home", "herdsman", "capture_js", "backup_root",
    "dsh_home", "dsh_storages",
    "localappdata", "programdata", "app_local", "require", "explain",
]


class PathUnset(RuntimeError):
    """该配的变量没配、也推导不出来。消息本身要能直接当提示用。"""


_CFG = None
_CONF = None


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def _pick(*values):
    r"""第一个非空的字符串。空串 / None 都算没设——以前 os.environ.get() 拿到 '' 会当路径用。"""
    for v in values:
        if v and str(v).strip():
            return str(v).strip()
    return None


def _machine_cfg():
    r"""可选的本机配置：%LOCALAPPDATA%\\SysPersona\\paths.json。读不到就是空字典。"""
    global _CFG
    if _CFG is not None:
        return _CFG
    _CFG = {}
    base = localappdata()
    if not base:
        return _CFG
    path = os.path.join(base, "SysPersona", "paths.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _CFG = data
    except (OSError, ValueError):
        pass
    return _CFG


def _cfg(key):
    return _pick(_machine_cfg().get(key))


def _persona_conf():
    r"""可选的 ops\persona.conf.json（本机配置，不入库）；读不到就是空字典。

    先看 SYSPERSONA_REPO\ops，再看本文件目录\ops，最后看仓库布局的 ../../ops。
    这里**不调用 repo()**，避免 repo() 读本配置时又回头调用自己。
    """
    global _CONF
    if _CONF is not None:
        return _CONF
    _CONF = {}
    cands = []
    rp = _pick(os.environ.get("SYSPERSONA_REPO"), _cfg("repo"))
    if rp:
        cands.append(os.path.join(rp, "ops", "persona.conf.json"))
    here = _here()
    cands.append(os.path.join(here, "ops", "persona.conf.json"))
    cands.append(os.path.join(os.path.dirname(os.path.dirname(here)),
                              "ops", "persona.conf.json"))
    for path in dict.fromkeys(cands):
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _CONF = data
                break
        except (OSError, ValueError):
            continue
    return _CONF


def _conf(key):
    return _pick(_persona_conf().get(key))


def localappdata():
    r"""%LOCALAPPDATA%；非 Windows 或没设就 None，**不猜**。"""
    return _pick(os.environ.get("LOCALAPPDATA"))


def programdata():
    r"""%ProgramData%；取不到就 None，**不猜**（这台机器就取不到真正的数据盘）。"""
    return _pick(os.environ.get("PROGRAMDATA"), os.environ.get("ProgramData"))


def app_local(*parts):
    r"""<LOCALAPPDATA>\\SysPersona\\<parts...>；LOCALAPPDATA 缺了就 None。"""
    base = localappdata()
    if not base:
        return None
    return os.path.join(base, "SysPersona", *parts) if parts else os.path.join(base, "SysPersona")


def _looks_like_vault(path):
    return os.path.isdir(os.path.join(path, "archive")) or \
        os.path.isdir(os.path.join(path, "index"))


def find_root():
    r"""宽松版：环境变量 → 含 archive/ 的脚本旁目录 → 脚本目录（老接口，保留兼容）。

    新代码用 root()：那个在找不到库时会报一句清楚的话，而不是把仓库目录当库用。
    """
    env = _pick(os.environ.get("SYSPERSONA_ROOT"), os.environ.get("PERSONA_ROOT"))
    if env:
        return env
    here = _here()
    for cand in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(cand, "archive")):
            return cand
    return here


def root(explicit=None, strict=True):
    r"""记忆库根目录。

    显式参数 > SYSPERSONA_ROOT > PERSONA_ROOT > 本机配置 root > 含 archive/ 或
    index/ 的脚本旁目录 > （strict 时）抛 PathUnset / （否则）None。
    """
    v = _pick(explicit, os.environ.get("SYSPERSONA_ROOT"),
              os.environ.get("PERSONA_ROOT"), _cfg("root"), _conf("root"))
    if v:
        return v
    here = _here()
    for cand in (here, os.path.dirname(here)):
        if _looks_like_vault(cand):
            return cand
    if not strict:
        return None
    raise PathUnset(
        "找不到记忆库根目录：请设 SYSPERSONA_ROOT（例如 SET SYSPERSONA_ROOT=<库目录>），"
        "或把本文件放到库根旁边（库根要有 archive\\ 或 index\\）。当前脚本目录：%s" % here)


def repo(explicit=None):
    r"""代码仓库根目录（含 tools\\ctxpack\\persona_root.py 与 docs\\）。

    显式参数 > SYSPERSONA_REPO > 本机配置 repo > 从本文件向上找 > None。
    单独拷到记忆库根下（平铺部署）时推导不出来，返回 None——调用处要能只跳过那一件任务。
    """
    v = _pick(explicit, os.environ.get("SYSPERSONA_REPO"), _cfg("repo"), _conf("repo"))
    if v:
        return v
    cand = _here()
    while True:
        parent = os.path.dirname(cand)
        if parent == cand:
            return None
        cand = parent
        if os.path.isfile(os.path.join(cand, "tools", "ctxpack", "persona_root.py")) and \
                os.path.isdir(os.path.join(cand, "docs")):
            return cand


def runtime_dir(explicit=None):
    r"""运行时根目录（其下有 python\\ 与 node\\bin\\）。

    显式参数 > SYSPERSONA_RUNTIME > 本机配置 runtime > 从 sys.executable 往前推
    （<runtime>\\python\\python.exe → <runtime>）> %ProgramData%\\SysPersona-Runtime
    （**存在才算**）> None。不写死任何盘符。
    """
    v = _pick(explicit, os.environ.get("SYSPERSONA_RUNTIME"), _cfg("runtime"),
              _conf("runtime"))
    if v:
        return v
    exe = os.path.abspath(sys.executable)
    d = os.path.dirname(exe)
    if os.path.basename(d).lower().startswith("python"):
        cand = os.path.dirname(d)
        # 认两种情况：<runtime>\python\python.exe 与 <runtime>\dependencies\python\python.exe
        if os.path.isdir(os.path.join(cand, "python")) or \
                os.path.isdir(os.path.join(cand, "node")):
            return cand
    pd = programdata()
    if pd:
        cand = os.path.join(pd, "SysPersona-Runtime")
        if os.path.isdir(cand):
            return cand
    return None


def runtime_py(explicit=None, windowed=False):
    r"""Python 解释器。

    显式参数 > PERSONA_PY > 本机配置 python > <runtime>\\python\\python(w).exe
    > sys.executable（正在跑的这个解释器，永远不会指错机器）。
    windowed=True 时尽量换成同目录的 pythonw.exe（GUI 入口，不弹黑窗）。
    """
    v = _pick(explicit, os.environ.get("PERSONA_PY"), _cfg("python"), _conf("python"))
    if not v:
        rt = runtime_dir()
        if rt:
            cand = os.path.join(rt, "python", "python.exe")
            if os.path.isfile(cand):
                v = cand
    if not v:
        v = sys.executable
    if windowed and os.path.basename(v).lower() in ("python.exe", "python3.exe"):
        cand = os.path.join(os.path.dirname(v), "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return v


def node(explicit=None):
    r"""node 可执行文件。

    显式参数 > PERSONA_NODE > NODE_BIN（旧名）> 本机配置 node
    > <runtime>\\node\\bin\\node.exe > PATH 上的 node > None。
    """
    v = _pick(explicit, os.environ.get("PERSONA_NODE"), os.environ.get("NODE_BIN"),
              _cfg("node"), _conf("node"))
    if v:
        return v
    rt = runtime_dir()
    if rt:
        cand = os.path.join(rt, "node", "bin", "node.exe")
        if os.path.isfile(cand):
            return cand
    return shutil.which("node")


def hermes_home(explicit=None):
    r"""Hermes 家目录（内含 state.db / desktop-ui.sqlite）。

    显式参数 > PERSONA_HERMES_HOME > SYSPERSONA_HERMES_HOME > 本机配置 hermes_home
    > %ProgramData%\\Hermes Agent CN Desktop\\data\\hermes-home（**存在才算**）> None。
    这台机器的 %ProgramData% 指向系统盘默认位置，而运行时数据放在另一块盘的
    \ProgramData 下——两者没有可推导关系，所以只能靠变量/本机配置；不设就由调用处
    报一句清楚的话。
    """
    v = _pick(explicit, os.environ.get("PERSONA_HERMES_HOME"),
              os.environ.get("SYSPERSONA_HERMES_HOME"), _cfg("hermes_home"),
              _conf("hermes_home"))
    if v:
        return v
    pd = programdata()
    if pd:
        cand = os.path.join(pd, "Hermes Agent CN Desktop", "data", "hermes-home")
        if os.path.isdir(cand):
            return cand
    return None


def herdsman(explicit=None):
    r"""herdsman.exe：显式参数 > PERSONA_HERDSMAN > 本机配置 herdsman_exe
    > %ProgramData%\\Herdsman\\herdsman.exe（存在才算）> None。"""
    v = _pick(explicit, os.environ.get("PERSONA_HERDSMAN"), _cfg("herdsman"),
              _conf("herdsman_exe"))
    if v:
        return v
    pd = programdata()
    if pd:
        cand = os.path.join(pd, "Herdsman", "herdsman.exe")
        if os.path.isfile(cand):
            return cand
    return None


def capture_js(explicit=None):
    r"""网页抓取的 node 脚本：显式参数 > PERSONA_CAPTURE > 本机配置 capture_js / capture > None。"""
    return _pick(explicit, os.environ.get("PERSONA_CAPTURE"), _cfg("capture"),
                 _conf("capture_js"), _conf("capture"))


def backup_root(explicit=None):
    r"""第二备份根：显式参数 > PERSONA_BACKUP2 > 本机配置 backup2 / backup2_root > None。

    故意不默认到 `<库根>\\备份`：第二份备份必须在另一块盘上，落进库里等于没有第二份。
    没配就由调用处报「跳过」，而不是把备份写进库内。
    """
    return _pick(explicit, os.environ.get("PERSONA_BACKUP2"), _cfg("backup2"),
                 _conf("backup2"), _conf("backup2_root"))


def dsh_home(explicit=None):
    r"""壳的私有目录（其下有 storages\ 与 sessions\）。

    显式参数 > SYSPERSONA_DSH_HOME > PERSONA_DSH_HOME > DSH_HOME（壳自己设的）
    > 本机配置 dsh_home > 用户主目录下的 .dsh > None。
    """
    v = _pick(explicit, os.environ.get("SYSPERSONA_DSH_HOME"),
              os.environ.get("PERSONA_DSH_HOME"), os.environ.get("DSH_HOME"),
              _cfg("dsh_home"), _conf("dsh_home"))
    if v:
        return v
    home = _pick(os.path.expanduser("~"))
    if home and home != "~":
        return os.path.join(home, ".dsh")
    return None


def dsh_storages(explicit=None):
    r"""壳的 storages 目录（cost-meter / session_projcache 所在）。

    显式参数 > SYSPERSONA_DSH_STORAGES > PERSONA_DSH_STORAGES > 本机配置
    dsh_storages > dsh_home()\storages > None。只读；本模块不往里写。
    """
    v = _pick(explicit, os.environ.get("SYSPERSONA_DSH_STORAGES"),
              os.environ.get("PERSONA_DSH_STORAGES"),
              _cfg("dsh_storages"), _conf("dsh_storages"))
    if v:
        return v
    home = dsh_home()
    return os.path.join(home, "storages") if home else None


def require(path, what, hint=None):
    r"""没解析出来就在调用处报一句清楚的话——不要把它当空串传下去。"""
    if path:
        return path
    raise PathUnset("找不到%s。%s" % (what, hint or "请用环境变量指定（见 docs/USAGE.md）。"))


def explain():
    r"""排障用：一次看清每个值从哪来、解析成了什么。"""
    keys = [
        ("root", root(strict=False)), ("repo", repo()), ("runtime", runtime_dir()),
        ("python", runtime_py()), ("pythonw", runtime_py(windowed=True)),
        ("node", node()), ("hermes_home", hermes_home()), ("herdsman", herdsman()),
        ("capture_js", capture_js()), ("backup2", backup_root()),
        ("dsh_home", dsh_home()), ("dsh_storages", dsh_storages()),
        ("LOCALAPPDATA", localappdata()), ("ProgramData", programdata()),
    ]
    width = max(len(k) for k, _ in keys)
    return "\n".join("%-*s = %s" % (width, k, v if v else "（未设置）") for k, v in keys)


ROOT = find_root()          # 老代码直接 `from persona_root import ROOT` 时还能用
