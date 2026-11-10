# 项目备忘（只和本仓库有关）

> 这份不许并进全局交接包。全局的人格 / 记忆 / 交互规则在记忆库的 `DECISIONS.md`；
> 这里只记**项目事务**：发版规矩、今天踩的坑、常用命令、定时任务、当前状态。
> 最后更新：2026-11-10 08:05

## 〇·〇、本机路径与脱敏（2026-11-10 定）

> 本仓库是**公开**的。备忘 / 文档里**一律不写**：本机用户名、盘符绝对路径、旧账号 ID、机器型号。
> 只用下面这些环境变量占位；真实值只存本机 `$env:LOCALAPPDATA\SysPersona\local-paths.ps1`（**不进仓库**）。

| 变量 | 含义 |
|---|---|
| `$env:MEM` | 记忆库根目录 |
| `$env:REPO` | 本仓库工作副本 |
| `$env:GIT_EXE` | `git.exe` 全路径（不在 PATH） |
| `$env:GH_EXE` | `gh.exe` 全路径（不在 PATH） |
| `$env:USERPROFILE` / `$env:LOCALAPPDATA` | 系统变量，直接用 |

换机器：先把 `local-paths.ps1` 拷过去并 `. ` 引一次；缺变量时脚本会报错，**不会静默跑错**。
提交前一律先扫一遍：`Select-String -Path docs\*.md -Pattern 'C:\\Users|D:\\|ProgramData'`，有命中就不许提交。
## 〇、动手前先查表（2026-11-08 定）

> **每次做什么，先读相关备忘，再动手。** 不读就做 = 重复踩同一个坑。

| 要做的事 | 先读 |
|---|---|
| 发版 / 改版本号 / 打 tag | 本备忘 §一 + `CHANGELOG.md` 顶部两条规则 |
| **GitHub 操作（发版 / tag / Release / 改仓库文件 / 查提交）** | **本备忘 §九 + `docs/RELEASING.md` + §一** |
| 改 PowerShell 脚本 / 计划任务 | 本备忘 §二（BOM、路径）+ §四（现有任务） |
| 出包 / 改交接格式 / 改索引 | 记忆库 `续接\交接文件规范-v2.md` + `DECISIONS.md` 0.1 |
| 判断"该不该动文件 / 该不该开工" | `DECISIONS.md` 0.1、0.2、5.4 |
| 写检索、索引、导出代码 | `tools/ctxpack/SKILL.md` + 本备忘 §二 |
| 回答"上次做到哪 / 下一步" | `HANDOFF.md` + `BACKLOG.md` |
| 加载 / 新增技能 | 本备忘 §十（注册位置与热加载规则） |


## 一、发版规则（2026-11-08 澄清）

- **已发布过的对象，只是修改，就不要发版**——不开新 tag、不建 Release、不动版本行。
- 文档修订一律不发版本；Release 只在**交付新的可运行产物**时创建。
- 今天踩过一次：把工具更新误打成 `v3.2.0` 并推了 tag。**已删除**本地与远端 tag，版本行回 `v3.10.0`，
  CHANGELOG 改成「不发版」条目。留这条就是提醒下次别再犯。
- 提交历史里 `d300620` 的标题仍写着 `release: v3.2.0`——历史不改写，知道即可。

## 二、今天踩过的坑（写下来省下次）

