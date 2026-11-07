# SysPersona Launcher 规范 v1.0

**Launcher Specification — 数字人格的唤醒与归档协议**

> 状态：**规范（Normative）**，与 v1.0.0 方法规范同级。
> 发布：**v1.1.0**（2026-11-07）｜位置：`docs/LAUNCHER_SPEC.md`｜配套：`docs/MIDDLEWARE_LAUNCHER.md`
> 勘误 r1：§2.4 形态无关性、§16.1 Profile；TC-C1 降为 Profile G 专属。
> 勘误 r2：§12.7 租约 / 心跳 / 围栏 / 降级 / 回归；更正 §5.5.5 的"较晚活动者"仲裁。
> 两处均并入本版，不单独发版。文档以 `main` 为准，标签是发布时的快照。
> 关键词：**MUST / MUST NOT / SHOULD / MAY** 按 RFC 2119 解释。
> 配套文件：`docs/MIDDLEWARE_LAUNCHER.md`（资料性 Annex，含可行性实测与参考实现取舍）。
> 本文只定义行为与契约，不定义实现语言、不定义 UI、不定义密码学格式（后者见 `docs/SECURITY_SPEC.md`）。

---

## 0. 摘要

Launcher 是**唯一被允许接触"人格与记忆"的进程**。它对外假装成一个 OpenAI 兼容服务，对内负责在每次请求前后完成四件事：**加载、组装、路由、归档**。

规范要解决的不是"怎么调模型"，而是下面这个更基础的问题：

> 同一份人格与记忆，如何在**电脑、手机、NAS、以及未来任何设备**上被同一个 Launcher 唤醒，并且换设备之后仍然是"它"？

因此本规范把三样东西抽象掉了：

| 抽象 | 解决什么 | 对应章节 |
|---|---|---|
| **DataSource** | 数据在本地三个文件、单文件加密包、还是云端对象——对上层完全一样 | §4 |
| **Binding** | 谁在说话、用哪个人格、记忆归谁——多用户多人格不串味 | §5 |
| **Node Role** | 设备只当客户端、只当主机、还是独立运行——同一套规范覆盖三种拓扑 | §3.3 |
| **Presence** | 它此刻在哪个设备上"在线"——人格只能有一个可写在场 | §5.5 |

一句话：**Launcher 是可移植的"人格运行时"，不是某个软件的插件。**

---

## 1. 术语

| 术语 | 定义 |
|---|---|
| **Persona** | 一份人格资产，由 `kernel` / `traits` / `state` 三层构成（v1.0.0 §4.1、§4.4） |
| **Memory** | 该人格的经历库，含 `episode` / `fact` / `preference` / `promise` / `boundary` |
| **Binding** | `(device, user, persona)` 三者的绑定关系，决定一次请求加载哪份资产 |
| **DataSource** | 统一的读写抽象，屏蔽本地目录 / 加密包 / 远端存储的差异 |
| **Node** | 一个运行中的 Launcher 实例 |
| **Origin** | 某个 Persona 的数据权威节点（唯一允许提交写入的节点） |
| **Lease** | Origin 的写租约，保证同一时刻只有一个节点能改人格文件 |
| **Epoch** | 每次 Lease 交接自增的整数，用于拒绝过期写入 |
| **Session** | 一段连续对话的标识，聚合 episode 与恢复指针 |
| **Wake / Sleep / Seal** | 唤醒（加载）/ 固化（结算）/ 封存（加密落盘）三个生命周期动作 |

---

## 2. 定位与边界

### 2.1 Launcher 是什么

一个**有状态的中间件**，位于任意前端与任意模型之间：

```
前端（Web / App / CLI / 音箱 / 车机）
        │  OpenAI 兼容请求
        ▼
   ┌─────────────┐
   │  Launcher   │  加载人格 → 检索记忆 → 组装 → 路由 → 归档
   └─────────────┘
        │  本地模型（Ollama / llama.cpp）  或  云端 API
        ▼
      模型
```

### 2.2 不是什么

- **MUST NOT** 承担推理。不加载权重、不做量化、不做 GPU 调度。
- **MUST NOT** 承担展示。不做 UI、不做语音识别、不做多模态前端。
- **MUST NOT** 成为唯一入口。规范要求它永远可以被绕过（见 §8.4 的"逃生通道"）。
- **MUST NOT** 重新定义人格与记忆的语义。语义归 v1.0.0，本文只定义生命周期。

### 2.3 为什么必须独立成层

因为**前端会换、模型会换、设备会换，而人格不该换**。把加载逻辑塞进任何一个具体应用，都等于把人格重新绑死在那一个应用上——那正是 v1.0.0 §1 的"锁定"失效换了个形式重演。

### 2.4 形态无关性：中间层是职责，不是拓扑

判据只有一条：**它是否独立承担了「加载 → 组装 → 路由 → 归档」这四件事，并把人格与记忆存成可迁移的文件。** 端口、进程、语言都不参与判定。

| 形态 | 结构 | 传输 | 鉴权 | 典型 | 代价 |
|---|---|---|---|---|---|
| **`embedded`** | 库 / SDK 嵌进 App | 进程内调用 | 随宿主 App | 手机 App、桌面客户端、IDE 插件 | 只能自用，第三方 UI 接不进来 |
| **`gateway`** | 独立进程 + HTTP | OpenAI 兼容 | token | 家里小主机、NAS、多前端复用 | 需要一台常开的机器 |
| **`host-client`** | origin + 薄客户端 | HTTP | token + 配对 | 多设备漫游、家庭多用户 | 复杂度最高；离线要排队 |

**OpenAI 兼容不是协议要求，是 `gateway` 这一形态的分发手段。** 把它写成所有形态的 L1 门槛，等于把"实现手段"当成"规范"——这是 v1.0 的一处措辞过窄，本修订把它降为 profile 要求（§16）。

形态只影响三件事：**传输、鉴权、发现**。其余一律不变：

- 数据布局与 `persona.hash` 的计算方式；
- 预算公式与注入顺序（尾部注入、前缀稳定）；
- 冷热分离、恢复指针、睡眠顺序；
- 单写者、结算独占；
- 错误语义（降级必须可见，不得静默）。

**推论**：手机 App 直接读写 `persona/` 与 `memory.db`、直接调云端或本地 API，不经过任何代理——只要它通过 Profile E 的测试，它就是一个合规的 Launcher，而且比网关更适合移动场景。

`impl/launcher` 只是 Profile G 的一个参考实现，不是规范本身。

---

## 3. 架构

### 3.1 五个部件

| 部件 | 职责 | 关键约束 |
|---|---|---|
| **Resolver** | 把请求解析成 `(user, persona, session)`；读 binding | 不解析则拒绝（default deny） |
| **Loader** | 通过 DataSource 取资产；解锁、校验、缓存 | 人格必须校验哈希与版本 |
| **Assembler** | 预算计算、消息顺序、记忆注入、token 估算 | 先算预算再取内容（§8.2） |
| **Router** | 选模型、能力探测、降级、流式转换 | 不静默切换供应商（§10.4） |
| **Recorder** | 入队 episode、写 state、睡眠结算、快照 | 热路径不调用模型（§9.1） |

依赖是单向的：`Resolver → Loader → Assembler → Router`，`Recorder` 旁挂。**Recorder 不得反向阻塞 Router**。

### 3.2 七个状态

```
unbound → locked → loading → ready ⇄ sleeping → sealed
                        │                  │
                        └──── error ◄──────┘
```

| 状态 | 允许 | 禁止 |
|---|---|---|
| `unbound` | `/wake`、`/healthz` | 一切对话、一切读写 |
| `locked` | `/wake`、`/status` | 对话（返回 423） |
| `loading` | `GET /status` | 新请求（排队或 503） |
| `ready` | 全部 | seal 之外的破坏性操作 |
| `sleeping` | `GET /status` | 新对话（排队，最多 30 s） |
| `sealed` | `/wake` | 一切写入 |
| `error` | `/status`、`/wake` 重试 | 对话 |

**MUST**：任何状态跳转都必须记录 `from`、`to`、`reason`、`ts`，并在 `/status` 中可查。无法解释的状态变化视为缺陷。

