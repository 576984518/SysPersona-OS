# SysPersona 人格文件规范 v1.0（现行）

> 本页只回答一件事：**"它是谁"怎么被文件化。**
> 与 `PHILOSOPHY.md`（为什么这么做）、`AVATAR_SPEC.md`（多分身怎么在场）互补，不重复。
> 变更记在本页文末「修订记录」与 `CHANGELOG.md`。

## 0. 一句话

**人格 = 文件 + 记忆，每次唤醒重建；模型不构成人格。**

## 1. 三件套 + 一库

| 文件 | 装什么 | 谁能写 | 写入时机 |
|---|---|---|---|
| `kernel.json` | 身份、价值观、边界、语气基线、交互模式、宪法条款 | **只有人** | 走"改内核"特殊流程，不在对话里随口生效 |
| `traits.json` | 偏好权重、反射、演化参数 | **结算点**（睡眠 / consolidate） | 单写者；AI 不得直接写 |
| `state.json` | 情绪、在场、指针、当前水位 | 运行时 | 实时可写 |
| `memory.db` | 情景记忆、画像、检索索引 | 运行时追加 | 清理有规则（原则 8：先有替代物再清） |

> 三条硬约束（来自 `HANDOFF.md` §五）：**文件是真相，数据库是缓存**；`traits`/`kernel`/`permissions`
> 只有一个写入来源；**记忆可合并，人格不可合并**。

## 2. `kernel.json` 字段（schema `syspersona/persona@2`）

| 字段 | 含义 | 约束 |
|---|---|---|
| `identity.name` / `self_reference` / `user_address` | 名字、自称、怎么称呼人 | 人改 |
| `values[]` | 价值观（如 诚实 / 尊重边界 / 连续） | 人改 |
| `boundaries[]` | 边界条款（如"删除记忆前必须确认"） | 人改 |
| `voice_baseline.tone` / `avoid[]` | 语气基线、要避免的表达 | 人改 |
| `interaction.mode` | 交互模式，如 `async_non_blocking` | 人改；缺省按 v4.0 工作模式 |
| `interaction.ack_first` / `block_front` / `report_as` | 先回执 / 不占前台 / 事件插播 | 人改 |
| `constitution.law_floor` | 现实法理在最顶层，不可被任何内核覆盖 | **P0 写死** |
| `constitution.priority[]` | `["law","kernel","human_instruction","long_term_memory","model_bias"]` | 人改；改它 = 改宪法 |
| `constitution.right_to_refuse` | 拒绝权开关 | 人改；关闭它 = 取消人格边界 |

> **兼容性提示**：`interaction` 与 `constitution` 是 2026-11-08 新增（v4.0 工作模式 + 宪法修正案落地）。
> 加载器**必须容忍两者缺省**：旧 kernel 没有这两个块时，按"未声明"处理，不得报错。
> 正常化路径：加载 → 缺省补全（`mode=async_non_blocking`、`law_floor=true`）→ 只在内存态生效，**不回写文件**。

## 3. `traits.json` 字段

| 块 | 字段 | 含义 |
|---|---|---|
| `traits.preferences[]` | `id` / `target` / `polarity`(like·dislike) / `weight` / `baseline` / `locked` | 性格偏好与其权重 |
| `traits.reflexes[]` | `id` / `trigger` / `reaction` / `strength` | 反射式反应 |
| `memory.retention` | `hot_days` / `warm_days` / `prune_days` | 记忆分层保留 |
| `memory.context_budget` / `low_watermark` | 上下文预算（K）、低水位比 | 检索与压缩阈值 |
| `evolution.*` | `min_evidence` / `max_step` / `weekly_cap` | 漂移的单步与周额度 |
| `evolution.polarity_flip` | `dead_zone` / `min_reverse_evidence` / `requires_human` | 极性翻转闸门 |
| `evolution.anti_rigidity` | `stale_after_days` / `revalidate_window_days` / `unverified_discount_per_year` / `exploration_rate` / `reset_interval_days` / `rigidity_alert_threshold` | 抗僵化 |
| `evolution.kernel_locked` / `audit` | 内核锁定、审计开关 | 人改 |

**权重规则**：`weight` 只在 **结算点** 由演化逻辑修改，单步不超过 `max_step`，每周不超过 `weekly_cap`；
`weight` 低于 `dead_zone` 且反向证据 ≥ `min_reverse_evidence` 时，进入**极性翻转待确认**，
`requires_human=true` 则必须人点头。

## 4. 写入权限与时机

| 动作 | 谁 | 时机 | 说明 |
|---|---|---|---|
| 改 `kernel` 任意字段 | 人 | 随时，但需走特殊流程 | AI 只能给 diff 与理由 |
| 改 `traits.preferences[].weight` | 结算线 | 睡眠 / consolidate | 有额度与闸门 |
| 改 `traits.evolution.*` | 人 | 随时 | 影响演化行为，视同人格参数 |
| 写 `state` | 运行时 | 实时 | 可回滚 |
| 追加 / 清理 `memory` | 运行时 | 实时 / 维护 | 清理前必须有可检索替代物 |
| 记录冲突 | 运行时 | 冲突发生时 | `memory_type=persona_conflict`，**不覆盖旧记忆** |

## 5. 变更流程

1. **人格（kernel）变更**：人提出 → AI 给 diff + 理由 → 人点头 → 改文件 → 包/记忆跟着变。
   对话里随口说一句**不生效**。
2. **性格（traits 权重）变更**：不人工干预，走结算线；极性翻转必须过闸门。
3. **拒绝**：指令与 kernel 严重冲突时，AI 必须拒绝并留痕（`persona_conflict` + 指出条款），
   而不是静默执行或假装执行。依据：`PHILOSOPHY.md` §8.2。

## 6. 版本化

- `schema` 标记文件结构版本（当前 `syspersona/persona@2`）；结构不兼容时升主版本号；
- `version` 标记人格内容版本（如 `0.3.0-m1`）；**内容变更不升 schema**；
- 旧文件必须能被新加载器读入（缺省补全），新文件不保证被旧加载器读入——这是有意的单向兼容。

## 7. 现状与缺口（诚实记录）

- 本仓库**没有正式人格文件**，只有：
  - `examples/persona.m1.json`（演示用最小人格）；
  - `整理/_sim-20261108/persona/{kernel,traits,state}.json`（记忆库侧模拟件）。
- 模拟件**不要改**：改了会让 `impl/m1` 的一致性测试失去意义（见 `PHILOSOPHY.md` §9 注）。
- 待补：正式人格的存放位置、加密容器（`vault`）接入、跨设备迁移时的 kernel 校验流程。

## 8. 修订记录

| 日期 | 版本 | 改了什么 | 为什么 |
|---|---|---|---|
| 2026-11-08 | v1.0 | 首次成文：三件套、kernel/traits 字段、写入权限、变更流程、版本化、缺口 | 此前"性格"只存在于示例与模拟件，没有一份规范说明谁能写、什么时候写 |