| 坑 | 现象 | 规矩 |
|---|---|---|
| PowerShell 缺 BOM | PS 5.1 按 ANSI 读脚本，中文注释把语法冲掉，报 `Unexpected token` | 所有 `.ps1` **存 UTF-8 带 BOM**，改完用解析器过一遍 |
| 替换 markdown 表格时吃掉标题 | 台账「## 二、已定但没落地」被当成 old_string 换掉，一级表混进二级表 | 换表格行时，别把上一节的 `##` 标题写进 old_string |
| 计划任务路径腐烂 | `Z:` 早没了，备份任务空跑，23:00 一直失败 | 任务里只写本地绝对路径；改完 `schtasks /Query` 核对 |
| 检索块缓存不失效 | 新抓的会话检索不到，旧缓存永远命中 | 缓存键带「文件数 + 总字节 + 最新 mtime」签名 |
| 导出器没有消息分隔线 | 电脑会话在索引里是"文档块"，角色全丢 | 消息之间写 `---`，索引与检索都靠它切块 |
| 页面未渲染完就归档 | 消息数从 402 掉到 151，残缺快照会覆盖完整版 | 写入前比对条数，**变少一律不写**；空壳页面不算新内容 |
| 测试按 Windows 语义写，CI 是 Linux | 断言 `C:\Windows\win.ini` 被拒，Ubuntu 上它不是绝对路径，CI 直接挂 | 路径类断言要同时覆盖**盘符 / UNC / POSIX 绝对路径**；判断统一走 `_looks_absolute()` |
| 混源向量 | 索引里既有**云端 bge-m3** 建的块（14,229），也有**本地嵌入**补的块（2026-11-08 回滚后补了 15 文件 / 1,920 块，日志 `0 tokens`） | 两批向量可能轻微不同分布；**别只重建一部分**——要么全量重建统一，要么维持现状（当前回归 20/20，无可观察劣化） |
| **`pythonw` 里 spawn 控制台程序会弹黑窗** | 托盘 `pythonw` 每 10 秒调 `tasklist`、每 60 秒调 `gauge`，屏幕上黑窗一闪一闪 | GUI 进程里所有 `subprocess` 一律带 `creationflags=0x08000000`（`CREATE_NO_WINDOW`）；`persona_daemon.py` 早就这么写，托盘 2026-11-10 才补上 |
| 重导历史快照会抹平时间切片 | 用"当前完整会话源"重导旧的 `续接前-*.md`，17 份快照变成同一份 | 历史快照**只在当时导出**；补分隔线只影响新导出，别回头重导 |
| git / gh 不在 PATH | 满世界找 git.exe，误判"机器上没有 git" | 路径固定写在 §九，先查备忘再动手 |

## 三、常用命令

```powershell
$PY = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$R  = $env:MEM

& $PY $R\ctxguard.py                       # 水位
& $PY $R\ctxguard.py --prepare             # 出包 + 剪贴板
& $PY $R\ctxguard.py --prepare --canonical --no-clip   # 覆盖 接续包-最新.md（周期任务用的就是它）
& $PY $R\index_l1.py                       # 一级索引（0.5 秒）
& $PY $R\read_watch.py --seconds 60        # 读不到新内容时后台等 60 秒
& $PY $R\zsearch.py "关键词" --k 100       # 混合检索；--no-vector 退关键词
& $PY $R\index_inc.py                      # 增量索引（--dry-run 只看；--fill-missing 只补新块）
& $PY $R\eval.py --merged --no-vector      # 20 题回归
& $R\from_phone.ps1 -Path x.md             # 手机端导出导入
```

## 四、定时任务（本机）

| 任务 | 周期 | 干什么 |
|---|---|---|
| `SysPersona-CtxGuard` | 10 分钟 | 测水位；≥800K 交接（先镜像再出包）；到线由 `persona_limits.py` 一处定义 |
| `SysPersona-Handoff-Refresh` | 30 分钟 | **覆盖** `续接\接续包-最新.md`（防丢） |
| `SysPersona-DeepSeek-LiveWatch` | 每天 03:30 | 整页重载一次，防积压 |
| `SysPersona-DeepSeek-FullSweep` | 每周日 21:00 | 全量巡检 |
| `SysPersona-Memory-Backup` | 每天 23:00 | 打包 zip，留 8 份 |
| `SysPersona-Memory-Maint` | 每天 22:00 | 压缩 / 清理维护 |

## 五、当前状态

