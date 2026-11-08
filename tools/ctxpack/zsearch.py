# -*- coding: utf-8 -*-
"""zsearch.py — Zero-Miss 混合检索原型

两条路并行，谁也别想漏：
  A) 关键词路：IDF 加权的字面命中（专有名词、代号、数字靠它）
  B) 向量路：bge-m3 语义近邻（换个说法也能找到）
融合：Reciprocal Rank Fusion，取 Top-K（默认 30，宁可塞垃圾不漏线索）
兜底：两路都空时，按时间倒序拉当天最近 N 块原文

用法：
  python zsearch.py "防止舔狗化的惩罚规则" --k 30
  python zsearch.py "960 显卡" --k 10 --no-vector
"""
import argparse
import io
import json
import math
import os
import re
import sys
import subprocess
import tempfile
import time

import numpy as np

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


def _cache_path():
    """缓存必须按库区分：以前固定叫 _blocks_cache.json，换一个记忆库就会读到上一个库的块。"""
    h = hash((os.path.abspath(ROOT), os.path.abspath(ARCHIVE))) & 0xFFFFFFFF
    return os.path.join(tempfile.gettempdir(), 'zsearch_blocks_%08x.json' % h)


_MEMO = {}


def blocks():
    """产出 (file, day, source, role, text)。进程内只读一遍，跨进程落盘缓存。"""
    if 'bl' in _MEMO:
        return _MEMO['bl']
    key = _cache_path()
    if os.path.exists(key):
        try:
            raw = json.load(io.open(key, encoding='utf-8'))
            _MEMO['bl'] = raw
            return raw
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
            if sep and ('- 会话：' in head or '- 抓取：' in head or '- 网址：' in head):
                body = rest
            else:
                body = text
            got = 0
            for blk in SEP_BLOCK.split(body):
                m = MSG_HEAD.match(blk)
                if not m:
                    continue
                role = m.group(1)
                clean = re.sub(r'(?m)^>\s?.*\n?', '', m.group(2)).strip()
                if len(clean) < 8:
                    continue
                out.append([rel, day, src, role, clean])
                got += 1
            # 非对话型文件（README/SKILL/DECISIONS/报告）：没有"## 用户/助手"结构，
            # 早先会被整份丢掉。这里按段落切成文档块并入语料。
            if got == 0:
                para = [p.strip() for p in re.split(r'\n\s*\n', body) if len(p.strip()) >= 20]
                buf = ''
                for p in para:
                    if len(buf) + len(p) > 900 and buf:
                        out.append([rel, day, src, '文档', buf])
                        buf = ''
                    buf += ('\n\n' if buf else '') + p
                if buf:
                    out.append([rel, day, src, '文档', buf])
    json.dump(out, io.open(key, 'w', encoding='utf-8'), ensure_ascii=False)
    return out


def idf(bl):
    if 'df' in _MEMO:
        return _MEMO['df']
    df = {}
    for _, _, _, _, t in bl:
        for w in set(tokens(t)):
            df[w] = df.get(w, 0) + 1
    n = max(1, len(bl))
    _MEMO['df'] = {w: math.log(1 + n / (1 + c)) for w, c in df.items()}
    return _MEMO['df']


def norm(s):
    return re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', s)


def keyword_rank(q, bl, df, topn=60):
    # 只保留语料里真实出现过的词元：中文滑窗会切出"份保""留几"这类永远匹配不上的碎片，
    # 留着它们只会稀释覆盖率、把正确块压下去。
    ts = [t for t in tokens(q) if t in df]
    if not ts:
        return []
    qn = norm(q)
    scored = []
    for i, b in enumerate(bl):
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
        docs = [c['text'][:1200] for c in cands[:topn]]
        res = vs.post('/rerank', {'model': 'bge-reranker-v2-m3', 'query': q, 'documents': docs}, timeout=180)
        sc = {r['index']: r['relevance_score'] for r in res['results']}
        order = sorted(range(len(docs)), key=lambda i: -sc.get(i, -1e9))
        return [cands[i] for i in order] + list(cands[topn:])
    except Exception as e:
        print('[warn] 重排不可用（保持 RRF 顺序）：%s' % e, file=sys.stderr)
        return None


