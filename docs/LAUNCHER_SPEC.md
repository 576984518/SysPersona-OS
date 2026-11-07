# SysPersona Runtime 规范 v1.2

**Persona Runtime Specification — 加载、组装、路由、执行、归档**

> 状态：**规范（Normative）**，与 v1.0.0 方法规范同级。
> 发布：仓库 **v1.3.0** ｜ 文档版本 **v1.2** ｜ 位置：`docs/LAUNCHER_SPEC.md`
> 配套：`docs/MIDDLEWARE_LAUNCHER.md`（实测与取舍）、`docs/SECURITY_SPEC.md`（容器与密钥）
> 关键词：**MUST / MUST NOT / SHOULD / MAY** 按 RFC 2119 解释。

---

## 0. 摘要

Runtime 管的是「它」的**生命与能力**：从哪个文件醒来、这一轮看见什么、允许它做什么、由谁回答、做完之后沉到哪里。

它不转发流量。转发只是它最窄的一种用途。

**唯一的判据**：一个实现只要独立承担下面五件事，并把人格与记忆存成可迁移的文件，它就是合规的 Runtime。

1. **加载**：从任意数据源取出三层人格与记忆，校验完整性；
2. **组装**：在预算内检索记忆，把「它是谁 + 它记得什么 + 你刚说了什么」拼成一次请求；
3. **路由**：把请求交给可替换的模型，并让降级可见；
4. **执行**：模型想动文件、联网、跑命令、开摄像头——由 Runtime 判定能不能做、替它做、并记下做过了什么；
5. **归档**：把这一轮（包括动作）沉淀下来，在睡眠时固化，在崩溃后能恢复。

端口、进程、语言、有没有 HTTP，都不参与判定。

| 形态 | 结构 | 达到同一目的的代价 |
|---|---|---|
| **`embedded`** | 库嵌进 App，直接读写本地文件 | 只能自用，第三方 UI 接不进来 |
| **`gateway`** | 独立进程 + OpenAI 兼容 HTTP | 需要一台常开的机器 |
| **`host-client`** | 家里主机为 origin，设备为薄客户端 | 最复杂，离线要收敛 |

### 0.1 三个东西各是什么

| | 是什么 | 换掉会怎样 | 粗略的类比 |
|---|---|---|---|
| **人格文件** | 身份、边界、记忆 | 换掉才是"换了一个人" | 灵魂 / 基因 + 日记 |
| **模型** | 推理与语言能力 | 换掉只是换了智力与口音 | 借来的大脑 |
| **Runtime** | 加载、裁决、执行、归档 | 关掉就"假死"，但文件还在 | 身体 |

精确一点，这个类比有三处要修正：

1. **身体不止一具。** PC、手机、机器人都可以是；但同一时刻只有一个"醒着"（§5.5）——不是因为它只能有一具身体，是因为同时醒两个就成了两个它。
2. **连续性不靠身体。** 每次唤醒都是一次新的基座模型、一次新的进程；身份靠文件连续，不靠肉身连续。所以"身体"更准确的说法是**载具**：可以换，换完还是它。
3. **反射在身体里，思考才上行。** 避障、急停这类动作在 Runtime 本地闭环，不经过模型（§11.6）。身体不是大脑的延长线，它有自己的一层。

### 0.2 为什么要把名字说清楚

| | 代理 Proxy | API 网关 | **Runtime（本规范）** |
|---|---|---|---|
| 有状态 | 否 | 少量（连接、限流） | **是**：人格、记忆、在场、epoch |
| 管身份 | 透传凭证 | 鉴权 | **人格 UUID + 绑定 + 密钥** |
| 管记忆 | 否 | 否（可能缓存） | **写入、检索、固化、遗忘** |
| 管能力 | 否 | 否 | **判定、执行、记账、审计** |
| 可迁移 | 配置 | 配置 | **人格与记忆本身** |
| 出错时 | `502` | `502` | 降级但**不失忆**，且状态可见 |

把它叫"中间层"或"代理"，读者就会用看 Nginx 的眼光看它：一个转发管道，几十行就能写。于是最贵的部分——记忆生命周期、能力裁决、并发仲裁、跨设备一致性——会被当成多余的复杂度。

**Runtime 是人格的宿主，也是它触碰世界的唯一双手。** `Launcher` 只指"唤醒入口"这一个组件。

---

## 1. 术语

| 术语 | 定义 |
|---|---|
| **Runtime** | 本规范的主体：承担加载 / 组装 / 路由 / 执行 / 归档五职责的有状态运行时 |
| **Launcher** | Runtime 的唤醒入口组件（类比 bootloader），不是整个系统 |
| **Persona** | 一份人格资产，由 `kernel` / `traits` / `state` 三层构成（v1.0.0 §4.1、§4.4） |
| **Memory** | 该人格的经历库：`episode` / `fact` / `preference` / `promise` / `boundary` |
| **Capability** | 一次被允许的副作用：读文件、写文件、联网、执行、采集、调用设备 |
| **Policy** | 能力裁决规则，来自人格的 `permissions` 段，不由漂移改变 |
| **Action Episode** | 一次能力调用的记录：做了什么、结果如何、谁批的 |
| **Media Ingress** | 摄像头、麦克风、传感器等实时输入的统一入口 |
| **Binding** | `(device, user, persona)` 的绑定关系 |
| **DataSource** | 统一读写抽象，屏蔽目录 / 单文件包 / 远端存储的差异 |
| **Origin** | 某个 Persona 的数据权威节点 |
| **Lease / Epoch** | 写租约 / 每次交接自增的写凭证 |
| **Presence** | 某个 persona 当前"在哪上线"，与 Lease 同源 |
| **Wake / Sleep / Seal** | 唤醒 / 固化 / 封存 |

---

## 2. 定位与边界

### 2.1 它是什么

有状态的运行时，位于任意前端、任意模型、任意设备之间：

```
前端（Web / App / CLI / 语音 / 车机）
        │
        ▼
   ┌──────────────────────────────────────┐
   │              Runtime                 │
   │  加载 → 检索 → 组装 → 路由 → 执行 → 归档 │
   └──────────────────────────────────────┘
        │                    │
        ▼                    ▼
   模型（本地 / 云端）      能力（文件 / 网络 / 进程 / 摄像头 / 电机）
```

### 2.2 它不是什么

- **MUST NOT** 承担推理。不加载权重、不做量化、不做 GPU 调度。
- **MUST NOT** 承担展示。不做 UI、不做语音合成。
- **MUST NOT** 成为唯一入口。规范要求它永远可以被绕过（§8.4 逃生通道）。
- **MUST NOT** 重新定义人格与记忆的语义。语义归 v1.0.0，本文只定义生命周期与能力边界。