- 设计版：**v5.1.0**（2026-11-09 托盘设计：`docs/TRAY_SPEC.md`，托盘不是智能体）
- 工具版：**v5.2.0 · 托盘状态显示**（2026-11-10 重写：`tools/tray/persona_tray.py`；按客户端数量并排画正方形、只显示在跑的、各自显示水位；只读不代办）
- 设计版：**v5.4 / v5.5 / v5.6**（2026-11-09 后台处理 / 任务调度 / 记忆网络**设计成文**，实现在后续小版：`docs/BACKGROUND_SPEC.md`、`docs/SCHEDULER_SPEC.md`、`docs/MEMORY_NET_SPEC.md`）
- 运行时：**v5.3.0**（2026-11-09 保活 + 交互代理：`tools/persona/`，唤醒→读总索引→报到→派发，含重试上限与「保活只报不拉」；协议 `docs/INTERACT_SPEC.md`）
- 落地版：**v5.0.0**（2026-11-09 寄生在开源壳上的记忆层：`docs/SHELL_ADOPTION.md`）——v5 线只讲壳接入。
- 设计版：**v4.4.0**（2026-11-09 画像 / 全局索引 / 技能**设计成文**，未落地：`docs/PROFILE.md`、`docs/GLOBAL_INDEX.md`、`docs/SKILLS.md`；脚本统一 `persona_` 前缀），属 v4 记忆工具线的延续。
- 同日另有 **v4.3.0**（网页会话全量抓取器 `tools/cdp_capture/`），两版并存、各自成立。
- 抓取链路：虚拟列表三个坑已定论（隐藏页不重绘 / 底部锚定 / 高度估算），详见 `docs/CAPTURE.md`；
  **未解决**：调试浏览器登录态偶发丢失、网页内部 API 带 PoW。
- 工具：`persona_gauge.py` / `persona_index_l1.py` / `persona_search.py` / `persona_index.py` / `persona_daemon.py` / `persona_bridge.py` / `eval.py` / `read_watch.py` 均在 `tools/ctxpack/`。
- 待办：无阻塞项；B8–B11f 已关账，B24「满 100」定为每跨 100K 提醒一次。
- 交接文件：v2.1（分锁定段 / 自动段 / 固定格式），规格见记忆库 `续接\交接文件规范-v2.md`。
- **已知偏差**：记忆库台账 / 检索最新只到 **v3.4.0**；实际已发到 **v3.10.0**（v3.5.0 修 dry-run 假信号、
  v3.6.0 工具可移植、v3.7.0 本地重排、v3.8.0 `--async`、v3.9.0 F5 触发、v3.10.0 链路修补）。
- **偏差已补（2026-11-08 17:05）**：`DECISIONS.md` **0.3 版本线**已记入 v3.5.0–v4.0.0，检索不再读到旧版本号。

## 六、你指出过的问题（按时间，一条都不删）

> 每次被指出问题，先在这里记一行，再改；不记的教训等于没发生。

| # | 你指出的问题 | 得出的规则 | 落在哪 |
|---|---|---|---|
| 1 | 读完交接就自己开工 | 读完先回「已读完，当前任务=…」，**等指示**；没指示不动文件 | DECISIONS 0.1、交接包首句 |
| 2 | 读会话时顺手改生产文件 | **未经指示不改文件**，读就只读 | DECISIONS 0.1 |
| 3 | 把交接文件整份“冻死” | 分三类：锁定段只人改 / 自动段可更新但限长 / 固定格式改需确认 | `续接\交接文件规范-v2.md` |
| 4 | 前台被长操作占着 | 前台只干三件事：等会话、等新内容、回话；**超过 5 秒一律后台** | DECISIONS 0.2、SKILL |
| 5 | 读不到新内容就下结论 | 后台重试 60 秒，有变化再总结，确实没有才说没有 | `read_watch.py` |
| 6 | 读完索引没有任务清单 | 总结 → 生成清单 → 排优先级 → 全部列出 → 等通知 | DECISIONS 0.1 第 4 条 |
| 7 | 已发布的工具只是改了改，却发了 v3.2.0 | **已发布对象只修改就不发版**；3.8.0/8/9 那种 13 分钟连发三个是反例 | 本备忘 §一 |
| 8 | 改完没有自备份、没有备忘 | 建自备份（git bundle）+ 每次问题记这里 | 本节 + `repo_backup.ps1` |
| 9 | 每次动手前没先读备忘 | **先查 §〇 的表，读对应备忘，再动手** | 本备忘 §〇、`SKILL.md` |
| 10 | 写备忘 / 出交接还要先问一句 | **写备忘、写交接、出包属自主执行**，直接写，不等确认；人只在出错时指出 | 本备忘 §〇、`DECISIONS.md` 5.4 |
| 11 | 每次读取只读一次，读完就停 | **起点 = 你说话的那一刻，读完立刻以新时间点续挂**，始终跟到最新 | `read_watch.py --since`、`SKILL.md` |
| 12 | 小事也要先问一句才做 | **耗时短、不会造成严重后果的，自己判断自己做**，不问；出了事再纠正 | 本备忘 §六、`DECISIONS.md` 5.4 |
| 13 | GitHub 工具路径没进备忘，每次都现找 | **GitHub 相关先读 §九**；不够再回检索翻以前做法，然后把新发现补回 §九 | 本备忘 §九 |
| 14 | 公开仓库的备忘里露出本机用户名 / 盘符路径 / 旧账号 ID | **公开文档只写环境变量占位**，真实路径留本机 `local-paths.ps1`；提交前扫一遍隐私串 | 本备忘 §〇·〇 |

