# M4 安全层（v0.8.0-dev）

把 `docs/SECURITY_SPEC.md` 落成代码：**封装 / 解封 / 验签 / 改口令 / 助记词恢复 / 明文包迁移**。

M1 的检索、漂移、清理逻辑一行未改；加密是外挂的一层。

## 依赖

核心格式与流程只用标准库；密码学原语必须由 Provider 提供：

```bash
pip install -r impl/m4/requirements-m4.txt
```

未安装时，`vault` 命令会提示安装，`syspersona.py` 的其它命令不受影响。

## 用法

```bash
# 1) 正常用 M1 工作目录
python impl/m1/syspersona.py --home ./data init --persona examples/persona.m1.json

# 2) 封装（默认 argon2id-mid；--recovery 生成 24 词恢复短语）
python impl/m1/syspersona.py vault seal --home ./data --out me.vault --recovery

# 3) 校验 / 只看元数据
python impl/m1/syspersona.py vault verify --file me.vault
python impl/m1/syspersona.py vault inspect --file me.vault

# 4) 解封到新目录（非空目录需 --force，并会保留备份）
python impl/m1/syspersona.py vault unlock --file me.vault --home ./data2

# 5) 改口令：只重包根密钥，载荷密文不动
python impl/m1/syspersona.py vault passwd --file me.vault

# 6) 助记词恢复
python impl/m1/syspersona.py vault recover --file me.vault --out ./data3 --mnemonic-stdin

# 7) 旧明文包迁移（默认拒绝，必须显式允许）
python impl/m1/syspersona.py vault import --file old.syspersona --home ./data4 --allow-plaintext
```

口令只从 TTY 或 `stdin` 读，绝不进 argv。自动化请用 `--passphrase-stdin`。

也可以直接调模块：

```bash
python -m m4.vault --help          # 需在 impl/ 下，或设置 PYTHONPATH
```

## 与规范对应的实现点

| 规范 | 代码 |
|---|---|
| §6 密钥体系 | `provider.py`（KDF/HKDF）、`container.py`（wraps/VK） |
| §7 容器格式 | `container.py`（`seal_container` / `open_container`），头部 + zip 载荷 |
| §8 规范化 JSON | `canonical.py` |
| §9 内核签名 | `container.py`（`manifest.kernel.sig`），内核变更需 `--approve-kernel-change` |
| §10 SQLite | `home.py`（`wal_checkpoint(TRUNCATE)` → 关库 → 收集；快照一起封装） |
| §12 恢复 | `bip39.py` + recovery wrap |
| §14 失败语义 | `errors.py`；错口令/错设备统一 `E_UNLOCK`「保险库无法打开」 |
| §15 Provider | `provider.py` 的 `Provider` 协议与注册表 |
| §17 并发/原子 | `home.py`（锁、暂存目录、备用目录） |

## 测试

```bash
python -m unittest discover -s impl/m4/tests -v
```

26 项：KAT（RFC 5869 / NIST GCM / Argon2id / scrypt / BIP39）+ 负向矩阵 N1–N14（N11 模板留给 M4.2）。

CI：`.github/workflows/encryption.yml`，与零依赖的 `simulate` job 并行。

## 本版不做

- 公开模板 `publish` / `fork`（Ed25519，M4.2）
- `device-key`（OS 密钥库）与账号绑定；目前只有 `none` / `device` 指纹
- 分块/增量封装（>1 GiB 或只重写变化页）
- TPM、多设备同步、防回滚

## 安全边界（务必读）

- 只保证**静止时**是密文；解封后明文在工作目录与内存里。
- 交换分区、休眠文件、崩溃转储都可能含明文，建议开全盘加密。
- `device` 绑定只防随手拷贝，不防有备而来的人。
- `--purge` 删除的是收集过的明文文件与 `snapshots/`；若目录里还有别的文件，需要自己清理。