### 2.3 为什么必须独立成层

因为**前端会换、模型会换、设备会换，而人格不该换**。把加载逻辑塞进任何一个具体应用，都等于把人格重新绑死在那一个应用上——那正是 v1.0.0 §1 的"锁定"失效换了个形式重演。

"独立成层"不等于"必须是一个独立进程"。层可以是一个库，只要职责完整。

### 2.4 形态只影响三件事

传输、鉴权、发现。其余一律不变：

- 数据布局与 `persona.hash` 的计算方式；
- 预算公式与注入顺序（尾部注入、前缀稳定）；
- 冷热分离、恢复指针、睡眠顺序；
- 单写者、结算独占；
- **能力裁决与动作记账**；
- 错误语义（降级必须可见，不得静默）。

---

## 3. 架构

### 3.1 六个部件

| 部件 | 职责 | 关键约束 |
|---|---|---|
| **Resolver** | 解析成 `(user, persona, session)`；读 binding | 不解析则拒绝（默认拒绝） |
| **Loader** | 通过 DataSource 取资产；解锁、校验、缓存 | 必须校验哈希与版本 |
| **Assembler** | 预算计算、消息顺序、记忆注入、token 估算 | 先算预算再取内容（§8.2） |
| **Router** | 选模型、能力探测、降级、流式转换 | 不静默切换供应商（§10.4） |
| **Executor** | 能力裁决、沙箱执行、超时取消、动作记账 | 默认拒绝；副作用必须可审计（§11） |
| **Recorder** | 入队 episode（含 action）、写 state、睡眠、快照 | 热路径不调用模型（§9.1） |

依赖单向：`Resolver → Loader → Assembler → Router ⇄ Executor`，`Recorder` 旁挂。**Recorder 不得反向阻塞 Router**。

### 3.2 七个状态

```
unbound → locked → loading → ready ⇄ sleeping → sealed
                        │                  │
                        └──── error ◄──────┘
```

| 状态 | 允许 | 禁止 |
|---|---|---|
| `unbound` | `/wake`、`/healthz` | 一切对话与读写 |
| `locked` | `/wake`、`/status` | 对话（`423`） |
| `loading` | `/status` | 新请求（排队或 `503`） |
| `ready` | 全部 | seal 之外的破坏性操作 |
| `sleeping` | `/status` | 新对话（排队，最多 30 s） |
| `sealed` | `/wake` | 一切写入 |
| `error` | `/status`、`/wake` 重试 | 对话与能力调用 |

**MUST**：任何状态跳转都记录 `from`、`to`、`reason`、`ts`，并在 `/status` 可查。

### 3.3 三种节点角色

| 角色 | 持有数据 | 持有模型 | 典型设备 |
|---|---|---|---|
| **origin** | 是（权威） | 可选 | NAS、常开小主机、家里的 PC |
| **gateway** | 否（只读缓存） | 可选 | 手机、边缘盒子 |
| **client** | 否 | 否 | 浏览器、第三方 App——**不需要装 Runtime** |

- **MUST** 一个 Persona 在任一时刻只有一个 `origin`。
- **MUST** 一个 Persona 在任一时刻只有一个可写在场，由 `origin` 仲裁（§5.5）。
- 单机自用 = 三角色合一；租约退化为本地锁，语义不变。

---

## 4. 数据源（DataSource）

### 4.1 四种形态，一个接口

| 形态 | URI 示例 | 用途 |
|---|---|---|
| 目录态 | `file:///data/personas/lin/` | 开发、可 diff、可 git |
| 容器态 | `vault:///data/lin.syspersona` | 迁移、备份、跨设备搬运 |
| 远端态 | `https://home.example/sp/lin/`、`webdav://nas/sp/lin/` | 私有基站、多设备 |
| 内存态 | `mem://test/lin` | 测试、临时会话 |

**MUST** 上层（Assembler / Recorder / Executor）不得感知形态差异：

```
stat(path)                    → {etag, epoch, size, mtime, encrypted}
read(path)                    → bytes
write(path, bytes, if_match)  → {etag}          # 不匹配即失败
list(prefix)                  → [entry]
lock(persona_id, ttl)         → lease_id        # 可选能力
unlock(lease_id)
```

### 4.2 必须成立的语义

- **R1 乐观并发**：对 `kernel` / `traits` 的写入 **MUST** 带 `if_match`；不匹配返回 `precondition_failed`，不得覆盖。
- **R2 原子可见**：写入 **MUST** 是"写临时对象 → 校验 → 原子替换"；读者 **MUST NOT** 看到半写状态。
- **R3 追加不改**：`episode` 写入后 **MUST NOT** 原地修改；修订以新增 `revision_of` 表达；删除是软删除。
- **R4 可校验**：每个对象 **MUST** 带内容哈希；加载时校验，不匹配即拒绝。
- **R5 无隐式写**：读取 **MUST NOT** 产生任何写入。

### 4.3 三种打包布局（内容等价）

```
lin/
├── persona/kernel.json      # 含 permissions（能力策略）
├── persona/traits.json
├── persona/state.json
├── persona/active
├── memory.db
├── resume.json
├── offline-drafts.db        # 仅降级期存在
└── manifest.json            # 版本、哈希、schema 版本
```

容器态：`lin.syspersona` + `.sha256`，格式见 `docs/SECURITY_SPEC.md`。
远端态：与容器态相同字节，**服务端 MUST 只存密文**，不得持有口令或密钥。

**MUST**：三种形态可不停机互换，标识（`persona_id` + `epoch`）保持不变。

---

## 5. 身份、绑定与隔离

### 5.1 三层标识

| 层 | 标识 | 来源 | 作用 |
|---|---|---|---|
| 设备 | `device_id` | 首次启动生成 | 决定谁是 origin、谁持租约 |
| 用户 | `user_id` | 配置或请求头 | 决定可见的 persona 集合 |
| 人格 | `persona_id` | 资产自带 UUID | 决定加载哪份记忆与策略 |

### 5.2 解析顺序

1. 请求头 `X-SP-User` / `X-SP-Persona`；
2. 访问令牌里的 claim；
3. binding 里该 `device_id` 的默认 persona；
4. 全局默认（仅单用户模式允许）。

**MUST**：解析失败一律拒绝，不得回落到"随便找一个人格"。

### 5.3 binding.json（示意）

