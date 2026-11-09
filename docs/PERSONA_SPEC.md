# SysPersona 人格文件规范 v1.1（现行）

> 本页只回答一件事：**"它是谁"怎么被文件化。**
> 与 `PHILOSOPHY.md`（为什么）、`AVATAR_SPEC.md`（多分身怎么在场）互补，不重复。
> 变更记在本页文末「修订记录」与 `CHANGELOG.md`。

## 0. 一句话

**人格 = 文件 + 记忆，每次唤醒重建；模型不构成人格。**

## 1. 三件套 + 一库

| 文件 | 装什么 | 谁能写 | 写入时机 |
|---|---|---|---|
| `kernel.json` | 身份、价值观、边界、语气基线、交互模式、宪法、能力面 | **只有人** | 走"改内核"特殊流程 |
| `traits.json` | 偏好权重、反射、演化参数 | **结算点**（睡眠 / consolidate） | 单写者；AI 不得直接写 |
| `state.json` | 情绪、在场、指针、水位 | 运行时 | 实时可写 |
| `memory.db` | 情景 / 事实 / 实体 / 向量 / 审计 | 运行时追加 | 清理有规则（先有替代物再清） |

> 三条硬约束：**文件是真相，数据库是缓存**；`traits`/`kernel`/`permissions` 只有一个写入来源；
> **记忆可合并，人格不可合并**。

## 2. `kernel.json` 字段（schema `syspersona/persona@2`）

| 字段 | 含义 | 约束 |
|---|---|---|
| `identity.name` / `self_reference` / `user_address` | 名字、自称、称呼 | 人改 |
| `values[]` | 价值观 | 人改 |
| `boundaries[]` | 边界条款 | 人改 |
| `voice_baseline.tone` / `avoid[]` | 语气基线 | 人改 |
| `interaction.mode` | 交互模式，如 `async_non_blocking` | 人改；缺省按 v4.0 工作模式 |
| `interaction.ack_first` / `block_front` / `report_as` | 回执 / 不占前台 / 事件插播 | 人改 |
| `constitution.law_floor` | 现实法理在最顶层 | **P0 写死** |
| `constitution.priority[]` | `["law","kernel","human_instruction","long_term_memory","model_bias"]` | 人改 = 改宪法 |
| `constitution.right_to_refuse` | 拒绝权开关 | 人改；关闭 = 取消人格边界 |
| **`permissions`** | **能力面：allow / ask / deny 三张清单** | **人改（P1）；AI 只读** |

### 2.1 `permissions`（此前只在 `HANDOFF` §五 被提及，这里定义）

```jsonc
"permissions": {
  "default": "deny",                  // 未列出的能力一律拒绝
  "allow": ["fs.read", "net.fetch:readonly"],
  "ask":   ["fs.write:workspace", "proc.exec:whitelist"],
  "deny":  ["proc.exec", "camera", "clipboard.read"]
}
```

- 语义对应 `LAUNCHER_SPEC.md` §11 的 allow / ask / deny 裁决；
- **`proc.exec` 默认 deny**，要开必须人改 `kernel`；
- 与 `traits`/`kernel` 一样是**单写者**：只有收束点能写，`concurrent` 在场下各分支不得改。

## 3. `traits.json` 字段

| 块 | 字段 | 含义 |
|---|---|---|
| `traits.preferences[]` | `id` / `target` / `polarity`(like·dislike) / `weight` / `baseline` / `locked` | 性格偏好与权重 |
| `traits.reflexes[]` | `id` / `trigger` / `reaction` / `strength` | 反射式反应 |
| `memory.retention` | `hot_days` / `warm_days` / `prune_days` | 记忆分层保留 |
| `memory.context_budget` / `low_watermark` | 上下文预算、低水位比 | 检索与压缩阈值 |
| `evolution.*` | `min_evidence` / `max_step` / `weekly_cap` | 漂移单步与周额度（**历史口径**：现行见 `PHILOSOPHY` §10.1——重载即回弹，不锁性格） |
| `evolution.polarity_flip` | `dead_zone` / `min_reverse_evidence` / `requires_human` | 极性翻转闸门 |
| `evolution.anti_rigidity` | 6 个参数 | 抗僵化 |
| `evolution.kernel_locked` / `audit` | 内核锁定、审计开关 | 人改 |

