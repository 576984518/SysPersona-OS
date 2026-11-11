# -*- coding: utf-8 -*-
# SysPersona-OS · 本文件：vs.py — 本地向量检索（bge-m3 + bge-reranker-v2-m3，走 Herdsman 本地 API）
"""
vs.py — 本地向量检索（bge-m3 + bge-reranker-v2-m3，走 Herdsman 本地 API）

  python vs.py build      # 建索引：把 archive/*.md 切成消息块并嵌入
  python vs.py query "记忆库怎么保证不丢东西" [--k 5] [--rerank]
  python vs.py eval       # 跑 15 道语义问题，报召回

零外部依赖：向量用 numpy，接口是 OpenAI 兼容的 /v1/embeddings。
"""
import io
import glob
import json
import os
import re
import sys
import time
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
IDX_DIR = os.path.join(ROOT, "index")
META = os.path.join(IDX_DIR, "meta.jsonl")
VECS = os.path.join(IDX_DIR, "vectors.npy")
DEFAULT_API = "http://127.0.0.1:8080/v1"
DEFAULT_EMB = "bge-m3"
DEFAULT_RERANK = "bge-reranker-v2-m3"


def _load_conf():
    """嵌入后端：ops/embed.json（云端）> 环境变量 > 本地 8080。密钥只认环境变量/.env。"""
    conf = {"api": os.environ.get("PERSONA_EMBED_API") or DEFAULT_API,
            "model": os.environ.get("PERSONA_EMBED_MODEL") or DEFAULT_EMB,
            "rerank": os.environ.get("PERSONA_RERANK_MODEL") or DEFAULT_RERANK,
            "key": os.environ.get("PERSONA_EMBED_KEY") or ""}
    f = os.path.join(ROOT, "ops", "embed.json")
    try:
        d = json.load(io.open(f, encoding="utf-8-sig"))
        conf["api"] = d.get("api") or conf["api"]
        conf["model"] = d.get("model") or conf["model"]
        if "rerank" in d:
            conf["rerank"] = d.get("rerank") or ""   # 显式空串 = 关掉重排
        if not conf["key"]:
            name = d.get("key_env") or ""
            conf["key"] = os.environ.get(name, "") if name else ""
            if not conf["key"] and name and d.get("env_file"):
                # 只读那一行，值不落盘、不进日志
                try:
                    for line in io.open(d["env_file"], encoding="utf-8-sig", errors="replace"):
                        k, _, v = line.strip().partition("=")
                        if k.strip() == name:
                            conf["key"] = v.strip()
                            break
                except OSError:
                    pass
    except (OSError, ValueError):
        pass
    return conf


CONF = _load_conf()
API = CONF["api"].rstrip("/")
EMB_MODEL = CONF["model"]
RERANK_MODEL = CONF["rerank"]
EMB_KEY = CONF["key"]


