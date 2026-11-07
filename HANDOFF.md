# HANDOFF · 交接说明

> 用途：下一次会话从这里开始，不必重读全部历史。读完这份 + `README.md` 的四节，就能接着干。
>
> **范围**：本仓库交付方法与规范 + 最小可行性证据，不是产品。`impl/` 是参考实现，第三方可按规范自行实现，不要求沿用这些代码；下面的「尚未实现」是**未验证的设计**，不是开发 backlog。

---

## 一、当前状态

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/576984518/SysPersona-OS |
| 最新版本 | **`v1.1.0`**（Launcher 规范）；语义仍以 `v1.0.0` 为准 |
| 规范 | `docs/LAUNCHER_SPEC.md`（v1.1.0，**规范性**）+ `docs/MIDDLEWARE_LAUNCHER.md`（Annex，实测与取舍） |
| CI | 绿：14 天仿真 12 条断言 + 换模型冒烟 + 导出导入冒烟 + 加密端到端 + M2 检索测试 |
| 代码 | 无变化（v1.1.0 **不含代码**）：`impl/m1/syspersona.py` + `impl/m4/vault/` + `impl/m2/embed/` |
| 数据布局 | `persona/{kernel,traits,state}.json` + `memory.db`（含 embeddings 表）+ `snapshots/`；静止时是 `*.vault` |
| 运行环境 | Python 3.10+；mock 模型可离线跑；加密需 `cryptography`；语义检索需 ollama 或 `fastembed`（都可选） |
| 安全规范 | `docs/SECURITY_SPEC.md` **v1.0（条款冻结）**；条款状态见附录 F |
| 一致性 | `docs/CONFORMANCE.md`：第三方如何声明 L1/L2/L3；Launcher 另有一套 L1–L4（规范 §16） |
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
| **向量检索（RRF 融合，provider 可插拔）** | 6.3 | `recall --retrieval hybrid` / `embed reindex` |
| **检索评测（recall@k / MRR）** | 15 | `eval --queries ...` |
| **启动器规范（文档，无代码）** | v1.1.0 | `docs/LAUNCHER_SPEC.md` |

---

## 三、v1.1.0 的边界（别误会它已经能用）

v1.1.0 只交付**协议**：人格与记忆如何被加载、组装、路由、归档，以及多设备下的三条铁律。它没有实现，也没有新代码。

- 判定标准是规范 §17 的 **TCK**，不是主观描述；
- 合规分 L1–L4，**L2 的工作量估算是 4 人日**（网关 + 人格注入 + FTS 记忆 + 睡眠快照 + 错误码）；
- 附录 C「并行在场」是 **v1.2 草案**，不属于 v1.1.0 的合规范围，别拿它做验收。

三条最容易被忽略、代价最大的判断：

1. **静态封存**，不是每轮加密落盘（KDF 实测 1.6 s，聊天会被 I/O 拖死）；
2. **记忆在尾部注入**，不进开头 system（否则前缀缓存全废，首 token 翻倍）；
3. **唯一在场 + 结算独占**：同一人格只能有一个可写在场，只有 `origin` 能结算。

---

## 四、尚未实现（按建议优先级）

1. **Launcher L2 参考实现**（v1.1 规范，4 人日）——目前最大的空缺，也是「能不能真用起来」的分水岭
2. **Launcher L3/L4**：三种 DataSource、seal/unlock 接入、唯一在场与交接、局域网鉴权与配对
3. **并行在场（附录 C）转正**：需要改 §5.5 与 L4 判据，并补 TC-N14–N17
4. **M4.2：公开模板 publish / fork**（Ed25519 签名、复制即新 UUID）
5. **M4.2：device-key（OS 密钥库）/ account 绑定、防回滚、增量分块**
6. **模型摘要替代截断**（现在「压成摘要」是截断，违反原则 8 的精神）
7. **图的多跳扩散**（`NAS → 项目 → 休眠` 走不通）
8. **真模型适配**（只有 mock / ollama，没有云端 API）
9. **探索预算执行、自动环境重置**（现在只出候选与提醒）
10. **M2 收尾**：facts / 摘要向量、ANN、rerank（当前只索引 episode）

---

## 五、不可违反的约束

改动任何代码前先确认没有违反这些：

1. **公理**：模型不构成人格；人格由文件与记忆在每次唤醒时重建。
2. **原则 8**：上下文不是被清空的，是被压缩的；清理前必须先有可检索的替代物。
3. **原则 9**：检索是主观的——关注度高、喜欢的排前面，无关的往后。
4. **自迭代边界**：技能与偏好可自改；内核（身份/价值观/边界）与「记忆写入规则」不可自改。
5. **权限分层**：`kernel.json` 只有人能改；`traits.json` 只在睡眠结算写；`state.json` 可实时写。
6. **文件是真相，数据库是缓存**：睡眠后回写 `traits.json`，重启以文件为准。
7. **加密模式不追求零依赖**：标准库没有 AEAD；必须走 Provider（规范 §15），不得自造密码学。
8. **单写者**：同一 `persona_id` 至多一个可写在场（LAUNCHER_SPEC §5.5）；多端只能顶替、旁观或排队，不能同时聊。
9. **结算独占**（§12.5）：只有 `origin` 能 sleep / consolidate / seal，且同一时刻只有一个。
10. **记忆可合并，人格不可合并**（§12.3、§5.5.6）：`episode` 追加去重，`traits` 单写者且绝不自动合并，`state` 随在场转移。
11. **不把 `memory.db` 放 SMB / NFS / OneDrive**（§12.6）：SQLite 的锁在那里不成立，会静默损坏且不报错；多设备共享走 API。