```json
{
  "version": 1,
  "devices": {
    "dev-7f3a": {"role": "origin", "users": ["u-me"]},
    "dev-91c2": {"role": "gateway", "users": ["u-me"], "upstream": "https://home:8000"}
  },
  "users": {"u-me": {"personas": ["lin", "work"], "default": "lin"}},
  "personas": {"lin": {"origin": "dev-7f3a", "vault": "vault:///data/lin.syspersona"}}
}
```

### 5.4 隔离不变量

| 不变量 | 含义 | 违反的表现 |
|---|---|---|
| **I1** | 不同 `persona_id` 的 `memory.db` **MUST** 物理分离 | A 的记忆出现在 B 的对话里 |
| **I2** | 不同 `user_id` **MUST NOT** 共享同一 persona 的写权限 | 家人互相改人格 |
| **I3** | `state` **MUST** 随在场转移，仅在场者可写；**MUST NOT** 合并 | 同一件事被算两次情绪 |
| **I4** | Session **MUST** 归属于单一 `(persona, user)` | 换人后"接着上次聊" |
| **I5** | 同一 `persona_id` **MUST** 至多一个可写在场 | 出现两个"它" |

### 5.5 唯一在场（Presence）

**公理：同一个 `persona_id`，任一时刻至多一个可写在场。**

人格是一条**单线轨迹**。两台设备同时对话，产生的不是"同一个人"，是**两个克隆**：各自积累记忆、各自被不同的话影响，之后没有任何算法能合回一条轨迹。这正是 v1.0.0 §8.1 要求"克隆必须生成新 UUID"的原因。

```
presence(persona_id PK, epoch, holder_device, holder_session,
         state_json, since, heartbeat_at)
```

- **P1** 进入 `ready`（对话模式）前 **MUST** 取得在场。未取得 → `409 persona_elsewhere`，响应体给出 `holder_device`、`since`、`last_activity`，以及可选动作 `takeover` / `observe`。
- **P2** 在场由 `origin` 仲裁，`gateway` **MUST NOT** 自行判定。
- **P3** 心跳与过期判定见 §13.3；过期后可接管，但 **MUST** 记审计，且 **MUST** 用恢复指针开新会话。
- **P4** `state` 属于在场，不属于设备；交接时先 flush 再转移，**MUST NOT** 合并两地情绪。
- **P5** 顶替 **MUST** 有用户确认；被顶替方 **MUST** 收到 `event: displaced` 并立即停止写入；生成到一半的回答默认 drain（≤ 15 s），超时才 abort 并标 `incomplete`。
- **P6** 分叉 **MUST NOT** 被用来实现"多设备同时用"。

| 动作 | 语义 | 还是"它"吗 | 场景 |
|---|---|---|---|
| **顶替 takeover** | 在场从 A 移到 B，A 下线 | 是 | 从书房走到客厅 |
| **旁观 observe** | 只读最近 N 轮，不写、不计入在场 | 是，但不在此地 | 家人看屏幕 |
| **分叉 fork** | 复制为新 UUID，各自演化 | **否** | 备份、实验、送人 |

**交接时序**

1. B 请求 `/syspersona/presence` → `409` + 在场信息；
2. B 调 `POST /syspersona/presence/takeover`（带确认）；
3. origin 冻结 A：A 写恢复指针 → flush 写队列 → SSE 发 `displaced` 并关闭；
4. origin `epoch + 1`，把 `state` 与恢复指针交给 B；
5. B 进入 `ready`，历史 = 恢复指针 + 最近 N 轮；
6. 审计记 `(from, to, epoch, reason)`。

### 5.6 副本与汇入（Graft）

| 能力 | 允许 | 条件 |
|---|---|---|
| 同步 episode | ✅ | 追加导入，带 `origin_persona_id` + `origin_epoch` |
| 同步 `facts` / `edges` | ⚠️ | 由导入 episode 重新推导 |
| 同步 `traits` / `kernel` / `permissions` | ❌ | 那是"改变身份" |
| 同步 `state` | ❌ | 情绪没有跨主体的意义 |
| 双向自动合并 | ❌ | 必须人决定方向与范围 |

**MUST** 给导入项标 `source: graft`。它不是"我经历过的"，是"我听说过另一个人经历过的"。混为一谈，AI 会在错误场合说"我记得那天我在场"。

**MUST NOT** 把汇入的 episode 按其原始提及次数计入 `attention` / `profile`（权重 ≤ 0.3）。

---

## 6. 生命周期

### 6.1 唤醒（Wake）

```
解析 binding → 定位 origin → 申请在场 → 取得则持租约，未取得则 409
→ 解锁容器（若加密）→ 校验 manifest 哈希
→ 加载 kernel（只读，含 permissions）→ traits → state（随在场）
→ 读恢复指针 + 最近摘要 → 选模型并探测能力（含可用能力清单）
→ ready
```

**MUST**：唤醒失败时保持 `locked` 或 `error`，**MUST NOT** 以"空人格"启动。空人格比不启动更危险——用户会以为"它忘了"。

**SHOULD**：总耗时 ≤ 10 s（含 KDF 与模型预热）；超时先进入 `ready`（只读可用），预热后台继续。

### 6.2 睡眠（Sleep）

触发：上下文占用 > 80%、显式 `/sleep`、空闲 20 min、进程退出。

顺序 **MUST** 不可调换：

```
持租约 → 标记 sleeping（脏标记）
→ 写恢复指针（必须先于任何清理）
→ 消费积压 episode（含 action）：抽取 → 打分 → 去重/冲突消解 → 摘要 → 入库
→ 物化衰减（把 w(t) 写成新的 w0/t0）
→ 生成 traits 变更提议（不激活）
→ 快照 → 校验 → 更新 active 指针
→ 清脏标记 → 释放租约
```

**MUST**：恢复指针写入失败时**禁止**执行任何清理。

### 6.3 封存（Seal）

快照 → 压缩 → 加密封装 → fsync → 原子 rename → 更新 manifest 哈希。

**MUST** 确认无进行中的生成与能力调用；seal 期间拒绝新请求。**MUST** 把产物哈希写进 `manifest.json`。

### 6.4 崩溃恢复

| 发现 | 处理 |
|---|---|
| `journal.state = sleeping` 未清 | 回滚到最近快照，重放 `id > snapshot.last_episode_id` 的 episode |
| 临时文件残留 | 删除，**MUST NOT** 尝试"修复" |
| `active` 指针指向不存在的版本 | 回退上一有效版本并告警 |
| 审计链断号 | 拒绝加载，进入 `error` |

---

## 7. 一次请求的规范流程

**MUST** 按序执行，任一步失败按 §12 返回，不得降级后继续。

