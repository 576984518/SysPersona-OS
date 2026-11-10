# -*- coding: utf-8 -*-
"""eval.py — 检索召回自测

给一份题库，分别跑几条路，报 recall@K：

  关键词（IDF + 回声降权） / 向量（bge-m3） / 合并（RRF）
  加 --merged 再测一遍真正的合并路，加 --recent N 同时报"最近 N 天优先"叠加前后的差异

题库格式（JSONL，一行一题）：
  {"q": "为什么一个人格要配 500GB 存储", "want": ["500GB", "500G"]}
  want 是"答案里必须出现的字面"——命中判定就看它是否出现在返回片段里。

用法：
  python eval.py                      # 内置 20 题（跨文件）
  python eval.py --set my.jsonl --k 50
  python eval.py --no-vector          # 只测关键词路
  python eval.py --merged --no-vector # 只对比合并路：开/关"最近 N 天优先"

为什么要"去回声"：问题本身常常就躺在语料里，检索会把"你刚问的那句"排到答案前面。
所以默认逐行剔除引用提问的行，再判命中——否则测的是"能不能找到自己刚说的话"。
"""
import argparse
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import persona_search  # noqa: E402

BUILTIN = [
    ("把笔记本的睡眠状态从 S3 改成 S4 是改哪个 ACPI 模块", ["C118F500"]),
    ("那个负责复位芯片的 UEFI 模块 GUID 是多少", ["F5883FC5"]),
    ("老显卡能用的是哪个 CUDA 版本的运行库", ["12.4"]),
    ("MinGit 是从哪个镜像站下的", ["npmmirror"]),
    ("网页版消息块的选择器类名是什么", ["ds-message"]),
    ("人格文件加密用的算法叫什么", ["Argon2id"]),
    ("向量检索之后用来重排的模型是哪个", ["bge-reranker"]),
    ("跨端同步为什么不采用后台静默浏览器", ["封号"]),
    ("每次备份打包多大", ["13.6"]),
    ("换成 12.4 之后 960 跑嵌入快了多少倍", ["3.6"]),
    ("摘要压缩比是多少", ["0.26"]),
    ("摘要里事实词元的覆盖率是多少", ["12.5"]),
    ("语义检索的召回率测出来多少", ["46.7"]),
    ("判重用的尾部指纹存在哪个文件里", ["_tail.json"]),
    ("抓全量时视口撑到多高", ["30000"]),
    ("网页首屏大约挂载多少条消息", ["213"]),
    ("当前打开的会话最多重抓几轮直到稳定", ["4 轮"]),
    ("备份保留几份", ["8 份"]),
    ("永存那一档的桶名叫什么", ["L1"]),
    ("冲突的记忆写成什么 memory_type", ["persona_conflict"]),
]


def norm(s):
    return re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', s)


def hit(snippets, want, q):
    qn = norm(q)
    kept = []
    for s in snippets:
        lines = [l for l in s.split('\n') if not (len(qn) >= 8 and qn in norm(l))]
        if lines:
            kept.append('\n'.join(lines))
    blob = '\n'.join(kept).lower()
    return any(w.lower() in blob for w in want)


def load(path):
    out = []
    for line in io.open(path, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        out.append((o['q'], o['want']))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--set', default=None, help='JSONL 题库；不给就用内置 20 题')
    ap.add_argument('--k', type=int, default=100)
    ap.add_argument('--no-vector', action='store_true')
    ap.add_argument('--merged', action='store_true', help='追加测合并路（persona_search.merged）')
    ap.add_argument('--recent', type=int, default=0,
                    help='合并路叠加"最近 N 天优先"，同时报叠加前后；0=不测')
    ap.add_argument('--root', default=os.environ.get('ZSEARCH_ROOT', persona_search.ROOT))
    a = ap.parse_args()

    persona_search.ROOT = a.root
    persona_search.ARCHIVE = os.path.join(a.root, 'archive')
    persona_search.INDEX = os.path.join(a.root, 'index')

    qs = load(a.set) if a.set else BUILTIN
    bl = persona_search.blocks()
    df = persona_search.idf(bl)

    routes = ['kw', 'vec', 'union']
    if a.merged:
        routes += ['merged', 'merged_recent']
    tally = {r: 0 for r in routes}
    labels = {'kw': '关键词', 'vec': '向量', 'union': '合并(并集)',
              'merged': '合并路', 'merged_recent': '合并路+近%d天' % a.recent}

    t0 = time.time()
    for q, want in qs:
        kr = persona_search.keyword_rank(q, bl, df, topn=a.k)
        ktext = [bl[i][4] for i, _ in kr]

        vr = [] if a.no_vector else persona_search.vector_rank(q, topn=a.k)
        vtext = [t for _, _, t, _ in vr]

        got = {'kw': ktext, 'vec': vtext, 'union': ktext + vtext}
        if a.merged:
            try:
                m0 = persona_search.merged(q, k=a.k, use_vector=not a.no_vector, use_rerank=False)
                got['merged'] = [r['text'] for r in m0]
                if a.recent:
                    m1 = persona_search.merged(q, k=a.k, use_vector=not a.no_vector, use_rerank=False,
                                        recent_days=a.recent)
                    got['merged_recent'] = [r['text'] for r in m1]
                else:
                    got['merged_recent'] = got['merged']
            except Exception as e:
                print('[warn] 合并路不可用：%s' % e, file=sys.stderr)
                got['merged'] = got['merged_recent'] = []

        flags = []
        for r in routes:
            ok = hit(got.get(r, []), want, q)
            tally[r] += ok
            flags.append(('✓' if ok else '✗') + r)
        print('%-46s %s' % (q[:44], ' '.join(flags)))

    n = len(qs)
    print('-' * 76)
    parts = []
    for r in routes:
        if r == 'vec' and a.no_vector:
            parts.append('%s -/%d' % (labels[r], n))
        else:
            parts.append('%s %d/%d' % (labels[r], tally[r], n))
    print('   '.join(parts) + '   %.0fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
