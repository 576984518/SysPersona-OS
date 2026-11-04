-- SysPersona 记忆库最小 schema（SQLite）
-- 设计约束：
--   1. 每条记忆可追溯到来源消息；
--   2. 人格版本与记忆分离，可独立回滚；
--   3. 删除为软删除，保留审计。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- 人格版本 ----------
CREATE TABLE IF NOT EXISTS persona_versions (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    version         TEXT    NOT NULL,
    parent_version  TEXT,
    body            TEXT    NOT NULL,      -- YAML/JSON 原文
    content_hash    TEXT    NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    UNIQUE (persona_uuid, version)
);

-- ---------- 性格结构：可漂移的偏好与反射 ----------
CREATE TABLE IF NOT EXISTS persona_traits (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('preference','reflex','habit','voice')),
    trait_id        TEXT    NOT NULL,      -- 稳定标识，如 pref_kb_sound
    target          TEXT,                  -- 偏好对象 / 触发条件
    polarity        TEXT    CHECK (polarity IN ('like','dislike','neutral')),
    weight          REAL    CHECK (weight BETWEEN 0 AND 100),
    baseline        REAL    CHECK (baseline BETWEEN 0 AND 100),
    strength        REAL,                  -- 反射/习惯强度
    payload         TEXT,                  -- JSON：反应、情绪偏移等
    evidence        TEXT,                  -- JSON 数组：来源 episode id
    is_locked       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL,
    last_revalidated_at TEXT,              -- 抗僵化：最近一次被新记忆重新支撑的时间
    UNIQUE (persona_uuid, trait_id)
);

-- ---------- 漂移审计：每一次微调都可追溯 ----------
CREATE TABLE IF NOT EXISTS persona_drift_log (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    trait_id        TEXT    NOT NULL,
    old_value       REAL,
    new_value       REAL,
    delta           REAL,
    evidence        TEXT,                  -- JSON 数组
    reason          TEXT,
    version_from    TEXT,
    version_to      TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_trait ON persona_drift_log(persona_uuid, trait_id, created_at);

-- ---------- 情景记忆：原始片段 ----------
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content         TEXT    NOT NULL,
    summary         TEXT,                  -- 压缩后的可检索摘要
    importance      REAL    NOT NULL DEFAULT 0.5,   -- 0..1
    occurred_at     TEXT    NOT NULL,
    source_msg_id   TEXT,                  -- 来源消息，便于追溯
    deleted_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(persona_uuid, occurred_at);
CREATE INDEX IF NOT EXISTS idx_episodes_importance ON episodes(persona_uuid, importance);

-- 全文检索
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    content, summary,
    content='episodes',
    content_rowid='id'
);

-- ---------- 语义记忆：事实 ----------
CREATE TABLE IF NOT EXISTS facts (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    predicate       TEXT    NOT NULL,
    object          TEXT    NOT NULL,
    confidence      REAL    NOT NULL DEFAULT 0.8,
    valid_from      TEXT,
    valid_to        TEXT,                  -- NULL 表示仍有效
    source_episode  INTEGER REFERENCES episodes(id),
    updated_at      TEXT    NOT NULL,
    deleted_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(persona_uuid, subject, predicate);

-- ---------- 实体与关系 ----------
CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    kind            TEXT    NOT NULL,      -- person / interest / habit / project / place / event
    aliases         TEXT,                  -- JSON 数组，处理词汇鸿沟
    confidence      REAL    NOT NULL DEFAULT 0.8,   -- 用户直说 1.0，模型推断 0.6
    first_seen      TEXT,
    last_seen       TEXT,
    UNIQUE (persona_uuid, name, kind)
);

CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    src_entity      INTEGER NOT NULL REFERENCES entities(id),
    dst_entity      INTEGER NOT NULL REFERENCES entities(id),
    relation        TEXT    NOT NULL,
    weight          REAL    NOT NULL DEFAULT 1.0,   -- 出现频次
    confidence      REAL    NOT NULL DEFAULT 0.8,
    priority        REAL    NOT NULL DEFAULT 1.0,   -- 频次 × 衰减 × 明确度 × 关系强度
    last_seen       TEXT,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_priority ON edges(persona_uuid, priority DESC);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_entity);

-- ---------- 标签（倒排） ----------
CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    tag             TEXT    NOT NULL,
    target_kind     TEXT    NOT NULL CHECK (target_kind IN ('episode','fact','entity')),
    target_id       INTEGER NOT NULL,
    weight          REAL    NOT NULL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_tags_lookup ON tags(persona_uuid, tag, target_kind);

-- ---------- 向量（可选：sqlite-vec 或外部索引引用） ----------
CREATE TABLE IF NOT EXISTS embeddings (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    target_kind     TEXT    NOT NULL,
    target_id       INTEGER NOT NULL,
    model           TEXT    NOT NULL,
    dim             INTEGER NOT NULL,
    vector          BLOB    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_target ON embeddings(persona_uuid, target_kind, target_id);

-- ---------- 长期画像 ----------
CREATE TABLE IF NOT EXISTS profile (
    persona_uuid    TEXT PRIMARY KEY,
    summary         TEXT    NOT NULL DEFAULT '',   -- 稳定特征摘要
    updated_at      TEXT    NOT NULL
);

-- ---------- 审计与快照 ----------
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    action          TEXT    NOT NULL,      -- write / update / delete / migrate / activate
    target_kind     TEXT,
    target_id       INTEGER,
    detail          TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY,
    persona_uuid    TEXT    NOT NULL,
    kind            TEXT    NOT NULL,      -- pre_consolidate / pre_migrate / daily
    path            TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);
