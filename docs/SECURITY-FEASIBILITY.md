# 加密层可行性验证报告

> 配套文档：[`SECURITY_SPEC.md`](SECURITY_SPEC.md) · 日期：2026-11-04
> 结论先行：**规范可实施，46/46 项检查通过，Python ↔ Node 双实现可互读。**
> 唯一硬约束：**Python 标准库没有 AEAD**，加密层必须引入一个 Provider（§9 给出选型）。

---

## 1. 结论摘要

| 问题 | 结论 | 证据 |
|---|---|---|
| 字节格式能否被独立实现读懂？ | 能 | Node 读取器解开 Python 写出的容器，5 个文件 hash 全部一致（R15） |
| 错口令/篡改/换设备能否在写盘前拦住？ | 能 | 14 项负向测试全部 `E_*` 失败（R2–R5、R8、R9） |
| 改口令是否必须重加密全部数据？ | 不必 | 只重包 KS：容器 2961 B → 2961 B，载荷密文 2189 B 逐字节未变（R10b） |
| 助记词能否独立恢复？ | 能 | 删除口令 wrap 后，仅凭 24 词仍可解（R6d） |
| 明文包能否安全升级？ | 能 | 默认拒绝；显式允许后原子封装，内容哈希一致（R11） |
| 性能是否可接受？ | 是 | KDF 108–221 ms，AEAD 约 1.9–2.4 GB/s，头部 732 B |
| 能否保持「零第三方依赖」？ | **不能**（仅加密模式） | 标准库无 AES-GCM；见 §9 |
| 有没有被忽略的明文残留？ | **有** | SQLite WAL 与已删除行留下明文（M3/M4） |

总计：**46 项通过，0 失败，约 35 s**（Windows，Python 3.12.14 + OpenSSL 3.5.8）。

---

## 2. 验证方法：三角测量

只用一种实现验证自己，等于没验证。这里刻意让三方互相牵制：

```
┌─────────────────────────┐      ┌──────────────────────────┐
│ Python：格式 + 状态机     │ ───► │ Node/OpenSSL：密码学原语   │
│ vault_poc.py            │      │ oracle.js                │
│ （容器字节、AAD、流程）   │ ◄─── │ （KDF/AEAD/HMAC/Ed25519） │
└───────────┬─────────────┘      └──────────────────────────┘
            │ 写出 .sysvault
            ▼
┌─────────────────────────┐
│ Node：独立读取器          │  只按规范读，不复用 Python 代码
│ reader.js               │  → 证明格式不依赖某种语言
└─────────────────────────┘
```

- **Python 侧**负责：字节布局、AAD 构造、wrap/载荷/签名流程、失败语义。
- **OpenSSL 侧**负责：Argon2id、scrypt、HKDF、AES-256-GCM、HMAC、Ed25519；它不是「我的实现」，格式写错它不会配合。
- **Node 读取器**只读规范、不复用 Python 代码，用来证明「别人能读懂」。

> 明确边界：这不是产品实现，也没有验证「纯 Python 的 AES-GCM」——标准库根本没有该原语。它验证的是**格式与流程**，以及**在有原生 Provider 的前提下**的可行性。

---

## 3. 环境与复现

| 项 | 值 |
|---|---|
| 操作系统 | Windows（x64） |
| Python | 3.12.14（零第三方包） |
| Node | 24.21.0（OpenSSL 3.5.8） |
| 工作目录 | `impl/m4/feasibility/` |

```powershell
# 46 项检查（约 40 秒）
python impl/m4/feasibility/feasibility.py

# 或只跑快速档
python impl/m4/feasibility/feasibility.py --quick
```

产物：`results.json`（机器可读）、`RESULTS.md`（表格）、`kat.json`（KAT 向量）、`kdf.json`、`throughput.json`。

实现文件：

| 文件 | 作用 |
|---|---|
| `vault_poc.py` | 格式、封装/解锁、轮换、迁移、模板分叉 |
| `oracle.js` | OpenSSL 原语（KDF/AEAD/HMAC/Ed25519） |
| `reader.js` | 独立读取器（只依赖 Node 内置） |
| `feasibility.py` | 测试矩阵、测量、KAT 生成 |
| `bip39_english.txt` | BIP39 英文词表（2048 词） |

---

## 4. 结果总表

完整逐条结果见 [`RESULTS.md`](../impl/m4/feasibility/RESULTS.md)，这里按组归纳。