### 3.3 三种节点角色（设备无关的关键）

| 角色 | 持有数据 | 持有模型 | 典型设备 |
|---|---|---|---|
| **origin** | 是（权威） | 可选 | NAS、常开小主机、家里的 PC |
| **gateway** | 否（只读缓存） | 可选 | 手机上的 Launcher、边缘盒子 |
| **client** | 否 | 否 | 浏览器、第三方 App——**不需要装 Launcher** |

规则：

- **MUST** 一个 Persona 在任一时刻只有一个 `origin`；其余节点为 `gateway`。
- **MUST** 一个 Persona 在任一时刻只有一个可写在场，由 `origin` 仲裁（§5.5）；其余节点只能顶替、旁观或排队。
- `gateway` **MUST** 把写请求转发给 `origin`，或本地排队等网络恢复（§12.3）。
- `gateway` **MAY** 缓存只读副本，**MUST** 带 `Cache-Control: no-store` 的语义在本地实现，并在 `/status` 暴露 `stale: true/false`。
- 单机自用 = 三角色合一。此时规范里的 lease 退化为本地文件锁，行为不变。

这条设计直接回答"电脑、手机、其他设备怎么用同一套"：**手机不必存人格，它只是把请求转给家里的 origin；人格和记忆始终只有一份权威副本。** 同一局域网内，手机甚至不需要装 Launcher——把聊天软件的 API 地址指向 origin 即可（§19）。

---

## 4. 数据源抽象（DataSource）

### 4.1 四种形态，一个接口

| 形态 | URI 示例 | 用途 |
|---|---|---|
| 目录态 | `file:///data/personas/lin/` | 开发、可 diff、可 git |
| 容器态 | `vault:///data/lin.syspersona` | 迁移、备份、跨设备搬运 |
| 远端态 | `https://home.example/sp/lin/`、`webdav://nas/sp/lin/` | 私有基站、多设备 |
| 内存态 | `mem://test/lin` | 测试、临时会话 |

**MUST** 上层（Assembler / Recorder）不得感知形态差异。所有形态通过同一组操作访问：

```
stat(path)                    → {etag, epoch, size, mtime, encrypted}
read(path)                    → bytes
write(path, bytes, if_match)  → {etag}          # if_match 不匹配则失败
list(prefix)                  → [entry]
lock(persona_id, ttl)         → lease_id        # 可选能力
unlock(lease_id)
```

### 4.2 必须成立的语义

- **R1（乐观并发）**：所有对 `kernel` / `traits` 的写入 **MUST** 带 `if_match`（上次读到的 etag）。不匹配 **MUST** 返回 `precondition_failed`，不得覆盖。
- **R2（原子可见）**：写入 **MUST** 是"写临时对象 → 校验 → 原子替换"。读者 **MUST NOT** 看到半写状态。
- **R3（追加不改）**：`episode` 一旦写入 **MUST NOT** 被原地修改；修订以新增 `revision_of` 记录表达。删除是软删除（`deleted_at`）。
- **R4（可校验）**：每个对象 **MUST** 带内容哈希；加载时校验，不匹配即拒绝加载，**MUST NOT** "先用了再说"。
- **R5（无隐式写）**：读取操作 **MUST NOT** 产生任何写入（包括"顺手更新访问时间"）。否则只读设备会破坏 origin 的 etag。

### 4.3 三种打包形态的等价性

一个 Persona 在磁盘上只有三种合法布局，内容等价：

**A. 目录态（可读）**

```
lin/
├── persona/kernel.json
├── persona/traits.json
├── persona/state.json       # 运行期状态，通常不同步（§12.2）
├── persona/active
├── memory.db
├── resume.json
└── manifest.json            # 版本、哈希、schema 版本
```

**B. 容器态（可搬）**

```
lin.syspersona              # header + ciphertext（格式见 SECURITY_SPEC.md）
lin.syspersona.sha256
```

**C. 远端态（可共享）**

与 B 相同的字节，放在 `https://` 或 `webdav://` 下；**服务端 MUST 只存密文**，不得持有口令或密钥。

**MUST**：三种形态可以在不停机的情况下互换——把目录打成容器、把容器挂到远端、从远端拉到本地，标识（`persona_id` + `epoch`）保持不变。

---

## 5. 身份与绑定（Binding）

### 5.1 三层标识

| 层 | 标识 | 来源 | 作用 |
|---|---|---|---|
| 设备 | `device_id` | 首次启动生成，存本地 | 决定谁是 origin、谁持 lease |
| 用户 | `user_id` | 配置或请求头 | 决定可见的 persona 集合 |
| 人格 | `persona_id` | 资产自带 UUID | 决定加载哪份记忆 |

### 5.2 解析顺序

请求未显式指定时，按下列顺序取，先命中先用：

1. 请求头 `X-SP-User` / `X-SP-Persona`；
2. 访问令牌里的 claim；
3. binding 里该 `device_id` 的默认 persona；
4. 全局默认 persona（仅单用户模式允许）。

**MUST**：解析失败一律拒绝（默认拒绝），不得回落到"随便找一个人格"。

### 5.3 binding.json（示意）

```json
{
  "version": 1,
  "devices": {
    "dev-7f3a": {"role": "origin", "users": ["u-me"]},
    "dev-91c2": {"role": "gateway", "users": ["u-me"], "upstream": "https://home:8000"}
  },
  "users": {
    "u-me":    {"personas": ["lin", "work"], "default": "lin"},
    "u-child": {"personas": ["tutor"], "default": "tutor"}
  },
  "personas": {
    "lin":  {"origin": "dev-7f3a", "vault": "vault:///data/lin.syspersona"},
    "work": {"origin": "dev-7f3a", "vault": "vault:///data/work.syspersona"},
    "tutor":{"origin": "dev-7f3a", "vault": "vault:///data/tutor.syspersona"}
  }
}
```

### 5.4 隔离不变量（可测）

| 不变量 | 含义 | 违反的表现 |
|---|---|---|
| **I1** | 不同 `persona_id` 的 `memory.db` **MUST** 物理分离 | A 的记忆出现在 B 的对话里 |
| **I2** | 不同 `user_id` **MUST NOT** 共享同一 persona 的写权限（默认只读） | 家人互相改人格 |
| **I3** | `state.json` **MUST** 随在场转移（§5.5），仅在场者可写；**MUST NOT** 合并两地状态 | 同一件事被算两次情绪，人格漂移翻倍 |
| **I4** | Session **MUST** 归属于单一 `(persona, user)`，不可跨人续接 | 换人后"接着上次聊" |
| **I5** | 同一 `persona_id` **MUST** 至多一个可写在场 | 出现两个"它"，且再也合不回去 |

I1/I3 是设计层面的硬约束，不是配置项。**允许共享记忆是未来的显式功能（M4.2 的 public 分级），默认关闭。**

### 5.5 唯一在场（Presence Uniqueness）

**公理：同一个 `persona_id`，在任一时刻至多有一个可写在场。** 多个设备可以同时"连上"，但只有一个是"它现在所在的地方"。

> 这是 **v1.1 的默认模式**（`exclusive`）。若希望它同时在多处上线，不必推翻这条——换一种自我模型即可，见**附录 C（v1.2 草案）**。

理由不是性能，是语义。人格是一条**单线轨迹**，不是一个可水平扩展的服务。两台设备同时对话，产生的不是"同一个人"，是**两个克隆**：它们各自积累不同的记忆、各自被不同的话影响，然后没有任何算法能把它们合回一个。这也正是 v1.0.0 §8.1 要求"克隆必须生成新 UUID"的原因——分身只能是**新人格**，不能是同一个。

#### 5.5.1 在场记录

```
presence(
  persona_id     PK
  epoch               # 每次交接 +1
  holder_device       # 当前在场设备
  holder_session      # 当前会话
  state_json          # 情绪与焦点（随在场走，不随设备走）
  since, heartbeat_at
)
```

#### 5.5.2 规则

