# M2 语义检索（v0.9.0-dev）

解决一个具体问题：**换个说法就召回不到**。

M1 的检索是词面闸门——`overlap == 0` 直接出局。M2 加一条向量通道，用 RRF 融合，但仍然遵守原则 9：**向量只影响 relevance，人格/画像/关注仍是加分项，不是门槛。**

## 启用

按可用性自动选择，不强制新依赖：

| Provider | 需要什么 | 说明 |
|---|---|---|
| `ollama` | 本地 ollama + `ollama pull bge-m3` | 中文强，零 Python 依赖 |
| `fastembed` | `pip install -r impl/m2/requirements-m2.txt` | 离线 ONNX，首次下载模型 |
| `hash` | 无 | 字 n-gram 哈希投影，**不是语义检索**，测试与兜底 |

`config.json`：

```json
{
  "retrieval": {
    "mode": "auto",          // auto | lexical | hybrid
    "provider": "auto",      // auto | ollama | fastembed | hash
    "model": "",             // 留空用 provider 默认
    "floor": 0.25,           // 余弦相似度地板
    "candidates": 50,
    "index_on_write": true
  }
}
```

`mode=auto` 时：provider 是语义模型才走 hybrid，否则保持 lexical。想强制走向量：`--retrieval hybrid`。

## 用法

```bash
# 状态：provider、覆盖率、维度
python impl/m1/syspersona.py --home ./data embed status

# 旧库补索引 / 全量重建
python impl/m1/syspersona.py --home ./data embed reindex
python impl/m1/syspersona.py --home ./data embed reindex --all

# 检索对照
python impl/m1/syspersona.py --home ./data recall --query "老机器休眠怎么解决的" --retrieval hybrid
python impl/m1/syspersona.py --home ./data eval --queries examples/eval/m2_queries.json --retrieval lexical
python impl/m1/syspersona.py --home ./data eval --queries examples/eval/m2_queries.json --retrieval hybrid
```

## 打分

```
lexical_rank  ─┐
vector_rank   ─┴─ RRF → relevance(0..1)
score = 0.6*relevance + 0.25*recency + 0.15*importance
      + 0.25*profile + 0.15*persona
```

- RRF：`1/(60+rank)` 求和后按峰值归一化；只在一侧出现的候选按另一侧 0 计。
- 余弦 < `floor` 的候选丢弃，避免「意思相近、事实无关」的噪声。
- 助手回复仍 ×0.7；归档条目仍走标签 + 摘要向量。

## 索引

- **写入时**：`chat` 落库后立即索引（失败不影响对话）。
- **睡眠时**：`sleep` 补建缺失索引（每次最多 200 条）。
- **命令行**：`embed reindex`（补缺）/ `--all`（全量）。
- 内容哈希不变就跳过；归档改写了内容会自动重建。
- 删除 episode 后 `embed prune` 清理失效向量。

## 性能

| 路径 | 10k×512 预计 |
|---|---|
| `math.sumprod`（Python 3.12+，C 实现） | 10–30 ms |
| `numpy`（已安装时自动用） | 2–5 ms |
| 纯 Python 兜底 | 0.5–1 s（不建议大库） |

小库实测：检索 ~3–5 ms（包含一次哈希 embedding）。

## 评测

`examples/eval/m2_queries.json` 是**模板**：`expect` 按子串匹配，命中任一即算该查询命中。请在你自己的记忆库上跑；`lexical` 与 `hybrid` 各跑一次，看 `recall@k` 与 MRR 的差值。

CI 只跑 hash 基线的确定性测试（`impl/m2/tests`），不下载模型、不依赖 ollama。

## 本版不做

- 事实（facts）与画像摘要的向量索引（先做 episode）
- ANN / 向量库（暴力余弦足够，规模上来再说）
- rerank 模型
- 云端 embedding（隐私边界）

## 测试

```bash
python -m unittest discover -s impl/m2/tests -v
```

9 项：provider 确定性、存储幂等、余弦地板、RRF 排序、增量索引、M1 hybrid 接线与 auto 回退。
