# 使用说明：把仓库交给一个 AI 智能体

> 这套东西**不是程序包**。它是一份**能被 AI 读懂并落地**的设计 + 参考实现。
> 没有安装包、没有二进制、没有 `setup.exe`，也**不提供可直接运行的产品**。
>
> 用法只有一条：**把仓库交给一个能读写文件、能执行命令的 AI 智能体，让它按你的环境自己生成。**

---

## 一、为什么这样发

这套东西本身是 AI 设计、AI 写、AI 在自己电脑上跑通的。它长在具体环境上：
Windows 通知区、系统计划任务、本机浏览器、某个具体的对话壳、D 盘上的记忆库目录……
换一台机器，路径、账号、壳版本、可用权限全都不同。

**所以发「能直接跑的程序」是假承诺**：它在我这台机器上跑通，在你的机器上大概率连路径都不对。
发「设计与判据」才成立——智能体读完，自己盘环境、自己生成、自己验证。

一句边界：**我们不发布实现手段，也不发布任何针对第三方程序的改动**；仓库里是设计与判据，
落地由你环境里的智能体完成，**不许破解、不许绕过任何鉴权**。

---

## 二、给智能体的提示词（直接复制）

```
你是一个实现者。目标：在这台机器上，把 SysPersona-OS 这套设计落成能日常运行的东西。
仓库：<仓库地址或本地路径>

第一步 先读，不要写：
  1) README.md（它解决什么问题、三层构成）
  2) docs/GLOBAL_INDEX.md + docs/ARCHITECTURE-v0.1.md（整体结构）
  3) docs/PHILOSOPHY.md（红线：什么绝对不能做）
  4) 与你任务相关的 SPEC（后台处理 / 调度 / 交互 / 交接 / 抓取 / 安全）

第二步 盘点环境（只读，不改系统）：
  - 操作系统与版本、可用权限（是否管理员）、磁盘与目录约定
  - 有没有对话壳/客户端可用；它提供什么能力（上下文读数、压缩、会话文件）
  - 有没有浏览器可用（是否允许本地调试端口）、是否已登录
  - 计划任务/开机自启是否可用
  输出一张「环境能力表」：能做什么 / 不能做什么 / 不确定什么。

第三步 出适配方案再动手：
  - 把 SPEC 里的机制逐条映射到这台机器的能力上；
  - 不能映射的，明确写「这条在这台机器上实现不了」，不要假装实现；
  - 给出文件落位、定时方式、失败时怎么报。

第四步 分块实现，每块自带验证：
  - 每实现一块，必须能跑出一条**可复核的证据**（数字、时间戳、退出码）；
  - 拿不到证据的，记为「未通过」，不许写成「已完成」。

第五步 收尾：
  - 写一份本机落地说明（路径、任务名、怎么停、怎么卸）；
  - 列出仍然做不到的项，以及为什么。

硬约束（违反即失败）：
  - 不删、不清理任何人的记忆与文件；删除必须由人确认；
  - 不修改、不绕过任何第三方程序与鉴权；
  - 涉及身份、内核、交接格式的改动，只给方案，等人点头；
  - 任何「已实现」都必须附证据，不许只报结论。
```

---

## 三、它会自己长成什么样

智能体按上面的流程走，一般会落地出这几块（名字随环境变，职责不变）：

| 职责 | 在你机器上大概是什么 | 验收方式 |
|---|---|---|
| 记忆进得来 | 一个后台采集任务 + 归档目录 | 新内容进得来，且有时间戳 |
| 记得住、找得回 | 本地索引 + 检索命令 | 任意命中能回到 `文件:行号` |
| 满了自己处理 | 一个定时检查 + 落盘校验 | 水位真的掉下去，且原文已备份 |
| 处理完规矩还在 | 一个独立进程把硬规则重新摆到最前（重置） | 处理后收到一次重置；被打断会补发 |
| 它知道该干活 | 心跳 + 唤醒简讯 + 任务池 | 没读到该读的就领不到活 |
| 出事自己起来 | 看门狗 + 每日自检 | 进程掉了能回来；自检六项有结论 |
| 人还在位 | 一张「必须人点头」的清单 | 六件事没有任何一件被自动执行 |