| 组 | 项数 | 内容 | 结果 |
|---|---|---|---|
| R1–R4 基础 | 8 | 往返一致、错口令、四类篡改、算法降级 | 全部通过 |
| R5 内核签名 | 3 | 签名被改、未批准变更、批准后重签 | 全部通过 |
| R6–R7 恢复 | 7 | 助记词生成/回读、仅凭助记词解封、删除口令 wrap 后仍可解、抄错词、备份哈希一致 | 全部通过 |
| R8 绑定 | 2 | 本机可解、换设备失败且同一句报错 | 全部通过 |
| R9 分叉 | 2 | 新 UUID + 空记忆、模板篡改失败 | 全部通过 |
| R10–R11 轮换/迁移 | 4 | 旧口令失效、载荷未变、明文默认拒绝、显式允许后升级 | 全部通过 |
| R12–R15 卫生 | 5 | nonce 不重复、容器无明文痕迹、错误不泄露差异、Node 独立读取 | 全部通过 |
| C1–C3 规范化 | 3 | 浮点/非 NFC 拒绝、键序无关 | 全部通过 |
| K1–K3 KAT | 3 | 生成向量、HKDF/HMAC 与公开向量一致 | 全部通过 |
| M1–M4 测量 | 9 | KDF 耗时、封装/解封、原生 AEAD 吞吐、SQLite 明文残留 | 全部通过（含 2 项「发现风险」） |

**负向安全测试共 14 项**（R2a/b、R3a–d、R4、R5a/b、R6c/e、R8b、R9b、R10a、R11a），全部按预期失败，且失败发生在写盘之前。

---

## 5. 测量数据

### 5.1 KDF（这台机器，单次）

| 档位 | 算法 | 参数 | 中位耗时（单次运行，±15%） |
|---|---|---|---|
| `argon2id-low` | Argon2id | m=19 MiB, t=2, p=1 | **≈110 ms** |
| `argon2id-mid` | Argon2id | m=64 MiB, t=3, p=1 | **≈220 ms** |
| `scrypt-low` | scrypt | N=32768, r=8, p=1 | **≈160 ms** |

对比：`argon2id-mid` 的内存是 low 的 3.4 倍，耗时只多 110 ms——**提高内存成本比提高迭代次数更划算**，这也是 Argon2 的设计意图。

### 5.2 封装 / 解封（端到端，含 4–6 次进程外 Provider 调用）

| `memory.db` | 容器 | 头部 | 载荷 | 封装 | 解封 |
|---|---|---|---|---|---|
| 48 KiB | 10.8 KiB | 732 B | 10.1 KiB | ≈720 ms | ≈750 ms |
| 168 KiB | 33.8 KiB | 732 B | 33.1 KiB | ≈740 ms | ≈740 ms |
| 656 KiB | 127.1 KiB | 732 B | 126.4 KiB | ≈790 ms | ≈740 ms |

**必须扣掉的固定成本**：本原型每次密码学调用都启动一个 Node 进程，单次约 **85 ms**；一次解封要 4 次，即约 **340 ms** 是进程启动，不是密码学。真实实现把 Provider 放在进程内，这 340 ms 不存在。

扣除后估计（进程内 Provider）：

- 48 KiB 容器解锁 ≈ **250 ms**（KDF 157 ms + AEAD/解压 < 10 ms + IO）；
- 656 KiB 容器解锁 ≈ **300 ms**；
- 4 MiB 容器 ≤ **400 ms**（AEAD 2.4 GB/s，4 MiB 仅 ~2 ms）。

这与 §19 的预算（low ≤ 600 ms / mid ≤ 1.2 s）一致。

### 5.3 原生 AEAD 吞吐

| 载荷 | 单次耗时 | 吞吐 |
|---|---|---|
| 1 MiB | 0.6 ms | 1899 MB/s |
| 8 MiB | 3.5 ms | 2409 MB/s |

结论：**AEAD 不是瓶颈，KDF 才是**。把时间花在 KDF 参数与 Provider 质量上，比优化压缩更值得。

### 5.4 体积

- 头部固定 **732 B**（含 2 个 wrap）；
- 载荷是 deflate 压缩后的 zip：测试数据 48 KiB → 10.8 KiB（约 4.4×），真实记忆库文本通常 2–3×；
- **容器大小不等于数据库大小**：如果只按「数据库多大」估算备份空间，会低估也可以高估（取决于压缩率）。