def post(path, payload, timeout=120):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if EMB_KEY:
        headers["Authorization"] = "Bearer " + EMB_KEY
    req = urllib.request.Request(API + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def rerank(query, docs):
    if not RERANK_MODEL:
        return None            # 云端没配重排：保持 RRF 顺序，不是错误
    """重排。云端多半没有 /rerank 或返回别家格式：拿不到就有序返回 None，调用方保持原序。"""
    try:
        res = post("/rerank", {"model": RERANK_MODEL, "query": query, "documents": docs}, timeout=60)
        items = (res or {}).get("results")
        if not isinstance(items, list):
            return None
        out = [{"index": it["index"], "relevance_score": it["relevance_score"]}
               for it in items
               if isinstance(it, dict) and "index" in it and "relevance_score" in it]
        return {"results": out} if out else None
    except Exception:
        return None


def embed(texts):
    out = []
    for i in range(0, len(texts), 16):
        batch = texts[i:i + 16]
        res = post("/embeddings", {"model": EMB_MODEL, "input": batch})
        out.extend([d["embedding"] for d in res["data"]])
    return np.array(out, dtype="float32")


def chunks_of(path):
    text = io.open(path, encoding="utf-8", errors="replace").read()
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else text
    out = []
    for blk in re.split(r"\n\n---\n\n", body):
        m = re.match(r"##\s*(用户|助手)\s*\n(.*)", blk, re.S)
        if not m:
            continue
        role, raw = m.group(1), m.group(2)
        clean = re.sub(r"(?m)^>\s?.*\n?", "", raw).strip()
        if len(clean) < 8:
            continue
        out.append({"role": role, "text": clean[:1200]})
    return out


def build(only=None):
    os.makedirs(IDX_DIR, exist_ok=True)
    # 拆雷：本函数用的是旧切块法（text[:1200] 截断、文档块丢失），
    # 直接写 index/ 会把 persona_index.py 建的全量索引（14851 块）覆盖成更少。
    _man = os.path.join(IDX_DIR, 'manifest.json')
    _vec = os.path.join(IDX_DIR, 'vectors.npy')
    if os.path.exists(_man) and os.path.exists(_vec) and '--force' not in sys.argv:
        try:
            _old = np.load(_vec, mmap_mode='r').shape[0]
        except Exception:
            _old = 0
        print("拒绝重建：index/ 现有 %d 块，由 persona_index.py 维护。" % _old)
        print("本脚本用的是旧切块法（截断长消息、丢文档块），重建会覆盖掉它。")
        print("日常增量请用：python persona_index.py")
        print("确实要重建：加 --force")
        return
    files = [p for p in glob.glob(os.path.join(ROOT, "archive", "**", "*.md"), recursive=True)]
    files = [p for p in files if "_tail" not in p]
    if only:
        files = [p for p in files if any(o in p for o in only)]
    print("归档文件 %d 个" % len(files))
    metas, texts = [], []
    for p in files:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        parts = rel.split("/")
        day = parts[2] if len(parts) > 2 else ""
        src = parts[3] if len(parts) > 3 else ""
        for ch in chunks_of(p):
            texts.append(("【%s】%s" % (ch["role"], ch["text"])))
            metas.append({"file": rel, "day": day, "source": src, "role": ch["role"], "text": ch["text"]})
    print("待嵌入消息 %d 条" % len(texts))
    t0 = time.time()
    vecs = embed(texts)
    print("嵌入完成 %.1fs，形状 %s" % (time.time() - t0, vecs.shape))
    np.save(VECS, vecs)
    with io.open(META, "w", encoding="utf-8", newline="\n") as f:
        for m in metas:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    print("已写 %s 与 %s" % (VECS, META))


def load():
    vecs = np.load(VECS)
    metas = [json.loads(l) for l in io.open(META, encoding="utf-8") if l.strip()]
    vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs, metas


def rerank(query, cands, topn=5):
    payload = {"model": RERANK_MODEL, "query": query, "documents": [c["text"] for c in cands]}
    try:
        res = post("/rerank", payload, timeout=60)
        order = sorted(range(len(cands)), key=lambda i: -res["results"][i]["relevance_score"])
        return [cands[i] for i in order[:topn]]
    except Exception as e:
        print("（rerank 不可用：%s，退回向量排序）" % e)
        return cands[:topn]


def query(q, k=10, do_rerank=False, topn=5):
    vecs, metas = load()
    qv = embed([q])[0]
    qv = qv / np.linalg.norm(qv)
    sims = vecs @ qv
    idx = np.argsort(-sims)[:k]
    cands = []
    for i in idx:
        m = dict(metas[i])
        m["score"] = float(sims[i])
        cands.append(m)
    if do_rerank:
        return rerank(q, cands, topn=topn)
    return cands[:topn]


QA = [
    ("为什么一个人格要配 500GB 存储", "500GB"),
    ("巡检多久跑一次", "1 分钟"),
    ("计划任务叫什么名字", "LiveWatch"),
    ("重启浏览器为什么会掉登录", "cookie"),
    ("网页一屏只挂载多少条消息", "213"),
    ("MinGit 从哪里下载的", "npmmirror"),
    ("这次发布的版本号是多少", "v3.0.0"),
    ("人格文件分哪三层", "kernel"),
    ("冲突的记忆要写成什么类型", "persona_conflict"),
    ("读档考试最后得了几分", "6/6"),
    ("备份保留几份", "8"),
    ("记忆库放在哪里", "NAS"),
    ("换模型前怎么验证人格没失效", "model_switch_probe"),
    ("网页会话怎么判断有没有更新", "指纹"),
    ("归档用什么压缩格式", ".gz"),
]


def evaluate(rerank_on, topn=20):
    ok, miss, lat = 0, [], []
    for q, ans in QA:
        t0 = time.time()
        cands = query(q, k=max(60, topn), do_rerank=rerank_on, topn=topn)
        dt = time.time() - t0
        lat.append(dt)
        blob = "\n".join(c["text"] for c in cands)
        if ans.lower() in blob.lower():
            ok += 1
        else:
            miss.append(q)
    print("[向量检索%s] %d/%d = %.1f%%  平均 %.2fs" %
          ("+重排" if rerank_on else "", ok, len(QA), 100.0 * ok / len(QA), sum(lat) / len(lat)))
    if miss:
        print("   未命中:", "；".join(miss[:6]))
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if cmd == "build":
        only = [a for a in sys.argv[2:] if not a.startswith("--")]
        build(only or None)
    elif cmd == "query":
        q = sys.argv[2]
        r = "--rerank" in sys.argv
        for c in query(q, k=10, do_rerank=r):
            print("--- %s  [%s] score=%s" % (c["file"], c["role"], round(c.get("score", 0), 3)))
            print(c["text"][:200].replace("\n", " "))
    else:
        evaluate(False)
        evaluate(True)