- **P1** 进入 `ready`（对话模式）前 **MUST** 取得在场。未取得 → `409 persona_elsewhere`，响应体给出 `holder_device`、`since`、`last_activity`，以及可选动作 `takeover` / `observe`。
- **P2** 在场由 `origin` 仲裁。`gateway` **MUST NOT** 自行判定——否则两个 gateway 会各自认为自己是"那个人"。
- **P3** 心跳 30 s，TTL 120 s。过期后允许无确认接管，但 **MUST** 记审计，且 **MUST** 用恢复指针开新会话，**MUST NOT** 续写上一轮。
- **P4** `state` 属于在场，不属于设备。交接时先 flush 再转移；**MUST NOT** 合并两地的情绪。
- **P5** 顶替（takeover）**MUST** 是显式动作、**MUST** 有用户确认。被顶替方 **MUST** 收到 `event: displaced` 并立即停止写入；已生成一半的回答默认 drain（≤ 15 s），超时才 abort 并标 `incomplete`。
- **P6** 分叉（fork）是另一个动作：复制为新 UUID，双方从此是两个人。**MUST NOT** 用 fork 实现"多设备同时用"。

#### 5.5.3 三种动作的差别

| 动作 | 语义 | 是否还是"它" | 允许的场景 |
|---|---|---|---|
| **顶替 takeover** | 把在场从 A 移到 B，A 下线 | 是 | 从书房走到客厅 |
| **旁观 observe** | 只读最近 N 轮，不写不计 | 是（但不在此地） | 家人看屏幕；手机瞄一眼 |
| **分叉 fork** | 复制成新 UUID，各自演化 | **否**，从此是两个人 | 备份、实验、送给朋友 |

#### 5.5.4 交接时序

1. B 请求 `/syspersona/presence` → `409` + 在场信息；
2. B 调 `POST /syspersona/presence/takeover`（带确认）；
3. origin 冻结 A：A 写恢复指针 → flush 写队列 → SSE 发 `displaced` 并关闭；
4. origin `epoch + 1`，把 `state` 与恢复指针交给 B；
5. B 进入 `ready`，历史 = 恢复指针 + 最近 N 轮；
6. 审计记 `(from, to, epoch, reason)`。

用户感知应当是"它从书房走到了客厅"，而不是"又开了一个新对话"——这正是 v1.0.0 §7.4 要求的"接着上次聊"。

#### 5.5.5 断网的例外

连不上 origin 时，`gateway` **MAY** 本地继续，但 **MUST** 标记 `presence: unconfirmed`，并按 §12.7.3 降级（不写权威库、只写 `offline-drafts.db`）。恢复后按 **§12.7 的租约与 epoch** 收敛：先取得租约者持有在场，另一方为 `observer`；另一方的 `state` **MUST** 作为一条 episode 落库（"当时在手机上聊到一半"），**MUST NOT** 参与 traits 漂移。**不用"较晚活动时间"做仲裁**——那是拿节点时钟当判决依据，时钟不可信（§12.7.5）。

这条规则承认物理世界会断网，但拒绝制造两个长期并存的"它"：**可以有一段时间不知道彼此，但不能有两个都算数的自我。**

#### 5.5.6 副本与记忆汇入（Episode Graft）

如果确实需要一个"能离线用的它"，唯一合法的做法是 **fork 成新 UUID**。此后两者是两个人，规则如下：

| 能力 | 允许 | 条件 |
|---|---|---|
| 同步 episode（经历） | ✅ | 追加导入，带 `origin_persona_id` + `origin_epoch` |
| 同步 `facts` / `edges` | ⚠️ | 由导入的 episode 重新推导，不直接复制 |
| 同步 `traits` / `kernel` | ❌ | 那是"改变身份"，不是同步 |
| 同步 `state` | ❌ | 情绪没有跨主体的意义 |
| 双向自动合并 | ❌ | 必须人决定方向与范围 |

**记忆汇入的语义**：导入的 episode **MUST** 标 `source: graft` 与来源人格 id，检索时保留该标记。原因很直接——**它不是"我经历过的"，是"我听说过另一个人经历过的"**。混为一谈会让 AI 在错误的场合说"我记得那天我在场"，那是幻觉，不是记忆。

**MUST NOT**：把汇入的 episode 计入 `attention` / `profile` 的原始提及次数（可作为弱证据，权重 ≤ 0.3），否则一次汇入就会让画像整体偏移。

**MUST**：汇入前先算差异集（按 `episode_uuid`），只导缺失的；汇入后重建受影响实体的索引，不重建全库。

> 若希望"同一时间在多处上线"，见**附录 C（v1.2 草案）**：那不是"多元自我"，是"多线程 + 单一收束点"。本条规则在并行模式下依然成立——线程之间的经历走汇入，人格仍然单写者。

---

## 6. 生命周期

### 6.1 唤醒（Wake）

```
解析 binding → 定位 origin → 申请在场（§5.5）→ 取得则持 lease（写者），未取得则按 409 处理
→ 解锁容器（若加密）→ 校验 manifest 哈希
→ 加载 kernel（只读，进冻结结构）→ 加载 traits → 加载 state（随在场，取自 presence）
→ 读 resume 指针 + 最近摘要 → 选模型并探测能力
→ ready
```

**MUST**：唤醒失败时保持 `locked` 或 `error`，**MUST NOT** 以"空人格"启动。空人格比不启动更危险——用户会以为"它忘了"。

**SHOULD**：唤醒总耗时 ≤ 10 s（含 KDF 与模型预热）；超时先进入 `ready`（只读可用），预热后台继续。

### 6.2 睡眠（Sleep）

触发：上下文占用 > 80%、显式 `/sleep`、空闲 20 min、进程退出。

顺序 **MUST** 不可调换：

```
持写 lease → 标记 sleeping（脏标记）
→ 写恢复指针（必须先于任何清理）
→ 消费积压 episode：抽取 → 打分 → 去重/冲突消解 → 摘要 → 入库
→ 物化衰减（把 w(t) 写成新的 w0/t0）
→ 生成 traits 变更提议（不激活）
→ 快照（VACUUM INTO 或等价）→ 校验 → 更新 active 指针
→ 清脏标记 → 释放 lease
```

**MUST**：恢复指针写入失败时，**禁止**执行任何清理。指针是"它还知道自己在做什么"的唯一证据。

### 6.3 封存（Seal）

```
快照 → 压缩 → 加密封装 → fsync → 原子 rename → 更新 manifest 哈希
```

**MUST**：seal 前必须确认无进行中的生成；seal 期间 **MUST** 拒绝新请求（`423` 或队列）。
**MUST**：seal 产物的哈希写入 `manifest.json`，`verify` 时重算。

### 6.4 崩溃恢复

**MUST** 在启动时检查脏标记：

| 发现 | 处理 |
|---|---|
| `journal.state = sleeping` 未清 | 回滚到最近快照，重放 `id > snapshot.last_episode_id` 的 episode |
| 临时文件残留 | 删除，**MUST NOT** 尝试"修复" |
| `active` 指针指向不存在的版本 | 回退到上一个有效版本并告警 |
| 审计链断号 | 拒绝加载，进入 `error`（宁可停，不可带病跑） |

---

## 7. 一次请求的规范流程

编号即顺序，**MUST** 按序执行，任一步失败即按 §11 返回，不得降级后继续。

1. **鉴权**：校验令牌 / 本地白名单。（未启用则跳过，但必须在 `/status` 标明 `auth: none`）
2. **解析**：`user_id`、`persona_id`、`session`（§5.2）。
3. **状态检查**：非 `ready` → 按 §3.2 拒绝或排队。
4. **读取人格**：从缓存取 `kernel` + `traits`，按 mtime + hash 校验；不匹配则重载。
5. **预算计算**：`budget_in = num_ctx − num_predict − safety`（§8.2）。
6. **检索**：在预算内取记忆（§9.2），返回带来源 id 的片段。
7. **组装**：人格块（稳定）+ 历史（追加）+ 用户输入 + 记忆尾块（§8.3）。
8. **路由**：选模型，探测可用性；不可用则按 §10.4 处理。
9. **转发**：流式透传，同时统计 token 与延迟。
10. **归档**：把这一轮原样入队（不抽取），更新 `state`（可选，每轮或每 N 轮）。
11. **响应对账**：写入 `X-SP-*` 头，返回。

