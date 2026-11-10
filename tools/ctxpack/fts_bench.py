# -*- coding: utf-8 -*-
r"""fts_bench.py — 五引擎对照台（只用于实测，不是运行时依赖）

引擎：
  fts-char   SQLite FTS5 + 单字/双字切分（纯标准库）
  fts-jieba  SQLite FTS5 + jieba 切分（没有 jieba 时由临时桩跑，报告里会标注）
  keyword    现有 persona_search.py 的 IDF 关键词路
  vector     bge-m3 向量路（本机不可用时整列标 UNAVAILABLE）
  hybrid     FTS + 关键词 RRF 二路融合

每类 5 条查询，k=20。输出 TSV（每条查询的 Top3 + 命中数 + 延迟），
并打印按查询类型的汇总矩阵。

用法：
  python fts_bench.py --out result.tsv
  python fts_bench.py --engines fts-char,keyword,vector,hybrid
"""
import argparse
import ctypes
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import persona_search as ps
import persona_fts as pf

QUERIES = [
    # ---- 精确标识符 ----
    ('精确标识符', 'persona_search', ['persona_search']),
    ('精确标识符', '接口 B75', ['B75']),
    ('精确标识符', 'B11e', ['B11e']),
    ('精确标识符', '_blocks_cache.json', ['_blocks_cache']),
    ('精确标识符', 'persona_index.py', ['persona_index']),
    # ---- 中文短语 ----
    ('中文短语', '上下文压缩', ['上下文压缩']),
    ('中文短语', '记忆分身', ['记忆分身']),
    ('中文短语', '回声降权', ['回声']),
    ('中文短语', '交接包', ['交接包']),
    ('中文短语', '内容去重', ['去重']),
    # ---- 混合中英 ----
    ('混合中英', 'DSH 水位 350K', ['350K']),
    ('混合中英', 'bge-m3 向量', ['bge-m3']),
    ('混合中英', 'Hermes 人格', ['Hermes']),
    ('混合中英', 'Persona 记忆库', ['记忆库']),
    ('混合中英', 'SQLite 索引', ['SQLite']),
    # ---- 模糊描述 ----
    ('模糊描述', '那个不换窗口的方案', ['换窗口', '接续', '交接包']),
    ('模糊描述', '防止舔狗化的惩罚规则', ['舔狗', '惩罚']),
    ('模糊描述', '怎么让新会话接上', ['接续', '交接']),
    ('模糊描述', '记忆老丢怎么办', ['备份', '丢']),
    ('模糊描述', '模型挂了还能查吗', ['降级', '关键词', '回落']),
    # ---- 长句/口语 ----
    ('长句口语', '我上次说那个检索慢的问题最后怎么处理的', ['缓存', '慢']),
    ('长句口语', '为什么每次换窗口都要重新讲一遍', ['接续', '交接']),
    ('长句口语', '那个记忆库到底能不能不依赖模型', ['不依赖', '关键词', '降级']),
    ('长句口语', '帮我找一下之前说过的自动抓取方案', ['抓取', 'webwatch']),
    ('长句口语', '上下文快满了应该怎么办', ['接续包', 'ctxguard', '水位']),
    # ---- 数字/型号 ----
    ('数字型号', '350K', ['350K']),
    ('数字型号', '960 显卡', ['960']),
    ('数字型号', '500GB', ['500GB']),
    ('数字型号', 'v3.0.0', ['v3.0.0']),
    ('数字型号', 'B75', ['B75']),
    # ---- 代码片段 ----
    ('代码片段', '--no-vector', ['--no-vector']),
    ('代码片段', 'vectors.npy', ['vectors.npy']),
    ('代码片段', 'python persona_index.py', ['persona_index']),
    ('代码片段', 'sha256', ['sha256']),
    ('代码片段', 'def merged(', ['def merged']),
    # ---- 时间性提问 ----
    ('时间性', '11月7日的会话', ['2026-11-07']),
    ('时间性', '昨天改了什么', ['2026-11-09']),
    ('时间性', '最近一次发版', ['v3.0.0', '发版']),
    ('时间性', '上周说过什么', ['2026-11-0']),
    ('时间性', '2026-11-10 的续接包', ['2026-11-10', '续接']),
]