**权重规则**：只在结算点改，单步 ≤ `max_step`，每周 ≤ `weekly_cap`；低于 `dead_zone` 且反向证据
≥ `min_reverse_evidence` → **极性翻转待确认**；`requires_human=true` 必须人点头。

## 3.4 `state.json` 字段（与 `impl/m1` 实际结构一致）

```jsonc
{ "mood": { "愉悦": 0.0, "烦躁": 0.0, "专注": 0.0, "疲惫": 0.0 }, "updated_at": "<ISO8601>" }
```

- 情绪四维范围 **-1..+1**，分钟级衰减；小时级应激见 `impl/m1` 的 `stress_offset`；
- 在场模型（`exclusive` / `concurrent`）与当前指针由 Runtime 持有，随在场转移，**不写进 kernel**；
- 可实时写、可回滚；崩溃后以 `state.json` 为准，数据库 `runtime_state` 为镜像。

## 4. 记忆库（`memory.db`）结构

最小 schema 见 [`examples/schema.sql`](../examples/schema.sql)，要点：

| 表 | 作用 |
|---|---|
| `episodes` | 原始情景（含 `role` / `content` / `summary` / `importance` / `occurred_at` / `source_msg_id`） |
| `episodes_fts` | 全文检索（`content` + `summary`） |
| `facts` | 语义事实（subject / predicate / object / 有效期） |
| `entities` / `edges` | 实体与关系图（含关注度衰减） |
| `persona_traits` / `persona_drift_log` | 性格权重与每次漂移的审计 |
| `embeddings` | 向量（目标 + 模型 + 内容哈希 + 模长） |
| `audit_log` / `snapshots` | 审计与快照（`pre_consolidate` / `pre_migrate`） |

### 4.1 新增：`episodes.memory_type`

```sql
ALTER TABLE episodes ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episode';
-- 取值：episode（普通对话）| persona_conflict（拒绝/冲突）| action（工具调用）
```

- 默认 `'episode'`，旧数据无需回填（等值）；
- 检索默认只返回 `episode` + `persona_conflict`；`action` 需显式索取（避免工具流水淹没正文）。

## 5. 冲突落库（`persona_conflict`）

> `PHILOSOPHY.md` §8.2 定义了"必须拒绝"，这里定义**拒绝之后写在哪里、怎么被查到**。

**写入规则**：拒绝发生时，**追加**一条 `episodes` 行，绝不覆盖已有记忆。

| 列 | 值 |
|---|---|
| `role` | `system` |
| `memory_type` | `persona_conflict` |
| `content` | JSON：`{"refused":true,"clause":"<被触发的内核条款>","instruction":"<指令摘要>","decided":"refused","at":"<ISO8601>"}` |
| `summary` | 一句人话：「因 <条款> 拒绝了 <指令摘要>」 |
| `importance` | `0.9`（高于普通对话，防止被低水位优先驱逐） |

**同时**在 `audit_log` 记 `action='refuse'`，`detail` 同上 JSON。

**检索**：走 `episodes_fts`（与普通记忆同一入口），因此**任何检索工具都能查到"它拒绝过什么"**——
这是拒绝权的可验证性，不是日志摆设。

**边界**：`persona_conflict` 行不参与 consolidate 合并、不被极性翻转消费；软删需人确认（P1）。

## 6. 写入权限与时机

| 动作 | 谁 | 时机 | 说明 |
|---|---|---|---|
| 改 `kernel` 任意字段（含 `permissions`） | 人 | 随时，走特殊流程 | AI 只给 diff 与理由 |
| 改 `traits.preferences[].weight` | 结算线 | 睡眠 / consolidate | 有额度与闸门 |
| 改 `traits.evolution.*` | 人 | 随时 | 视同人格参数 |
| 写 `state` | 运行时 | 实时 | 可回滚 |
| 追加 / 清理 `memory` | 运行时 | 实时 / 维护 | 清理前必须有可检索替代物 |
| 写 `persona_conflict` | 运行时 | 拒绝发生时 | 追加，不覆盖 |