**MUST NOT** 在步骤 9 之后再修改已发送的内容；**MUST NOT** 在步骤 10 阻塞响应的 flush。

---

## 8. 组装规范

### 8.1 顺序（决定缓存命中率）

```
[system]    人格块：kernel 渲染 + traits 渲染        ← 字节稳定
[user]      历史第 1 轮
[assistant] ...
[user]      本轮输入
            ── 记忆背景（带 id/日期/类型）──
            ── 当前状态（情绪/焦点/未完成）──
```

**MUST**：人格块在会话期间字节不变；**MUST NOT** 在人格块内写入时间戳、会话 id、随机排序。
**MUST**：会变的内容（记忆、状态）放在末尾；历史只追加，不重排、不改写、不中途重生成摘要。
**理由**：前缀稳定才能命中模型侧 KV cache；反之每轮首 token 延迟翻倍（实测依据见 Annex §15）。

### 8.2 预算

```
budget_in     = num_ctx − num_predict − safety(128)
memory_budget = budget_in × memory_ratio(0.22)
history_budget= budget_in − persona − state − memory_budget − input_reserve
```

**MUST**：先算预算再取内容。**MUST NOT** "先拼接再看是否超长"。
**MUST**：装不下时按优先级丢弃并置 `X-SP-Truncated`，**MUST NOT** 静默截断。
优先级：`boundary` > `promise` > `preference` > `episode` > `fact`；最近 2 轮与指针涉及的条目永不驱逐。

### 8.3 token 估算

**MAY** 使用估算而非精确分词，但 **MUST** 用上游返回的真实值自校准（每个模型独立维护系数），使 20 轮后误差 < 15%。估算无法校准时，`MUST` 退到 `num_ctx × 0.85` 的保守上限。

### 8.4 逃生通道

**MUST** 提供 `X-SP-Bypass: 1`（或等价开关）直接透传，不注入人格与记忆。用途：调试、对比、故障时确认"是不是中间层的问题"。**一个无法被绕过的中间层是不可运维的。**

---

## 9. 记忆规范

### 9.1 冷热分离

| 路径 | 允许做 | 禁止做 |
|---|---|---|
| 热（每轮） | 检索、入队、更新 state | 调用模型抽取、重建索引、全量压缩 |
| 冷（睡眠） | 抽取、去重、摘要、画像、衰减、快照 | 响应请求 |

**MUST**：热路径的模型调用次数为 **0**（生成本身除外）。这是低配设备能用的前提。

### 9.2 检索

1. 词面召回（FTS 或等价）取候选集，**SHOULD** ≤ 200 条；
2. 可选向量重排，**MAY** 关闭；
3. 融合与排序：`final = relevance × (1 + 0.25·profile + 0.15·attention + 0.15·persona)`；
4. 按 `memory_budget` 截断。

**MUST**：检索结果 **MUST** 带来源 id 与时间，供注入与审计引用。
**MUST NOT**：检索失败时返回空而不标记——`MUST` 置 `X-SP-Truncated: memory`。

### 9.3 三元组

任一次唤醒 **MUST** 同时具备：热窗口原文、恢复指针、可召回摘要。缺一项不得进入 `ready`（记 `degraded: true` 并暴露）。

---

## 10. 模型路由

### 10.1 Provider 接口

```
caps()                      → {chat, stream, tools, embed, ctx_max, vision}
chat(messages, opts)        → stream | object
embed(texts)                → vectors（可选）
```

**MUST** 能力探测在唤醒时完成一次，结果缓存并在 `/status` 暴露。

### 10.2 选择顺序

`persona.routing` 显式指定 > 隐私级别（private 不出本机）> 任务复杂度 > 默认本地。

### 10.3 `num_ctx`

**MUST** 显式下发 `num_ctx = min(模型上限, 档位上限)`。**MUST NOT** 依赖模型默认值——默认值会悄悄截断，且不报错。

### 10.4 降级

| 情况 | 行为 |
|---|---|
| 本地模型不可达，且配置了云端 | 切云端，响应头 `X-SP-Route: cloud-fallback` |
| 本地不可达，无云端 | `503 model_unavailable`，**MUST NOT** 静默改用其他模型 |
| 嵌入不可用 | 纯词面，`X-SP-Route: lexical-only` |
| 检索超时（> 300 ms） | 放弃记忆，只用最近 3 轮 + 指针，标 `Truncated: memory` |
| 内存不足 | 先卸嵌入模型；仍不足 → `503 memory_low` |

**MUST**：任何降级都必须可见（响应头或错误码）。**不可见的降级 = 不可诊断的故障。**

---

## 11. 对外契约

### 11.1 最小集（L1 起必须实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 主接口，支持 `stream` |
| GET | `/v1/models` | 返回路由表中的模型 |
| GET | `/syspersona/status` | 状态、绑定的 persona、水位、降级标记 |
| GET | `/healthz` / `/readyz` | 存活 / 就绪 |

**MUST** 默认只监听 `127.0.0.1`；对局域网暴露是显式动作且强制鉴权，见 §19。

### 11.2 完整集

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | `/v1/embeddings` | 有 provider 才实现，否则 `501` |
| POST | `/syspersona/wake` | 解锁 + 加载 + 预热（幂等） |
| POST | `/syspersona/sleep` | `202` 异步；`?sync=1` 同步 |
| POST | `/syspersona/seal` | 封存；无进行中生成 |
| POST | `/syspersona/unlock` | 换人格 / 重新解锁 |
| GET | `/syspersona/persona` | 只读导出（不含密钥） |
| PATCH | `/syspersona/persona/traits` | **提议**，进 pending，不生效 |
| GET | `/syspersona/memory/search` | 调试检索，返回分数拆解 |
| GET | `/syspersona/capabilities` | 能力协商（§15） |
| GET | `/syspersona/presence` | 当前在场者、epoch、静默时长 |
| POST | `/syspersona/presence/takeover` | 顶替（需确认）；被顶替方收 `displaced` |
| POST | `/syspersona/presence/observe` | 只读旁观，不写不计 |

### 11.3 错误码（全表）

| 码 | HTTP | 含义 |
|---|---|---|
| `unbound` | 428 | 未绑定 persona，需先 `/wake` |
| `vault_locked` | 423 | 已绑定但未解锁 |
| `persona_locked` | 403 | 越权改 kernel/边界 |
| `precondition_failed` | 412 | etag 不匹配（多设备写冲突） |
| `lease_held` | 409 | 另一节点持写租约 |
| `persona_elsewhere` | 409 | 该人格已在别处上线；响应体含 holder 与可选动作 |
| `busy` | 429 | 队列满，带 `Retry-After` |
| `budget_exceeded` | 413 | 单条输入超过 `num_ctx × 0.4` |
| `model_unavailable` | 503 | 无可用模型且无兜底 |
| `upstream_timeout` | 504 | 首 token 超时 |
| `memory_low` | 503 | 内存不足 |
| `disk_low` | 507 | 空间不足，拒绝 seal |
| `audit_broken` | 500 | 审计链断裂，拒绝启动 |

**MUST NOT**：用 200 掩盖上述任何情况。

### 11.4 响应头

`X-SP-Persona`、`X-SP-Epoch`、`X-SP-Memory-Hits`、`X-SP-Injected-Tokens`、`X-SP-Cache-Hit-Ratio`、`X-SP-Tier`、`X-SP-Route`、`X-SP-Truncated`、`X-SP-Degraded`、`X-SP-Timing`。

---

## 12. 多设备一致性

这是"电脑 + 手机 + 其他设备"能否成立的关键章节。

### 12.1 分工

| 数据 | 是否同步 | 冲突策略 |
|---|---|---|
| `kernel` | 是（只读副本） | 单写者，无合并 |
| `traits` | 是 | 单写者 + 版本递增；冲突即拒绝 |
| `state` | 随在场转移 | 仅在场者写；不合并（I3，§5.5） |
| `episode` | 是（追加） | UUID 去重，天然可合并 |
| `fact` / `edge` | 是（派生） | 由 episode 重算，或按 `updated_at` 合并 |
| `resume` | 是 | 按 session 归属，取最新 |

