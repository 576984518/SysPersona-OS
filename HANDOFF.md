# HANDOFF · 交接说明

> 用途：下一次会话从这里开始，不必重读全部历史。读完这份 + `README.md` 的四节，就能接着干。

---

## 一、当前状态

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/576984518/SysPersona-OS |
| 最新版本 | `v0.8.0`（M4 加密核心） |
| CI | 绿：14 天仿真 12 条断言 + 换模型冒烟 + 导出导入冒烟 + 加密端到端 |
| 代码 | `impl/m1/syspersona.py`（约 2000 行，零依赖）+ `impl/m4/vault/`（加密层） |
| 数据布局 | `persona/{kernel,traits,state}.json` + `memory.db` + `snapshots/`；静止时是 `*.vault` |
| 运行环境 | Python 3.10+；mock 模型可离线跑；加密需 `pip install -r impl/m4/requirements-m4.txt` |
| 安全规范 | `docs/SECURITY_SPEC.md` v1.0 草案（已实现 v1 核心） |
| 可行性验证 | 46/46；`docs/SECURITY-FEASIBILITY.md` + `impl/m4/feasibility/` |

---

## 二、已实现（能跑、有验证）

| 能力 | 对应规范 | 入口 |
|---|---|---|
| 分层人格文件 + 写权限 | 4.4 | `init` / `layers` |
| 人格漂移（单次、周额度、回弹、极性闸门） | 5.4 | `sleep` / `flip-review` |
| 抗僵化（再验证、僵化指数、探索候选） | 5.5 | `sleep` / `simulate 90d` |
| 情绪与应激（分钟级 / 小时级） | 4.1 | `chat` |
| 画像图（边权衰减与累积） | 6.5 | `sleep` |
| 主观检索（画像 + 关注 + 人格） | 6.3 / 原则 9 | `chat` / `recall` |
| 分级清理 + 压力驱逐 + 恢复指针 | 6.4 / 7.3 | `prune` / `sleep` |
| 归档打标签，压缩后仍可召回 | 6.4 | 自动 |
| 迁移包（export / import / verify） | 7.5 | 三条命令 |
| 常驻与仿真 | — | `serve` / `simulate` |
| **安全规范（v1）** | §8 | `docs/SECURITY_SPEC.md` |
| **规范可行性验证（46 项）** | §8 | `impl/m4/feasibility/` |
| **加密容器（seal/unlock/verify/passwd/recover/import）** | §6–§14 | `syspersona vault ...` |
| **内核签名 + 变更批准** | §9 | `vault seal --approve-kernel-change` |
| **SQLite 生命周期（checkpoint → 关库 → 封装）** | §10 | `seal` 自动 |
| **设备指纹绑定（none / device）** | §6.4 | `seal --bind device` |

---

## 三、尚未实现（按建议优先级）

1. **M4.2：公开模板 publish / fork**（Ed25519 签名、复制即新 UUID）
2. **M4.2：device-key（OS 密钥库）/ account 绑定、防回滚、增量分块**
3. **向量/语义检索**（词面之外，换个说法就召回不到）
4. **模型摘要替代截断**（现在「压成摘要」是截断，违反原则 8 的精神）
4. **图的多跳扩散**（`NAS → 项目 → 休眠` 走不通）
5. **真模型适配**（只有 mock / ollama，没有云端 API）
6. **探索预算执行、自动环境重置**（现在只出候选与提醒）

---

## 四、不可违反的约束

改动任何代码前先确认没有违反这些：

1. **公理**：模型不构成人格；人格由文件与记忆在每次唤醒时重建。
2. **原则 8**：上下文不是被清空的，是被压缩的；清理前必须先有可检索的替代物。
3. **原则 9**：检索是主观的——关注度高、喜欢的排前面，无关的往后。
4. **自迭代边界**：技能与偏好可自改；内核（身份/价值观/边界）与「记忆写入规则」不可自改。
5. **权限分层**：`kernel.json` 只有人能改；`traits.json` 只在睡眠结算写；`state.json` 可实时写。
6. **文件是真相，数据库是缓存**：睡眠后回写 `traits.json`，重启以文件为准。
7. **加密模式不追求零依赖**：标准库没有 AEAD；必须走 Provider（规范 §15），不得自造密码学。

