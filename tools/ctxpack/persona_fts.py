# -*- coding: utf-8 -*-
r"""persona_fts.py — 不依赖模型/向量的本地检索层（SQLite FTS5）

设计目标只有一句：**没有 jieba、没有 bge-m3、没有网络时，中文记忆仍然查得到。**

两种分词模式（同一个库必须用同一种，建库时记进 build_meta）：

  char  纯标准库：中文切成「单字 + 相邻双字」，英文/数字/下划线整体保留。
        例：AI会话检索记录 -> AI 会 话 检 索 记 录 会话 检索 记录
  jieba 有 jieba 时用它切词；没有就明确报错，**不静默换模式**（换了查询就错位）。

存储：
  <root>\index\fts.db
    chunks(rowid, file, day, source, role, text, device, model, avatar)
                                                    原文，一行一块；后三列给过滤用
    chunk_meta(rowid, file, sha, tokenize)         增量记账 + 分词结果
    chunks_fts(tokenize)                            FTS5 倒排（contentless）
    build_meta(key, value)                          建库参数/耗时

用法：
  python persona_fts.py --build                 # 全量或增量（按 file + sha256(text)）
  python persona_fts.py --build --rebuild       # 推倒重建
  python persona_fts.py --build --force-compat  # 模拟低版本 SQLite（无 contentless_delete）
  python persona_fts.py --search "上下文压缩" --k 20
  python persona_fts.py --search "DSH 水位 350K" --rerank
  python persona_fts.py --stats                 # 行数/库大小/建库耗时/20 条查询 p50·p95

只用标准库；jieba 只做 `try: import`。
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

try:
    import jieba as _jieba
except Exception:                       # 没装 jieba 是正常状态，不是错误
    _jieba = None

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SCHEMA_VER = 2
CJK = r'\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'
CJK_RUN = re.compile(r'[' + CJK + r']+')
ALNUM_RUN = re.compile(r'[A-Za-z0-9_]+')
MIX_RUN = re.compile(r'[' + CJK + r']+|[A-Za-z0-9_]+')
SEP = re.compile(r'\s+')
# 工具调用流水不是正文。与 persona_search 的默认口径一致：查的时候剔掉，建库仍保留原文。
_TOOL_RE = re.compile(r'🔧\s*`?(edit|read|write|pwsh|job_output|glob|grep|present|task)`?\s*\{')


def _is_tool_noise(text):
    return bool(_TOOL_RE.search(text or ''))

# 20 条固定查询：覆盖精确标识符 / 中文短语 / 中英混合 / 模糊 / 长句 / 数字 /
# 代码片段 / 时间性提问。--stats 的 p50/p95 就基于这一组，换机器可直接对比。
STATS_QUERIES = [
    'persona_search',
    '接口 B75',
    '上下文压缩',
    '记忆分身',
    'DSH 水位 350K',
    '那个不换窗口的方案',
    '回声降权',
    'bge-m3 向量',
    '防止舔狗化的惩罚规则',
    '960 显卡',
    '交接包',
    'CREATE VIRTUAL TABLE',
    '--no-vector',
    'sha256(text)',
    '11月7日的会话',
    '最近一次发版',
    '自动抓取方案',
    '记忆老丢怎么办',
    '上下文快满了应该怎么办',
    'FTS5 建库',
]


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------
def _is_cjk(ch):
    return '\u3400' <= ch <= '\u9fff' or '\uf900' <= ch <= '\ufaff'


def char_tokens(text):
    """单字 + 相邻双字。英文/数字/下划线整体保留（FTS5 会再按 unicode61 切）。"""
    out = []
    for m in MIX_RUN.finditer(text or ''):
        s = m.group(0)
        if _is_cjk(s[0]):
            out.extend(s)                              # 单字
            for i in range(len(s) - 1):                # 双字
                out.append(s[i:i + 2])
        else:
            out.append(s.lower())
    return out


def _jieba_tokens(text):
    out = []
    for w in _jieba.cut(text or ''):
        w = w.strip()
        if not w:
            continue
        if CJK_RUN.fullmatch(w):
            out.append(w)
        elif ALNUM_RUN.fullmatch(w):
            out.append(w.lower())
        else:
            # 标点、混合片段：拆成可索引的原子，别把标点写进 tokenize 列。
            out.extend(char_tokens(w))
    return out


def tokenize_text(text, mode):
    if mode == 'jieba':
        if _jieba is None:
            raise RuntimeError('jieba 不可用：请先安装 jieba，或改用 --tokenizer char')
        return ' '.join(_jieba_tokens(text))
    return ' '.join(char_tokens(text))


def query_terms(query, mode):
    """查询词去重保序；空串/纯标点返回空列表。"""
    if mode == 'jieba':
        if _jieba is None:
            raise RuntimeError('这个库是 jieba 模式建的，但当前环境没有 jieba；'
                               '请装 jieba，或用 --tokenizer char 重建一个库')
        raw = _jieba_tokens(query)
    else:
        raw = char_tokens(query)
    return [t for t in dict.fromkeys(raw) if t.strip()]


# ---------------------------------------------------------------------------
# 路径 / 输入
# ---------------------------------------------------------------------------
def default_root():
    env = os.environ.get('SYSPERSONA_ROOT') or os.environ.get('PERSONA_ROOT')
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(cand, 'archive')) or os.path.isdir(os.path.join(cand, 'index')):
            return cand
    return os.getcwd()


def default_db(root):
    return os.environ.get('PERSONA_FTS_DB') or os.path.join(root, 'index', 'fts.db')


def _read_jsonl(path):
    with open(path, encoding='utf-8-sig', errors='replace') as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def iter_docs(paths):
    """把 meta.jsonl / L1.jsonl 统一成 {file, day, source, role, text}。

    meta.jsonl：每行一个块（text 是正文）。
    L1.jsonl  ：每行一个来源，只有首句 head；没有 meta 时用它兜底，role 标成「一级索引」。
    文件后缀/列名自动判断，不要求调用方先转换。
    """
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        for obj in _read_jsonl(p):
            text = obj.get('text')
            role = obj.get('role')
            if text is None:
                text = obj.get('head') or ''
                role = role or '一级索引'
            text = str(text)
            if len(text.strip()) < 8:
                continue
            yield {
                'file': str(obj.get('file') or ''),
                'day': str(obj.get('day') or ''),
                'source': str(obj.get('source') or ''),
                'role': str(role or ''),
                'text': text,
                # 分身/设备过滤用；meta.jsonl v2 已有，L1 里叫 avatar/agent，取不到就空串。
                'device': str(obj.get('device') or obj.get('agent') or ''),
                'model': str(obj.get('model') or ''),
                'avatar': str(obj.get('avatar') or obj.get('agent') or ''),
            }


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------
def _connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.execute('PRAGMA synchronous=OFF')
    con.execute('PRAGMA temp_store=MEMORY')
    return con


# contentless_delete 是 SQLite 3.43 才有的 FTS5 选项；低版本建表会直接报
# `unrecognized option: "contentless_delete"`。低版本走「不带该参数」的建表，
# 删除时用 FTS5 官方的 'delete' 指令（见 _delete_row），内容照样可检索。
CLD_MIN = (3, 43, 0)


def _supports_contentless_delete(force_compat=False):
    return (not force_compat) and tuple(sqlite3.sqlite_version_info) >= CLD_MIN


def _create_schema(con, contentless_delete):
    con.execute('''CREATE TABLE IF NOT EXISTS chunks(
        rowid INTEGER PRIMARY KEY,
        file TEXT NOT NULL,
        day TEXT,
        source TEXT,
        role TEXT,
        text TEXT NOT NULL,
        device TEXT DEFAULT '',
        model TEXT DEFAULT '',
        avatar TEXT DEFAULT '')''')
    con.execute('''CREATE TABLE IF NOT EXISTS chunk_meta(
        rowid INTEGER PRIMARY KEY,
        file TEXT NOT NULL,
        sha TEXT NOT NULL,
        tokenize TEXT NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_chunk_meta_file ON chunk_meta(file)')
    con.execute('CREATE INDEX IF NOT EXISTS idx_chunk_meta_file_sha ON chunk_meta(file, sha)')
    extra = ', contentless_delete=1' if contentless_delete else ''
    con.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        tokenize, content=''%s, tokenize='unicode61 remove_diacritics 2')''' % extra)
    con.execute('CREATE TABLE IF NOT EXISTS build_meta(key TEXT PRIMARY KEY, value TEXT)')


def _meta_get(con, key, default=None):
    try:
        row = con.execute('SELECT value FROM build_meta WHERE key=?', (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


def _meta_set(con, key, value):
    con.execute('INSERT INTO build_meta(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def _table_exists(con, name):
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)).fetchone())


def build(db, inputs, tokenizer='auto', rebuild=False, prune=False, root=None, quiet=False,
          force_compat=False):
    """建库/增量。返回 (rows, seconds, inserted, reused, deleted)。

    force_compat=True：即使 SQLite ≥3.43 也走低版本兼容建表，用来验证回退路径。
    """
    t0 = time.time()
    want_cld = _supports_contentless_delete(force_compat)
    # 显式要 jieba 但环境没有：在创建/改动任何文件之前就失败，别留下半个空库。
    if tokenizer == 'jieba' and _jieba is None:
        raise SystemExit('jieba 不可用：请装 jieba，或改用 --tokenizer char')
    os.makedirs(os.path.dirname(os.path.abspath(db)) or '.', exist_ok=True)
    con = _connect(db)
    try:
        if rebuild:
            for t in ('chunks_fts', 'chunk_meta', 'chunks', 'build_meta'):
                con.execute('DROP TABLE IF EXISTS %s' % t)
        _create_schema(con, contentless_delete=want_cld)
        fts_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone()
        actual_cld = bool(fts_sql and 'contentless_delete' in (fts_sql[0] or ''))
        if not actual_cld:
            print('[fts] sqlite %s → 兼容模式（无 contentless_delete）'
                  % sqlite3.sqlite_version)
        elif force_compat:
            print('[fts] --force-compat：现有库已带 contentless_delete，未改变；'
                  '用 --db 另建库可验证兼容模式', file=sys.stderr)
        cols = set(r[1] for r in con.execute('PRAGMA table_info(chunks)'))
        if 'text' not in cols:
            raise SystemExit('这个库的表结构不认识；请加 --rebuild')
        if 'device' not in cols:
            raise SystemExit('这是 v1 库（没有 device/model/avatar 列）；请加 --rebuild')

        old_tokenizer = _meta_get(con, 'tokenizer')
        if old_tokenizer and old_tokenizer != 'auto':
            if tokenizer == 'auto':
                tokenizer = old_tokenizer
            elif tokenizer != old_tokenizer:
                raise SystemExit('这个库是 %s 模式建的，当前要求 %s；请加 --rebuild 或换 --db'
                                 % (old_tokenizer, tokenizer))
        if tokenizer == 'auto':
            tokenizer = 'jieba' if _jieba is not None else 'char'
        if tokenizer == 'jieba' and _jieba is None:
            raise SystemExit('jieba 不可用：请装 jieba，或改用 --tokenizer char')
        existing = {}
        if _table_exists(con, 'chunk_meta'):
            for rid, file, sha in con.execute('SELECT rowid,file,sha FROM chunk_meta'):
                existing.setdefault(file, {}).setdefault(sha, []).append(rid)

        seen_files = set()
        inserted = reused = deleted = 0
        with con:
            _meta_set(con, 'schema_ver', SCHEMA_VER)
            _meta_set(con, 'tokenizer', tokenizer)
            _meta_set(con, 'contentless_delete', '1' if actual_cld else '0')
            _meta_set(con, 'sqlite_version', sqlite3.sqlite_version)
            _meta_set(con, 'root', os.path.abspath(root or default_root()))
            _meta_set(con, 'db_path', os.path.abspath(db))
            _meta_set(con, 'sources', json.dumps([os.path.abspath(p) for p in inputs if p],
                                                 ensure_ascii=False))
            _meta_set(con, 'built_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
            source_bytes = sum(os.path.getsize(p) for p in inputs if p and os.path.exists(p))
            _meta_set(con, 'source_bytes', source_bytes)

            for i, doc in enumerate(iter_docs(inputs), 1):
                file = doc['file']
                seen_files.add(file)
                sha = hashlib.sha256(doc['text'].encode('utf-8', 'replace')).hexdigest()
                pool = existing.get(file, {}).get(sha)
                if pool:
                    # 已经有一模一样的块：行号、FTS 倒排都不动。
                    pool.pop()
                    reused += 1
                    continue
                tok = tokenize_text(doc['text'], tokenizer)
                cur = con.execute(
                    'INSERT INTO chunks(file,day,source,role,text,device,model,avatar) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (file, doc['day'], doc['source'], doc['role'], doc['text'],
                     doc.get('device', ''), doc.get('model', ''), doc.get('avatar', '')))
                rid = cur.lastrowid
                con.execute('INSERT INTO chunk_meta(rowid,file,sha,tokenize) VALUES(?,?,?,?)',
                            (rid, file, sha, tok))
                con.execute('INSERT INTO chunks_fts(rowid,tokenize) VALUES(?,?)', (rid, tok))
                inserted += 1
                if not quiet and i % 5000 == 0:
                    print('  ... %d 块（新增 %d，复用 %d）' % (i, inserted, reused),
                          file=sys.stderr)

            # 本文件剩下没被复用的旧块：文件改过，删掉旧版本。
            for file in seen_files:
                for sha, rids in existing.get(file, {}).items():
                    for rid in rids:
                        _delete_row(con, rid)
                        deleted += 1
            if prune:
                # 显式 --prune 才清理输入里已经消失的文件；默认不删，避免用 L1 建库时误删 meta 块。
                for file in list(existing):
                    if file in seen_files:
                        continue
                    for sha, rids in existing[file].items():
                        for rid in rids:
                            _delete_row(con, rid)
                            deleted += 1

            rows = con.execute('SELECT count(*) FROM chunks').fetchone()[0]
            secs = round(time.time() - t0, 3)
            _meta_set(con, 'rows', rows)
            # build_seconds 永远指“全量建库”，增量只写 last_seconds；否则 --stats 会把
            # 2.7 秒的增量说成建库耗时，看起来像性能造假。
            if rebuild or not _meta_get(con, 'build_seconds'):
                _meta_set(con, 'build_seconds', secs)
            _meta_set(con, 'last_seconds', secs)
            _meta_set(con, 'last_inserted', inserted)
            _meta_set(con, 'last_reused', reused)
            _meta_set(con, 'last_deleted', deleted)
        return rows, secs, inserted, reused, deleted
    finally:
        con.close()


def _delete_row(con, rid):
    row = con.execute('SELECT tokenize FROM chunk_meta WHERE rowid=?', (rid,)).fetchone()
    if not row:
        con.execute('DELETE FROM chunks WHERE rowid=?', (rid,))
        return
    try:
        con.execute('DELETE FROM chunks_fts WHERE rowid=?', (rid,))
    except sqlite3.OperationalError:
        # 老 SQLite 不支持 contentless delete：走官方的 'delete' 指令。
        con.execute("INSERT INTO chunks_fts(chunks_fts,rowid,tokenize) VALUES('delete',?,?)",
                    (rid, row[0]))
    con.execute('DELETE FROM chunk_meta WHERE rowid=?', (rid,))
    con.execute('DELETE FROM chunks WHERE rowid=?', (rid,))


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def _match_expr(terms, op='OR'):
    return (' %s ' % op).join('"%s"' % t.replace('"', '""') for t in terms)


def _score_rows(con, terms, limit, op='OR'):
    sql = ('SELECT c.rowid,c.file,c.day,c.source,c.role,c.text,'
           'c.device,c.model,c.avatar,bm25(chunks_fts) AS r '
           'FROM chunks_fts JOIN chunks c ON c.rowid=chunks_fts.rowid '
           'WHERE chunks_fts MATCH ? ORDER BY r LIMIT ?')
    return con.execute(sql, (_match_expr(terms, op), limit)).fetchall()


def _density(terms, text):
    """关键词命中密度：覆盖了查询里几个词元 + 每 500 字出现几次。

    用分词后的 2 字以上词元（不是整句）：口语问句整句几乎不可能原样出现，
    拿整句算密度会恒为 0，二路融合就退化成纯 FTS 排名。
    """
    segs = [t for t in terms if len(t) >= 2]
    if not segs:
        segs = list(terms)
    if not segs:
        return 0.0
    low = text.lower()
    hit = sum(1 for s in segs if s in low)
    occ = sum(low.count(s) for s in segs)
    coverage = hit / float(len(segs))
    per500 = occ / max(1.0, len(text) / 500.0)
    return 0.7 * coverage + 0.3 * min(1.0, per500)


def rerank_rows(query, rows, terms):
    """纯 Python 二路融合：FTS bm25 排名 + 关键词命中密度。没有任何模型。"""
    if not rows:
        return []
    n = len(rows)
    out = []
    qn = re.sub(r'\s+', '', query)
    for rank, r in enumerate(rows):
        text = r[5]
        fts_part = 1.0 - rank / float(max(1, n))
        dens = _density(terms, text)
        score = 0.6 * fts_part + 0.4 * dens
        # 原样包含整句（去空白后）的块再加一点；口语查询通常靠这条拉开差距。
        if len(qn) >= 4 and qn in re.sub(r'\s+', '', text):
            score += 0.15
        out.append((score, r))
    out.sort(key=lambda x: -x[0])
    return out


def _rare_terms(con, terms, n):
    """按文档频率取最稀有的 n 个词元；df 用 FTS5 自己数，别另建统计表。"""
    scored = []
    for t in terms:
        try:
            c = con.execute('SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?',
                            ('"%s"' % t.replace('"', '""'),)).fetchone()[0]
        except sqlite3.Error:
            c = 0
        scored.append((c, t))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [t for _, t in scored[:n]]


def _dedupe_rows(rows, limit):
    """同一段正文在多个快照里重复出现时只留第一条（与 zsearch 的去重口径一致）。"""
    seen, out = set(), []
    for r in rows:
        key = hashlib.sha1(re.sub(r'\s+', ' ', r[5]).strip()[:600].encode('utf-8', 'replace')).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def search_conn(con, query, k=20, rerank=False, pool=None, dedupe=True, keep_tools=False):
    tokenizer = _meta_get(con, 'tokenizer', 'char')
    terms = query_terms(query, tokenizer)
    if not terms:
        return []
    # 两段式：先用「2 字以上词元 AND」拿精确候选（快且准），不够再用 OR 扩召回。
    # 单字不参与 AND：中文里“的/是/不”这种单字几乎每块都有，AND 上去只会把正确答案筛掉。
    strong = [t for t in terms if len(t) >= 2]
    limit = pool or (max(k * 8, 200) if rerank else max(k * 4, 100))
    def _clean(rs):
        # 先去重、再剔工具流水，然后才判断“够不够”。7 条快照里的同一段正文、
        # 或 5 条 `🔧 edit` 流水，都会让“看起来够”变成“实际只剩 1 条”。
        if dedupe:
            rs = _dedupe_rows(rs, limit)
        if not keep_tools:
            rs = [r for r in rs if not _is_tool_noise(r[5])]
        return rs

    rows = []
    if len(strong) >= 2:
        rows = _score_rows(con, strong, limit, op='AND')
    rows = _clean(rows)
    if len(rows) < min(k, 5):
        # OR 的项数直接决定扫描量：长句十几个双字全 OR 要 500ms+。
        # 只挑文档频率最低的 6 个词元，既快又不至于被“的/了”这种高频词淹没。
        picked = _rare_terms(con, strong, 6) if len(strong) > 6 else strong
        seen = set(r[0] for r in rows)
        rest = _score_rows(con, picked, limit * 2)
        rows = _clean(rows + [r for r in rest if r[0] not in seen])
        if len(rows) < min(k, 5) and picked != strong:
            rest = _score_rows(con, strong, limit * 2)
            seen = set(r[0] for r in rows)
            rows = _clean(rows + [r for r in rest if r[0] not in seen])
        if len(rows) < min(k, 5):
            rest = _score_rows(con, terms, limit * 2)
            seen = set(r[0] for r in rows)
            rows = _clean(rows + [r for r in rest if r[0] not in seen])
    def _item(r, score):
        return {'rowid': r[0], 'file': r[1], 'day': r[2], 'source': r[3],
                'role': r[4], 'text': r[5], 'device': r[6], 'model': r[7],
                'avatar': r[8], 'score': round(float(score), 6)}

    if rerank:
        return [_item(r, s) for s, r in rerank_rows(query, rows, terms)[:k]]
    return [_item(r, -float(r[9])) for r in rows[:k]]


def search(query, db=None, k=20, rerank=False, root=None):
    db = db or default_db(root or default_root())
    if not os.path.exists(db):
        raise FileNotFoundError('fts.db 不存在：%s（先运行 python persona_fts.py --build）' % db)
    con = _connect(db)
    try:
        return search_conn(con, query, k=k, rerank=rerank)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 行号（输出 文件:行号，和现有 persona_search.py 完全一致）
# ---------------------------------------------------------------------------
_LINES = {}


def locate(root, rel, text):
    """在原文里找块首行；块可能从半行开始，所以按子串找，不要求整行相等。"""
    if not root:
        return 0
    if rel not in _LINES:
        p = os.path.join(root, rel.replace('/', os.sep))
        try:
            _LINES[rel] = open(p, encoding='utf-8', errors='replace').read()
        except OSError:
            _LINES[rel] = ''
    content = _LINES[rel]
    firsts = []
    for ln in (text or '').splitlines():
        if ln.strip():
            firsts.append(ln.strip())
            if len(firsts) >= 5:
                break
    # 40 字找不到就用 24/16 字；第一行找不到就试下一行。
    # 块常被切在半行/被 `> ` 引用过，拿整行等值匹配会假报 0。
    for first in firsts:
        for n in (40, 24, 16):
            probe = first[:n]
            if probe:
                pos = content.find(probe)
                if pos >= 0:
                    return content.count('\n', 0, pos) + 1
    return 0


def snippet(text, n=80):
    s = SEP.sub(' ', text or '').strip()
    return s[:n]


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    i = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * p))))
    return vals[i]


def stats(db, root=None, queries=None):
    queries = queries or STATS_QUERIES
    con = _connect(db)
    try:
        rows = con.execute('SELECT count(*) FROM chunks').fetchone()[0]
        size = os.path.getsize(db)
        secs = float(_meta_get(con, 'build_seconds', 0) or 0)
        last = float(_meta_get(con, 'last_seconds', 0) or 0)
        tokenizer = _meta_get(con, 'tokenizer', '?')
        sources = json.loads(_meta_get(con, 'sources', '[]') or '[]')
        source_bytes = int(_meta_get(con, 'source_bytes', 0) or 0)
        # 预热一次，别把首次开页/页缓存算进 p50。
        search_conn(con, queries[0], k=20)
        lat = []
        for q in queries:
            t0 = time.perf_counter()
            search_conn(con, q, k=20)
            lat.append((time.perf_counter() - t0) * 1000.0)
        print('数据库      : %s' % db)
        print('行数        : %d' % rows)
        print('库大小      : %d bytes (%.1f MiB)' % (size, size / 1048576.0))
        print('分词模式    : %s' % tokenizer)
        print('全量建库    : %.3f s' % secs)
        if last and abs(last - secs) > 0.001:
            print('上次增量    : %.3f s' % last)
        print('源文件      : %d bytes (%.1f MiB)  %s' %
              (source_bytes, source_bytes / 1048576.0,
               ', '.join(os.path.basename(x) for x in sources)))
        print('查询        : %d 条  p50=%.2f ms  p95=%.2f ms  min=%.2f  max=%.2f' %
              (len(lat), _pct(lat, 0.50), _pct(lat, 0.95), min(lat), max(lat)))
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='SQLite FTS5 本地检索（零模型）')
    ap.add_argument('--root', default=None, help='记忆库根（默认 SYSPERSONA_ROOT / 脚本旁带 archive|index 的目录）')
    ap.add_argument('--db', default=None, help='库路径（默认 <root>\\index\\fts.db）')
    ap.add_argument('--meta', default=None, help='meta.jsonl 路径（默认 <root>\\index\\meta.jsonl）')
    ap.add_argument('--l1', default=None, help='L1.jsonl 路径（可选）')
    ap.add_argument('--with-l1', action='store_true', dest='with_l1', help='建库时把 L1.jsonl 的首句也收进去')
    ap.add_argument('--tokenizer', choices=('auto', 'char', 'jieba'), default='auto')
    ap.add_argument('--build', action='store_true', help='建库/增量')
    ap.add_argument('--rebuild', action='store_true', help='推倒重建')
    ap.add_argument('--prune', action='store_true', help='删除输入里已消失的文件对应的块（默认不删）')
    ap.add_argument('--force-compat', action='store_true', dest='force_compat',
                    help='强制走低版本 SQLite 兼容建表（无 contentless_delete），用于验证回退路径')
    ap.add_argument('--search', metavar='QUERY', default=None)
    ap.add_argument('--k', type=int, default=20)
    ap.add_argument('--rerank', action='store_true', help='FTS 分数 + 关键词密度二路融合（纯 Python）')
    ap.add_argument('--keep-tools', action='store_true', dest='keep_tools',
                    help='保留工具调用流水（🔧 edit {...}）；默认过滤')
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--json', action='store_true', help='检索结果输出 JSON 行')
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root) if a.root else None
    db = os.path.abspath(a.db or default_db(root or default_root()))
    if root is None and os.path.exists(db):
        # 没给 --root 时，用建库时记下的库根算行号；别让 文件:0 冒充定位成功。
        try:
            _c = sqlite3.connect(db)
            _r = _meta_get(_c, 'root')
            _c.close()
            if _r and os.path.isdir(_r):
                root = _r
        except sqlite3.Error:
            pass
    root = root or os.path.abspath(default_root())

    if a.build or a.rebuild:
        meta = a.meta or os.path.join(root, 'index', 'meta.jsonl')
        l1 = a.l1 or os.path.join(root, 'index', 'L1.jsonl')
        inputs = []
        if os.path.exists(meta):
            inputs.append(meta)
        # 显式给了 --l1 就收；只给 --with-l1 也收；都没有就只在 meta 缺失时兜底。
        if (a.with_l1 or a.l1) and os.path.exists(l1):
            inputs.append(l1)
        if not inputs:
            if os.path.exists(l1):
                inputs.append(l1)
            else:
                ap.error('找不到 meta.jsonl 或 L1.jsonl：%s' % os.path.join(root, 'index'))
        print('建库：%s' % db)
        print('输入：%s' % ', '.join(inputs))
        print('模式：%s%s' % (a.tokenizer, '（--rebuild）' if a.rebuild else '（增量）'))
        rows, secs, ins, reused, deleted = build(
            db, inputs, tokenizer=a.tokenizer, rebuild=a.rebuild, prune=a.prune, root=root,
            force_compat=a.force_compat)
        size = os.path.getsize(db)
        print('完成：%d 块，用时 %.3f s，新增 %d，复用 %d，清理 %d，库 %.1f MiB'
              % (rows, secs, ins, reused, deleted, size / 1048576.0))
        return 0

    if a.search is not None:
        if not os.path.exists(db):
            print('fts.db 不存在：%s' % db, file=sys.stderr)
            print('先运行：python %s --build' % os.path.basename(__file__), file=sys.stderr)
            return 2
        con = _connect(db)
        try:
            hits = search_conn(con, a.search, k=a.k, rerank=a.rerank,
                               keep_tools=a.keep_tools)
        except RuntimeError as e:
            print('错误：%s' % e, file=sys.stderr)
            return 2
        finally:
            con.close()
        if a.json:
            for h in hits:
                h = dict(h)
                h['line'] = locate(root, h['file'], h['text'])
                print(json.dumps(h, ensure_ascii=False))
        else:
            for h in hits:
                line = locate(root, h['file'], h['text'])
                print('%.6f\t%s:%d\t%s\t%s' % (h['score'], h['file'], line,
                                               h['source'], snippet(h['text'])))
        return 0

    if a.stats:
        if not os.path.exists(db):
            print('fts.db 不存在：%s' % db, file=sys.stderr)
            return 2
        stats(db, root=root)
        return 0

    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
