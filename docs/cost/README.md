# data/cost — 成本原始数据（脱敏）

> 汇总结论见 [`docs/COST_BASELINE.md`](../COST_BASELINE.md)。
> 这里放的是**可复算的原始数据**，不是结论。

## 文件

| 文件 | 内容 | 列 |
|---|---|---|
| `cost-2026-11-03_2026-11-09.csv` | 按天聚合的成本（CNY） | 起止时间、模型、计费类型、金额、币种 |
| `amount-2026-11-03_2026-11-09.csv` | 按天聚合的用量与单价 | 起止时间、模型、token 类型、单价、数量 |

## 脱敏说明

上传前删掉了三类列：`user_id`、`api_key_name`、`api_key`。

- `user_id` 是账号标识（UUID），对复算没有用；
- `api_key` 本来就是空的，但列名保留下来仍会诱导误填；
- 其余列（模型、单价、数量、金额）**一字未改**，所以任何人都能按 `单价 × 数量` 复算到同一分钱。

## 口径

- **这是 DeepSeek API 一侧**的账（`deepseek-flash`）；
- **不含硅基流动**（Hermes 侧）——那边 2026-11-09 前走免费额度，之后才开始计费，
  等我拿到硅基的导出，会单独放一个 `siliconflow-*.csv`，两边不合并相加；
- 按天聚合，没有单次调用的明细。

## 怎么用

```python
import csv
rows = list(csv.DictReader(open("docs/cost/amount-2026-11-03_2026-11-09.csv", encoding="utf-8")))
tok = sum(float(r["amount"]) for r in rows if r["type"] == "input_cache_hit_tokens")
```

复算校验：`sum(单价 × 数量)` 应当与 `cost-*.csv` 的总和一致（¥77.51，±0.02 舍入）。