---

## 五、如何验证（改完必须跑）

### 5.1 M1 回归（现有 CI）

```powershell
python impl/m1/syspersona.py --home ./sim init --persona examples/persona.m1.json
python impl/m1/syspersona.py --home ./sim simulate --scenario examples/scenario.14d.json
# 期望：断言通过：12 条，退出码 0

python impl/m1/syspersona.py --home ./sim export --out ./me.syspersona
python impl/m1/syspersona.py verify --file ./me.syspersona
```

CI（`.github/workflows/simulate.yml`）跑的就是这三段，本地过了基本就绿。

### 5.2 加密层（需可选依赖）

```powershell
pip install -r impl/m4/requirements-m4.txt

python -m unittest discover -s impl/m4/tests -v
# 期望：26 tests OK（KAT + 负向矩阵）

python impl/m1/syspersona.py vault seal --home ./sim --out ./me.vault --recovery
python impl/m1/syspersona.py vault verify --file ./me.vault
python impl/m1/syspersona.py vault unlock --file ./me.vault --home ./sim2
# 期望：sim/memory.db 与 sim2/memory.db 哈希一致
```

CI：`.github/workflows/encryption.yml`（与零依赖的 `simulate` job 并行）。

### 5.3 规范可行性夹具（可选，需 Node ≥ 22）

```powershell
python impl/m4/feasibility/feasibility.py
# 期望：通过 46/46
```

这是**验证夹具**，不是产品代码：它用 Python 写格式、用 OpenSSL 提供原语、用独立 Node 读取器验证互操作。

---

## 六、下一个任务：M4.2 / 向量检索

M4 核心已完成（见 `impl/m4/README.md`）。剩下的按优先级：

1. **M4.2 公开模板与 fork**：`vault publish` / `vault fork`，Ed25519 签名，复制即新 UUID、空记忆；规范 §13 已定，实现缺口在 `template.py`。
2. **M4.2 更强绑定**：`device-key`（DPAPI / Keychain / libsecret）、账号绑定；防回滚计数器。
3. **M2 向量检索**：词面之外换个说法就召回不到；这是检索质量的下一个台阶。

### 做 M4.2 前先读

- 规范 §13（模板与 fork）、§6.4（绑定）、§12（恢复）；
- `impl/m4/vault/container.py` 的 `seal_container` / `open_container`；
- 负向矩阵 N11（模板篡改）目前在 `tests/` 里是缺口，做模板时一起补。

### 验收标准（M4.2）

- 模板被改一个字节 → `E_SIGNATURE`；
- fork 后 UUID 与 KS 都是新的，记忆为空，`meta.forked_from` 正确；
- 删掉口令 wrap 后助记词仍可解（已有测试，别退化）；
- CI 绿。

### 不要顺手做的

- 不要在 M4.2 里改容器 v1 的字节格式；模板是独立格式（`syspersona-template/1`）。
- 不要为了 fork 方便就复用原 UUID 或原 KS——那等于复制人格而非新生。

---

## 七、恢复步骤（下次照做）

1. 读本文件
2. 读 `README.md` 的 §0（公理）、§5.4（漂移）、§6.3（检索）、§8（安全）
3. 要动加密，再读 `docs/SECURITY_SPEC.md` 的 §4–§9、§13–§15，以及 `impl/m4/README.md`
4. 跑第五节的验证命令，确认环境没坏（M1 仿真 + 26 项加密测试）
5. 按第六节的顺序实现，每完成一项：commit → 跑测试 → 发版

> 一句话提醒：这个项目所有的「聪明」都在文件里，不在上下文里。上下文会断，文件不会。