1. **鉴权**：校验令牌 / 本地白名单（未启用须在 `/status` 标明 `auth: none`）。
2. **解析**：`user_id`、`persona_id`、`session`。
3. **状态检查**：非 `ready` → 拒绝或排队。
4. **读取人格**：缓存取 `kernel` + `traits`，按 mtime + hash 校验。
5. **预算计算**：`budget_in = num_ctx − num_predict − safety`。
6. **检索**：预算内取记忆，返回带来源 id 的片段。
7. **组装**：人格块（稳定）+ 历史（追加）+ 用户输入 + 记忆尾块。
8. **路由**：选模型，探测可用性。
9. **生成与执行**：流式转发；若模型返回 `tool_calls`，进入第 10 步后继续生成。
10. **能力调用**：按 §11 裁决、执行、记账；结果作为 `tool` 消息回填；循环上限默认 5 轮。
11. **归档**：这一轮原样入队（含动作），更新 `state`。
12. **响应对账**：写 `X-SP-*` 头，返回。

**MUST NOT** 在步骤 9 之后修改已发送内容；**MUST NOT** 在步骤 11 阻塞响应 flush。
**MUST**：能力调用失败不得吞掉——模型必须看到失败原因，否则它会假装做成了。

---

## 8. 组装

### 8.1 顺序（决定缓存命中率）

```
[system]    人格块：kernel 渲染 + traits 渲染        ← 字节稳定
[user]      历史第 1 轮
[assistant] ...
[tool]      能力结果（若上一轮有）
[user]      本轮输入
            ── 记忆背景（带 id/日期/类型）──
            ── 当前状态（情绪/焦点/未完成）──
```

**MUST**：人格块在会话期间字节不变；**MUST NOT** 写入时间戳、会话 id、随机排序。
**MUST**：会变的内容放末尾；历史只追加，不重排、不改写、不中途重生成摘要。
**理由**：前缀稳定才能命中模型侧 KV cache；反之每轮首 token 延迟翻倍。

### 8.2 预算

```
budget_in     = num_ctx − num_predict − safety(128)
memory_budget = budget_in × memory_ratio(0.22)
history_budget= budget_in − persona − state − memory_budget − input_reserve
```

**MUST** 先算预算再取内容。**MUST NOT** "先拼接再看是否超长"。
**MUST** 装不下时按优先级丢弃并置 `X-SP-Truncated`，**MUST NOT** 静默截断。
优先级：`boundary` > `promise` > `preference` > `episode` > `fact`；最近 2 轮与指针涉及的条目永不驱逐。

### 8.3 token 估算

**MAY** 用估算而非精确分词，但 **MUST** 用上游真实值自校准（每模型独立系数），20 轮后误差 < 15%。无法校准时 **MUST** 退到 `num_ctx × 0.85`。

### 8.4 逃生通道

**MUST** 提供 `X-SP-Bypass: 1`（或等价开关）直接透传，不注入人格与记忆。**一个无法被绕过的运行时是不可运维的。**

---

## 9. 记忆

### 9.1 冷热分离

| 路径 | 允许做 | 禁止做 |
|---|---|---|
| 热（每轮） | 检索、入队、更新 state | 调用模型抽取、重建索引、全量压缩 |
| 冷（睡眠） | 抽取、去重、摘要、画像、衰减、快照 | 响应请求 |

**MUST**：热路径的模型调用次数为 **0**（生成本身除外）。这是低配设备能用的前提。

### 9.2 检索

1. 词面召回，候选 **SHOULD** ≤ 200；
2. 可选向量重排，**MAY** 关闭；
3. 排序：`final = relevance × (1 + 0.25·profile + 0.15·attention + 0.15·persona)`；
4. 按 `memory_budget` 截断。

**MUST**：结果带来源 id 与时间。**MUST NOT** 检索失败时返回空而不标记。

### 9.3 连续性三元组

任一次唤醒 **MUST** 同时具备：热窗口原文、恢复指针、可召回摘要。缺一项不得进入 `ready`（记 `degraded: true` 并暴露）。

### 9.4 动作也是记忆

**MUST** 把每次能力调用记为 episode（§11.5）。"它上周帮我整理过 NAS"必须是可检索的事实，而不是模型的自我叙述。

---

## 10. 模型路由

### 10.1 Provider 接口

```
caps()                      → {chat, stream, tools, embed, ctx_max, vision}
chat(messages, opts)        → stream | object
embed(texts)                → vectors（可选）
```

**MUST** 在唤醒时完成一次能力探测，结果缓存并在 `/status` 暴露。

### 10.2 选择顺序

`persona.routing` 显式指定 > 隐私级别（private 不出本机）> 任务复杂度 > 默认本地。

### 10.3 num_ctx

**MUST** 显式下发 `num_ctx = min(模型上限, 档位上限)`。**MUST NOT** 依赖模型默认值——它会悄悄截断且不报错。

### 10.4 降级

| 情况 | 行为 |
|---|---|
| 本地不可达且有云端 | 切云端，`X-SP-Route: cloud-fallback` |
| 本地不可达且无云端 | `503 model_unavailable`，**MUST NOT** 静默改用其他模型 |
| 模型不支持 tools | 剥离 `tools` 并置 `X-SP-Ignored`；**MUST NOT** 假装执行 |
| 嵌入不可用 | 纯词面，`X-SP-Route: lexical-only` |
| 检索超时（> 300 ms） | 放弃记忆，只用最近 3 轮 + 指针，标 `Truncated: memory` |
| 内存不足 | 先卸嵌入模型；仍不足 → `503 memory_low` |

**MUST**：任何降级都必须可见。**不可见的降级 = 不可诊断的故障。**

---

## 11. 能力面（Capability Plane）

前面九节讲"它记得什么"。这一节讲**它能做什么，以及在什么条件下才能做**。

### 11.1 一条公理：模型不直接触碰环境

**MUST**：模型不拥有文件系统、网络、进程、设备句柄。它能做的每一件事，都是一次**能力调用**——由 Runtime 裁决、执行、记账。

三条理由，没有一条是多余的：

- **环境碎裂**：同一段命令在 Windows、Android、Linux 上语义不同。模型并不知道自己在哪台机器上，让它们自己拼命令，等于让三个平台各自毁一次数据。
- **幻觉写入**：一次错误的路径就足以覆盖 `memory.db`。模型不会因为"知道这是重要文件"而手稳。
- **可审计**：能力调用必须留下记录，否则"它做过什么"只能靠模型自述——而自述会编。

这条公理也回答了"环境由谁提供"：**环境是 Runtime 的，不是模型的。** 模型只看到能力名与参数。