---

## 6. 互操作证据（R15）

1. Python 按规范写出 `arg2id-low` 容器；
2. Node 读取器（仅 `crypto` + `zlib`）解析字节、派生 KEK、解 wrap、验头部 MAC、解密载荷、解 zip、验文件 hash、验内核签名；
3. 读出的 5 个文件 hash 与源文件逐一相等，内核 hash 一致。

这一步证明了四件事：

- 字节布局与 AAD 定义没有歧义（否则 Node 算不出同样的 MAC）；
- 规范化 JSON 在 Python / JavaScript 下一致（否则头部 MAC 对不上）；
- 解压格式是标准 zip，不依赖 Python 的 zipfile 特性；
- 规范里的「任何人都能实现」不是口号。

---

## 7. 验证过程中发现、并已写回规范的约束

验证不是「跑通就算」，下面几条是**实测逼出来的设计修正**：

### 7.1 payload AAD 不能覆盖整个头部

最初设想用「整个头部字节」做载荷 AAD。问题：改口令会改变头部（salt、wrap、mac），载荷就得跟着重加密——**改一次口令 = 重写整个记忆库**，这既慢又增加损坏风险。

修正：payload AAD 只绑定**不可变字段**（`container_id`、`content.salt`、`persona.id`）；头部元数据另用 `mac` 覆盖。

结果：R10b 实测改口令后容器字节数不变、载荷密文逐字节未变。

### 7.2 需要单独的头部 MAC

只用 payload AAD 无法保护 `persona.version`、`created_at` 这类「不影响解密、但会影响判断」的字段。加入 `K_hdr` 派生的 HMAC 后，R3b（改头部元数据）从「无法发现」变成 `E_INTEGRITY`。

### 7.3 SQLite 的明文不止在主库

M3/M4 实测：

- 提交后 `memory.db-wal` 中**能直接搜到明文**（`SECRET_MARKER_12345` → True）；
- `wal_checkpoint(TRUNCATE)` + 关闭连接后，旁路文件消失；
- 删除行后不 `VACUUM`，明文**仍留在主库空页**（True）；`VACUUM` 后消失（False）。

因此规范把「checkpoint → 关闭 → 收集」写成封装前的强制步骤（§10），并建议删除敏感行后 `VACUUM`。这条不写进规范，实现者几乎一定会踩。

### 7.4 进程外 Provider 的隐性成本

每次调用 85 ms；一次解锁 4 次调用 = 340 ms 固定开销，且密钥要经过 IPC。规范因此要求 Provider **必须可进程内调用**，跨进程「可以用但自担风险」。

### 7.5 降级必须是显式的

明文包与密文包在同一扩展名下并存。如果 `import` 自动识别并接受明文，攻击者只要把容器换成明文包，就能让系统在「无保护」状态下启动。规范因此默认拒绝（`E_POLICY`），并要求 `require_encryption` 闸门。

---

## 8. 明文残留与运行边界（必须让用户知道）

| 位置 | 实测 | 规范处置 |
|---|---|---|
| WAL | 含明文 | 封装前 checkpoint(TRUNCATE) |
| 已删除行 | 含明文 | 建议 VACUUM |
| 运行中的工作目录 | 明文 | v1 明确接受（「睡着时加密」） |
| 解锁暂存目录 | 短暂明文 | 原子替换 + 失败清理 |
| 交换/休眠/崩溃转储 | 可能含明文 | 文档声明，建议全盘加密 |

**结论**：加密层保证的是「静止时、离开本机时是密文」，不是「运行时不可见」。把它宣传成后者，是虚假安全。

---

## 9. 零依赖还能不能保住？

这是本次验证最重要的工程结论。

| 方案 | 可行性 | 评价 |
|---|---|---|
| A. 纯标准库 | **不可行** | `hashlib` 无 AES-GCM，`hmac` 只是 MAC；scrypt/Argon2 有（scrypt 有，Argon2 无） |
| B. 可选原生依赖 `cryptography` + `argon2-cffi` | **可行，推荐** | 有 wheel，CPython 3.10–3.13 全平台；审计成熟；进程内、GB/s 级 |
| C. 外部进程 Provider（OpenSSL/Node） | 可行（本报告即用） | 每次 85 ms；密钥过管道；不建议生产 |
| D. 自写纯 Python AEAD | 技术上可行，**不建议** | 性能低 2–3 个数量级；侧信道与审计成本高；加密代码最不该「自己写」 |