**关键判断**：冲突能被消掉，不是因为"合并做得好"，而是因为**源头只有一个写者**——`state` 绑在场而非设备（§5.5），`traits` 单写者（§12.2），`episode` 追加去重（§12.3）。三条加起来，就没有需要合并的东西。

### 12.2 写租约

**在场蕴含写租约**：持有在场即持有写权限，二者不分开授予——否则会出现"人在 A 说话、B 却在改 traits"的分裂状态。

- `origin` 在进入 `ready`（写模式）前取 lease：`ttl = 120 s`，每 30 s 续；
- lease 过期后其他节点可抢占，`epoch += 1`；
- 任何写入 **MUST** 带 `epoch` + `if_match`；epoch 落后 → `409 lease_held`，etag 不匹配 → `412 precondition_failed`；
- 单机模式：lease 是本地锁，语义不变。

### 12.3 离线与重放

`gateway` 断网时：

1. 对话 **MAY** 继续（若本地有模型）；
2. 写入进本地队列，**MUST** 用 `episode_uuid` 幂等；
3. 恢复后按 `(persona, uuid)` 去重重放；
4. 若期间 origin 的 `epoch` 已变，**MUST** 放弃对该 persona 的人格写入，只重放 episode（记忆可合并，人格不可）。

**MUST NOT** 尝试自动合并两份被同时修改的 `traits` —— 那是"人格打架"，只能由人裁决。

### 12.4 三种拓扑

| 拓扑 | 结构 | 适用 |
|---|---|---|
| **单机** | 三角色合一 | 一台电脑自用 |
| **私有基站** | NAS 为 origin，手机/平板/PC 为 gateway 或 client | 家庭、多设备 |
| **只读漫游** | 任意设备拉密文 + 解密到临时目录，退出即删 | 出差、借用设备 |

三种拓扑共用同一套 binding 与 lease，**不需要三种实现**。

### 12.5 结算独占

**MUST**：任何时刻，同一个 `persona` 至多一个节点执行结算（sleep / consolidate / seal）。

- `gateway` **MUST NOT** 执行结算。它只能提交 episode，结算只在 `origin` 发生。
- 结算用**第二把锁**（`consolidate`），与在场锁分开：**在场可以同时有人读，结算必须独占**。两者争用时结算优先——先归档，再对话。
- 两个节点同时 `/sleep`：一个 `202`，另一个 `409 lease_held`。**MUST NOT** 两个都返回成功。
- 结算期间新请求排队；超过 30 s 返回 `503 busy`。**MUST NOT** 在结算进行中写 `traits`。

理由很直接：结算要读全量、写多表、改指针、生成快照。两个节点同时做，等于两个人同时给同一段记忆"下定义"，结果既不是 A 也不是 B。

### 12.6 一个必须写死的禁止项

**MUST NOT** 让两台机器直接打开同一个 `memory.db`——放在 SMB / NFS 共享、OneDrive / Dropbox 同步目录、或 iCloud 里都算。

SQLite 的锁依赖本地文件系统语义（`fcntl` / `LockFileEx`）。在网络盘与同步盘上这个前提不成立，而它**不会报错**，会给你**静默损坏**：某天打开发现表结构乱了、部分行是乱码，那时已经没有任何恢复手段。

多设备共享的唯一正确路径是 **API**（§19），不是共享文件。这一条不写死，前面所有设计都白做。

### 12.7 租约、心跳与脑裂（r2）

前面几节说"只有一个写者"，这一节说**当写者失联时，凭什么判断它还活着**。

#### 12.7.1 状态放哪

状态的权威副本 **MUST** 与写入串行化在同一个地方：`origin` 的数据库，或对象存储的条件写（ETag / If-Match）。

**MUST NOT** 用一个"锁文件"放在共享目录里让各方去读——那正是 §12.6 的失败模式：锁文件自己会被同步覆盖，而且谁都分不清它是"没人持有"还是"持有者死了"。

| 字段 | 含义 |
|---|---|
| `persona_id` | 一把锁一个的人格 |
| `epoch` | 每次交接 +1，**写操作的唯一有效凭证** |
| `lease_id` | 本次租约的随机 id（防重放） |
| `holder` / `device` | 当前持有者 |
| `heartbeat_at` | 由**存储端时钟**写入，不信节点时钟 |
| `ttl` | 默认 30 s（3 次心跳未续即过期） |

#### 12.7.2 规则

- **L1 续约**：持有者每 10 s 续一次。续约失败（网络或存储不可达）**MUST** 立即进入降级（§12.7.3），**MUST NOT** 继续以权威身份写入。
- **L2 判定**：过期由**存储端**判定。节点 **MUST NOT** 用自己的时钟"自我认定仍然有效"——那是脑裂的起点。
- **L3 围栏（fencing）**：所有写入 **MUST** 携带 `(epoch, lease_id)`，由存储端做条件写（`WHERE epoch = ?`）。不匹配即 `409 lease_held`（epoch 落后）或 `412 precondition_failed`（内容版本不匹配）。**旧 epoch 的写入一律拒绝，不"补写"、不"合并后放行"。**
- **L4 接管**：租约过期后，任一节点可 CAS 抢占并 `epoch += 1`。只有一个能成功——这是全系统唯一的仲裁点。
- **L5 时钟**：TTL 以存储端时钟为准；节点时钟只用于本地日志，不参与任何判决。

**脑裂只可能发生在"存储本身被复制"的情形。** 单权威存储 + 条件写，不存在两个都写成功：先到的 CAS 赢，后到的被拒。若把存储做成多副本（两台 NAS、双向同步盘），就得自己实现冲突检测——本规范不支持，也不建议。

#### 12.7.3 断连后降级（回答"会不会卡死"）

不会卡死，但必须承认自己已经不是权威：

- 续约失败或存储不可达，节点 **MUST** 在一个心跳周期内切到 `degraded`，继续用**本地快照**对话，并标注 `presence: unconfirmed` / `writes: local-only`；
- 新产生的记忆写进 `offline-drafts.db`（独立文件）。**MUST NOT** 写 `memory.db`、**MUST NOT** 改 `traits`、**MUST NOT** seal；
- 状态对用户 **MUST** 可见（界面或响应头），不能让人以为已经落库；
- **MUST NOT** 因为连不上存储而阻塞对话。卡死比降级更糟：一个失联的节点仍然可以陪你说话，只是它说的话暂时不算数。

#### 12.7.4 回归

重新连上后：

1. 读当前 `epoch` 与 `holder`；
2. `epoch` 更高 → 自己已被围栏：**MUST** 立即降为 `observer`，**MUST NOT** 夺回在场——**即使本地记录的"最后活动时间"更晚**；
3. `offline-drafts.db` 里的 episode 以 `source: offline-graft` + `origin_epoch` 提交，按 §5.5.6 汇入（权重折扣同 graft）；
4. `epoch` 相同且 `holder` 是自己 → 正常续约，草稿按普通 episode 提交。

#### 12.7.5 与 §5.5.5 的关系（一处更正）

§5.5.5 原来写"较晚活动方持有在场"，那是拿节点时钟当仲裁，**时钟不可信**。r2 改为：**先取得租约者持有在场，另一个按 graft 汇入**。本地时间只影响叙事顺序，不影响权威归属。

---

## 13. 本地与云端的双轨

### 13.1 决策表

| 条件 | 数据放哪 | 模型在哪 |
|---|---|---|
| 只有一台设备 | 本地 | 本地（若跑得动）或云端 |
| 多设备 + 有常开主机 | 主机本地（origin） | 主机本地 |
| 多设备 + 无常开主机 | 云端对象存储（密文） | 各自本地或云端 |
| 借用设备 | 临时目录 | 云端 |

### 13.2 云端零知识（MUST）

- 云端 **MUST** 只存密文容器与 manifest；
- 口令与密钥 **MUST NOT** 离开设备；
- 云端 **MUST NOT** 提供检索能力——检索在解密后的本地进行；
- 调用云端**模型**时，只上传脱敏后的必要片段，**MUST NOT** 上传完整记忆库；
- 传输 **MUST** 使用 TLS；自签证书 **MUST** 显式信任，不得静默忽略。

