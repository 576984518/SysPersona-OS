# -*- coding: utf-8 -*-
"""分身主键 avatar：`<agent>-<model>[-<version>]`，以及性格目录的唯一入口。

规范：`docs/AVATAR_SPEC.md` §1.2（性格隔离，2026-11-09 人定）

四条纪律，写在这里当唯一实现：
1. 三段全同才算同一分身；缺字段写 `unknown`，**不猜**；
2. 性格文件 `memory\\分身\\<avatar>\\persona.json` **只归本人读写**；
3. 检索层只读性格文件（本模块只提供 `load_persona`，没有 save）；
4. 取名不取专名，只留简称——模型的出厂身份已经有了，别叠加。
"""
import io
import json
import os
import re

ROOT = os.environ.get("SYSPERSONA_ROOT") or r"D:\4590-PC-文件\AI会话检索记录"

# 头里有两种方言：老导出（设备 / 底座）与 Hermes 导出（来源 / 模型）。
# 两样都认，缺了就留空——宁可为空，也不要瞎猜。
_RE_AGENT = re.compile(r'(?m)^-\s*(?:设备|客户端|来源)：\s*(.+?)\s*$')
_RE_MODEL = re.compile(r'(?m)^-\s*(?:底座|模型)：\s*(.+?)\s*$')

# 只有"来源桶"是事实，不是猜：网页对话一定来自网页。
_BUCKET = {
    '网页对话': ('网页', 'deepseek-web'),
    '手机端': ('手机', ''),
}

# 来源一栏常写成长句（`Hermes Agent（本地 state.db）`），这里只留简称。
_AGENT_HINTS = (
    ('hermes', 'hermes'),
    ('dsh', 'dsh'),
    ('deepseek harness', 'dsh'),
)


def norm(s):
    """小写、去组织前缀、非字母数字汉字压成 `-`。`deepseek-ai/DeepSeek-V4-Flash` → `deepseek-v4-flash`。"""
    s = (s or '').strip()
    if not s:
        return ''
    s = s.split('/')[-1]              # 去掉 `deepseek-ai/` 这类组织前缀
    s = re.sub(r'[（(].*?[）)]', '', s)  # 去掉括号注释
    s = s.lower()
    s = re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '-', s)
    s = re.sub(r'-{2,}', '-', s).strip('-')
    return s


_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _agent_of(raw, src=''):
    """来源一栏用途太杂：可能是 `Hermes Agent`、可能是本地路径、也可能是 URL。

    顺序：**来源桶（事实）> URL（网页）> 关键词（dsh / hermes）> 原样归一（UUID 一律丢）**。
    """
    raw = (raw or '').strip().strip('`')
    if src in _BUCKET:
        return _BUCKET[src][0]
    if re.match(r'(?i)^https?://', raw):
        return '网页'
    low = raw.lower()
    for hint, short in _AGENT_HINTS:
        if hint in low:
            return short
    r = norm(raw)
    if _UUID.match(r):
        return ''
    return r


def from_head(head, src=''):
    """从元信息头取 (agent, model, avatar)。head 只要前 4 KB。"""
    head = (head or '').partition('\n---\n')[0]
    m_agent = _RE_AGENT.search(head)
    m_model = _RE_MODEL.search(head)
    agent = _agent_of(m_agent.group(1) if m_agent else '', src)
    model = norm(m_model.group(1)) if m_model else ''
    if not model and src in _BUCKET:
        model = _BUCKET[src][1]
    if not agent and src in _BUCKET:
        agent = _BUCKET[src][0]
    return agent, model, join(agent, model)


def join(agent, model, version=''):
    """avatar 永远是 `<agent>-<model>`：缺哪段写 `unknown`，不缩写成一个词。

    `dsh` 这种单段会让人以为是完整身份，实际只是"壳知道、模型没记"——写 `dsh-unknown` 才诚实。
    """
    agent = norm(agent) or 'unknown'
    model = norm(model) or 'unknown'
    if agent == 'unknown' and model == 'unknown':
        return 'unknown'
    parts = [agent, model] + ([norm(version)] if norm(version) else [])
    return '-'.join(parts)


def of(dev, mod, src=''):
    """给 blocks() 里的 device / model 两列用；中文设备名直接归一。"""
    return join(norm(dev) or _BUCKET.get(src, ('', ''))[0], norm(mod) or _BUCKET.get(src, ('', ''))[1])


def of_file(path, rel=''):
    """读一个档案文件，求它的 avatar。rel 用于来源桶兜底。"""
    try:
        head = io.open(path, encoding='utf-8', errors='replace').read(4096)
    except OSError:
        return 'unknown'
    src = ''
    parts = (rel or '').replace('\\', '/').split('/')
    if len(parts) > 3:
        src = parts[2]
    return from_head(head, src)[2]


def persona_dir(avatar):
    """性格目录。检索层只读，不写——路径上就把它和记忆分开。"""
    return os.path.join(ROOT, 'memory', '分身', avatar or 'unknown')


def persona_path(avatar):
    return os.path.join(persona_dir(avatar), 'persona.json')


def load_persona(avatar):
    """只读性格面。没有就返回 None——**绝不在这里创建**（创建/漂移是本人记忆的事）。"""
    p = persona_path(avatar)
    if not os.path.exists(p):
        return None
    try:
        return json.load(io.open(p, encoding='utf-8'))
    except ValueError:
        return None


def list_personas():
    """已存在的性格面：{avatar: persona}。只读。"""
    base = os.path.join(ROOT, 'memory', '分身')
    out = {}
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        p = load_persona(name)
        if p is not None:
            out[name] = p
    return out