**建议**：

1. M4 把加密做成**可选特性**：未安装 Provider 时，系统以「未加密模式」运行并明确提示，不静默降级；
2. 加密模式下，`cryptography` 与 `argon2-cffi` 作为 `pip install syspersona[crypto]` 的额外依赖；
3. 保留 `scrypt` 作为 KDF 后备（标准库可用），但 AEAD 仍需要 Provider；
4. CI 增加一个安装 `cryptography` 的 job 跑加密回归，M1 的零依赖 job 保持不变。

> 一句话：**M1 证明了「不依赖第三方库也能跑通链路」；M4 要证明的是「加密不自己造轮子」。** 这两件事不矛盾——把算法做成 Provider，正是为了不把系统绑死在某一种实现上。

---

## 10. KAT 向量（节选）

完整文件：[`kat.json`](../impl/m4/feasibility/kat.json)。Provider：OpenSSL 3.5.8。

```json
{
  "hkdf_rfc5869_case1": {
    "ikm_hex": "0b0b…0b",
    "salt_hex": "000102030405060708090a0b0c",
    "info_hex": "f0f1f2f3f4f5f6f7f8f9",
    "L": 42,
    "okm_hex": "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
  },
  "aes256gcm_empty": {
    "key_hex": "00…00",
    "nonce_hex": "00…00",
    "ciphertext_and_tag_hex": "530f8afbc74536b9a963b4f1c4cb738b"
  },
  "bip39": {
    "entropy_hex": "00…00",
    "mnemonic": "abandon ×23 + art"
  }
}
```

三项都与公开标准一致（RFC 5869、NIST GCM、BIP39），说明 Provider 本身没有被「调成能配合我的实现」的样子。

---

## 11. 未验证 / 需要 M4 补做的部分

| 项 | 状态 | 说明 |
|---|---|---|
| Argon2id 的 `argon2id-high`（256 MiB） | 未测 | 本机可用，但耗时/内存需在目标设备上标定 |
| 并发与锁 | 未测 | 规范要求单写者，原型未实现锁 |
| 崩溃注入（rename 前后断电） | 未测 | 需要故障注入工具 |
| TPM / OS 密钥库（device-key） | 未测 | 只有 `device` 指纹模式验证了输入链路 |
| 移动端 / ARM | 未测 | KDF 参数需重标定 |
| 长时运行的内存清零 | 无法保证 | 语言层限制，已在 §21 声明 |
| 多设备同步 | 未测 | v1 只支持「搬运/恢复」，不支持并发同步 |

---

## 12. 给 M4 的实施顺序（建议）

1. **抽象 Provider 接口**（§15），先把 `cryptography` 接进来，跑通 KAT；
2. **实现容器读写**（§7），只做 `seal/unseal/verify`，不做 CLI 美化；
3. **接内核签名与批准流程**（§9），把 M1 的「kernel 只有人能改」升级成密码学约束；
4. **接 SQLite 生命周期**（§10）：checkpoint → 关库 → 封装；
5. **实现助记词与恢复**（§12）；
6. **实现迁移**（§14.3）：默认拒绝明文，显式允许后升级；
7. **接公开模板与 fork**（§13）；
8. **补 CI**：加密 → 解密 → 断言；负向矩阵 14 条；
9. 最后再谈「增量封装 / 分块」——先让它正确，再让它快。

---

## 13. 复现清单

```text
docs/
├── SECURITY_SPEC.md         规范
└── SECURITY-FEASIBILITY.md  本文件

impl/m4/feasibility/
├── README.md                声明：这是验证夹具，不是产品代码
├── vault_poc.py             格式与流程原型
├── oracle.js                OpenSSL 原语
├── reader.js                独立读取器
├── feasibility.py           46 项检查
├── kat.json                 KAT 向量
├── results.json             机器可读结果
├── RESULTS.md               逐条表格
├── kdf.json / throughput.json
└── bip39_english.txt        BIP39 词表
```

运行环境需要 `node`（≥ 22，需要 `crypto.argon2`）。若 Node 不可用，`scrypt-low` 档仍可验证格式，但 Argon2id 相关项会跳过——**跳过必须显式标注，不得算通过**。
