# -*- coding: utf-8 -*-
"""index_inc.py — 增量向量索引（与实时抓取共存）

和 vs.py build 的差别：
  1. 按文件 sha256 记账，只重嵌新增/变过的文件，其余行原样保留 → 抓取一分钟写一次也不怕；
  2. 长消息按滑窗切（默认 600 字 / 80 重叠），不再 text[:1200] 硬截断；
  3. 非对话文件（README/SKILL/DECISIONS/报告）按段落切块，不再被整份丢弃；
  4. 先写 .tmp 再原子替换，重建期间旧索引始终可用。

用法：
  python index_inc.py            # 增量（第一次等于全量）
  python index_inc.py --dry-run  # 只报要嵌多少块
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

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
IDX = os.path.join(ROOT, 'index')
MANIFEST = os.path.join(IDX, 'manifest.json')
WIN, OVERLAP = 600, 80
SEP_BLOCK = re.compile(r'\n\n---\n\n')
MSG_HEAD = re.compile(r'##\s*(用户|助手)\s*\n(.*)', re.S)

sys.path.insert(0, ROOT)
import vs  # noqa: E402

if os.environ.get('VS_API'):
    vs.API = os.environ['VS_API']


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def split_win(text):
    if len(text) <= WIN + OVERLAP:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + WIN])
        if i + WIN >= len(text):
            break
        i += WIN - OVERLAP
    return out


def chunks_of(path):
    text = io.open(path, encoding='utf-8', errors='replace').read()
    head, sep, rest = text.partition('\n---\n')
    if sep and ('- 会话：' in head or '- 抓取：' in head or '- 网址：' in head):
        body = rest
    else:
        body = text
    out, got = [], 0
    for blk in SEP_BLOCK.split(body):
        # 同 zsearch：分隔线后的空行会让块以 "\n## 用户" 开头，直接 match 会漏掉第一句。
        m = MSG_HEAD.match(blk.lstrip('\n'))
        if not m:
            continue
        got += 1
        clean = re.sub(r'(?m)^>\s?.*\n?', '', m.group(2)).strip()
        if len(clean) < 8:
            continue
        for w in split_win(clean):
            out.append((m.group(1), w))
    if got == 0:
        paras = [p.strip() for p in re.split(r'\n\s*\n', body) if len(p.strip()) >= 20]
        buf = ''
        for p in paras:
            if buf and len(buf) + len(p) > 700:
                out.append(('文档', buf))
                buf = ''
            buf += ('\n\n' if buf else '') + p
        if buf:
            out.append(('文档', buf))
    return out


def header_fields(path):
    """从元信息头取分身字段 设备 / 底座（B11e）。只读前 4 KB——两行元数据不值得读整份。

    没有这两行的老文件返回空串；宁可为空，也不要瞎猜成"网页"或"电脑"。
    """
    try:
        head = io.open(path, encoding='utf-8', errors='replace').read(4096)
    except OSError:
        return '', ''
    head = head.partition('\n---\n')[0]
    dev = re.search(r'(?m)^-\s*设备：\s*(.+?)\s*$', head)
    mod = re.search(r'(?m)^-\s*底座：\s*(.+?)\s*$', head)
    return (dev.group(1) if dev else ''), (mod.group(1) if mod else '')


def bucket_defaults(rel):
    """头里没写时按来源桶兜底：网页对话一定来自网页，这条是事实不是猜。

    电脑会话不兜底——旧记忆可能来自另一台机器，写死本机主机名就是编造。
    """
    parts = rel.split('/')
    src = parts[2] if len(parts) > 3 else ''
    if src == '网页对话':
        return '网页', 'deepseek-web'
    return '', ''


def backfill_fields(dry_run=False):
    """给现有 index/meta.jsonl 补 device/model，不重嵌、不动 vectors.npy。

    补字段本来可以等"文件下次变动时顺便带上"，但那样新字段要等全部文件重写一遍才齐；
    14851 块重嵌一次是 5 分钟 + 几百万 token，而补两列只要读 68 个文件头。
    """
    mp = os.path.join(IDX, 'meta.jsonl')
    if not os.path.exists(mp):
        print('没有 meta.jsonl，先跑一次全量索引')
        return 0
    metas = [json.loads(l) for l in io.open(mp, encoding='utf-8') if l.strip()]
    cache = {}
    changed = 0
    for m in metas:
        f = m['file']
        if f not in cache:
            dev, mod = header_fields(os.path.join(ROOT, f.replace('/', os.sep)))
            if not dev and not mod:
                dev, mod = bucket_defaults(f)
            cache[f] = (dev, mod)
        dev, mod = cache[f]
        if m.get('device') != dev or m.get('model') != mod:
            m['device'], m['model'] = dev, mod
            changed += 1
    print('meta %d 行｜需补字段 %d 行｜文件 %d 个' % (len(metas), changed, len(cache)))
    if dry_run or not changed:
        return changed
    tmp = mp + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        for m in metas:
            f.write(json.dumps(m, ensure_ascii=False) + '\n')
    os.replace(tmp, mp)
    print('已补字段：%s' % mp)
    return changed


def safe_embed(texts, depth=0):
    """单条坏样本不能拖垮整轮 8 万字的嵌入：失败就对半拆，最后兜底截断重试。"""
    try:
        return vs.embed(texts)
    except Exception as e:
        if len(texts) > 1:
            mid = len(texts) // 2
            a = safe_embed(texts[:mid], depth + 1)
            b = safe_embed(texts[mid:], depth + 1)
            return np.vstack([a, b])
        t = texts[0][:300]
        for attempt in range(2):
            try:
                time.sleep(1 + attempt)
                return vs.embed([t])
            except Exception:
                continue
        print('  [warn] 单条嵌入失败，用零向量占位：%s' % str(e)[:80], flush=True)
        return np.zeros((1, 1024), dtype='float32')


# ---- 云端嵌入（一次性建库用；模型仍是 bge-m3，1024 维，与本地同空间） ----
CLOUD_API = os.environ.get('CLOUD_API')
CLOUD_KEY = os.environ.get('CLOUD_KEY')
CLOUD_MODEL = os.environ.get('CLOUD_MODEL', 'BAAI/bge-m3')
TOKENS = [0]


def _cloud_post(batch):
    payload = json.dumps({'model': CLOUD_MODEL, 'input': batch}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(CLOUD_API, data=payload, headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + (CLOUD_KEY or ''),
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        j = json.loads(r.read().decode('utf-8'))
    TOKENS[0] += (j.get('usage') or {}).get('total_tokens') or 0
    data = sorted(j['data'], key=lambda d: d.get('index', 0))
    return np.array([d['embedding'] for d in data], dtype='float32')


def cloud_embed(texts):
    for attempt in range(5):
        try:
            return _cloud_post(texts)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    if len(texts) > 1:
        mid = len(texts) // 2
        return np.vstack([cloud_embed(texts[:mid]), cloud_embed(texts[mid:])])
    raise RuntimeError('云端单条嵌入失败')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--backfill-fields', action='store_true',
                    help='只给现有 meta.jsonl 补 设备/底座 两列，不重嵌、不改向量')
    a = ap.parse_args()

    if a.backfill_fields:
        backfill_fields(dry_run=a.dry_run)
        return

    t0 = time.time()
    files = []
    for dp, _, fns in os.walk(ARCHIVE):
        for fn in fns:
            if fn.endswith('.md'):
                files.append(os.path.join(dp, fn))
    files.sort()

    man = {}
    if os.path.exists(MANIFEST):
        man = json.load(io.open(MANIFEST, encoding='utf-8'))

    old_vecs = None
    old_metas = []
    if os.path.exists(os.path.join(IDX, 'vectors.npy')):
        try:
            old_vecs = np.load(os.path.join(IDX, 'vectors.npy'))
            old_metas = [json.loads(l) for l in io.open(os.path.join(IDX, 'meta.jsonl'), encoding='utf-8') if l.strip()]
            if len(old_vecs) != len(old_metas):
                old_vecs, old_metas = None, []
        except Exception as e:
            print('旧索引不可用，按全量处理：%s' % e, flush=True)
            old_vecs, old_metas = None, []

    rows_by_file = {}
    for i, m in enumerate(old_metas):
        rows_by_file.setdefault(m['file'], []).append(i)

    keep, todo = [], []
    for p in files:
        rel = os.path.relpath(p, ROOT).replace('\\', '/')
        h = sha(p)
        if man.get(rel) == h and rel in rows_by_file:
            keep.extend(rows_by_file[rel])
            continue
        todo.append((rel, h, p))

    new_rows, new_texts = [], []
    for rel, h, p in todo:
        parts = rel.split('/')
        day = parts[1] if len(parts) > 2 else ''
        src = parts[2] if len(parts) > 3 else ''
        dev, mod = header_fields(p)
        if not dev and not mod:
            dev, mod = bucket_defaults(rel)
        for role, txt in chunks_of(p):
            new_rows.append({'file': rel, 'day': day, 'source': src, 'role': role,
                             'device': dev, 'model': mod, 'text': txt})
            new_texts.append('【%s】%s' % (role, txt))

    print('文件 %d｜未变 %d｜待重嵌 %d 个文件 / %d 块｜总块 %d'
          % (len(files), len(files) - len(todo), len(todo), len(new_texts), len(keep) + len(new_texts)), flush=True)
    if a.dry_run:
        return

    vecs_new = None
    if new_texts:
        fn = cloud_embed if CLOUD_API else safe_embed
        step = 32 if CLOUD_API else 400
        parts = []
        for i in range(0, len(new_texts), step):
            parts.append(fn(new_texts[i:i + step]))
            done = min(i + step, len(new_texts))
            print('  嵌入 %d/%d（%.1f 分钟，%d tokens）'
                  % (done, len(new_texts), (time.time() - t0) / 60, TOKENS[0]), flush=True)
        vecs_new = np.vstack(parts)
        print('嵌入完成 %.1f 分钟，合计 %d tokens' % ((time.time() - t0) / 60, TOKENS[0]), flush=True)

    if old_vecs is not None and keep:
        mat = np.vstack([old_vecs[keep]] + ([vecs_new] if vecs_new is not None else []))
        metas = [old_metas[i] for i in keep] + new_rows
    else:
        mat = vecs_new if vecs_new is not None else np.zeros((0, 1024), dtype='float32')
        metas = new_rows

    tmp_v = os.path.join(IDX, 'vectors.tmp')
    tmp_m = os.path.join(IDX, 'meta.jsonl.tmp')
    with open(tmp_v, 'wb') as fh:      # 传文件句柄，numpy 才不会偷偷加 .npy 后缀
        np.save(fh, mat.astype('float32'))
    with io.open(tmp_m, 'w', encoding='utf-8', newline='\n') as f:
        for m in metas:
            f.write(json.dumps(m, ensure_ascii=False) + '\n')
    os.replace(tmp_v, os.path.join(IDX, 'vectors.npy'))
    os.replace(tmp_m, os.path.join(IDX, 'meta.jsonl'))

    man = {rel: h for rel, h, _ in todo}
    man.update({os.path.relpath(p, ROOT).replace('\\', '/'): sha(p) for p in files})
    json.dump(man, io.open(MANIFEST, 'w', encoding='utf-8'), ensure_ascii=False)
    print('DONE 索引 %d 块 / %d 文件｜用时 %.1f 分钟'
          % (len(metas), len(rows_by_file) + len(todo), (time.time() - t0) / 60), flush=True)


if __name__ == '__main__':
    main()