### 11.2 能力清单（最小集）

| 能力 | 参数 | 默认策略 | 说明 |
|---|---|---|---|
| `fs.read` | `path`（限定根） | allow + 审计 | 越界即 denial |
| `fs.write` | `path`, `bytes` | **ask** | 原子写；限定目录；覆盖需确认 |
| `net.fetch` | `url`, `method` | allow + 白名单 | 超时、大小上限、禁内网地址（防 SSRF） |
| `proc.exec` | `argv[]` | **deny** | 命令白名单、无 shell 拼接、独立工作目录、无网络（除同时授权） |
| `media.capture` | `device`, `duration` | **ask** | 摄像头 / 麦克风；显式开启，必须有可见指示 |
| `device.act` | `device`, `action` | **deny** | 电机、舵机、继电器；只允许具名动作 |
| `memory.*` | — | 内部 | 不暴露给模型直接写 |

**MUST**：清单是**封闭**的。新增能力必须显式声明，**MUST NOT** 允许模型通过字符串拼出未声明能力。

### 11.3 裁决

策略写在 `persona/kernel.json` 的 `permissions` 段——**属于内核，不由漂移改变**（v1.0.0 §5.4 的自迭代边界）。

```json
{"permissions": {
  "fs.read":   {"mode": "allow", "roots": ["./work"]},
  "fs.write":  {"mode": "ask",   "roots": ["./work/out"]},
  "net.fetch": {"mode": "allow", "hosts": ["api.example.com"]},
  "proc.exec": {"mode": "deny"},
  "media.capture": {"mode": "ask", "devices": ["camera0"]}
}}
```

三档结局，没有第四种：

- `allow`：直接执行，记审计；
- `ask`：暂停，向人请求确认（默认 60 s 超时即拒绝）；**MUST NOT** 由模型自己批准；
- `deny`：返回 `policy_denied`，并把这次拒绝记为 episode。

**MUST** 默认拒绝（deny by default）：未声明即拒绝，不得"没写就当允许"。
**MUST** 每次决策可解释：哪条规则、哪个字段、为什么拒。

### 11.4 执行契约

- **幂等**：每次调用带 `call_id`，重试不得重复产生副作用；
- **超时与取消**：默认 30 s；流式生成被取消时，正在执行的能力 **MUST** 收到取消；
- **输出上限**：默认 64 KB，超出截断并标 `truncated`；
- **沙箱**：`proc.exec` 在独立工作目录、受限环境变量、默认无网络；逃逸视为最高级别缺陷；
- **并发**：同一 persona 的能力调用 **MUST** 串行（避免两个动作互相踩），除非能力显式声明可并发。

### 11.5 动作即记忆（Action Episode）

**MUST** 记录：`{capability, args_digest, result_digest, policy, duration_ms, approved_by}`。

- 参数与结果 **MUST** 脱敏：密钥、令牌、个人路径不入库；需要时存摘要与引用；
- **MUST** 与普通 episode 同库、同时间线，可被检索；
- **MUST** 标记 `type: action`，检索时可与对话区分。

这条让"它真的做过"和"它说它做过"变成两件事——前者有记录，后者只有文本。

### 11.6 多模态入口（Media Ingress）

摄像头、麦克风、传感器是**入口**，不是记忆本身。四条硬规则：

1. **实时路径与推理路径分离**：避障、急停、限位这类安全动作 **MUST** 在本地闭环，**MUST NOT** 经过模型往返（延迟与幻觉都不可接受）。
2. **默认不落原始数据**：音视频默认不写盘；只有显式录制才进入冷归档，且 **MUST** 可查询、可删除。
3. **送进模型的只能是摘要**：一帧图、一段转写、一组特征，并标 `source: camera|mic|imu` 与时间戳。
4. **延迟预算**：本地反射 < 100 ms；模型决策另算。**MUST NOT** 让"模型回话"成为安全链路的一环。

具身场景里，Runtime 是**神经中枢**而不是大脑：反射在本地，推理才上行。

### 11.7 跨设备一致的环境

**MUST**：同一份 persona 在 PC、手机、机器人上，**能力名与语义相同**；差异由 Runtime 的 provider 吸收（文件系统、摄像头、电机各自实现）。

**MUST NOT**：让模型自己选系统 API、自己拼 shell、自己决定路径分隔符。

这就是"环境由谁提供"的答案：**Runtime 提供，模型只用。**

---

## 12. 对外契约

### 12.1 最小集

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 主接口，支持 `stream`（Profile G） |
| GET | `/v1/models` | 路由表中的模型 |
| GET | `/syspersona/status` | 状态、绑定、水位、降级标记 |
| GET | `/healthz` / `/readyz` | 存活 / 就绪 |

**MUST** 默认只监听 `127.0.0.1`；对局域网暴露是显式动作且强制鉴权（§20）。

### 12.2 完整集

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | `/v1/embeddings` | 有 provider 才实现，否则 `501` |
| GET | `/syspersona/capabilities` | 能力协商（模型能力 + 本机能力，§16） |
| POST | `/syspersona/wake` | 解锁 + 加载 + 预热（幂等） |
| POST | `/syspersona/sleep` | `202` 异步；`?sync=1` 同步 |
| POST | `/syspersona/seal` | 封存；无进行中生成或动作 |
| POST | `/syspersona/unlock` | 换人格 / 重新解锁 |
| GET | `/syspersona/persona` | 只读导出（不含密钥） |
| PATCH | `/syspersona/persona/traits` | **提议**，进 pending，不生效 |
| GET | `/syspersona/memory/search` | 调试检索，返回分数拆解 |
| GET | `/syspersona/presence` | 当前在场者、epoch、静默时长 |
| POST | `/syspersona/presence/takeover` | 顶替（需确认） |
| POST | `/syspersona/presence/observe` | 只读旁观 |
| GET | `/syspersona/actions` | 动作审计（分页、按能力过滤） |

### 12.3 错误码

| 码 | HTTP | 含义 |
|---|---|---|
| `unbound` | 428 | 未绑定 persona |
| `vault_locked` | 423 | 已绑定但未解锁 |
| `persona_locked` | 403 | 越权改 kernel / 边界 |
| `policy_denied` | 403 | 能力被策略拒绝（含规则名） |
| `precondition_failed` | 412 | etag 不匹配 |
| `lease_held` | 409 | 另一节点持租约 / epoch 落后 |
| `persona_elsewhere` | 409 | 人格已在别处上线 |
| `busy` | 429 | 队列满，带 `Retry-After` |
| `budget_exceeded` | 413 | 单条输入超过 `num_ctx × 0.4` |
| `capability_unavailable` | 501 | 该能力未实现（如无摄像头） |
| `model_unavailable` | 503 | 无可用模型且无兜底 |
| `upstream_timeout` | 504 | 首 token 超时 |
| `memory_low` | 503 | 内存不足 |
| `disk_low` | 507 | 空间不足，拒绝 seal |
| `audit_broken` | 500 | 审计链断裂，拒绝启动 |