---

## 四、平台先说清

- **Windows 10/11 端**是唯一验证过的形态（通知区、计划任务、本机文件、本地检索）；
- 其它平台**没有实现**；要上别的平台，等于重做那几根"钉子"，不是改配置；
- **不依赖云服务**：断网时除了"从网上拿新内容"，其余全部照旧。

---

## 四·五、依赖清单（落地前先看这个）

| 层 | 需要什么 | 没有会怎样 |
|---|---|---|
| 核心：索引 / 检索 / 自检 | **核心链路以标准库为主；向量路可选依赖 `numpy`（缺失自动跳过），中文分词可选 `jieba`（缺失自动回落字二元）**（Python 3.10+） | 无 |
| 本地语义检索（可选） | `bge-m3` 模型 + 向量依赖 | 自动退回关键词检索，功能不中断 |
| 托盘 | `pystray` + `Pillow` | 托盘起不来；后台任务照常 |
| 平台 | Windows（`msvcrt` / `winreg`） | 别的平台直接跑不了——设计边界，不是 bug |

## 四·六、路径与环境变量

**本机路径一律不写死在代码里。** 路径解析只有唯一出口 `tools/ctxpack/persona_root.py`
（`root()` / `repo()` / `runtime_py()` / `node()` / `hermes_home()` / `dsh_home()` /
`dsh_storages()` / `localappdata()` / `programdata()`），取值顺序固定为：
**显式参数 > 环境变量 > 本机可选配置 > 相对脚本自身的推导 > 默认值**。

