# HANDOFF · 交接说明

> 用途：下一次会话从这里开始，不必重读全部历史。读完这份 + `README.md` 的四节，就能接着干。
>
> **范围**：本仓库交付方法与规范 + 最小可行性证据，不是产品。`impl/` 是参考实现，第三方可按规范自行实现，不要求沿用这些代码；下面的「尚未实现」是**未验证的设计**，不是开发 backlog。

---

## 一、当前状态

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/576984518/SysPersona-OS |
| 最新版本 | 规范 **`v1.1.0`**（唯一现行页，持续修订）｜ 实现 **`v1.2.0`**；语义仍以 `v1.0.0` 为准 |
| 规范 | `docs/LAUNCHER_SPEC.md`（**现行 v1.1**，规范性，五职责：加载/组装/路由/执行/归档）+ `docs/MIDDLEWARE_LAUNCHER.md`（Annex） |
| CI | 绿：14 天仿真 12 条断言 + 换模型冒烟 + 导出导入冒烟 + 加密端到端 + M2 检索测试 |
| 代码 | `impl/m1/syspersona.py` + `impl/m4/vault/` + `impl/m2/embed/` + **`impl/launcher/`（L2 PoC，零依赖）** |
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
| **人格运行时规范（文档，无代码）** | v1.1.0 | `docs/LAUNCHER_SPEC.md` |
| **启动器 L2（对话 / 记忆注入 / 睡眠 / 崩溃恢复）** | LAUNCHER_SPEC §7–§9 | `impl/launcher/`（`smoke.py` 12/12） |

---

## 三、版本边界（别误会它已经能用）

v1.1.0 只交付**协议**；v1.2.0 补了 **L2 参考实现**（PoC，12 项冒烟全绿）。两者都还不是 L3/L4。

- 判定标准是规范 §17 的 **TCK**，不是主观描述；
- v1.2.0 的实现覆盖 L2：OpenAI 兼容、人格注入、FTS 记忆、睡眠快照、崩溃恢复、12 个错误码；
- **规范 v1.2 多了第五职责：执行**（§11）——模型想动文件 / 联网 / 跑命令 / 开摄像头，由 Runtime 裁决（allow / ask / deny）、执行、记成 action episode。**`impl/launcher` 尚未实现这一层**，所以它只是 L2；
- **形态不等于规范**（§2.4）：`impl/launcher` 只是 Profile G（gateway）；把同一套逻辑嵌进 App、不起 HTTP，是 Profile E，同样合规——判据是五件事，不是端口；
- **在场有两种模型**（§5.5）：`exclusive`（一次一个）与 `concurrent`（多分支并行、事后收束）；后者放开的是经历，`traits` 仍只有一个写入来源；
- **没有**加密容器、多设备租约、局域网鉴权——那些是 L3/L4；
- 附录 C「并行在场」是 **v1.2 草案**，不属于任何已发布版本的合规范围，别拿它做验收。

三条最容易被忽略、代价最大的判断：

1. **静态封存**，不是每轮加密落盘（KDF 实测 1.6 s，聊天会被 I/O 拖死）；
2. **记忆在尾部注入**，不进开头 system（否则前缀缓存全废，首 token 翻倍）；
3. **在场模型 + 结算独占**：`exclusive` 下一个可写在场，`concurrent` 下各写各分支；只有 `origin` 能结算与合并。

---

## 四、尚未实现（按建议优先级）

