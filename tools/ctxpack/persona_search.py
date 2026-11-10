# -*- coding: utf-8 -*-
"""persona_search.py — Zero-Miss 混合检索原型

两条路并行，谁也别想漏：
  A) 关键词路：IDF 加权的字面命中（专有名词、代号、数字靠它）
  B) 向量路：bge-m3 语义近邻（换个说法也能找到）
融合：Reciprocal Rank Fusion，取 Top-K（默认 30，宁可塞垃圾不漏线索）
兜底：两路都空时，按时间倒序拉当天最近 N 块原文

引擎（--engine，默认 auto，全部路径都保留）：
  auto      有向量走原混合；向量不可用优先 FTS；FTS 也没有回落关键词
  fts / fts-char / fts-jieba   纯 FTS（零模型；fts.db 由 persona_fts.py --build 生成）
  keyword   原 IDF 关键词路（--no-vector 的老行为）
  vector    纯向量路
  hybrid    FTS + 关键词两路 RRF（不碰向量）

用法：
  python persona_search.py "防止舔狗化的惩罚规则" --k 30
  python persona_search.py "960 显卡" --k 10 --no-vector
  python persona_search.py "上下文压缩" --engine fts-char --k 20
"""
import argparse
import datetime
import io
import json
import math
import os
import hashlib
import re
import sys
import subprocess

try:
    import persona_win as _pw  # 无窗口：GUI 进程里 spawn 控制台程序不弹黑窗
except Exception:
    pass

try:
    import persona_fts as _fts  # 零模型 FTS5 路；缺这个文件时自动回落关键词
except Exception:
    _fts = None

import tempfile
import time

# numpy 只在向量路用；FTS/关键词路不该因为没装 numpy 就起不来。
# 输出编码：GBK 控制台遇到 emoji（🔧）会抛 UnicodeEncodeError，统一转 UTF-8。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from persona_root import find_root as _find_root  # noqa: E402  唯一实现：persona_root.py


import persona_avatar as av

ROOT = _find_root()
ARCHIVE = os.path.join(ROOT, 'archive')
INDEX = os.path.join(ROOT, 'index')

# 全引擎：不替使用者删路径。auto 只在向量路不可用时改走 FTS，其余行为保持原样。
ENGINES = ('auto', 'fts', 'fts-char', 'fts-jieba', 'keyword', 'vector', 'hybrid')
FTS_DB = os.environ.get('PERSONA_FTS_DB') or os.path.join(INDEX, 'fts.db')

SEP_BLOCK = re.compile(r'\n\n---\n\n')
MSG_HEAD = re.compile(r'##\s*(用户|助手)\s*\n(.*)', re.S)
STOP = set('的了是在有和与或就都也还很太我你他它这那什么怎么为什么一个一下可以')


def tokens(q):
    q = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]+', ' ', q)
    out = []
    for seg in q.split():
        if not seg:
            continue
        if re.match(r'^[A-Za-z0-9]', seg):
            out.append(seg.lower())
        else:
            out.extend(seg[i:i + 2] for i in range(max(1, len(seg) - 1)))
    return [t for t in dict.fromkeys(out) if t not in STOP]


_CACHE_VER = 5


def _cache_path():
    """缓存必须按库区分：以前固定叫 _blocks_cache.json，换一个记忆库就会读到上一个库的块。

    _CACHE_VER 进哈希：块的字段结构一变（v2 加了 device/model），旧缓存的 5 元组
    就会让新代码按下标取空值——升版本号等于换文件名，旧缓存自然作废。
    """
    h = hash((os.path.abspath(ROOT), os.path.abspath(ARCHIVE), _CACHE_VER)) & 0xFFFFFFFF
    return os.path.join(tempfile.gettempdir(), 'zsearch_blocks_%08x.json' % h)


_MEMO = {}


def _corpus_sig():
    """语料签名：文件数 + 总字节 + 最新 mtime。

    没有它，缓存一旦写下就永远命中——新抓的会话在检索里根本看不见
    （2026-11-08 实测：14:25 的缓存让 15:0x 的档案查不到）。
    """
    n = total = newest = 0
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in fns:
            if not fn.endswith('.md'):
                continue
            try:
                st = os.stat(os.path.join(dp, fn))
            except OSError:
                continue
            n += 1
            total += st.st_size
            newest = max(newest, int(st.st_mtime))
    return [n, total, newest]