def merged(q, k=100, use_vector=True, use_rerank=True, rerank_top=40):
    """两路合并后的结果，给 ctxpack.py --hybrid 等调用方用。

    返回 [{file, day, source, role, text, score, why}, ...]
    """
    bl = blocks()
    df = idf(bl)
    kr = keyword_rank(q, bl, df)
    vr = vector_rank(q) if use_vector else []

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
    for key, sc in sorted(fused.items(), key=lambda x: -x[1])[:k]:
        if key[0] == 'b':
            f, day, src, role, text = bl[key[1]]
        else:
            _, f, r, t = key
            base = next((b for b in bl if b[0] == f and b[3] == r and b[4][:40] == t), None)
            if base is None:
                continue
            f, day, src, role, text = base
        out.append({'file': f, 'day': day, 'source': src, 'role': role,
                    'text': text, 'score': sc, 'why': ','.join(why[key])})

    if use_rerank and len(out) > 1:
        rr = rerank_all(q, out, topn=rerank_top)
        if rr is not None:
            for it in rr:
                it['why'] = (it.get('why', '') + ',rerank').strip(',')
            out = rr
    return out[:k]

JOBS = os.path.join(ROOT, 'index', 'jobs')


def _job_path(jid):
    return os.path.join(JOBS, jid + '.json')


def run_job(jid, q, k, deep):
    try:
        out = merged(q, k=k, rerank_top=(100 if deep else 40))
        data = {'id': jid, 'q': q, 'done': True, 'at': time.time(), 'items': out}
    except Exception as e:
        data = {'id': jid, 'q': q, 'done': True, 'error': str(e), 'at': time.time()}
    os.makedirs(JOBS, exist_ok=True)
    io.open(_job_path(jid), 'w', encoding='utf-8').write(json.dumps(data, ensure_ascii=False))


def start_async(q, k, deep):
    """立刻返回 job id；真正的检索在 detached 子进程里跑，结果写 index/jobs/<id>.json。"""
    os.makedirs(JOBS, exist_ok=True)
    jid = time.strftime('%Y%m%d-%H%M%S') + '-%d' % os.getpid()
    cmd = [sys.executable, os.path.abspath(__file__), '--run-job', jid, q, '--k', str(k)]
    if deep:
        cmd.append('--deep')
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
    ap.add_argument('--async', dest='async_', action='store_true',
                    help='立刻打印 job id，检索在 detached 子进程里跑')
    ap.add_argument('--run-job', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--result', default=None, help='取某个 async 任务的结果（没跑完则打印 RUNNING）')
    a = ap.parse_args()

    if a.run_job:
        run_job(a.run_job, a.query, a.k, a.deep)
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
        print(start_async(a.query, a.k, a.deep))
        return 0

    t0 = time.time()

    bl = blocks()
    out = merged(a.query, k=a.k, use_vector=not a.no_vector, use_rerank=not a.no_rerank,
                 rerank_top=(100 if a.deep else 40))
    if not out:
        recent = sorted(bl, key=lambda b: b[1], reverse=True)[:a.k]
        out = [{'file': b[0], 'role': b[3], 'text': b[4], 'score': 0.0, 'why': 'timeline-fallback'} for b in recent]

    for it in out:
        print('--- %.4f  [%s] %s  %s' % (it['score'], it['role'], os.path.basename(it['file'])[:46], it['why']))
        print(it['text'][:260].replace('\n', ' '))
    print('\n[%d 条 / %.2fs]' % (len(out), time.time() - t0))


if __name__ == '__main__':
    main()