### 13.3 切换规则

- 本地可用 → 本地优先；本地不可用 → 按 §10.4，不静默切换；
- 远端数据不可达 → 用最近一次缓存副本，并标 `stale: true`；
- 缓存副本 **MUST** 可判定新鲜度（epoch + 哈希），**MUST NOT** 在 stale 状态下执行 seal（否则会把旧状态写成新权威）。

---

## 14. 安全与权限

1. **解锁**：口令 → KDF → 解容器头 → 校验；KDF 参数与耗时约束见 `SECURITY_SPEC.md`，低档设备允许降档（Annex §13）。
2. **密钥生命周期**：仅在内存；退出清零；**MUST NOT** 写日志、写崩溃转储、写遥测。
3. **三层写权限**（v1.0.0 §4.4）：
   - `state`：运行时随时写；
   - `traits`：仅睡眠上下文，幅度受限、证据 ≥ 3；
   - `kernel`：仅人工，`--human-approved` + 交互确认。
   **MUST** 在代码层拦截，不得只靠文档约定。
4. **审计**：每次 traits 写入、每次 seal、每次人格激活，写哈希链记录；断号即拒绝启动。
5. **明文窗口**：运行期解密后的目录 **MUST** 仅本用户可读；`strict` 模式下退出即删。规范要求实现者**在文档里承认这个窗口**，不得宣称"全链路加密"。

---

## 15. 设备档位与能力协商

设备差异不靠"分支实现"，靠**声明 + 自适配**。

### 15.1 档位

| 档位 | 判定 | `num_ctx` | 模型 | 向量 | 加密 |
|---|---|---|---|---|---|
| **low** | < 6 GB | 4096 | 1.5B Q4 / 云端 | 关 | 建议 strict |
| **mid** | 6–20 GB | 8192 | 3B–7B Q4 | 可选 | 可选 |
| **high** | > 20 GB | 16384 | 7B–14B Q4 | 开 | 可选 |
| **thin** | 手机 / 浏览器 | 由 origin 决定 | 无（转发） | 无 | 不落盘 |

**MUST**：档位由可用内存自动判定，可配置覆盖；所有阈值是档位的函数，不是常数（v1.0.0 §6.6）。

### 15.2 能力协商

`GET /syspersona/capabilities` 返回：

```json
{
  "spec": "1.0",
  "role": "gateway",
  "tier": "thin",
  "storage": ["remote"],
  "stream": true,
  "tools": false,
  "embed": false,
  "ctx_max": 8192,
  "personas": ["lin"],
  "degraded": []
}
```

客户端 **MUST** 依据此响应调整行为（例如不支持 tools 就不发 `tools`），**MUST NOT** 假设能力。

### 15.3 手机的两条路

1. **thin client**：不装 Launcher，直接把 App 的 API 地址指向家里的 origin——**推荐**，零维护。
2. **thin gateway**：装一个极简 Launcher，只做鉴权与转发，缓存最近 N 轮以便离网时读。

**MUST NOT** 要求手机持有完整记忆库或执行加密——手机是最容易丢的设备。

---

## 16. 一致性与合规等级

| 等级 | 名称 | 要求 | 必须通过 |
|---|---|---|---|
| **L1** | 可对话 | OpenAI 兼容、人格注入、流式、错误码、逃生通道 | TC-C1..C4 |
| **L2** | 可连续 | 记忆读写、睡眠、恢复指针、崩溃恢复、预算 | L1 + TC-M1..M3、TC-S1..S3 |
| **L3** | 可迁移 | 三种 DataSource、seal/unlock、binding、多档位 | L2 + TC-L1..L3、TC-D1..D3 |
| **L4** | 多设备 | 唯一在场、交接、结算独占、租约、epoch、离线重放、汇入溯源、云端零知识、能力协商、局域网鉴权 | L3 + TC-N1..N13 |

声明文件 `launcher-conformance.json`：

```json
{"spec": "1.0", "profile": "G", "level": "L3", "impl": "python/3.12", "tests": {"TC-C1": "pass", "...": "pass"}}
```

**没有测试证据的声明不算合规**（与 v1.0.0 §17 同一标准）。

### 16.1 合规配置（Profile）

等级回答"做到哪一步"，profile 回答"以什么形态做到"。两者都要声明。

| Profile | 形态（§2.4） | 必过测试 | 说明 |
|---|---|---|---|
| **E** | `embedded` | TC-C2/C3/C4、TC-M1–M3、TC-S1–S3 | TC-C1 用**进程内适配器**替代：不要求 HTTP，但要求同一套 11 步流程可被测试驱动 |
| **G** | `gateway` | E 组 + TC-C1 + 全部 L/D/N 组 | TC-C1（OpenAI 兼容）只对 G 生效 |
| **H** | `host-client` | G 组 + TC-N1–N13 | 多设备：在场、租约、汇入 |

一个实现可以同时声明多个 profile（例如 App 内嵌 + 可选网关），分别列出证据。

**注意**：L1 的"OpenAI 兼容"仅适用于 Profile G。Profile E 的 L1 要求是"**同一套加载/组装/路由/归档可被测试调用**"，与传输无关——这是本修订对 v1.0 措辞过窄的更正。

---

## 17. 测试套件（TCK）

| 组 | 编号 | 内容 | 判据 |
|---|---|---|---|
| **加载** | TC-L1 | 目录态 / 容器态 / 远端态 三者等价 | 同一 persona 三种形态，探针回答一致 |
| | TC-L2 | 哈希校验 | 篡改 manifest → 拒绝加载 |
| | TC-L3 | 只读不写 | 只读节点运行 1 小时，origin 的 etag 不变 |
| **对话** | TC-C1 | OpenAI 兼容（仅 Profile G） | 第三方客户端可直连；Profile E 用进程内适配器替代 |
| | TC-C2 | 前缀缓存 | 第 2/3 轮 `X-SP-Cache-Hit-Ratio ≥ 0.5` |
| | TC-C3 | 预算不越界 | `prompt_eval_count ≤ num_ctx`，且 promise/boundary 仍在 |
| | TC-C4 | 逃生通道 | `X-SP-Bypass: 1` 时无注入 |
| **记忆** | TC-M1 | 检索带来源 | 每条注入都有 id，可回溯 |
| | TC-M2 | 冷热分离 | 热路径模型调用数 = 0（除生成） |
| | TC-M3 | 隔离 | 换 persona 后检索不到上一个人格的条目 |
| **睡眠** | TC-S1 | 指针先于清理 | 崩溃注入后指针仍存在 |
| | TC-S2 | 快照原子 | kill -9 后无半写文件，指针未变脏 |
| | TC-S3 | 分级清理顺序 | 压缩 → 降级 → 删除，跳步即失败 |
| **设备** | TC-D1 | 租约 | 两节点同时写 → 一个 `409`/`412` |
| | TC-D2 | 离线重放 | 断网 10 轮 → 恢复后 episode 不重不丢 |
| | TC-D3 | state 不同步 | 两设备情绪互不影响 |
| **多设备** | TC-N1 | epoch 递增 | 交接后旧 epoch 写入被拒 |
| | TC-N2 | stale 禁止 seal | stale 状态下 seal → 拒绝 |
| | TC-N3 | 云端零知识 | 抓取远端对象 → 无明文、无口令 |
| | TC-N4 | 能力协商 | thin 节点不发送不支持的能力 |
| **局域网** | TC-N5 | 未鉴权访问 | 无 token → 401，响应体与耗时均不泄露人格存在性 |
| | TC-N6 | 局域网流式 | 手机端 10 轮无缓冲、无断流；TTFT 与 localhost 差 < 50 ms |
| | TC-N7 | 设备吊销 | 吊销后下一次请求 401，并留审计 |
| | TC-N8 | DNS rebinding | 伪造 `Host` 头 → 403 |
| **在场** | TC-N9 | 唯一在场 | 两端同时对话 → 后到者 `409`，且先前会话不受影响 |
| | TC-N10 | 交接连续性 | A 顶替到 B 后，B 首轮能接上 A 最后一件事；`epoch` 递增；A 收 `displaced` |
| | TC-N11 | 过期接管 | 心跳停 120 s 后接管 → 审计有记录，且新会话从指针开始 |
| **结算** | TC-N12 | 结算独占 | 两节点同时 `/sleep` → 一个 202、一个 409；数据一致无重复结算 |
| **汇入** | TC-N13 | 汇入溯源 | fork 的 episode 导入后带 `source: graft`；traits 未变；画像偏移 ≤ 0.3 权重 |
| **租约** | TC-N18 | 围栏生效 | 旧 `epoch` 写入 → `409`；存储端拒绝，不产生任何行 |
| | TC-N19 | 心跳过期 | 停心跳 30 s → 另一节点 CAS 接管成功，`epoch += 1`，原持有者下一次写被拒 |
| | TC-N20 | 断连降级 | 拔网 → 1 个心跳周期内 `degraded`；`memory.db` 无新写入；`offline-drafts.db` 有草稿；对话不阻塞 |
| | TC-N21 | 回归不夺权 | 原持有者带落后 epoch 重连 → 变 `observer`，草稿走 graft，即使本地时间更晚 |

