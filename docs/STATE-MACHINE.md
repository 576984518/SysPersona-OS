# 状态机

> **SysPersona-OS** · 本文件定义：状态机：任务四态、可跳转的关系、超期怎么处理
> 规范全文以仓库 `main` 为准；改了要同步 README / CHANGELOG / 台账。

本文件用三张图描述 SysPersona 的运行状态。图用 Mermaid 编写，GitHub 会直接渲染。

- 图 1：智能体生命周期（一次「活着 → 睡着 → 醒来 → 换机器」的循环）
- 图 2：人格权重演化（一次「证据 → 权重演化 → 再验证 → 重置」的循环）
  > **历史口径**：这是 v1.0 的模型。现行见 `PHILOSOPHY` §10.1——重载即重置，情绪/应激不写权重。
- 图 3：一次唤醒的时序（启动器做了什么）

---

## 图 1 · 智能体生命周期

```mermaid
stateDiagram-v2
    [*] --> Cold

    Cold --> Verifying: 启动
    Verifying --> Sealed: 校验失败（密钥/设备不匹配）
    Sealed --> Verifying: 重试 / 走恢复流程
    Verifying --> Loading: 校验通过

    Loading --> Active: 规则 + 记忆索引注入完成
    Loading --> Recovering: 索引损坏
    Recovering --> Loading: 从快照恢复

    Active --> Active: 对话 / 检索
    Active --> Consolidating: 上下文超过 80% / 显式「睡吧」/ 长时间空闲
    Active --> Degraded: 断网 / 模型不可用
    Degraded --> Active: 恢复

    Consolidating --> Sleeping: 摘要 + 标签 + 索引 + 画像更新完成
    Consolidating --> Recovering: 固化异常
    Sleeping --> Active: 唤醒（重新加载规则与摘要）

    Active --> Switching: 检测到新模型 / 新机器可用
    Switching --> Sandbox: 兼容性自检（工具调用/上下文长度/结构化输出）
    Sandbox --> Switching: 试跑失败 → 保留旧模型
    Sandbox --> Sleeping: 试跑通过 → 夜间原子切换
    Sleeping --> Active: 次日首次唤醒
```

要点：

- **Sealed 不是错误状态，是一个安全状态。** 密钥或设备不匹配时，系统应当「打不开」，而不是「降级打开」。
- **Consolidating 只处理归档，不处理对话。** 期间前台请求排队或降级到只读。
- **Switching 必须可回滚。** 旧模型与旧设定文件版本在切换后保留至少一个周期。

---

## 图 2 · 人格权重演化

这张图是 5.4 与 5.5 两节的合并视图：左边防「变得太快」，右边防「变得太死」。

```mermaid
stateDiagram-v2
    [*] --> Stable

    Stable --> Evidence: 新记忆进入
    Evidence --> Stable: 证据 < 3 条
    Evidence --> Evolving: 证据 ≥ 3 条

    Evolving --> Stable: Δw 绝对值 ≤ 2 且 7 天累计 ≤ 5
    Evolving --> FlipReview: w 绝对值 < 20 且反向证据 ≥ 5
    FlipReview --> Stable: 人工确认
    FlipReview --> Evolving: 拒绝翻转，按强度变化处理

    Stable --> Revalidating: 标签 > 90 天未被引用
    Revalidating --> Stable: 最近 30 天有新记忆支撑
    Revalidating --> Evolving: 无支撑 → 按年 −10% 折扣

    Stable --> Exploring: 固化时分配 1% 探索预算
    Exploring --> Stable: 新主题进入候选集
    Exploring --> Evolving: 候选被反复命中，转为正式偏好

    Stable --> Reset: 每月环境重置
    Reset --> Stable: 记录适配过程，重校准 baseline

    Evolving --> Stress: 极端事件
    Stress --> Stable: 半衰期 14 天衰减回 baseline
```

要点：

- **Stable 是常态。** 大多数时间不应该发生任何权重变化。
- **Evolving 是唯一会改权重的状态**，且有双重闸门（单次幅度、周累计）。
- **Revalidating 只折扣、不删除。** 旧经验要重新被引用才能维持权重。
- **Exploring 只影响候选集**，不触碰内核与边界。
- **Stress 是临时状态**，不会直接改写 baseline。

---

## 图 3 · 一次唤醒的时序

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant L as 启动器
    participant V as 安全层
    participant P as 人格层
    participant M as 记忆层
    participant R as 运行时
    participant X as 模型

    U->>L: 打开
    L->>V: 校验密钥 / 设备 / 容器完整性
    alt 校验失败
        V-->>L: 拒绝
        L-->>U: 进入 Sealed（不给明文）
    else 校验通过
        V-->>L: 解密规则与索引
        L->>P: 加载内核 + 性格结构
        L->>M: 读取 L3 画像 + 最近 L1 摘要
        L->>X: 挂载模型（本地或云端）
        L->>R: 注入规则 + 预取记忆 + 分配上下文预算
        R-->>U: 就绪
        U->>R: 说话
        R->>M: 按标签/向量/时间/重要性检索
        M-->>R: 带来源的片段
        R->>X: 组装提示并请求
        X-->>R: 回复
        R-->>U: 流式输出
    end
```

要点：

- 校验在解密之前，解密在加载之前——顺序不能换。
- 记忆是「按预算预取」，不是「全量加载」；全量加载会在启动时就把上下文挤爆。
- 检索结果必须带来源，否则后续无法做冲突消解与审计。

---

## 状态与代码的对应

| 状态 | 对应模块 | 关键文件 |
|---|---|---|
| Cold / Verifying / Sealed | `launcher` + `vault` | 容器头、设备指纹 |
| Loading / Active | `runtime` | `persona.yaml`、记忆索引 |
| Consolidating / Sleeping | 后台维护进程 | `episodes`、`persona_traits` |
| Switching / Sandbox | 换机器流程 | 模型清单、测试集 |
| Degraded / Recovering | 降级与快照 | `snapshots` |

> 这些状态是**设计约定**，不是某个已发布实现的内部状态机。实现时可以合并状态，但不应跳过「校验 → 解密 → 加载」的顺序。
