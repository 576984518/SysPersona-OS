# -*- coding: utf-8 -*-
"""persona_search.py — Zero-Miss 混合检索原型

两条路并行，谁也别想漏：
  A) 关键词路：IDF 加权的字面命中（专有名词、代号、数字靠它）
  B) 向量路：bge-m3 语义近邻（换个说法也能找到）
融合：Reciprocal Rank Fusion，取 Top-K（默认 30，宁可塞垃圾不漏线索）
兜底：两路都空时，按时间倒序拉当天最近 N 块原文

用法：
  python persona_search.py "防止舔狗化的惩罚规则" --k 30
  python persona_search.py "960 显卡" --k 10 --no-vector
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
import tempfile
import time

import numpy as np

# 输出编码：GBK 控制台遇到 emoji（🔧）会抛 UnicodeEncodeError，统一转 UTF-8。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

def _find_root():
    """记忆库根目录：优先环境变量 SYSPERSONA_ROOT，否则向上找含 archive/ 的目录。"""
    env = os.environ.get('SYSPERSONA_ROOT')
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(cand, 'archive')):
            return cand
    return here


import persona_avatar as av

ROOT = _find_root()
ARCHIVE = os.path.join(ROOT, 'archive')
INDEX = os.path.join(ROOT, 'index')

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
                # 老文件头里没这两行：网页对话按来源桶兜底（事实，不是猜）；
                # 电脑会话不兜底——旧记忆可能来自另一台机器，写死本机名就是编造。
                if not dev and not mod and src == '网页对话':
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


def vector_rank(q, topn=60):
    try:
        vpath = os.path.join(INDEX, 'vectors.npy')
        mpath = os.path.join(INDEX, 'meta.jsonl')
        vecs = np.load(vpath)
        vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
        metas = [json.loads(l) for l in io.open(mpath, encoding='utf-8') if l.strip()]
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


def merged(q, k=100, use_vector=True, use_rerank=True, rerank_top=40, weighted=False,
           keep_tools=False, recent_days=0, device=None, model=None, avatar=None):
    """两路合并后的结果，给 persona_pack.py --hybrid 等调用方用。

    返回 [{file, day, source, role, text, device, model, score, why}, ...]
    device / model：按分身字段过滤（B11e），None = 不过滤。
    recent_days：最近 N 天优先（B11c），0 = 关。
    """
    bl = blocks()
    df = idf(bl)

    # 分身/设备/底座过滤必须在**取候选之前**做：先在 60 名里挑再过滤，
    # 会出现“206 条 dsh 命中，却因为前 60 名全是网页而报 0 条”。实测踩过。
    allow = None
    if device or model or avatar:
        allow = set(i for i, b in enumerate(bl)
                    if (not device or b[5] == device)
                    and (not model or b[6] == model)
                    and (not avatar or b[7] == avatar))
        if not allow:
            return []
    kr = keyword_rank(q, bl, df, allow=allow)
    vr = vector_rank(q) if use_vector else []
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
    # 不在这里截断：先按 device/model 过滤再取前 k，否则过滤只能从 3 个候选里挑，
    # 明明有 50 条网页命中，却因为前 3 名都是电脑会话而显得"网页里没有"。
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

    # 先剔噪声与重复，再重排：省算力，也别让副本把 Top-K 稀释。
    if not keep_tools:
        out = [it for it in out if not _is_tool_noise(it['text'])]
    out = _dedupe(out)

    # 分身字段过滤：放在重排之前，省得为一个注定被滤掉的块去调 cross-encoder。
    if device or model or avatar:
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


def start_async(q, k, deep, recent=0, device=None, model=None, keep_tools=False):
    """立刻返回 job id；真正的检索在 detached 子进程里跑，结果写 index/jobs/<id>.json。"""
    os.makedirs(JOBS, exist_ok=True)
    jid = time.strftime('%Y%m%d-%H%M%S') + '-%d' % os.getpid()
    cmd = [sys.executable, os.path.abspath(__file__), '--run-job', jid, q, '--k', str(k)]
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
    kw = dict(recent_days=a.recent, device=a.device, model=a.model, avatar=a.avatar)

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
        print(start_async(a.query, a.k, a.deep, a.recent, a.device, a.model, a.keep_tools))
        return 0

    t0 = time.time()

    bl = blocks()
    out = merged(a.query, k=a.k, use_vector=not a.no_vector, use_rerank=not a.no_rerank,
                 rerank_top=(100 if a.deep else 40), weighted=a.weighted,
                 keep_tools=a.keep_tools, **kw)
    if not out and not (a.device or a.model or a.avatar):
        recent = sorted(bl, key=lambda b: b[1], reverse=True)[:a.k]
        out = [{'file': b[0], 'role': b[3], 'text': b[4], 'score': 0.0, 'why': 'timeline-fallback',
                'avatar': b[7] if len(b) > 7 else ''} for b in recent]
    elif not out:
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