1. **并行在场实现（规范 v1.3 新增，代码未落地）**：分支写入、merge（并集 + `merge_record`）、分支说明注入、`contested` 冲突标记
2. **能力面实现（规范 v1.2 新增，代码未落地）**：`tool_calls` 拦截 → 策略裁决（allow/ask/deny）→ 沙箱执行 → action episode；先做 `fs.read` / `net.fetch` 两个只读能力，`proc.exec` 保持 deny
3. **Launcher L3**：三种 DataSource 等价（目录 / 容器 / 远端）、`seal/unlock` 接入、binding
4. **Launcher L4**：租约与围栏（保护收束与人格写入）、局域网鉴权与配对码
5. **L2 加固**：检索 p95 现在 25 ms（目标 15）、并发压测、真实 ollama 的 `prompt_eval_cached_count` 对照
6. **M4.2：公开模板 publish / fork**（Ed25519 签名、复制即新 UUID）
7. **M4.2：device-key（OS 密钥库）/ account 绑定、防回滚、增量分块**
8. **模型摘要替代截断**（现在「压成摘要」是截断，违反原则 8 的精神）
9. **图的多跳扩散**（`NAS → 项目 → 休眠` 走不通）
10. **真模型适配**（只有 mock / ollama，没有云端 API）
11. **探索预算执行、自动环境重置**（现在只出候选与提醒）
12. **M2 收尾**：facts / 摘要向量、ANN、rerank（当前只索引 episode）

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
8. **人格单写者**：`traits` / `kernel` / `permissions` 只有一个写入来源——收束点；`exclusive` 下在场唯一，`concurrent` 下只放开 episode（§5.5.2、§13.2）。
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

### 6.5 Launcher（L2，零依赖）

```powershell
python impl/launcher/smoke.py     # 期望：12/12 passed
python impl/launcher/bench.py 30  # 开销：总 p95 ≈ 35 ms，检索 p95 ≈ 25 ms，睡眠 ≈ 320 ms
```

mock 模型即可跑，不需要 ollama。要接真模型：`--provider ollama --model qwen2.5:1.5b`。

---

## 七、下一个任务

### A（建议先做）· Launcher L3 的第一步：DataSource 等价

**同优先级的另一条路**：Profile E 适配器——把 `Launcher` 类包成进程内接口（`load / chat / sleep` 三个方法），让 TC-C2/C3/C4、TC-M、TC-S 不经过 HTTP 也能跑。成本约半天，收益是直接证明「手机 App 不需要代理」这件事，而不只是写在规范里。

不需要先做加密。目标是：同一个 `persona`（`kernel/traits/state` + `memory.db`）能从三种形态读出来，且字节等价：

1. 目录 `file://`（现在的 `--home`）
2. 单文件包 `vault://`（先做**未加密**的 tar/zip，只验证搬运）
3. 远端 `https://`（只读拉取，带 etag 校验）

**验收**

- 同一 `persona` 从三种形态加载，`persona.hash` 与首轮检索结果完全一致；
- 只读形态运行 30 分钟后，origin 的 etag 不变（不产生隐式写）；
- 哈希不匹配时拒绝加载，不"先用了再说"。

**不要顺手做的**

- 不要在这一步引入加密（L4，`docs/SECURITY_SPEC.md` 已有格式，别自造）；
- 不要做多设备租约与在场（L4）；
- 不要把三种形态做成三套代码路径——差异只允许出现在 DataSource 实现里。

### B · L2 加固

- 检索 p95 25 ms → 目标 15 ms（候选降到 30 条，或把打分换成 `numpy` 向量化；先测再改）；
- 用真 ollama 跑一次，把 `prompt_eval_cached_count / prompt_eval_count` 与 mock 的代理值对照，确认 TC-C2 不是自欺；
- 两个并发客户端压一次，确认 `429 busy` 与写队列不丢数据。

### C · 原有队列（M2 收尾 / M4.2）

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

1. 读本文件，再看 `CHANGELOG.md` 最近三条（知道哪些规则刚被改过）
2. 读 `README.md` 的 §0（公理）、§5.4（漂移）、§6.3（检索）、§8（安全）
3. 要动检索，读 `impl/m2/README.md`；要动加密，读 `docs/SECURITY_SPEC.md` 与 `impl/m4/README.md`
4. 要动启动器，读 `docs/LAUNCHER_SPEC.md` 的 §5.5（在场模型）、§7（请求流程）、§8（组装）、§12（多设备）、§19（局域网），再读 `impl/launcher/README.md` 的「已知偏差」
5. 跑第六节的验证命令（M1 仿真 + 26 项加密 + 11 项向量 + 12 项 launcher 冒烟）
6. 每完成一项：commit → 跑测试 → 发版

> 一句话提醒：这个项目所有的「聪明」都在文件里，不在上下文里。上下文会断，文件不会。
