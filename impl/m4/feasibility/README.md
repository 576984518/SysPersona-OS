# M4 可行性验证夹具（不是实现）

这个目录**不是** M4 加密层的实现，而是为了回答一个问题：

> 把《[安全与加密规范](../../docs/SECURITY_SPEC.md)》写成字节、流程和失败语义之后，它真的能跑、能互操作、能在写盘前拦住篡改吗？

结论与数据见《[可行性验证报告](../../docs/SECURITY-FEASIBILITY.md)》。

## 组成

| 文件 | 作用 |
|---|---|
| `vault_poc.py` | 按规范实现容器格式与状态机（Python，仅做格式） |
| `oracle.js` | 密码学原语，交给 OpenSSL（Node 24） |
| `reader.js` | 独立读取器：只依赖 Node 内置，验证「别人也能读懂」 |
| `feasibility.py` | 46 项检查 + 测量 + KAT 生成 |
| `bip39_english.txt` | BIP39 英文词表 |
| `kat.json` / `results.json` / `RESULTS.md` | 验证产物 |

## 运行

```bash
# 需要 Node >= 22（crypto.argon2）
python impl/m4/feasibility/feasibility.py

# 若 node 不在 PATH：
# PowerShell: $env:SYSPERSONA_NODE="C:\Program Files\nodejs\node.exe"
# bash:       SYSPERSONA_NODE=/usr/local/bin/node python ...

# 快速档（跳过 argon2id-mid 与部分计时）
python impl/m4/feasibility/feasibility.py --quick
```

约 35 秒。所有产物写在同目录。

## 边界

- 它**不**实现产品 CLI、不接 `syspersona.py`、不写用户数据格式；
- 它**不**验证纯 Python 的 AEAD（标准库没有，这正是需要一个 Provider 的原因）；
- 它**不**代表 M4 可以照着抄——它只证明规范自洽且可实现；
- 它**不**进 CI（依赖外部 Node）。M4 实施时应把负向矩阵搬进 CI，并换成进程内 Provider。