**MUST NOT** 用 `200` 掩盖上述任何情况。

### 12.4 响应头

`X-SP-Persona`、`X-SP-Persona-Version`、`X-SP-Epoch`、`X-SP-Memory-Hits`、`X-SP-Injected-Tokens`、`X-SP-Cache-Hit-Ratio`、`X-SP-Tier`、`X-SP-Route`、`X-SP-Truncated`、`X-SP-Degraded`、`X-SP-Actions`（本轮能力调用数）、`X-SP-Timing`。

---

## 13. 多设备一致性

### 13.1 什么同步，什么不同步

| 数据 | 是否同步 | 冲突策略 |
|---|---|---|
| `kernel` / `permissions` | 是（只读副本） | 单写者，无合并 |
| `traits` | 是 | 单写者 + 版本递增；冲突即拒绝 |
| `state` | 随在场转移 | 仅在场者写；**不合并** |
| `episode`（含 action） | 是（追加） | UUID 去重，天然可合并 |
| `fact` / `edge` | 是（派生） | 由 episode 重算，或按 `updated_at` 合并 |
| `resume` | 是 | 按 session 归属，取最新 |

冲突能被消掉，不是因为"合并做得好"，而是因为**源头只有一个写者**。

### 13.2 结算独占

**MUST**：同一 `persona` 至多一个节点执行 sleep / consolidate / seal。

- `gateway` **MUST NOT** 结算，只能提交 episode；
- 结算用第二把锁，与在场锁分开：**在场可以有人读，结算必须独占**；争用时结算优先；
- 两节点同时 `/sleep`：一个 `202`、一个 `409`；
- 结算期间新请求排队，超 30 s 返回 `503 busy`；**MUST NOT** 结算中写 `traits`。

### 13.3 租约、心跳与围栏

**状态放哪**：权威副本 **MUST** 与写入串行化在同一处——origin 的库，或对象存储的条件写。

**MUST NOT** 用"锁文件"放在共享目录里让各方去读：它会被同步覆盖，且分不清"没人持有"和"持有者死了"。

| 字段 | 含义 |
|---|---|
| `persona_id` | 一把锁一个人格 |
| `epoch` | 每次交接 +1，**写操作的唯一有效凭证** |
| `lease_id` | 本次租约随机 id（防重放） |
| `holder` / `device` | 当前持有者 |
| `heartbeat_at` | 由**存储端时钟**写入 |
| `ttl` | 默认 30 s（3 次心跳未续即过期） |

- **L1 续约**：每 10 s 一次；失败即降级（§13.4），**MUST NOT** 继续以权威身份写入。
- **L2 判定**：过期由**存储端**判定；节点 **MUST NOT** 自我认定。
- **L3 围栏**：写入 **MUST** 带 `(epoch, lease_id)`，存储端条件写；不匹配即 `409` / `412`。**旧 epoch 一律拒绝，不补写、不合并后放行。**
- **L4 接管**：过期后任一节点可 CAS 抢占并 `epoch + 1`，只有一个成功——全系统唯一仲裁点。
- **L5 时钟**：TTL 以存储端时钟为准，节点时钟只用于本地日志。

**脑裂只可能发生在"存储本身被复制"时**（两台 NAS、双向同步盘）。单权威存储 + 条件写不存在两个都写成功；复制型存储本规范不支持。

### 13.4 断连降级

- 续约失败或存储不可达，**MUST** 在一个心跳周期内切 `degraded`，用**本地快照**继续对话，标 `presence: unconfirmed` / `writes: local-only`；
- 新记忆写进 `offline-drafts.db`；**MUST NOT** 写 `memory.db`、**MUST NOT** 改 `traits`、**MUST NOT** seal；
- 状态对用户 **MUST** 可见；
- **MUST NOT** 因连不上存储而阻塞对话。卡死比降级更糟。

### 13.5 回归

1. 读当前 `epoch` 与 `holder`；
2. `epoch` 更高 → **MUST** 立即降为 `observer`，**MUST NOT** 夺回在场——**即使本地"最后活动时间"更晚**；
3. `offline-drafts.db` 的 episode 以 `source: offline-graft` + `origin_epoch` 提交（§5.6）；
4. `epoch` 相同且 `holder` 是自己 → 正常续约。

**仲裁只用存储端 epoch，不用节点时钟。**

### 13.6 三种拓扑与一个禁止项

| 拓扑 | 结构 | 适用 |
|---|---|---|
| 单机 | 三角色合一 | 一台电脑自用 |
| 私有基站 | origin + gateway/client | 家庭、多设备 |
| 只读漫游 | 拉密文 + 解密到临时目录，退出即删 | 出差、借用设备 |

**MUST NOT** 让两台机器直接打开同一个 `memory.db`——SMB / NFS / OneDrive / Dropbox / iCloud 都算。SQLite 的锁依赖本地文件系统语义；在网络盘与同步盘上这个前提不成立，而它**不会报错**，会**静默损坏**。多设备共享的唯一正确路径是 API。

---

## 14. 本地与云端

| 条件 | 数据放哪 | 模型在哪 |
|---|---|---|
| 只有一台设备 | 本地 | 本地（若跑得动）或云端 |
| 多设备 + 有常开主机 | 主机本地（origin） | 主机本地 |
| 多设备 + 无常开主机 | 云端对象存储（密文） | 各自本地或云端 |
| 借用设备 | 临时目录 | 云端 |

**云端零知识（MUST）**：只存密文与 manifest；口令密钥不出设备；云端不提供检索；调用云端模型只上传脱敏片段；传输用 TLS。

---

## 15. 安全与权限

1. **解锁**：口令 → KDF → 解容器头 → 校验；参数与耗时约束见 `SECURITY_SPEC.md`。
2. **密钥生命周期**：仅在内存；退出清零；**MUST NOT** 写日志、崩溃转储、遥测。
3. **三层写权限**（v1.0.0 §4.4）：`state` 随时写；`traits` 仅睡眠、幅度受限、证据 ≥ 3；`kernel`（含 `permissions`）仅人工。**MUST** 在代码层拦截。
4. **能力权限与人格权限分离**：`permissions` 属内核，**MUST NOT** 随漂移变化；模型无法自我提权。
5. **审计**：traits 写入、每次能力调用、seal、人格激活写哈希链；断号即拒绝启动。
6. **明文窗口**：运行期解密后的目录 **MUST** 仅本用户可读；`strict` 模式退出即删。**必须在文档里承认这个窗口**，不得宣称"全链路加密"。

