# HANDOFF · 交接说明

> 用途：下一次会话从这里开始，不必重读全部历史。读完这份 + `README.md` 的四节，就能接着干。

---

## 一、当前状态

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/576984518/SysPersona-OS |
| 最新版本 | `v0.7.0`（tag = `754f993`） |
| CI | 绿（14 天仿真 12 条断言 + 换模型冒烟 + 导出导入冒烟） |
| 代码 | `impl/m1/syspersona.py`，单文件约 2000 行，零第三方依赖 |
| 数据布局 | `persona/{kernel,traits,state}.json` + `memory.db` + `snapshots/` |
| 运行环境 | Python 3.10+；mock 模型可离线跑，ollama 可选 |
| 安全规范 | `docs/SECURITY_SPEC.md` — v1.0 草案，已定稿，待实现 |
| 可行性验证 | 46/46 通过；`docs/SECURITY-FEASIBILITY.md` + `impl/m4/feasibility/` |

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

---

## 三、尚未实现（按建议优先级）

1. **加密层实现**（规范已定稿；当前包仍是明文，这是 M4 的主任务）
2. **向量/语义检索**（词面之外，换个说法就召回不到）
3. **模型摘要替代截断**（现在「压成摘要」是截断，违反原则 8 的精神）
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

### 5.2 安全规范验证（可选，需 Node ≥ 22）

```powershell
python impl/m4/feasibility/feasibility.py
# 期望：通过 46/46
```

这是**验证夹具**，不是产品代码：它用 Python 写格式、用 OpenSSL 提供原语、用独立 Node 读取器验证互操作。M4 实现时要把负向矩阵搬进 CI。

---

## 六、下一个任务：实现加密层

规范已定稿：`docs/SECURITY_SPEC.md`。可行性已验证：`docs/SECURITY-FEASIBILITY.md`。

**先读**：规范 §4（范围）、§6（密钥）、§7（格式）、§9（签名）、§14（失败语义）、§15（Provider）；可行性报告 §7（验证逼出来的修正）、§9（零依赖的结论）、§12（实施顺序）。

### 5 个必须先回答的问题——已答，见规范附录 D

1. 主密钥：KS 只以密文存在于 wraps；解锁后在内存。
2. 丢失：助记词（BIP39 24 词）+ 离线备份；托管只留接口。
3. 边界：kernel/traits 强制；state 与 memory.db 可选；config 明文不含密钥。
4. 验签失败：拒绝启动，绝不降级。
5. 迁移：显式允许 → 校验 → 原子封装 → 保留明文 → 人工确认后删除。

### 建议实施顺序

1. Provider 接口 + `cryptography` 接入，跑通 KAT；
2. 容器读写（`seal` / `unseal` / `verify`）；
3. 内核签名 + 内核变更批准流程；
4. SQLite 生命周期（checkpoint → 关库 → 封装）；
5. 助记词与恢复；
6. 迁移（默认拒绝明文）；
7. 公开模板与 fork；
8. 补 CI：加密 → 解密 → 断言 + 14 条负向矩阵；
9. 最后再谈增量封装 / 分块。

### 验收标准

- 错口令 → 无法解密，且不泄露「差一点就对」的信息；
- 篡改任意字节 → `verify` 与 `import` 都失败并退出非 0；
- 用备份恢复后，`memory.db` 内容哈希一致；
- CI 增加「加密 → 解密 → 断言」一段；
- 内核被改 → 拒绝启动，而不是降级。

### 不要顺手做的

换加密库、换数据库、改检索算法。加密这一版只做加密，否则出问题无法定位。

---

## 七、恢复步骤（下次照做）

1. 读本文件
2. 读 `README.md` 的 §0（公理）、§5.4（漂移）、§6.3（检索）、§8（安全）
3. 要动加密，再读 `docs/SECURITY_SPEC.md` 的 §4–§9、§14–§15
4. 跑第五节的验证命令，确认环境没坏（M1 仿真 + 可选的 46 项）
5. 按第六节的顺序实现，每完成一项：commit → 跑 simulate → 发版

> 一句话提醒：这个项目所有的「聪明」都在文件里，不在上下文里。上下文会断，文件不会。
