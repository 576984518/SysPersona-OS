# M1 · 最小闭环

这不是产品，是**唯一目的**的验证程序：证明「人格 + 记忆 + 模型」三者可以拆开，并且换模型之后，它还记得之前的事。

- 依赖：Python 3.10+，**只用标准库**
- 规模：单文件约 430 行
- 模型：默认 `mock`（确定性回复），可选 `ollama:<model>`
- 不包含：加密、设备绑定、漂移/抗僵化、向量检索、多任务调度（属 M3–M4）

## 数据布局

```
data/
├─ persona.json     人格（init 时从 examples/persona.m1.json 复制）
├─ config.json      当前模型与会话 id
├─ memory.db        SQLite：episodes / facts / profile / snapshots / audit_log
└─ snapshots/       每次归档前的快照
```

## 一次完整演示

```bash
python syspersona.py --home ./data init --persona ../../examples/persona.m1.json
python syspersona.py --home ./data chat  --say "你好，我叫黄，我在做一个 NAS 调优项目，我喜欢简洁的回答。"
python syspersona.py --home ./data sleep
python syspersona.py --home ./data switch-model --model mock-b
python syspersona.py --home ./data chat  --say "我叫什么？我在做什么？"
python syspersona.py --home ./data status
```

实际输出（Windows / PowerShell，UTF-8）：

```
（mock-a · 阿岚）我还不了解这件事，先记下来。
归档完成：2 条对话 → 3 条事实
  新事实：称呼=黄；在做=NAS 调优项目；喜欢=简洁的回答
模型已切换：mock-a → mock-b（新会话 bb00c58a）
（mock-b · 阿岚）我记得：在做：NAS 调优项目；称呼：黄
人格：阿岚  v0.1.0-m1  id=b7d1c2a4
模型：mock-b   会话：bb00c58a
记忆：4 条对话，3 条事实，2 条待归档
画像：称呼：黄；在做：NAS 调优项目；喜欢：简洁的回答
```

**关键点**：第 4 步换了模型，第 5 步它仍然答得出姓名与项目。模型只是被换掉的执行者；人格与记忆留在文件里。

## 对应规范

| 规范 | 这里的实现 |
|---|---|
| 4.1 人格文件 | `init` 读取 JSON，`kernel.identity` 注入回复；版本写入 `persona_versions` |
| 6.2 写入管线 | `chat` 落库为 episode（未抽取） |
| 7.3 睡眠 | `sleep`：抽取事实 → 更新画像 → 标记已归档 → 写快照 |
| 7.5 迁移 | `switch-model`：换模型、开新会话；记忆不动 |

## 已知限制

- **人格用 JSON 而非 YAML**：避免引入 PyYAML。YAML 解析放 M2。
- **抽取是正则**：只认「我叫 / 我在做 / 我喜欢 / 我讨厌」四种句式，不是模型抽取。
- **检索是意图映射 + LIKE**：没有向量、没有时间衰减、没有重要性重排。
- **单进程**：没有后台维护、没有配额、没有热重载。
- **无加密**：`persona.json` 是明文，不要放真实隐私。

## 下一步（M2）

1. 向量检索 + 时间衰减 + 重要性重排（对应 6.3）；
2. YAML 人格解析，直接读 `examples/persona.example.yaml`；
3. 抽取改用模型（本地小模型即可），替换正则；
4. 归档时写 `persona_traits`，让偏好权重开始漂移（对应 5.4）。