# 「部分相关」的判据单独写：不能拿查询里的任意双字去凑——那会让“persona_search”
# 只要出现 search 就算部分相关，矩阵会虚高。标识符/数字/代码/时间一律严格。
SOFT = {
    '上下文压缩': ['上下文', '压缩'],
    '记忆分身': ['记忆', '分身'],
    '回声降权': ['回声', '降权'],
    '交接包': ['交接'],
    '内容去重': ['去重'],
    'DSH 水位 350K': ['水位', '350'],
    'bge-m3 向量': ['向量', 'bge'],
    'Hermes 人格': ['人格', 'hermes'],
    'Persona 记忆库': ['记忆库', 'persona'],
    'SQLite 索引': ['索引', 'sqlite'],
    '那个不换窗口的方案': ['换窗口', '接续', '交接', '窗口'],
    '防止舔狗化的惩罚规则': ['舔狗', '惩罚', '防止'],
    '怎么让新会话接上': ['接续', '交接', '新会话'],
    '记忆老丢怎么办': ['备份', '丢', '记忆'],
    '模型挂了还能查吗': ['降级', '关键词', '回落', '模型'],
    '我上次说那个检索慢的问题最后怎么处理的': ['检索', '慢', '缓存'],
    '为什么每次换窗口都要重新讲一遍': ['换窗口', '接续', '交接'],
    '那个记忆库到底能不能不依赖模型': ['不依赖', '关键词', '降级', '模型'],
    '帮我找一下之前说过的自动抓取方案': ['抓取', 'webwatch', '自动'],
    '上下文快满了应该怎么办': ['接续包', 'ctxguard', '水位', '压缩'],
}

ENGINE_LABEL = {
    'fts-char': 'fts-char',
    'fts-jieba': 'fts-jieba',
    'keyword': 'keyword',
    'vector': 'vector',
    'hybrid': 'hybrid',
}