def blocks():
    """产出 (file, day, source, role, text, device, model)。进程内只读一遍，跨进程落盘缓存。

    device / model 来自正文前的元信息头（`- 设备：` / `- 底座：`），老文件没有就是空串。
    缓存按语料签名校验：档案一变就重建，绝不把新记忆挡在门外。
    """
    if 'bl' in _MEMO:
        return _MEMO['bl']
    key = _cache_path()
    sig = _corpus_sig()
    if os.path.exists(key):
        try:
            raw = json.load(io.open(key, encoding='utf-8'))
            if isinstance(raw, dict) and raw.get('sig') == sig and isinstance(raw.get('blocks'), list):
                _MEMO['bl'] = raw['blocks']
                return raw['blocks']
        except Exception:
            pass
    out = []
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in sorted(fns):
            if not fn.endswith('.md'):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, ROOT).replace('\\', '/')
            parts = rel.split('/')
            day = parts[1] if len(parts) > 2 else ''
            src = parts[2] if len(parts) > 3 else ''
            try:
                text = io.open(p, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            head, sep, rest = text.partition('\n---\n')
            # 只有"会话导出"才有那条元信息分隔线；README/DECISIONS 里的 --- 是正文分节，
            # 早先一刀切会让文档前半截（含决策表）整段消失。
            dev, mod = '', ''
            if sep and ('- 会话：' in head or '- 抓取：' in head or '- 网址：' in head):
                body = rest
                m1 = re.search(r'(?m)^-\s*设备：\s*(.+?)\s*$', head)
                m2 = re.search(r'(?m)^-\s*底座：\s*(.+?)\s*$', head)
                dev = m1.group(1) if m1 else ''
                mod = m2.group(1) if m2 else ''
                # 老文件头里没这两行：deepseek网页按来源桶兜底（事实，不是猜）；
                # 电脑会话不兜底——旧记忆可能来自另一台机器，写死本机名就是编造。
                if not dev and not mod and src == 'deepseek网页':
                    dev, mod = '网页', 'deepseek-web'
                # 老导出写 设备/底座，Hermes 导出写 来源/模型——两样都认，缺了留空。
                a_agent, a_model, _ = av.from_head(head, src)
                dev = dev or a_agent
                mod = mod or a_model
            else:
                body = text
            avatar = av.of(dev, mod, src)
            got = 0
            for blk in SEP_BLOCK.split(body):
                # 元信息分隔线后通常留着一个空行，块会以 "\n## 用户" 开头；
                # 直接 match 会因为锚在行首而失败——第一句话就这样从检索里消失了。
                m = MSG_HEAD.match(blk.lstrip('\n'))
                if not m:
                    continue
                role = m.group(1)
                clean = re.sub(r'(?m)^>\s?.*\n?', '', m.group(2)).strip()
                if len(clean) < 8:
                    continue
                out.append([rel, day, src, role, clean, dev, mod, avatar])
                got += 1
            # 非对话型文件（README/SKILL/DECISIONS/报告）：没有"## 用户/助手"结构，
            # 早先会被整份丢掉。这里按段落切成文档块并入语料。
            if got == 0:
                para = [p.strip() for p in re.split(r'\n\s*\n', body) if len(p.strip()) >= 20]
                buf = ''
                for p in para:
                    if len(buf) + len(p) > 900 and buf:
                        out.append([rel, day, src, '文档', buf, dev, mod, avatar])
                        buf = ''
                    buf += ('\n\n' if buf else '') + p
                if buf:
                    out.append([rel, day, src, '文档', buf, dev, mod, avatar])
    json.dump({'sig': sig, 'blocks': out}, io.open(key, 'w', encoding='utf-8'), ensure_ascii=False)
    return out


def idf(bl):
    if 'df' in _MEMO:
        return _MEMO['df']
    df = {}
    for b in bl:
        for w in set(tokens(b[4])):
            df[w] = df.get(w, 0) + 1
    n = max(1, len(bl))
    _MEMO['df'] = {w: math.log(1 + n / (1 + c)) for w, c in df.items()}
    return _MEMO['df']


def norm(s):
    return re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', s)


def keyword_rank(q, bl, df, topn=60, allow=None):
    # 只保留语料里真实出现过的词元：中文滑窗会切出"份保""留几"这类永远匹配不上的碎片，
    # 留着它们只会稀释覆盖率、把正确块压下去。
    ts = [t for t in tokens(q) if t in df]
    if not ts:
        return []
    qn = norm(q)
    scored = []
    for i, b in enumerate(bl):
        if allow is not None and i not in allow:
            continue
        low = b[4].lower()
        hit = [t for t in ts if t in low]
        if not hit:
            continue
        s = sum(df.get(t, 1.0) for t in hit) * (len(hit) / len(ts)) ** 1.5
        # 回声降权：哪条消息原样引用了这句提问（多半是"问题本身"而不是答案）
        if len(qn) >= 8 and qn in norm(b[4]):
            s *= 0.15
        scored.append((s, i))
    scored.sort(reverse=True)
    return [(i, s) for s, i in scored[:topn]]


_VEC = {}


def _load_vectors():
    """向量矩阵与 meta 只读一次；同一进程里反复查询不再重读 231 MB .npy。
    文件被重建（mtime/size 变）时缓存自动作废，不会拿旧矩阵配新 meta。"""
    vpath = os.path.join(INDEX, 'vectors.npy')
    mpath = os.path.join(INDEX, 'meta.jsonl')
    import numpy as np
    st = (os.stat(vpath).st_mtime_ns, os.stat(vpath).st_size,
          os.stat(mpath).st_mtime_ns, os.stat(mpath).st_size)
    if _VEC.get('sig') != st:
        vecs = np.load(vpath)
        vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
        metas = [json.loads(l) for l in io.open(mpath, encoding='utf-8') if l.strip()]
        _VEC.update({'sig': st, 'vecs': vecs, 'metas': metas})
    return _VEC['vecs'], _VEC['metas']


def vector_rank(q, topn=60):
    try:
        import numpy as np
        vecs, metas = _load_vectors()
        sys.path.insert(0, ROOT)
        import vs
        qv = vs.embed([q])[0]
        qv = qv / np.linalg.norm(qv)
        idx = np.argsort(-(vecs @ qv))[:topn]
        return [(metas[i]['file'], metas[i]['role'], metas[i]['text'], float(vecs[i] @ qv)) for i in idx]
    except Exception as e:
        print('[warn] 向量路不可用：%s' % e, file=sys.stderr)
        return []


def rerank_all(q, cands, topn=40):
    """用本地 cross-encoder 重排候选。失败返回 None，调用方保持 RRF 顺序——绝不因为重排挂了就丢结果。"""
    try:
        sys.path.insert(0, ROOT)
        import vs
        if not getattr(vs, 'RERANK_MODEL', ''):
            return None            # 云端没配重排：直接保持 RRF 顺序
        docs = [c['text'][:1200] for c in cands[:topn]]
        res = vs.rerank(q, docs)          # 云端没有 /rerank 就返回 None
        if not res or not isinstance(res, dict) or not res.get('results'):
            return None
        sc = {}
        for r in res['results']:
            if isinstance(r, dict) and 'index' in r and 'relevance_score' in r:
                sc[r['index']] = r['relevance_score']
        if not sc:
            return None            # 格式对不上（别家 rerank）就当没有
        order = sorted(range(len(docs)), key=lambda i: -sc.get(i, -1e9))
        return [cands[i] for i in order] + list(cands[topn:])
    except Exception as e:
        print('[warn] 重排不可用（保持 RRF 顺序）：%s' % e, file=sys.stderr)
        return None


# ---- 三维权重的轻量版：只看内容与角色，不查 memory.db ----
_DECIDE = ('决定', '必须', '不要', '以后都', '记住', '改用', '禁止', '优先', '定下来')
_FLATTER = ('太对了', '绝了', '封神', '完美', '极其', '漂亮', '满分', '犀利', '深刻',
            '牛逼', '降维打击', '精准', '到位', '顶级', '硬核')


def importance(role, text):
    """0..1：用户亲口说的、含决定词的抬高；纯捧场话术压低。"""
    s = 0.0
    if role == '用户':
        s += 0.35
    if any(w in text for w in _DECIDE):
        s += 0.25
    hits = sum(1 for w in _FLATTER if w in text)
    if hits >= 2:
        s -= min(0.45, 0.15 * hits)
    return max(0.0, min(1.0, s))

def recent_boost(out, bl, recent_days):
    """给"最近 N 天"的结果加一点分，然后按分数重排。

    只加分、不减法：老记忆的分数原封不动，所以最坏情况是"顺序微调"，不会是"老的真答案被挤掉"。
    默认关（recent_days=0 / CLI 不给 --recent），要时间优先时再开。
    """
    if not recent_days:
        return out
    days = [b[1] for b in bl if b[1]]
    if not days:
        return out
    try:
        latest = datetime.date.fromisoformat(max(days)[:10])
    except Exception:
        return out
    for it in out:
        try:
            d = datetime.date.fromisoformat((it.get('day') or '')[:10])
        except Exception:
            continue
        age = (latest - d).days
        if 0 <= age <= recent_days:
            it['score'] = it['score'] * (1.0 + 0.3 * (1.0 - age / float(recent_days + 1)))
            it['why'] = (it.get('why', '') + ',recent').strip(',')
    out.sort(key=lambda x: -x['score'])
    return out


_TOOL_RE = re.compile(r'🔧\s*`?(edit|read|write|pwsh|job_output|glob|grep|present|task)`?\s*\{')


def _norm(text):
    return re.sub(r'\s+', ' ', text).strip()


def _is_tool_noise(text):
    """工具调用流水（🔧 edit {...} / 🔧 pwsh {...}）不是正文，默认丢弃。"""
    return bool(_TOOL_RE.search(text))


def _dedupe(rows):
    """同一段正文在多个快照（接续包互相嵌套）里重复出现时只留一条。"""
    seen, out = set(), []
    for it in rows:
        key = hashlib.sha1(_norm(it['text'])[:600].encode('utf-8')).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _fts_db_for(engine):
    if engine == 'fts-char':
        return os.environ.get('PERSONA_FTS_DB_CHAR') or os.path.join(INDEX, 'fts-char.db')
    if engine == 'fts-jieba':
        return os.environ.get('PERSONA_FTS_DB_JIEBA') or os.path.join(INDEX, 'fts-jieba.db')
    return FTS_DB


def fts_rank(q, topn=60, engine='fts', rerank=False):
    """FTS 路。任何失败都只返回 [] 并给一句可执行提示，绝不抛到调用方。"""
    if _fts is None:
        print('[warn] 找不到 persona_fts.py，FTS 路不可用', file=sys.stderr)
        return []
    db = _fts_db_for(engine)
    if not os.path.exists(db) and engine in ('fts-char', 'fts-jieba'):
        # 只建了一个默认库时，单一库也能被 fts-char/fts-jieba 用；分词模式由库内记录决定。
        alt = FTS_DB if os.path.exists(FTS_DB) else ''
        if alt:
            db = alt
    if not os.path.exists(db):
        print('[warn] fts.db 不存在：%s；先运行 python persona_fts.py --build' % db, file=sys.stderr)
        return []
    try:
        return _fts.search(q, db=db, k=topn, rerank=rerank)
    except Exception as e:
        print('[warn] FTS 路不可用（回落关键词）：%s' % e, file=sys.stderr)
        return []


def _bl_lookup(bl):
    d = {}
    for i, b in enumerate(bl):
        d.setdefault((b[0], b[3], b[4]), i)
    return d


def _fts_items(rows, bl=None, allow=None, rerank=False):
    out = []
    lookup = _bl_lookup(bl) if bl is not None else None
    for r in rows:
        item = {'file': r['file'], 'day': r.get('day', ''), 'source': r.get('source', ''),
                'role': r.get('role', ''), 'text': r['text'],
                'device': r.get('device', ''), 'model': r.get('model', ''),
                'avatar': r.get('avatar', ''), 'score': r.get('score', 0.0),
                'why': 'fts+rerank' if rerank else 'fts'}
        if not item['avatar']:
            item['avatar'] = av.of(item['device'], item['model'], item['source'])
        if lookup is not None:
            i = lookup.get((r['file'], r.get('role', ''), r['text']))
            if i is None:
                # meta 与 archive 的切块边界可能差几个字符：退回文件+角色+前 40 字。
                for j, b in enumerate(bl):
                    if b[0] == r['file'] and b[3] == r.get('role', '') and b[4][:40] == r['text'][:40]:
                        i = j
                        break
            if i is not None:
                b = bl[i]
                item.update({'day': b[1], 'source': b[2], 'role': b[3], 'text': b[4],
                             'device': b[5], 'model': b[6], 'avatar': b[7] if len(b) > 7 else ''})
            if allow is not None:
                if i is None or i not in allow:
                    continue
        elif allow is not None:
            continue
        out.append(item)
    return out


def _vector_items(vr, bl, allow):
    out = []
    lookup = _bl_lookup(bl)
    for f, r, t, sc in vr:
        i = lookup.get((f, r, t))
        if i is None and t:
            for j, b in enumerate(bl):
                if b[0] == f and b[3] == r and b[4][:40] == t[:40]:
                    i = j
                    break
        if allow is not None and (i is None or i not in allow):
            continue
        if i is None:
            out.append({'file': f, 'day': '', 'source': '', 'role': r, 'text': t,
                        'device': '', 'model': '', 'avatar': '', 'score': sc, 'why': 'vec'})
        else:
            b = bl[i]
            out.append({'file': b[0], 'day': b[1], 'source': b[2], 'role': b[3], 'text': b[4],
                        'device': b[5], 'model': b[6], 'avatar': b[7] if len(b) > 7 else '',
                        'score': sc, 'why': 'vec'})
    return out


def _kw_items(kr, bl):
    out = []
    for rank, (i, sc) in enumerate(kr):
        b = bl[i]
        out.append({'file': b[0], 'day': b[1], 'source': b[2], 'role': b[3], 'text': b[4],
                    'device': b[5], 'model': b[6], 'avatar': b[7] if len(b) > 7 else '',
                    'score': sc, 'why': 'kw#%d(%.1f)' % (rank + 1, sc)})
    return out


def _fuse_rrf(lists, k):
    """把若干已排序的 item 列表做加权 RRF。lists = [(name, items[, weight]), ...]。

    FTS 权重 1.0、关键词 0.8：标识符查询里 FTS 的 Top-10 足以压过关键词的 Top-1，
    不会被关键词的“persona / search”拆词结果把精确命中挤下去；模糊查询里关键词
    仍能把它独有的候选带进来。
    """
    fused, why, first = {}, {}, {}
    for spec in lists:
        name, items = spec[0], spec[1]
        weight = spec[2] if len(spec) > 2 else 1.0
        for rank, it in enumerate(items):
            key = (it['file'], it['role'], it['text'][:80])
            fused[key] = fused.get(key, 0.0) + weight / (60 + rank)
            why.setdefault(key, []).append('%s#%d' % (name, rank + 1))
            first.setdefault(key, it)
    out = []
    for key, sc in sorted(fused.items(), key=lambda x: -x[1]):
        it = dict(first[key])
        it['score'] = sc
        it['why'] = ','.join(why[key])
        out.append(it)
    return out[:k]


def _finish(out, q, k, weighted, keep_tools, recent_days, bl):
    if not keep_tools:
        out = [it for it in out if not _is_tool_noise(it['text'])]
    out = _dedupe(out)
    if weighted:
        for it in out:
            it['score'] = it['score'] * (0.8 + 0.4 * importance(it['role'], it['text']))
        out.sort(key=lambda x: -x['score'])
    out = recent_boost(out, bl, recent_days)
    return out[:k]


def merged(q, k=100, use_vector=True, use_rerank=True, rerank_top=40, weighted=False,
           keep_tools=False, recent_days=0, device=None, model=None, avatar=None, engine='auto'):
    """全引擎检索：auto / fts / fts-char / fts-jieba / keyword / vector / hybrid。

    返回 [{file, day, source, role, text, device, model, score, why}, ...]
    auto：有向量走原混合路；向量不可用时优先 FTS；FTS 也没有才回落关键词。
    device / model：按分身字段过滤（B11e），None = 不过滤。
    recent_days：最近 N 天优先（B11c），0 = 关。
    """
    engine = (engine or 'auto').lower()
    if engine not in ENGINES:
        engine = 'auto'
    filters = bool(device or model or avatar)

    # 纯 FTS：不需要 blocks()/idf()（冷启动建块要一二十秒，FTS 的意义就是不等它）。
    # device/model/avatar 在建库时已写进 chunks，过滤也在这里做，不回头读 archive。
    if engine in ('fts', 'fts-char', 'fts-jieba'):
        rr = fts_rank(q, topn=max(k * 3, 60), engine=engine, rerank=use_rerank)
        items = _fts_items(rr)
        if filters:
            items = [it for it in items
                     if (not device or it['device'] == device)
                     and (not model or it['model'] == model)
                     and (not avatar or it['avatar'] == avatar)]
        return _finish(items, q, k, weighted, keep_tools, recent_days, [])

    auto_vr = None
    if engine == 'auto':
        auto_vr = vector_rank(q) if use_vector else []
        if not auto_vr:
            fr = fts_rank(q, topn=max(k * 3, 60), rerank=use_rerank)
            if fr:
                items = _fts_items(fr)
                if filters:
                    items = [it for it in items
                             if (not device or it['device'] == device)
                             and (not model or it['model'] == model)
                             and (not avatar or it['avatar'] == avatar)]
                print('[info] 向量路不可用，已切 FTS（%s）'
                      % os.path.basename(_fts_db_for('fts')), file=sys.stderr)
                return _finish(items, q, k, weighted, keep_tools, recent_days, [])
            print('[info] 向量与 FTS 都不可用，回落关键词', file=sys.stderr)
            engine = 'keyword'

    bl = blocks()
    df = idf(bl) if engine != 'vector' else {}
    allow = None
    if filters:
        allow = set(i for i, b in enumerate(bl)
                    if (not device or b[5] == device)
                    and (not model or b[6] == model)
                    and (not avatar or b[7] == avatar))
        if not allow:
            return []

    if engine == 'vector':
        vr = vector_rank(q) if use_vector else []
        return _finish(_vector_items(vr, bl, allow), q, k, weighted, keep_tools, recent_days, bl)

    if engine == 'keyword':
        kr = keyword_rank(q, bl, df, allow=allow)
        return _finish(_kw_items(kr, bl), q, k, weighted, keep_tools, recent_days, bl)

    if engine == 'hybrid':
        kr = keyword_rank(q, bl, df, allow=allow)
        fr = fts_rank(q, topn=max(k, 60), rerank=use_rerank)
        fi = _fts_items(fr)
        if filters:
            fi = [it for it in fi
                  if (not device or it['device'] == device)
                  and (not model or it['model'] == model)
                  and (not avatar or it['avatar'] == avatar)]
        out = _fuse_rrf([('fts', fi, 1.0), ('kw', _kw_items(kr, bl), 0.8)], k * 3)
        return _finish(out, q, k, weighted, keep_tools, recent_days, bl)

    # ---- auto：只有向量路拿到了候选才走到这里（没向量时上面已经切 FTS/关键词）----
    kr = keyword_rank(q, bl, df, allow=allow)
    vr = auto_vr or []

    if allow is not None and vr:
        ok_files = set(bl[i][0] for i in allow)
        vr = [t for t in vr if t[0] in ok_files]

    fused, why = {}, {}
    for rank, (i, s) in enumerate(kr):
        key = ('b', i)
        fused[key] = fused.get(key, 0) + 1.0 / (60 + rank)
        why.setdefault(key, []).append('kw#%d(%.1f)' % (rank + 1, s))
    for rank, (f, r, t, sc) in enumerate(vr):
        key = ('v', f, r, t[:40])
        fused[key] = fused.get(key, 0) + 1.0 / (60 + rank)
        why.setdefault(key, []).append('vec#%d(%.3f)' % (rank + 1, sc))

    out = []
    for key, sc in sorted(fused.items(), key=lambda x: -x[1]):
        if key[0] == 'b':
            f, day, src, role, text, dev, mod, row_avatar = bl[key[1]]
        else:
            _, f, r, t = key
            base = next((b for b in bl if b[0] == f and b[3] == r and b[4][:40] == t), None)
            if base is None:
                continue
            f, day, src, role, text, dev, mod, row_avatar = base
        out.append({'file': f, 'day': day, 'source': src, 'role': role,
                    'text': text, 'device': dev, 'model': mod, 'avatar': row_avatar,
                    'score': sc, 'why': ','.join(why[key])})

    if not keep_tools:
        out = [it for it in out if not _is_tool_noise(it['text'])]
    out = _dedupe(out)
    if filters:
        out = [it for it in out
               if (not device or it.get('device') == device)
               and (not model or it.get('model') == model)
               and (not avatar or it.get('avatar') == avatar)]
    if weighted:
        for it in out:
            it['score'] = it['score'] * (0.8 + 0.4 * importance(it['role'], it['text']))
        out.sort(key=lambda x: -x['score'])
    if use_rerank and len(out) > 1:
        rr = rerank_all(q, out, topn=rerank_top)
        if rr is not None:
            for it in rr:
                it['why'] = (it.get('why', '') + ',rerank').strip(',')
            out = rr
    out = recent_boost(out, bl, recent_days)
    return out[:k]

_LOC = {}


def _locate(rel, snippet, cache=None):
    """命中块在原文里的行号与全文行（溯源用）。找不到返回 (0, lines)。"""
    cache = cache if cache is not None else _LOC
    if rel not in cache:
        p = os.path.join(ROOT, rel.replace('/', os.sep))
        try:
            cache[rel] = io.open(p, encoding='utf-8', errors='replace').read().splitlines()
        except OSError:
            cache[rel] = []
    lines = cache[rel]
    probe = ''
    for ln in (snippet or '').splitlines():
        if ln.strip():
            probe = ln.strip()[:40]
            break
    if probe:
        for i, ln in enumerate(lines):
            if probe in ln:
                return i + 1, lines
    return 0, lines


JOBS = os.path.join(ROOT, 'index', 'jobs')


def _job_path(jid):
    return os.path.join(JOBS, jid + '.json')


def run_job(jid, q, k, deep, **kw):
    try:
        out = merged(q, k=k, rerank_top=(100 if deep else 40), **kw)
        data = {'id': jid, 'q': q, 'done': True, 'at': time.time(), 'items': out}
    except Exception as e:
        data = {'id': jid, 'q': q, 'done': True, 'error': str(e), 'at': time.time()}
    os.makedirs(JOBS, exist_ok=True)
    io.open(_job_path(jid), 'w', encoding='utf-8').write(json.dumps(data, ensure_ascii=False))


def start_async(q, k, deep, recent=0, device=None, model=None, keep_tools=False, engine='auto'):
    """立刻返回 job id；真正的检索在 detached 子进程里跑，结果写 index/jobs/<id>.json。"""
    os.makedirs(JOBS, exist_ok=True)
    jid = time.strftime('%Y%m%d-%H%M%S') + '-%d' % os.getpid()
    cmd = [sys.executable, os.path.abspath(__file__), '--run-job', jid, q, '--k', str(k)]
    if engine and engine != 'auto':
        cmd += ['--engine', engine]
    if deep:
        cmd.append('--deep')
    if recent:
        cmd += ['--recent', str(recent)]
    if device:
        cmd += ['--device', device]
    if model:
        cmd += ['--model', model]
    if keep_tools:
        cmd.append('--keep-tools')
    flags = 0x00000008 if os.name == 'nt' else 0   # DETACHED_PROCESS
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    return jid

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('query', nargs='?')
    ap.add_argument('--k', type=int, default=100)
    ap.add_argument('--no-vector', action='store_true')
    ap.add_argument('--no-rerank', action='store_true')
    ap.add_argument('--engine', choices=ENGINES, default='auto',
                    help='auto=有向量走混合/无向量优先 FTS；fts/fts-char/fts-jieba=纯 FTS；'
                         'keyword=原关键词；vector=纯向量；hybrid=FTS+关键词二路融合')
    ap.add_argument('--deep', action='store_true', help='重排池放到 100：更准但慢约 6 倍')
    ap.add_argument('--keep-tools', action='store_true', dest='keep_tools',
                    help='保留工具调用流水（🔧 edit {...}）；默认过滤')
    ap.add_argument('--weighted', action='store_true', help='叠加三维权重（用户/决定词抬高，捧场话术压低）')
    ap.add_argument('--recent', type=int, default=0, metavar='N',
                    help='最近 N 天优先：只加分、不减分，默认 0=关')
    ap.add_argument('--device', default=None, help='只保留该设备来源的块（如 网页 / 主机名）')
    ap.add_argument('--model', default=None, help='只保留该底座来源的块（如 deepseek-web）')
    ap.add_argument('--avatar', default=None,
                    help='只保留该分身的块（如 hermes-deepseek-v4-flash / 网页-deepseek-web）')
    ap.add_argument('--show-source', action='store_true', dest='show_source',
                    help='每条命中带 文件:行号（三层溯源第二层）')
    ap.add_argument('--context', type=int, default=0, metavar='N',
                    help='连原文前后 N 行一起打（隐含 --show-source）')
    ap.add_argument('--async', dest='async_', action='store_true',
                    help='立刻打印 job id，检索在 detached 子进程里跑')
    ap.add_argument('--run-job', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--result', default=None, help='取某个 async 任务的结果（没跑完则打印 RUNNING）')
    a = ap.parse_args()

    if a.context:
        a.show_source = True
    kw = dict(recent_days=a.recent, device=a.device, model=a.model, avatar=a.avatar,
              engine=a.engine)

    if a.run_job:
        run_job(a.run_job, a.query, a.k, a.deep, **kw)
        return 0

    if a.result:
        p = _job_path(a.result)
        if not os.path.exists(p):
            print('RUNNING %s' % a.result)
            return 0
        d = json.load(io.open(p, encoding='utf-8'))
        if d.get('error'):
            print('ERROR %s' % d['error'])
            return 1
        for it in d.get('items', []):
            print('--- %.4f  [%s] %s  %s' % (it['score'], it['role'],
                                             os.path.basename(it['file'])[:46], it['why']))
            print(it['text'][:260].replace('\n', ' '))
        print('\n[%d 条]' % len(d.get('items', [])))
        return 0

    if a.async_:
        if not a.query:
            ap.error('--async 需要查询词')
        print(start_async(a.query, a.k, a.deep, a.recent, a.device, a.model,
                          a.keep_tools, a.engine))
        return 0

    t0 = time.time()

    bl = blocks()
    out = merged(a.query, k=a.k, use_vector=not a.no_vector, use_rerank=not a.no_rerank,
                 rerank_top=(100 if a.deep else 40), weighted=a.weighted,
                 keep_tools=a.keep_tools, **kw)
    if not out and not (a.device or a.model or a.avatar) and a.engine in ('auto', 'keyword'):
        recent = sorted(bl, key=lambda b: b[1], reverse=True)[:a.k]
        out = [{'file': b[0], 'role': b[3], 'text': b[4], 'score': 0.0, 'why': 'timeline-fallback',
                'avatar': b[7] if len(b) > 7 else ''} for b in recent]
    elif not out and (a.device or a.model or a.avatar):
        print('（该 设备/底座/分身 下没有命中；去掉 --device/--model/--avatar 再试）', file=sys.stderr)

    cache = {}
    for it in out:
        print('--- %.4f  [%s] %-28s %s  %s' % (it['score'], it['role'],
              (it.get('avatar') or 'unknown')[:28],
              os.path.basename(it['file'])[:40], it['why']))
        print(it['text'][:260].replace('\n', ' '))
        if a.show_source:
            ln, lines = _locate(it['file'], it['text'], cache)
            print('    来源：%s:%d  （%s / %s）' % (it['file'], ln,
                  it.get('avatar') or 'unknown', it.get('source') or ''))
            if a.context and ln:
                lo = max(0, ln - 1 - a.context)
                hi = min(len(lines), ln + a.context)
                for k in range(lo, hi):
                    mark = '>>' if k == ln - 1 else '  '
                    print('%s %5d | %s' % (mark, k + 1, lines[k].rstrip()[:220]))
    print('\n[%d 条 / %.2fs]' % (len(out), time.time() - t0))


if __name__ == '__main__':
    main()