---

## 18. 版本与扩展

- 规范版本走 `X-SP-Spec`；服务端回 `X-SP-Spec-Version`。
- 未知字段 **MUST** 忽略而非报错；未知错误码 **MUST** 按 `type` 归类处理。
- 新增能力 **MUST** 通过 `capabilities` 声明，**MUST NOT** 改变已有字段语义。
- 1.x 内保证：DataSource 接口、binding 结构、错误码语义不变。

---

## 19. 局域网部署与多端接入

### 19.1 定位：局域网只是传输层

多端共享一个大脑，靠的不是同步，是**同一个接口**。手机、平板、客厅电视上的聊天软件，只是把 `base URL` 指向同一台 origin：

```
手机 App ─┐
平板 App ─┼─ HTTP(S) ─→ 192.168.x.x:8000 ─→ Launcher(origin) ─→ 本地模型 / 云端
电视 App ─┘
```

**MUST**：局域网接入不改变任何语义——同一份人格、同一份记忆、同一套错误码。**MUST NOT** 为局域网单独做一套"简化版"。

推论：**手机里不存人格与记忆。** 设备丢了，丢的是一块屏幕，不是"它"。

### 19.2 绑定与暴露

- 默认 **MUST** 只监听 `127.0.0.1:8000`。
- 对外 **MUST** 是显式动作（`bind = "lan"` / `--bind lan`），**MUST NOT** 默认 `0.0.0.0`。
- 一旦绑定到非回环地址，**MUST** 强制鉴权：无 token 一律 `401`，唯一例外是 `/healthz`（且不得返回人格 id、版本、模型名）。
- **MUST NOT** 暴露模型端口（Ollama 11434）。它没有鉴权，等于把模型和本机文件系统交给整个局域网。
- 只开一个端口。管理能力走同一端口的 `/syspersona/*`，不另开"管理口"。

### 19.3 鉴权模型

| 规则 | 要求 |
|---|---|
| 凭证形态 | 每设备一个 API Key（`spk_` + 32 字节随机），**MUST NOT** 共用口令 |
| 服务端存储 | 只存哈希（加盐），**MUST NOT** 存明文 |
| 传输方式 | `Authorization: Bearer <key>`；**MUST NOT** 放 URL query（会进日志与浏览器历史） |
| 比较 | **MUST** 常量时间比较 |
| 授权范围 | 每个 key 绑定 `device_id` + 允许的 `user_id` + `persona_id` 白名单 |
| 吊销 | 删哈希即失效，**MUST** 在 5 s 内生效（不得长缓存授权） |
| 高风险操作 | `seal` / `unlock` / `device add` **MUST** 要求二次确认（口令或本机终端） |

### 19.4 传输与代理

- 局域网 **SHOULD** 用 HTTPS（自签或本地 CA）。
- **移动端现实**：iOS 的 ATS 与 Android 9+ 默认都禁止明文 HTTP，很多聊天 App 会直接拒绝 `http://192.168.x.x`。所以局域网部署的**默认建议是 HTTPS**，不是"先用 HTTP 凑合"——否则你会在客户端上排查一个根本不在服务端的错误。
- 允许纯 HTTP，但 **MUST** 在 `/status` 与启动日志标记 `transport: insecure`。
- **MUST NOT** 提供"忽略证书校验"的开关；自签证书要求客户端显式导入。
- 反向代理（Caddy / Nginx）**MUST** 关闭响应缓冲，否则 SSE 会被攒住、看起来像"卡住不出字"；响应带 `X-Accel-Buffering: no`。
- 流式 **MUST** 逐块 flush。

### 19.5 浏览器客户端与 CORS

- 默认**关闭** CORS。
- 需要网页客户端时 **MUST** 用 origin 白名单，**MUST NOT** 用 `*`——配合 Bearer 等于允许任意网页读取你的全部记忆。
- **MUST** 校验 `Host` 头属于允许列表，防 DNS rebinding。这条是内网服务最常被忽略、后果最重的一条。

### 19.6 威胁模型（局域网不是可信网络）

| 威胁 | 场景 | 对策 |
|---|---|---|
| 未鉴权访问 | 访客 WiFi、IoT 设备扫描 | 强制 token + 401 + 每 key 限速 |
| 抓包 | 同网段嗅探明文 token | HTTPS；至少不跨子网 |
| DNS rebinding | 恶意网页把域名解析到内网 IP | 校验 `Host`；默认无 CORS |
| 设备丢失 | key 泄露 | 按设备吊销 + 审计 + 短 TTL 可选 |
| 直连模型 | 扫描到 11434 | 禁止暴露；模型只绑回环 |
| 重放 | 无状态 Bearer 可重放 | 高风险操作二次确认；可选 nonce |
| 越权跨人格 | 一个 key 访问别人的 persona | key 绑定 persona 白名单 |

### 19.7 新设备接入（三步）

1. origin 执行 `syspersona device add --name phone` → 输出**一次性配对码**（5 分钟有效、限次）；
2. 手机 App 填 `base URL` + 配对码 → 换发长期 key（key 只显示一次）；
3. 之后只填 `base URL`；或扫二维码（二维码内**只放 base URL + 一次性码**，**MUST NOT** 放长期 key）。

### 19.8 多端并发与会话

- 生成锁仍然只有一把：多端 ≠ 并行推理。只有持有在场者能对话，其余返回 `409 persona_elsewhere`（含顶替入口）；队列深度 2，超出 `429 busy`。
- 同一 session 被多端同时写：按 `(session, turn_hash)` 幂等，后到的视为重复，**MUST NOT** 产生两条 episode。
- 有活跃 SSE 连接时 **MUST NOT** 进入 `sleeping`；最后一个连接断开后开始空闲计时。
- 多端 `state` 各自独立（§5.4 I3），互不覆盖。

### 19.9 部署脚本的 MUST

一个"一键部署"脚本必须满足：

1. **幂等**：重复执行不产生第二份配置、第二份数据；
2. **不覆盖**：已存在的 `data/` 只补缺，**MUST NOT** 清空（除非显式 `--purge`）；
3. **可离线**：支持 `--offline <wheel_dir>`，内网机器不依赖公网；
4. **可自检**：起服务 → `/healthz` → `/readyz` → 一次空跑对话，失败回滚配置并打印原因；
5. **打印接入信息**：监听地址、局域网 IP、token **指纹**（不是明文）、二维码路径；
6. **注册服务**：Windows 服务 / systemd，开机自启 + 崩溃重启；
7. **可卸载**：`uninstall` **MUST NOT** 删除 `data/`，除非显式 `--purge`。

### 19.10 验收

TC-N5 ~ TC-N8（§17）。另加一条工程判据：**从零到手机能用 ≤ 10 分钟**（含配对），超出即说明接入流程设计有问题。

---

## 20. 与既有文档的关系