def _rss_mb():
    try:
        class PMC(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
        fn.restype = ctypes.c_int
        fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.WorkingSetSize / 1048576.0
    except Exception:
        return 0.0


def _label(q, item, expect):
    blob = (item.get('file', '') + '\n' + item.get('text', '')).lower()
    if any(e.lower() in blob for e in expect):
        return '相关'
    soft = SOFT.get(q, [])
    if any(s.lower() in item.get('text', '').lower() for s in soft):
        return '部分'
    return '不相关'


def _line(item):
    return '%s:%d' % (item.get('file', ''), pf.locate(ps.ROOT, item.get('file', ''),
                                                       item.get('text', '')))


def run_one(engine, q, k, rerank):
    if engine == 'fts-char':
        return ps.merged(q, k=k, engine='fts-char', use_vector=False, use_rerank=rerank)
    if engine == 'fts-jieba':
        return ps.merged(q, k=k, engine='fts-jieba', use_vector=False, use_rerank=rerank)
    if engine == 'keyword':
        return ps.merged(q, k=k, engine='keyword', use_vector=False, use_rerank=False)
    if engine == 'vector':
        return ps.merged(q, k=k, engine='vector', use_vector=True, use_rerank=False)
    if engine == 'hybrid':
        return ps.merged(q, k=k, engine='hybrid', use_vector=False, use_rerank=rerank)
    raise ValueError(engine)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='')
    ap.add_argument('--engines', default='fts-char,fts-jieba,keyword,vector,hybrid')
    ap.add_argument('--k', type=int, default=20)
    ap.add_argument('--no-rerank', action='store_true')
    ap.add_argument('--perf', action='store_true',
                    help='只跑 100 次查询（20 条 × 5 轮）测 p50/p95，不出相关性矩阵')
    a = ap.parse_args()
    engines = [x.strip() for x in a.engines.split(',') if x.strip()]
    rerank = not a.no_rerank

    # 每引擎预热一次：冷启动的 blocks()/idf/npy 加载只影响第一次查询，
    # 不预热就会把 13 秒的首次 idf 算进 p50，看起来像关键词路一直很慢。
    print('[warmup] 建 blocks/idf 与各引擎预热……', file=sys.stderr)
    t0 = time.time()
    ps.blocks()
    print('[warmup] blocks=%d，%.1fs，RSS %.0f MiB'
          % (len(ps.blocks()), time.time() - t0, _rss_mb()), file=sys.stderr)
    for engine in engines:
        t0 = time.perf_counter()
        try:
            run_one(engine, '预热查询', 3, rerank)
            print('[warmup] %-9s %.1fs' % (engine, time.perf_counter() - t0),
                  file=sys.stderr)
        except Exception as e:
            print('[warmup] %-9s FAILED %s' % (engine, e), file=sys.stderr)

    if a.perf:
        print('\n引擎\t查询数\tp50 ms\tp95 ms\tmin\tmax\t平均命中')
        for engine in engines:
            lat, hits = [], []
            for rep in range(5):
                for q in pf.STATS_QUERIES:
                    t0 = time.perf_counter()
                    try:
                        out = run_one(engine, q, a.k, rerank)
                        lat.append((time.perf_counter() - t0) * 1000.0)
                        hits.append(len(out))
                    except Exception:
                        continue
            if not lat:
                print('%s\t0\t-\t-\t-\t-\t-' % engine)
                continue
            lat.sort()
            print('%s\t%d\t%.1f\t%.1f\t%.1f\t%.1f\t%.1f' % (
                engine, len(lat), statistics.median(lat),
                lat[min(len(lat) - 1, int(round((len(lat) - 1) * 0.95)))],
                lat[0], lat[-1], sum(hits) / float(max(1, len(hits)))))
        return

    rows = []
    for etype, q, expect in QUERIES:
        for engine in engines:
            try:
                t0 = time.perf_counter()
                out = run_one(engine, q, a.k, rerank)
                dt = (time.perf_counter() - t0) * 1000.0
            except Exception as e:
                rows.append({'type': etype, 'q': q, 'engine': engine, 'hits': 0,
                             'ms': 0.0, 'rank': 0, 'loc': '', 'score': 0.0,
                             'label': 'ERROR:%s' % e, 'text': ''})
                continue
            for rank in range(1, 4):
                if rank <= len(out):
                    it = out[rank - 1]
                    rows.append({'type': etype, 'q': q, 'engine': engine,
                                 'hits': len(out), 'ms': dt, 'rank': rank,
                                 'loc': _line(it), 'score': it.get('score', 0.0),
                                 'label': _label(q, it, expect),
                                 'text': ' '.join(it.get('text', '').split())[:120]})
                else:
                    rows.append({'type': etype, 'q': q, 'engine': engine,
                                 'hits': len(out), 'ms': dt, 'rank': rank,
                                 'loc': '', 'score': 0.0, 'label': '未命中', 'text': ''})
        print('[done] %s %s' % (etype, q), file=sys.stderr)

    if a.out:
        with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
            f.write('type\tquery\tengine\thits\tms\trank\tloc\tscore\tlabel\ttext\n')
            for r in rows:
                f.write('\t'.join(str(r[k]) for k in
                                  ('type', 'q', 'engine', 'hits', 'ms', 'rank', 'loc',
                                   'score', 'label', 'text')) + '\n')
        print('[tsv] %s' % a.out, file=sys.stderr)

    # 汇总矩阵
    print('\n类型\t引擎\tP@3(相关/部分/不相关/未命中)\t平均命中\tp50 ms\tp95 ms')
    types = list(dict.fromkeys(x[0] for x in QUERIES))
    for etype in types:
        for engine in engines:
            cell = [r for r in rows if r['type'] == etype and r['engine'] == engine
                    and r['rank'] in (1, 2, 3)]
            if not cell:
                continue
            labels = [r['label'] for r in cell]
            cnt = {x: labels.count(x) for x in ('相关', '部分', '不相关', '未命中')}
            qms = {}
            for r in cell:
                qms[r['q']] = r['ms']
            lat = sorted(qms.values())
            hits = [r['hits'] for r in cell if r['rank'] == 1]
            p95 = lat[min(len(lat) - 1, int(round((len(lat) - 1) * 0.95)))] if lat else 0
            print('%s\t%s\t%d/%d/%d/%d\t%.1f\t%.1f\t%.1f' % (
                etype, engine, cnt['相关'], cnt['部分'], cnt['不相关'], cnt['未命中'],
                sum(hits) / float(max(1, len(hits))),
                statistics.median(lat) if lat else 0, p95))

    print('\n成本：')
    for name, path in (('fts-char', os.environ.get('PERSONA_FTS_DB_CHAR')
                        or os.path.join(ps.INDEX, 'fts-char.db')),
                       ('fts-jieba', os.environ.get('PERSONA_FTS_DB_JIEBA')
                        or os.path.join(ps.INDEX, 'fts-jieba.db')),
                       ('meta.jsonl', os.path.join(ps.INDEX, 'meta.jsonl')),
                       ('vectors.npy', os.path.join(ps.INDEX, 'vectors.npy'))):
        try:
            print('%s\t%d bytes\t%.1f MiB' % (name, os.path.getsize(path),
                                              os.path.getsize(path) / 1048576.0))
        except OSError:
            print('%s\t（不存在）' % name)
    print('RSS\t%.1f MiB' % _rss_mb())


if __name__ == '__main__':
    main()