---

## 六、如何验证（改完必须跑）

### 6.1 M1 回归（现有 CI）

```powershell
python impl/m1/syspersona.py --home ./sim init --persona examples/persona.m1.json
python impl/m1/syspersona.py --home ./sim simulate --scenario examples/scenario.14d.json
# 期望：断言通过：12 条，退出码 0

python impl/m1/syspersona.py --home ./sim export --out ./me.syspersona
python impl/m1/syspersona.py verify --file ./me.syspersona
```

CI（`.github/workflows/simulate.yml`）跑的就是这三段，本地过了基本就绿。

### 6.2 加密层（需可选依赖）

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

### 6.3 向量检索（零依赖可跑）

```powershell
python -m unittest discover -s impl/m2/tests -v
# 期望：11 tests OK

python impl/m1/syspersona.py --home ./sim embed status
python impl/m1/syspersona.py --home ./sim recall --query "老机器休眠怎么解决的" --retrieval hybrid
python impl/m1/syspersona.py --home ./sim eval --queries examples/eval/m2_queries.json --retrieval lexical
python impl/m1/syspersona.py --home ./sim eval --queries examples/eval/m2_queries.json --retrieval hybrid
```

没有语义 provider 时 `auto` 会回退 lexical，`--retrieval hybrid` 仍可用 hash 基线做通路测试。

**实测（bge-m3，5 条记忆 / 5 条改写查询）**：lexical recall@5 = 0.60 → hybrid **1.00**，MRR 0.60 → 1.00；
延迟 3.4 ms → 64.2 ms（其中约 50 ms 是每条查询一次 HTTP embedding 调用）。
样本很小，只证明「换个说法」这条通道有效，不是 benchmark。详见 `impl/m2/README.md` 的「实测」。

### 6.4 规范可行性夹具（可选，需 Node ≥ 22）

```powershell
python impl/m4/feasibility/feasibility.py
# 期望：通过 46/46
```

这是**验证夹具**，不是产品代码：它用 Python 写格式、用 OpenSSL 提供原语、用独立 Node 读取器验证互操作。

---

## 七、下一个任务

### A（建议先做）· Launcher L2 参考实现

只做规范 §16 的 L2，不要顺手做 L3/L4。范围：单进程网关 + OpenAI 兼容 + 人格注入 + FTS 记忆 + 睡眠快照 + 12 个错误码。

**验收（全绿才算完）**

- TC-C1 第三方客户端可直连（`/v1/chat/completions`，流式与非流式都通）
- TC-C2 第 2/3 轮 `X-SP-Cache-Hit-Ratio ≥ 0.5`
- TC-C3 注入后 `prompt_eval_count ≤ num_ctx`，且 `promise/boundary` 未被驱逐
- TC-C4 `X-SP-Bypass: 1` 时零注入
- TC-M1 每条注入带来源 id；TC-M2 热路径模型调用数 = 0；TC-M3 换 persona 后检索不到上一个人格的条目
- TC-S1 指针先于清理；TC-S2 kill -9 无半写；TC-S3 清理顺序不可跳步

**不要顺手做的**

- 不要做多用户、租约、局域网、加密——那是 L3/L4；
- 不要在热路径做记忆抽取或向量重建；
- 不要用 200 掩盖降级，任何降级都要可见（响应头或错误码）。

### B · 原有队列（M2 收尾 / M4.2）

M2 的 episode 向量检索已落地（见 `impl/m2/README.md`）；M4 核心已完成（见 `impl/m4/README.md`）。剩下的：

1. **M2 收尾**：facts / 摘要也建向量；`hash` 之外用真实模型跑一次 recall@5 对照，把数字写进 README；必要时再上 ANN。
2. **M4.2 公开模板与 fork**：`vault publish` / `vault fork`，Ed25519 签名，复制即新 UUID、空记忆；规范 §13 已定。
3. **M4.2 更强绑定**：`device-key`（DPAPI / Keychain / libsecret）、账号绑定；防回滚计数器。

### 做 M2 收尾前先读

- `impl/m2/README.md`（公式、provider、性能）；
- `impl/m1/syspersona.py` 的 `retrieve()`（约 974 行起）；
- `examples/eval/m2_queries.json`：在你自己的库上跑，别用空库下结论。

### 验收标准（M2 收尾）

- 同一查询，lexical 与 hybrid 的 recall@5 差值有数字、有查询样例；
- 1 万条规模检索 ≤ 30 ms（mid 档，`math.sumprod` 路径）；
- 没有 provider 时行为与 M1 完全一致。

### 不要顺手做的（M2）

- 不要把向量分加进人格项；原则 9 的三项仍然是加法，不是替代；
- 不要为跑分调 `floor` 或权重却不留记录——阈值是硬件与数据的函数，写进 README 再改。

---

## 八、恢复步骤（下次照做）

1. 读本文件
2. 读 `README.md` 的 §0（公理）、§5.4（漂移）、§6.3（检索）、§8（安全）
3. 要动检索，读 `impl/m2/README.md`；要动加密，读 `docs/SECURITY_SPEC.md` 与 `impl/m4/README.md`
4. 要动启动器，读 `docs/LAUNCHER_SPEC.md` 的 §5.5（唯一在场）、§7（请求流程）、§8（组装）、§12（多设备）、§19（局域网）
5. 跑第六节的验证命令（M1 仿真 + 26 项加密 + 11 项向量）
6. 每完成一项：commit → 跑测试 → 发版

> 一句话提醒：这个项目所有的「聪明」都在文件里，不在上下文里。上下文会断，文件不会。