| 文档 | 关系 |
|---|---|
| `README.md` §0–§9 | 语义来源；本规范不重定义 |
| `docs/SECURITY_SPEC.md` | 容器格式与密钥体系；本规范只引用 |
| `docs/conformance` | 三级声明；本规范扩展为 L1–L4 |
| `docs/MIDDLEWARE_LAUNCHER.md` | **Annex A（资料性）**：可行性实测、预算数值、参考实现取舍 |

规范与 Annex 的分工：**本文说"必须怎样"，Annex 说"为什么这样在 4 GB 机器上跑得动"。**

---

## 附录 A：最小实现清单

一个 L2 级 Launcher 的最小构件：

1. HTTP 服务 + 1 worker + 一把生成锁；
2. DataSource 的 `file://` 实现（`vault://` 可后补）；
3. 三个 JSON 的加载与校验 + 一个 SQLite（FTS）；
4. 预算计算与尾部注入；
5. Ollama `/api/chat` 转发 + SSE 转换；
6. 写队列 + 睡眠（指针、快照）；
7. 12 个错误码；
8. `X-SP-*` 头。

不需要：多用户、云端、租约、加密、向量检索。**这些是 L3/L4 的事，不影响 L2 成立。**

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
| 把 `data/` 放进 OneDrive / SMB 共享 | SQLite 锁失效，静默损坏且不报错 |
| 默认模式下多设备同时对话 | `exclusive` 下就是两个"它"；要并行必须走附录 C 的收束，不能只是"同时聊" |
| 把 fork 当成"多端同步" | 复制出的新 UUID 已经是另一个人，不是同步 |
| 绑 `0.0.0.0` 且不开鉴权 | 全屋设备、访客 WiFi、恶意网页都能读你的记忆 |
| 把 Ollama 的 11434 一起暴露 | 模型端口无鉴权，等于开放本机 |
| 用 200 掩盖降级 | 故障不可诊断 |

---

## 附录 C（草案）：并行在场 —— 一个自我，多条线程

> **状态：草案，不属于 v1.1.0 的合规范围。** 目标版本 v1.2。本文的 MUST 是"草案级 MUST"，用于讨论实现，不用于认证。
> 动机：§5.5 的"唯一在场"是把**人的物理约束**套在 AI 上。人同一时间只能在一个地方，因为它有身体；AI 没有。约束不该来自物理，该来自**叙事一致性**——而叙事一致性可以靠"收束"获得，不必靠"禁止并行"。

### C.1 并行的正确形态

不是"允许两个写者"，而是：

**允许多条只追加的时间线，加一个唯一的收束点。**

这个区别决定成败：

- 允许两个写者 → 冲突、覆盖、数据库损坏；
- 允许 N 条只追加的 episode 流 → 没有冲突（追加天然可合并），只有**收束时的取舍**。

所以并行模式的安全性，来自三件事而不是来自锁：**append-only、traits 冻结、单一收束者**。

### C.2 线程模型

```
Thread(
  thread_id      # 每次上线一个
  persona_id
  instance_id    # 设备 / 进程
  started_at, last_seq
  state_json     # 情绪与焦点，线程私有
  status         # live | closed | merged
)
```

- **草案 MUST**：`episode` 带 `thread_id` 与线程内单调 `seq`。全局时间戳不可靠（时钟偏移、离线），`(ts, instance_id, seq)` 才是确定性排序键。
- **草案 MUST**：`state` 属于线程。并行期间两条线程各自情绪演化，互不覆盖——这也正是并行不打架的原因。
- **草案 MUST**：**并行期间 `traits` 冻结**，所有线程只读。想改性格，只能等收束。这一条是并行安全的全部前提；没有它，并行立刻退化成"人格打架"。

### C.3 收束（Collapse）

触发：所有线程 `closed`，或显式 `POST /syspersona/presence/merge`。

```
取结算锁 → 收集 closed 线程的 episode（含未提交队列）
→ 排序 (ts, instance_id, seq) → 按 (persona_id, uuid) 去重
→ 冲突检测（C.6）→ 写 merge_record（参与线程、重叠区间、冲突清单、哈希）
→ 生成收束提示（C.5）→ 解冻 traits → 跑一次 drift（带折扣，C.4）
→ 释放锁
```

**草案 MUST**：`merge_record` 使收束幂等。同一组线程重复 merge，直接返回首次结果，**MUST NOT** 二次计入证据。

### C.4 漂移权重：并行最容易错的地方

并行意味着**同一段时间被经历了 N 次**。若两条线程的证据都按 1.0 计入，一天就攒出两天的证据，人格会"加速老化"——用户会觉得"它怎么突然变了"。

- **草案 MUST**：每条 episode 的 drift 证据权重 `w = 1 / max(1, n_overlap(t))`，`n_overlap(t)` 为该时刻活跃线程数；无法精确计算时退化为 `1 / sqrt(n_threads)`。
- 证据**条数**门槛（≥ 3）仍按原始条数算；**权重**按折扣累加。
- 单次 ±2、7 天累计 ±5 的上限不变——折扣不能让上限失效。

### C.5 收束提示（防止虚构因果）

合并后第一次唤醒，**草案 MUST** 注入一段结构化的"并行说明"：

```
[并行经历] 你有 2 条时间线在 09:00–12:00 重叠：
- 线程 A（书房）：讨论 GitHub 架构
- 线程 B（手机）：红烧肉做法
这两段是同时发生的，不是先后。不要叙述成"先…然后…"。
```

没有这段，模型会把"并行"讲成"先后"，凭空造出因果——这是并行模式独有的幻觉，也是最难在事后发现的一种：它听起来完全合理。

### C.6 冲突三类

| 类型 | 例子 | 处理 |
|---|---|---|
| **事实冲突** | A 记"换 N100"，B 记"先不换" | 两条都留，标 `contested`，检索时并列给出；**不得静默取新** |
| **承诺冲突** | A 答应"明天给"，B 答应"今晚给" | 标 `unresolved_commitment`，唤醒时主动提示 |
| **情绪 / 状态** | A 烦躁，B 愉快 | 丢弃，不合并（state 属于线程，C.2） |

原则：**并行产生的分歧，不交给算法裁决，交给下一次对话。** 系统的职责是"如实标记"，不是"悄悄抹平"。算法一旦替人做决定，用户看到的就不是"它记得"，而是"它替我改了主意"。

### C.7 什么时候不该开并行

- 算力不足：两条线程同时推理互相拖慢（4 GB 档位直接排除）；
- 强连续性任务：同一件事两头切换，收束后仍会显得跳跃；
- 隐私边界不同：一条线程在公用设备上，另一条在私人设备上。

**草案 SHOULD**：默认 `exclusive`；转 `concurrent` 必须是显式动作，并在 `/status` 暴露 `mode` 与活跃线程数。

### C.8 验收（草案）

| ID | 内容 | 判据 |
|---|---|---|
| TC-N14 | 收束幂等 | 同组线程 merge 两次 → 证据只计一次，`merge_record` 唯一 |
| TC-N15 | 冲突标记 | 构造事实冲突 → 两条并列、标 `contested`，无静默覆盖 |
| TC-N16 | 漂移不超速 | 并行 1 天的人格变化 ≤ 串行 1 天的 1.2 倍 |
| TC-N17 | 不虚构因果 | 问"上午先做哪件" → 回答承认同时发生，不编顺序 |

### C.9 与 §5.5 的关系

两者不是替代，是三种模式：

| 模式 | 自我模型 | 适用 |
|---|---|---|
| `exclusive`（v1.1 默认） | 一个人在一个地方 | 单机、算力有限、强连续 |
| `concurrent`（附录 C 草案） | 一个自我，多条线程，收束为一 | 多设备、有中心节点 |
| `fork` | 两个自我 | 备份、实验、送人 |

**关键判断**：并行模式没有推翻"唯一性"，它把唯一性从**空间**（只能在一个地方）挪到了**时间**（最终收束成一条叙事）。身份仍然只有一个——因为 `traits` 只在收束点演化一次，`merge_record` 也只有一份。

---

> 规范的完成标志不是被读完，而是**有人照着它做出了一个能通过 TCK 的实现，而你可以直接拿来用**。