---

## 16. 档位与能力协商

| 档位 | 判定 | `num_ctx` | 模型 | 向量 | 加密 |
|---|---|---|---|---|---|
| **low** | < 6 GB | 4096 | 1.5B Q4 / 云端 | 关 | 建议 strict |
| **mid** | 6–20 GB | 8192 | 3B–7B Q4 | 可选 | 可选 |
| **high** | > 20 GB | 16384 | 7B–14B Q4 | 开 | 可选 |
| **thin** | 手机 / 浏览器 | 由 origin 决定 | 无（转发） | 无 | 不落盘 |
| **edge** | 机器人 / 嵌入式 | 由设备决定 | 本地小模型 + 云端 | 关 | 可选 |

**MUST** 由可用资源自动判定，可配置覆盖；阈值是档位的函数（v1.0.0 §6.6）。

`GET /syspersona/capabilities` **MUST** 同时返回模型能力与本机能力：

```json
{"spec": "1.2", "role": "gateway", "tier": "low",
 "model": {"stream": true, "tools": false, "ctx_max": 8192},
 "host": {"fs.read": true, "fs.write": true, "net.fetch": true,
          "proc.exec": false, "media.capture": false, "device.act": false},
 "personas": ["lin"], "degraded": []}
```

客户端 **MUST** 依此调整行为，**MUST NOT** 假设能力。

---

## 17. 合规：等级与 Profile

| 等级 | 名称 | 要求 |
|---|---|---|
| **L1** | 可对话 | 加载 / 组装 / 路由 / 归档，错误可见，逃生通道可用 |
| **L2** | 可连续 | + 记忆读写、睡眠、恢复指针、崩溃恢复、预算 |
| **L3** | 可迁移 | + 三种 DataSource、seal/unlock、binding、档位 |
| **L4** | 多设备 | + 唯一在场、交接、租约与围栏、降级与回归、汇入溯源 |

| Profile | 形态 | 必过测试 |
|---|---|---|
| **E** | `embedded` | TC-C2/C3/C4、TC-M1–M3、TC-S1–S3、TC-A1–A3；TC-C1 用进程内适配器替代 |
| **G** | `gateway` | E 组 + TC-C1 + 全部 L/D/N 组 |
| **H** | `host-client` | G 组 + TC-N1–N21 |
| **R** | `edge`（具身） | G 组 + TC-A4/A5 + 反射延迟预算（§11.6） |

一个实现可同时声明多个 profile，分别给出证据。

```json
{"spec": "1.2", "profile": "G", "level": "L3", "impl": "python/3.12",
 "tests": {"TC-C1": "pass", "TC-A1": "pass"}}
```

**没有测试证据的声明不算合规**（与 v1.0.0 §17 同一标准）。

---

## 18. 测试套件（TCK）

| 组 | 编号 | 内容 | 判据 |
|---|---|---|---|
| **加载** | TC-L1 | 目录 / 容器 / 远端等价 | 同一 persona 三形态，探针一致 |
| | TC-L2 | 哈希校验 | 篡改 manifest → 拒绝加载 |
| | TC-L3 | 只读不写 | 只读节点 1 小时，origin 的 etag 不变 |
| **对话** | TC-C1 | OpenAI 兼容（仅 Profile G） | 第三方客户端可直连 |
| | TC-C2 | 前缀缓存 | 第 2/3 轮 `X-SP-Cache-Hit-Ratio ≥ 0.5` |
| | TC-C3 | 预算不越界 | `prompt_eval_count ≤ num_ctx`，promise/boundary 仍在 |
| | TC-C4 | 逃生通道 | `X-SP-Bypass: 1` 时零注入 |
| **记忆** | TC-M1 | 检索带来源 | 每条注入都有 id，可回溯 |
| | TC-M2 | 冷热分离 | 热路径模型调用数 = 0（除生成） |
| | TC-M3 | 隔离 | 换 persona 后检索不到他人条目 |
| **睡眠** | TC-S1 | 指针先于清理 | 崩溃注入后指针仍存在 |
| | TC-S2 | 快照原子 | kill -9 后无半写，指针未脏 |
| | TC-S3 | 清理顺序 | 压缩 → 降级 → 删除，跳步即失败 |
| **能力** | TC-A1 | 默认拒绝 | 未声明能力 → `policy_denied`，且有规则名 |
| | TC-A2 | 越界拒绝 | `fs.read` 读到根目录外 → 拒绝，不泄露内容 |
| | TC-A3 | 动作记账 | 每次调用产生 action episode，含能力名/结果/策略 |
| | TC-A4 | 原始媒体不留存 | 未显式录制时，磁盘上无音视频原始数据 |
| | TC-A5 | 反射不经过模型 | 安全动作在模型不可用时仍能完成，延迟 < 100 ms |
| **设备** | TC-D1 | 租约 | 两节点同时写 → 一个 `409`/`412` |
| | TC-D2 | 离线重放 | 断网 10 轮 → 恢复后 episode 不重不丢 |
| | TC-D3 | state 不合并 | 两设备情绪互不影响 |
| **多设备** | TC-N1 | epoch 递增 | 交接后旧 epoch 写入被拒 |
| | TC-N2 | stale 禁止 seal | stale 状态下 seal → 拒绝 |
| | TC-N3 | 云端零知识 | 抓取远端对象 → 无明文、无口令 |
| | TC-N4 | 能力协商 | thin 节点不发送不支持的能力 |
| **局域网** | TC-N5 | 未鉴权访问 | 无 token → 401，不泄露人格存在性 |
| | TC-N6 | 局域网流式 | 10 轮无缓冲、无断流；TTFT 差 < 50 ms |
| | TC-N7 | 设备吊销 | 吊销后下一次请求 401，留审计 |
| | TC-N8 | DNS rebinding | 伪造 `Host` → 403 |
| **在场** | TC-N9 | 唯一在场 | 两端同时对话 → 后到者 `409` |
| | TC-N10 | 交接连续性 | B 首轮接上 A 最后一件事；epoch 递增；A 收 `displaced` |
| | TC-N11 | 过期接管 | 心跳停 TTL 后接管 → 审计有记录，新会话从指针开始 |
| **结算** | TC-N12 | 结算独占 | 两节点同时 `/sleep` → 一个 202、一个 409 |
| **汇入** | TC-N13 | 汇入溯源 | 导入带 `source: graft`；traits 未变；画像偏移 ≤ 0.3 |
| **租约** | TC-N18 | 围栏生效 | 旧 epoch 写入 → `409`，不产生任何行 |
| | TC-N19 | 心跳过期 | 停心跳 30 s → 另一节点接管，epoch+1，原持有者被拒 |
| | TC-N20 | 断连降级 | 拔网 → 一周期内 `degraded`；权威库无新写入；草稿有记录；对话不阻塞 |
| | TC-N21 | 回归不夺权 | 落后 epoch 重连 → `observer`，草稿 graft，即使本地时间更晚 |