## 七、待做：记忆库根目录瘦身（2026-11-08 记，未执行）

现状：根目录 **36 项**，全部平铺。目标：根目录只留 `接续\`（交接），其余进二级目录。

| 步骤 | 动作 | 风险 |
|---|---|---|
| 1 | 停 6 个计划任务（`/Change /DISABLE`） | 无（可恢复） |
| 2 | 建 `数据\` `脚本\` `文档\` `接续\` 四个顶层 | 无 |
| 3 | `数据\` ← sessions / archive / index / index_full / index_old / memory.db / state.json / 备份 / 整理 | 中：写入方正在跑，必须先停任务 |
| 4 | `脚本\` ← 全部 .py / .js / .ps1 | 中：6 个计划任务与包装脚本要同步改路径 |
| 5 | `文档\` ← README / SKILL / DECISIONS / BACKLOG / HANDOFF / LATEST | 中：ctxguard 读 DECISIONS/BACKLOG 的路径要改成 `..\文档` |
| 6 | 设 `SYSPERSONA_ROOT=…\数据`，所有脚本改读该环境变量 | 中 |
| 7 | 改 6 个计划任务 + 5 个包装脚本的路径，重新启用 | 高：漏一个就静默失败 |
| 8 | 冒烟：`ctxguard --prepare`、`index_l1`、`zsearch`、`read_watch`、备份 | 必做 |

**为什么没在 2026-11-08 直接做**：写入方（NavWatch 每分钟）在跑，边写边搬会丢一轮抓取甚至写坏归档；
必须选一个停几分钟的窗口。交接文件部分已经完成（`续接\` 只剩 3 项：`历史\`、`规范-v2.md`、`接续包-最新.md`）。


## 八、自备份

- 脚本：`$env:LOCALAPPDATA\SysPersona\repo_backup.ps1`
- 做法：`git bundle create 备份\SysPersona-OS-<日期>.bundle --all`（含全部分支与历史，单文件可克隆）+
  把本备忘复制一份明文；**只留最近 8 份**。
- 计划任务：`SysPersona-Repo-Backup`，每天 23:30（记忆库备份 23:00 之后）。
- 校验：`git bundle verify 备份\xxx.bundle`；恢复用 `git clone 备份\xxx.bundle 新目录`。

## 九、GitHub 工具链与提交动作（2026-11-08 补记）

> **凡是 GitHub 相关的事——发版、打 tag、建/改 Release、改仓库文件、查提交——先读本节 +
> `docs/RELEASING.md` + 本备忘 §一；本节不够，再回检索翻以前的做法，翻到新东西就补回本节。**

| 项 | 值 |
|---|---|
| GitHub CLI | `$env:GH_EXE`（v2.103.3，**不在 PATH**） |
| Git | `$env:GIT_EXE`（2.56.0，**不在 PATH**） |
| 认证 | `gh auth status` → 账号 `MirHuang666`（keyring，scope: gist / read:org / repo） |
| git 凭据 | `credential.helper = manager`（Git Credential Manager，走系统登录态） |
| 工作副本 | `$env:REPO`（`main` 与 `origin/main` 同步；远端是权威） |
| 远端 | `https://github.com/MirHuang666/SysPersona-OS.git` |

提交文档（本备忘 / CHANGELOG / docs 下任何 md）：