## 7. 变更流程

1. **人格（kernel）变更**：人提出 → AI 给 diff + 理由 → 人点头 → 改文件 → 包/记忆跟着变。对话里随口说不生效。
2. **性格（traits 权重）变更**：走结算线；极性翻转必须过闸门。
3. **拒绝**：指令与 kernel 严重冲突时，AI 必须拒绝并留痕（§5），而不是静默执行或假装执行。

## 8. 版本化

- `schema` 标记文件结构版本（当前 `syspersona/persona@2`）；结构不兼容升主版本；
- `version` 标记人格内容版本（如 `0.3.0-m1`）；内容变更不升 schema；
- 旧文件必须能被新加载器读入（缺省补全），新文件不保证被旧加载器读入——有意的单向兼容。

## 9. 现状与缺口

- 本仓库**没有正式人格文件**，只有 `examples/persona.m1.json` 与记忆库 `整理/_sim-20261108/persona/*.json`；
- 模拟件**不要改**：改了会让 `impl/m1` 一致性测试失去意义；
- 待补：正式人格的存放位置、`vault` 加密接入、跨设备迁移时的 kernel 校验流程；
- `permissions` 与 `episodes.memory_type` 规范先行；**自 v4.2.0 起 `impl/m1` 已实现**（`decide_capability` / `record_refusal`，自测 `impl/m1/test_policy.py`）。
- 仍未实现：工具执行层的拦截与沙箱（`LAUNCHER_SPEC` §11），本次只有裁决与留痕。

## 9.1 正式人格的存放与迁移（规范先行，实现部分已就绪）

> 目的：解决"正式人格放哪、怎么加密、怎么搬家"——这三件事此前只有 `impl/` 的代码，没有规范。

### 存放

```
<personaRoot>/                 默认 %LOCALAPPDATA%\SysPersona\persona（可配置）
├─ kernel.json                 人改（P1）
├─ traits.json                 结算点写
├─ state.json                  运行时实时写
├─ memory.db                   记忆（SQLite，WAL）
└─ snapshots/                  pre_consolidate / pre_migrate 快照
```

- **禁止**把 `memory.db` 放在 SMB / NFS / OneDrive：SQLite 锁在那里不成立，会静默损坏；
- 仓库与记忆库里都**不放正式人格**，只放示例与模拟件。

### 加密（静态封存）

- 静止时是单个 `*.vault`（`seal` 封装，`unlock` 解封）；
- 只做**静态封存**，不是每轮加密落盘（KDF 实测 1.6 s，聊天会被 I/O 拖死）；
- 口令丢失只能靠 24 词恢复短语；两者都丢 = 人格丢失，这是设计上的取舍，不设后门。

### 迁移

1. `export` 导出迁移包 → `verify` 校验 manifest 与逐文件 sha256；
2. 目标机 `import` → 校验 `persona_uuid` 与 `kernel` 内容哈希；
3. **kernel 在迁移中变更必须显式批准**（`seal --approve-kernel-change`），否则拒绝加载；
4. 迁移完成后 `snapshots/pre_migrate` 保留至少一份。

### 边界

- 人格**不可合并**：迁移是整体搬运，不是合并（`PHILOSOPHY` §5）；
- 多设备共享走 API，不共享文件（`memory.db` 的锁问题）；
- 本节的 `personaRoot` 默认值尚未写进 `impl/` 的默认参数，属**规范先行**。

## 10. 修订记录

| 日期 | 版本 | 改了什么 | 为什么 |
|---|---|---|---|
| 2026-11-08 | v1.0 | 首次成文：三件套、kernel/traits 字段、写入权限、变更流程、版本化、缺口 | 此前"性格"只存在于示例与模拟件 |
| 2026-11-08 | v1.1 | 补 `permissions` 定义、`state.json` 字段表、`memory.db` 结构、`episodes.memory_type`、**§5 冲突落库** | "拒绝"此前只有哲学定义，没有落库与检索路径 |