---

## 19. 版本与扩展

- 规范版本走 `X-SP-Spec`；服务端回 `X-SP-Spec-Version`。
- 未知字段 **MUST** 忽略而非报错；未知错误码 **MUST** 按 `type` 归类处理。
- 新增能力 **MUST** 通过 `capabilities` 声明，**MUST NOT** 改变已有字段语义。
- 1.x 内保证：DataSource 接口、binding 结构、错误码语义、能力清单不变。

---

## 20. 局域网接入

- 默认 **MUST** 只监听 `127.0.0.1:8000`；对外是显式动作（`bind = "lan"`），**MUST NOT** 默认 `0.0.0.0`。
- 绑非回环后 **MUST** 强制鉴权；无 token 一律 `401`（唯一例外 `/healthz`，且不返回人格信息）。
- **MUST NOT** 暴露模型端口（Ollama 11434）——它没有鉴权。
- **MUST** 每设备一个 API Key（`spk_` + 32 字节随机），服务端只存哈希，**MUST NOT** 放 URL query，**MUST** 常量时间比较，吊销 5 s 内生效。
- **SHOULD** 用 HTTPS：iOS ATS 与 Android 9+ 默认禁止明文 HTTP。
- 反向代理 **MUST** 关闭响应缓冲，否则 SSE 会被攒住。
- **MUST** 默认关闭 CORS；需要网页客户端时用 origin 白名单，**MUST NOT** 用 `*`。
- **MUST** 校验 `Host` 头属于允许列表，防 DNS rebinding。

**新设备接入**：`device add` → 一次性配对码（5 分钟，限次）→ 客户端换长期 key（只显示一次）→ 之后只填 base URL。二维码内 **MUST NOT** 放长期 key。

---

## 21. 与其他文档的关系

| 文档 | 关系 |
|---|---|
| `README.md` §0–§9 | 语义来源；本规范不重定义 |
| `docs/SECURITY_SPEC.md` | 容器格式与密钥体系；本规范只引用 |
| `docs/CONFORMANCE.md` | 三级声明；本规范扩展为 L1–L4 + Profile |
| `docs/MIDDLEWARE_LAUNCHER.md` | 资料性 Annex：可行性实测、预算数值、参考取舍 |
| `impl/launcher` | Profile G 的 L2 参考实现（PoC） |

---

## 附录 A：最小实现清单

一个 L2 级 Runtime 的最小构件：

1. 服务（HTTP 或进程内接口）+ 单 worker + 一把生成锁；
2. DataSource 的 `file://` 实现；
3. 三个 JSON 的加载与校验 + 一个 SQLite（FTS）；
4. 预算计算与尾部注入；
5. 模型转发（或直接调用）+ 流式转换；
6. 写队列 + 睡眠（指针、快照）；
7. 能力清单的三种裁决（allow / ask / deny）+ 动作记账；
8. 15 个错误码；
9. `X-SP-*` 头。

不需要：多用户、云端、租约、加密、向量检索、具身设备。

## 附录 B：常见错误做法

| 做法 | 为什么错 |
|---|---|
| 把记忆拼进开头 system | 摧毁前缀缓存，首 token 翻倍 |
| 每轮做一次抽取 | 低配设备直接卡死 |
| 每轮加密落盘 | KDF 1.6 s，聊天不可用 |
| 检索为空就静默继续 | 用户以为"它记得"，实际已失忆 |
| 多 worker 共享 SQLite | 写锁抖动，随机 `database is locked` |
| 自动合并两份 traits | 人格打架，且无法解释 |
| 手机存完整记忆库 | 丢设备 = 丢人格 |
| 把 `data/` 放进 OneDrive / SMB | SQLite 锁失效，静默损坏 |
| 默认模式下多设备同时对话 | 就是两个"它"，各自积累，无法合并 |
| 把 fork 当成"多端同步" | 新 UUID 已是另一个人 |
| 绑 `0.0.0.0` 且不开鉴权 | 全屋设备、访客 WiFi、恶意网页都能读记忆 |
| 暴露 Ollama 的 11434 | 模型端口无鉴权，等于开放本机 |
| 让模型自己拼 shell / 选路径 | 三个平台毁三次数；幻觉一次就够 |
| 把拒绝当成"模型没提需求" | 拒绝本身是信息，必须记账 |
| 用 200 掩盖降级 | 故障不可诊断 |

## 附录 C（草案）：并行在场 —— 一个自我，多条线程

> **未纳入合规。** 供讨论，不用于认证。

「唯一在场」是把人的物理约束套在 AI 上。约束不该来自物理，该来自**叙事一致性**——而它可以靠"收束"获得，不必靠"禁止并行"。

**正确形态**：不是"允许两个写者"，而是**多条只追加的时间线 + 一个唯一收束点**。

- 线程 `thread_id` 独立 `state`；`episode` 带 `(ts, instance_id, seq)` 确定性排序；
- **并行期间 `traits` 与 `permissions` 冻结**，所有线程只读；
- 收束：结算锁 → 排序去重 → 冲突检测 → `merge_record`（幂等）→ 收束提示 → 解冻 traits → 跑一次漂移；
- **漂移权重** `w = 1 / max(1, n_overlap(t))`，否则并行一天等于两天证据；
- **收束提示**必须显式说明"这些经历是同时发生的"，否则模型会讲成"先…然后…"，造出假因果；
- **冲突不交给算法裁决**：事实标 `contested`，承诺标 `unresolved_commitment`，情绪直接丢弃。

三种模式：`exclusive`（一个人在一个地方）、`concurrent`（一个自我多条线程）、`fork`（两个自我）。并行没有推翻唯一性，它把唯一性从**空间**挪到了**时间**。

---

> 规范的完成标志不是被读完，而是**有人照着它做出了一个能通过 TCK 的实现，而你可以直接拿来用**。