```powershell
$git = $env:GIT_EXE
$R   = $env:REPO

& $git -C $R status --short          # 先看改了什么、有没有夹带私密文件
& $git -C $R add -A
& $git -C $R commit -m "docs(memo): …"
& $git -C $R push origin main
```

查 / 发版：

```powershell
$gh = $env:GH_EXE
& $gh release list  --repo MirHuang666/SysPersona-OS --limit 5     # 看最新版本
& $gh release view  v3.10.0 --repo MirHuang666/SysPersona-OS        # 看正文全文
& $gh api repos/MirHuang666/SysPersona-OS/commits --jq '.[0:3][]|.sha[0:7]+" "+.commit.message'
```

规矩：

1. **文档修订只提交，不发版**（§一）；要发版先过 `docs/RELEASING.md` §七 自检六问。
2. **不建 Release 就不要打 tag**；打错了按 §一 删 tag，并在 CHANGELOG 记「撤销」。
3. 提交前扫一眼 `status`，`sessions/`、`archive/`、`*.db`、`*.zip` 一律不许进仓库。
4. 改了仓库文件，记得同步记忆库那边对应的 `备忘` / `DECISIONS`，否则下一次交接又读不到。
5. 找不到 git / gh 时**先查本节**，不要再满世界搜（2026-11-08 已踩：误判机器上没有 git）。

## 十、技能注册表（2026-11-08 建立）

> 规则：**备忘是事实与教训，技能是"该怎么做"。** 技能只是入口，命令与细节仍以本备忘和 `docs/` 为准，
> 所以**改了备忘里的命令/路径，必须同步技能**，否则技能会教出错的做法。

**发现机制**（`@deepseek-ai/dsh-skill-filesystem`，本机实测生效）：

| 优先级 | 位置 | 说明 |
|---|---|---|
| 100 | `<项目根>/.dsh/skills` | 项目根 = 最近的含 `.git` 的祖先目录 |
| 200 | `<项目根>/.agents/skills` | 兼容 agents 约定 |
| 400 | `$env:USERPROFILE\.dsh\skills` | **本机实际注册位置** |
| 500 | `~/.agents/skills` | 共享 |

- 结构：`<name>/SKILL.md`；`name` 必须 kebab-case，`description` 必填；
- 热加载：新增/改名/删目录或改 frontmatter 会触发 catalog 刷新，**不用重启**；改正文不需要缓存失效。

**已注册（运行域 3 + 代码域 2）**

| 技能 | 域 | 管什么 |
|---|---|---|
| `session-recall` | 运行 | 唤醒顺序、一级索引、zsearch 检索 |
| `ctxguard-ops` | 运行 | 水位、800K 交接、剪贴板、定时任务核对 |
| `capture-pipeline` | 运行 | F5 抓取、read_watch、导出、残缺快照防护 |
| `release-ops` | 代码 | 发版判定、CHANGELOG、tag、Release、台账回写 |
| `ctxpack-tools` | 代码 | 索引、检索、eval 回归、排障 |

**与旧文件的关系**：记忆库 `SKILL.md`（session-recall 全文，16.6 KB）降级为**详版手册**；
技能是**入口与流程**，两者冲突时以本备忘 + `docs/` 正文为准。仓库 `tools/ctxpack/SKILL.md` 是它的备份副本。

**同步责任（技能 ↔ 正文）**

| 技能 | 依赖的正文 | 正文改了必须检查 |
|---|---|---|
| `session-recall` | `HANDOFF_SPEC.md`、`index_l1.py` 用法 | 唤醒顺序、命令块 |
| `ctxguard-ops` | `HANDOFF_SPEC.md`、本备忘 §四 | 水位阈值、任务名 |
| `capture-pipeline` | 本备忘 §二（坑表）、`tools/ctxpack/` | 抓取命令、`---` 分隔线规则 |
| `release-ops` | `RELEASING.md`、本备忘 §一/§九 | 发版判定、git/gh 路径、四段式 |
| `ctxpack-tools` | `tools/ctxpack/SKILL.md` | 参数默认值（K=100）、回归基线 |

> 规矩：**技能里的命令块必须能在正文里找到出处**；找不到出处 = 技能在裸奔，补正文或删命令。