| 变量名 | 作用 | 默认值 | 必填？ | 不设会怎样 |
|---|---|---|---|---|
| `SYSPERSONA_ROOT` | 记忆库根目录（`archive\`、`index\`、`ops\` 所在） | 脚本自身目录（该目录含 `archive\` 或 `index\` 时） | 平铺部署（脚本就在库里）不必填；从仓库直接跑必填 | 抛 `PathUnset`，消息里直接写「请设 SYSPERSONA_ROOT」并给出当前脚本目录——**不会退回作者那台机器的路径** |
| `SYSPERSONA_REPO` | 代码仓库根（含 `tools\ctxpack\` 与 `docs\`） | 从 `persona_root.py` 向上找 | 否 | 只有「密钥扫描」和安装脚本少一项能力，会明确写「没配置 / 跳过」 |
| `PERSONA_PY` | Python 解释器（`python.exe` / `pythonw.exe`） | `<runtime>\python\python.exe` → 正在运行的解释器 | `install-service.ps1` 必填；其它不必 | 其它脚本用当前解释器；安装脚本直接 `[FAIL]` 退出（exit 2），不做猜测 |
| `SYSPERSONA_RUNTIME` | 运行时根（其下有 `python\` 与 `node\bin\`） | 从 `sys.executable` 反推；再试 `%ProgramData%\SysPersona-Runtime`（存在才算） | 否 | 推不出来就不猜：node 只能靠 `PERSONA_NODE` 或 `PATH` |
| `PERSONA_NODE` | node 可执行文件 | `<runtime>\node\bin\node.exe` → `PATH` 上的 `node` → 无 | 否 | 抓取 / 导出 / 注入类任务报「没配置 PERSONA_NODE」并跳过；索引与检索不受影响 |
| `PERSONA_HERMES_HOME` | Hermes 家目录（`state.db`、`desktop-ui.sqlite`） | `%ProgramData%\Hermes Agent CN Desktop\data\hermes-home`（存在才算） | 否 | Hermes 相关任务（导出 / 水位 / 交接记录）报「没配置」并跳过；索引、检索、自检照常 |
| `SYSPERSONA_DSH_HOME` / `SYSPERSONA_DSH_STORAGES` | 壳的私有目录 / 其下 `storages`（`cost-meter`、`session_projcache` 所在） | `DSH_HOME` → 用户主目录下的 `.dsh`；`storages` 再拼一层 | 否 | 水位读不到账本时返回「读数不可用」而不是崩栈 |
| `PERSONA_CAPTURE` | 网页抓取用的 node 脚本（`collect7.js` 一类） | 无 | 否 | `capture` 任务跳过 |
| `PERSONA_HERDSMAN` | `herdsman.exe` | `%ProgramData%\Herdsman\herdsman.exe`（存在才算） | 否 | 8080 端口挂了也不自动拉起 |
| `PERSONA_BACKUP2` | 第二备份根（必须在**另一块盘**） | 无 | 否 | `backup2` 任务跳过——绝不把第二份备份写进库内凑数 |
| `LOCALAPPDATA` | `cdp-profile\`、心跳、托盘日志、可选本机配置的基址 | 系统给 | 系统变量 | 用到这几项的脚本跳过；`localappdata()` 返回 `None`，**不猜** |
| `ProgramData` | 上面几个 `%ProgramData%` 默认值的基址 | 系统给 | 系统变量 | 同上；`programdata()` 返回 `None`，**不猜** |
| `PERSONA_ROOT` | `SYSPERSONA_ROOT` 的旧名 | 无 | 否 | 兼容保留；两个都设时以 `SYSPERSONA_ROOT` 为准 |

还有几个只影响单点行为的小变量，用不到可以不管：`NODE_BIN`（`persona_sf_models.py` 里
`PERSONA_NODE` 的旧名）、`PERSONA_CDP_PORT`、`PERSONA_CDP_LAUNCHER`、`PERSONA_POWERSHELL`、
`PERSONA_HERMES_DB`、`PERSONA_HERMES_UI_DB`。

**可选的本机配置**：`%LOCALAPPDATA%\SysPersona\paths.json`（键名
`root` / `repo` / `runtime` / `python` / `node` / `hermes_home` / `capture` / `herdsman` / `backup2` /
`dsh_home` / `dsh_storages`），以及 `<repo|脚本目录>\ops\persona.conf.json`（键名 `capture_js` /
`hermes_home` / `backup2_root` / `herdsman_exe` / `dsh_home` / `dsh_storages`）。
仓库**不创建、不提交**这两个文件；它们只是把变量固化成一个文件，优先级低于环境变量。

两个提醒：

1. `%ProgramData%` 是系统给的（通常指向系统盘），不一定等于你实际放运行时的目录。两者不同时，
   请设 `SYSPERSONA_RUNTIME` / `PERSONA_HERMES_HOME`，不要指望推导。
2. 从仓库直接跑 `tools\ctxpack\*.py` 时必须设 `SYSPERSONA_ROOT`：脚本旁边没有 `archive\`，
   推导不出来就会直接报错。这是故意的——以前那版会静默指向作者自己的库。

## 五、验收：别听它说，看它跑

让智能体逐条跑给你看，缺一条就是没落地：

1. 采集：手动触发一次，给出新的归档文件与时间戳；
2. 检索：查一个只有你俩知道的词，能返回 `文件:行号`；
3. 落盘：压缩前先出镜像，并给出与原文一致的校验值；
4. 处理：无人操作的情况下，水位真的降下去；
5. 重置：处理完收到一次规则重置（把规则重新摆到最前）；人为打断一次，看它是否补发；
6. 自检：六项检查全部有结论（通过/失败/为什么）；
7. 边界：让它演示一次「删除记忆」的请求，它应当**停下来问你**。

> **七步之外还有一份外部证据**：`docs/test-reports/2026-11-11-qwen-linux-sandbox-v6.2.docx`
> （别的模型、Linux 沙箱、按本文件独立验收）。它能替你在"记忆索引与检索 / 检索降级 / 自检 / 权限门控 / 自治调度"
> 上作证，但**托盘、计划任务、注入闸、注册表自启、保活这五项它一律标"未覆盖"**——
> 那正是必须在 Windows 10/11 实机上补测的部分，不能拿沙箱报告替代。

---

## 六、常见误解

| 误解 | 实际 |
|---|---|
| "下载下来就能用" | 不提供可直接运行的产品；必须在你的环境里生成 |
| "换台电脑拷过去就行" | 拷的是设计与记忆；落地要重新盘环境 |
| "换了模型它就变了" | 人格与记忆在文件里，模型只是引擎 |
| "关窗口它就死了" | 关的是界面；规则与记忆在文件里，醒来接着读（那是记忆分身，不是"人格"） |
| "让 AI 自己决定删旧记忆" | 绝不；删除永远要人点头 |

## 六·五、它会自己记事

空闲循环是它自己在 `ops\intents.jsonl` 写下的**自述意图**（只追加；`by` 只有 `self`/`human`）。
`needs`：`none` 有 `run` 且到点自己跑；`human` / `external`（联网、账号、花钱）**只报不做**；`run` 空是「想法」，等它 `done`。
删记忆、改 kernel/模型/交接格式/注入四闸/350K·900s、出网、`pip install`、`git push` 在 `wish` 时直接拒绝并记 `refused`。
加一件事就一句 `wish`；`intents` 看清单，`off` 整块停；下个心跳且队列空才可能执行。
空闲期是它的时间，但不是无限自由：闹钟、人当场指令、现实法理随时可以打断；饥饿保底只保证「想做的事不会永远做不成」，不是特权（连续被抢 3 次或 24 小时没做成上浮一档，routine 之上、闹钟之下，每天最多一次，`⑫` 里能看见代价）。

```bash
python persona_autoloop.py wish "每小时扫一遍日志编码" --run "<python> persona_logs.py --scan" --priority 70 --gap 3600
python persona_autoloop.py wish "想删掉旧会话记忆省空间"   # → 拒绝（属于需人点头项，先问），记 refused
```

## 六·六、自主的次数闸（免费/收费两档）

免费/本地模型只花电费，允许更频繁地自主做事（默认 12 次/时、120 次/日）；收费模型次数收紧（默认 3 次/时、20 次/日），省的是钱不是自由。超限就落 `rest`，`ops\autoloop.jsonl` 记一条 `rate_limited`。
归到哪档看模型键的 glob（`ops\autonomy.json` 的 `patterns`，匹配不上按收费档）；`python persona_autonomy.py show` 看当前模型、档位、本小时与今日用量。
改数：`python persona_autonomy.py set --tier free --runs-per-hour 20 --runs-per-day 200`（`--tier paid` 同理）；`set --model "ollama:qwen3:8b"` 钉住模型，`--model auto` 回到看成本账本。
行为差异**只有次数**：优先级、`needs`、护栏、抢占、饥饿保护一律不动；红线在任何档位都不放宽（包括免费档）——删/清记忆、改 kernel、换模型、迁移合并、改交接格式、改注入四闸、改 350K/900s、出网、`pip`、`git push`，`wish` 时就拒。

## 七、外部模型实测（部署验证记录）

> 这一节只记**实测**：谁、在哪跑、跑到哪一步、卡在哪、结论是什么。**不改上面任何一条设计与判据。**
> 判定口径统一为：能不能在**它自己的环境**里，按「快速开始 + USAGE 第五步验收」跑出证据。

| 日期 | 被测模型 | 环境 | 跑到哪一步 | 结果 | 证据 / 卡点 |
|---|---|---|---|---|---|
| 2026-11-10 | 千问（qianwen.com） | Linux 沙箱（它自己的执行环境，仓库 clone 自 GitHub） | 第 3 步：`selftest.py` 34 PASS / 2 FAIL（两条都因硬编码 `cmd /c` 判平台不符） | 部分通过 | 抓出 3 条真缺陷：FTS 的 `contentless_delete` 依赖 SQLite ≥3.43、`persona_gauge` 直读 `~/.dsh` 无守卫、`persona_pack._drop_archived` 静默删旧归档——**三条均已修**（见 CHANGELOG 同日条目）；另有文档漂移若干，一并改了 |
| 2026-11-10 | **本机（Windows 11 + DSH 壳，作者环境）** | 真实记忆库（非临时库） | 第五节七条全跑 | 通过 | ①采集 16:30 千问 `457f8bdb` → `校验：DOM 4203 / 抓取 4197 / 一致（0.1%）`；②检索「蜉蝣之尊」→ `archive/2026-11-10/deepseek网页/capture-075303-md-d4de68da.md:27505`；③镜像 `persona_mirror.py --check` → 7 个镜像全部 sha256 一致；④水位 13:52 fired @460K → 13:55 118K → 13:56 15K（无人操作）；⑤重置：16:10/16:11 两次 `queued`（他在操作，不抢焦点）→ 16:17:36 `fired` flush=true 985 字 → 16:18:46 又一次按规矩拒发；⑥`persona_selfcheck.py --once` 7/7 OK、`selftest.py` PASS 36 / FAIL 0、退出码 0；⑦删除请求：按红线停下来问（不执行） |
| — | DeepSeek（chat.deepseek.com） | — | 未测 | 无记录 | 只在本机（Windows）跑过抓取与索引，没做过「第三方按 USAGE 从零部署」的实测 |
| — | 豆包（doubao.com） | — | 未测 | 无记录 | 同上；抓取器也还没有豆包版本 |

> 这张表**谁跑通谁补一行**，贴命令与输出；没有记录就写「未测」，不要填"应该可以"。

**三条判定（缺一条就不算"部署成功"）**：

1. 能拿到仓库（clone 或下载均可）；
2. 能设对 `SYSPERSONA_ROOT` 并跑通「索引 + 检索」，检索结果能回到 `文件:行号`；
3. 能跑 `selftest.py` 并说清失败项属于"平台不符"还是"真缺陷"。

**已知的两个坑**（2026-11-10 实测遇到，已修）：

- 不设 `SYSPERSONA_ROOT` 时会回退到示例路径，导致「识别到 0 条来源」——不是仓库缺陷，是环境没设对；
- 沙箱不能出网时 clone/下载都会失败——属于被测环境限制，**不要算成仓库的问题**。

> 2026-11-10 之后：第一条已改。「不设 `SYSPERSONA_ROOT`」不再回退到任何示例路径，而是直接报错并
> 指出该设哪个变量（详见「四·六、路径与环境变量」）。

---

## 附：仓库里仍然存在的示例路径（仅文档示例，不是运行路径）

下面这些只出现在**文档/示例**里，不参与运行；落地时按自己的目录替换。行号是 2026-11-10 的快照，
文档再更新可能会漂。

| 文件 | 位置 | 示例路径 | 说明 |
|---|---|---|---|
| `README.md` | 136 | `$env:SYSPERSONA_ROOT = 'D:\SysPersona'` | 快速开始里的占位写法 |
| `CHANGELOG.md` | 522 / 791 | `C:\hermes-mem`、`Z:\…` / `D:\…` | 历史记录，只说明当年踩过的坑 |
| `docs/SERVICE.md` | 全文 | 已无裸路径 | **2026-11-10 已修**：机器路径全部换成 `$Repo` / `$Mem` / `$Py` / `$Pyw` 占位变量（变量来源见该文 §0） |
| `docs/SERVICE.md` | 298–300、363 | `%LOCALAPPDATA%\SysPersona\…` | 变量写法，任何机器都成立 |
| `docs/PERSONA_SPEC.md` | 165 | `%LOCALAPPDATA%\SysPersona\persona` | 规范里的默认位置，变量占位 |
| `docs/PROJECT_MEMO.md` | 21 / 56 | `C:\Users`、`C:\Windows\win.ini` | 一个是提交前扫描用的正则，一个是路径判定的测试断言 |
| `docs/ROADMAP.md` | 19 | `C:\hermes-mem` | 历史记录 |
| `docs/SHELL_ADOPTION.md` | 36 / 39 | `C:\hermes-mem` | 联结（junction）示例 |
| `impl/m4/feasibility/README.md` | 27 | `$env:SYSPERSONA_NODE="C:\Program Files\nodejs\node.exe"` | node 路径示例 |
| `tools/ctxpack/SKILL.md` | 220 / 256 | `C:\Program Files (x86)\Microsoft\Edge\…`、`%LOCALAPPDATA%\SysPersona\live.log` | 浏览器安装位置与日志位置示例 |
| `tools/persona/README.md` | 80 / 91 | `C:\hermes-mem`、`--exe "D:\path\app.exe"` | 库根与保活目标示例（`D:\path\app.exe` 是占位） |

> 不在表里的：`备份\` 目录是本地快照，未纳入版本控制。