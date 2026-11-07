# Launcher L2 参考实现（PoC）

零第三方依赖的中间层启动器，用来证明 [`docs/LAUNCHER_SPEC.md`](../../docs/LAUNCHER_SPEC.md) 的 **L2（可连续）** 能跑通：

```
加载人格 → 预算内检索记忆 → 尾部注入 → 转发模型 → 异步落库 → 睡眠固化
```

**这不是产品。** 它证明协议可实施、给出可测的数字，然后停在 L2。L3/L4（加密、唯一在场、多设备、局域网鉴权）刻意不做。

**它只是一个形态。** 按规范 §2.4，合规判据是「加载 / 组装 / 路由 / 归档」这四件事，不是"有没有 8000 端口"。本实现是 **Profile G（gateway）**；同一套 `Launcher` 类嵌进手机 App、不起 HTTP，就是 **Profile E**，同样合规。

---

## 快速开始

```bash
# 离线跑（mock 模型，不需要 ollama）
python impl/launcher/launcher.py --home ./data --port 8000 --provider mock

# 接本地 ollama
python impl/launcher/launcher.py --home ./data --port 8000 \
  --provider ollama --model qwen2.5:1.5b --num-ctx 4096
```

然后任何支持自定义 base URL 的客户端都能连：

```
Base URL: http://127.0.0.1:8000/v1
API Key : 任意（本实现不校验，L4 才有）
Model   : syspersona/mock-a 或 syspersona/qwen2.5:1.5b
```

`data/` 首次运行自动生成：

```
data/
├── persona/{kernel,traits,state}.json
├── memory.db                 # SQLite + FTS5
├── resume.json               # 恢复指针
├── journal.state             # clean | sleeping
└── snapshots/                # 睡眠快照
```

---

## 验证

```bash
python impl/launcher/smoke.py        # 12 项，全绿
python impl/launcher/bench.py 30     # 开销测量
```

`smoke.py` 逐条对应规范 §17 的测试：

| 测试 | 内容 | 结果 |
|---|---|---|
| TC-C1 / C1b | OpenAI 兼容（非流式 + SSE） | PASS |
| TC-C2 | 前缀稳定：第 3 轮缓存命中率 ≥ 0.5 | PASS（0.63 / 0.68 / 0.78） |
| TC-C3 | 记忆注入且不越预算 | PASS |
| TC-C4 | `X-SP-Bypass` 零注入 | PASS |
| TC-M1 | 注入项带来源 id | PASS |
| TC-M2 | 热路径模型调用数 = 请求数 | PASS（8 次请求 = 8 次调用） |
| TC-M3 | 人格隔离（另一个 home 检索不到） | PASS |
| TC-S1 | 睡眠：先写指针再清理，journal 归 clean | PASS（21 条结算） |
| TC-S2 | 脏标记被识别并重放 | PASS |
| TC-S3 | 快照落盘且非空 | PASS（60 KB） |
| TC-E1 | 空 messages → 400 + 错误码 | PASS |

### 实测开销（4 核 / 3.9 GB，mock 模型，30 轮）

| 指标 | 值 |
|---|---|
| 单次请求总耗时（含 HTTP + JSON） | mean **29.5 ms** / p95 **35.2 ms** |
| 其中检索 + 组装 | mean 18.1 ms / p95 25.2 ms |
| 睡眠（60 条 episode → 结算 + 快照） | **324 ms** |
| 快照体积 | 110 KB |
| 第 30 轮前缀缓存命中率 | 0.72 |

规范 §6.1 给检索的目标是 p95 15 ms，本实现是 **25 ms**，超出 10 ms。原因是纯 Python 逐条打分 + 每轮最多 100 条候选；规模再大要换 numpy 或把候选降到 30 条以内。这个偏差写在这里，不藏。

---

## 五个关键设计（都有对应的失败模式）

**1. 记忆放尾部，不放开头 system。**
人格块字节稳定（`canonical()`：排序键 + 紧凑分隔符 + 无时间戳），记忆与状态拼在最后一条 user 消息后面。这样前缀（system + 历史）可以持续命中 KV cache；反过来，每轮变化的内容放在开头，会让后面所有 token 的缓存失效。

**2. 中文用双字 gram 建索引。**
SQLite FTS5 的 `unicode61` 不切中文，整段会被当成一个 token，导致「休眠」搜不到「NAS 休眠脚本」。这里把 CJK 连续串转成双字 gram（休眠 / 眠脚 / 脚本），查询同法，等价于一个极简分词器。

**3. 先算预算再取内容，超了就丢，不静默截断。**
`budget_in = num_ctx − num_predict − 128`；记忆占 22%；历史从最近往回收。仍然超就先丢最老的历史，再丢记忆尾块，最后才拒绝（`413 budget_exceeded`）。任何丢弃都会置 `X-SP-Truncated`。

**4. 热路径不调用模型。**
抽取、打标签、算重要性全部是规则（正则 + 词频 + 长度），一轮对话只花一次模型调用——就是生成本身。TC-M2 直接断言这个数字。

**5. 单写入者 + 异步队列。**
请求返回后 episode 才入队，批量提交（20 条 / 200 ms）。SQLite 用 WAL + `synchronous=NORMAL`；只有快照前才 checkpoint。

---

## 与规范的对齐

| 规范条款 | 实现位置 |
|---|---|
| §7 请求流程（11 步） | `Launcher.chat` / `Handler._chat` |
| §8 组装与预算 | `Launcher.build` / `_budget` / `_memory_block` |
| §9 检索与冷热分离 | `Memory.search` / `Writer` |
| §6.2 睡眠（指针先于清理） | `Launcher.sleep` |
| §6.4 崩溃恢复 | `Launcher._recover` |
| §11 错误码 | `BadRequest` / `OverBudget` / `ModelUnavailable` |
| §5.5 唯一在场 | **未实现**（返回 501，L4 才做） |
| §13 加密 | **未实现**（L4，`seal` 返回 501） |

---

## 刻意不做的

- 加密容器、`seal/unlock`、审计链（L4）
- 唯一在场、交接、租约、多设备（L4）
- 局域网鉴权、配对码（L4）
- 多用户 / 多人格（一个 `--home` 一个人格）
- 向量检索（靠双字 gram 的词面通道足够证明 L2）
- 真实 tokenizer（用估算 + 真实用量校准的位置）

## 已知偏差

1. **mock 的缓存命中率是"前缀稳定性代理"**，不是真的 KV cache 计数。它比较的是上一轮前缀是否是本轮前缀的前缀——这决定真模型会不会命中，但不是命中本身。接 ollama 时改用 `prompt_eval_cached_count`。
2. **中文分词很粗**：双字 gram 会产生一些无意义匹配（如「的休」「眠脚」），靠后续的重叠计分与 22% 预算兜住。
3. **单人格式**：一个进程一个 `persona`，多人格要起多个实例或等 L4 的 binding。
4. **没有真实并发测试**：单生成锁 + 队列深度未压测。

---

## 下一步

按 HANDOFF 的优先级：M2 收尾（facts/摘要向量）与 M4.2（publish/fork），或者接着做 L3 的 DataSource 抽象（`file://` / `vault://` / `https://` 三形态等价）。L3 的第一步不需要加密：先让同一个 `persona` 从目录、从 tar 包、从远端 HTTP 读出来是同一份字节。
