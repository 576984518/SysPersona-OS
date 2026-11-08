---
name: session-recall
description: 从 NAS 记忆库取回历史会话上下文（DSH 会话 + DeepSeek 网页对话），用 digest 生成交接摘要、用 search 只回命中片段；也覆盖 DeepSeek 网页版的抓取与增量巡检。
---

# 会话检索（SysPersona 记忆外挂）

记忆库在 `D:\4590-PC-文件\AI会话检索记录\`（映射盘 `Z:`）。
放的是**记忆**，不是源码；源码在 `D:\文档\deepseek-harness\default-workspace`。

## 什么时候用

- 用户说"接着做 / 上次聊到哪 / 我昨天说过什么"，需要**恢复上下文**；
- 需要查某段历史细节（某个命令、某个结论、某次踩坑），但不想把几万字全读进来；
- 需要把当前会话**导出归档**，或把 DeepSeek 网页对话**抓进记忆库**。

## 0. 唤醒顺序

1. **`LATEST.md`**（每分钟自动刷新：当前会话最近 4 条 + 条数 + 状态，先看有没有新动静）
2. **`BACKLOG.md`**（待办清单：哪些做完了、下一个是什么；**做完一件就勾掉，直接推下一件**）
3. **`DECISIONS.md`**（决策台账，一屏看完当前决定与状态）
3. `HANDOFF.md`（按来源的摘要，日期倒序，最近的在前）
4. `search` 命中片段 → 才读原始 .md

> 纪律：**每做完一件事、每次接新任务，先读 1 和 2**；没有任务时也保持每分钟由脚本刷新 `LATEST.md`。
先读摘要，几千字，通常就够开工；不够再检索，**不要**一上来读整篇会话。

> **约定（2026-11-08 定）：用户只回 `1`、`继续`、单个字符或任何无意义输入 —— 一律视为「读检索并继续」。**
> 收到这种输入时按顺序做三件事，**不要追问**：
> 1. 读 `LATEST.md`，看网页会话有没有新内容；
> 2. 用 `zsearch.py` 检索与当前任务相关的片段（不要整篇读）；
> 3. 直接推 `BACKLOG.md` 里的下一件，做完再汇报。
>
> 这条的用处：把"继续"从一句需要解释的话变成一个固定动作，换哪个模型接手都一样。
>
> **补充（2026-11-08）：用户说"读会话 / 看一下会话"时，他一定会先按 F5。**
> 如果 `LATEST.md` 仍显示「正文未变」，**先提醒他刷新**（"你还没按 F5"），
> 不要只回一句"没有新内容"——那会让用户以为监听坏了。
> 抓取由常驻的 `watch_nav.js` 负责：它监听页面跳转，F5 后 1–2 秒自动入库。

## 1. 运行时（本机没有全局 python / node）

```powershell
$PY   = "C:\Users\Huang\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$NODE = "C:\Users\Huang\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
$env:PYTHONIOENCODING = "utf-8"   # 不设则中文在控制台显示为乱码（文件本身没问题）
```

node **不能直接跑 UNC 路径**（会去找 `\\Nas-1900\package.json` 而报错），先映射：

```powershell
net use Z: '\\Nas-1900\文档' /persistent:no
```

## 2. 布局

```
会话检索261107\
├─ ctxpack.py  zsearch.py  index_inc.py  dsh_export.js  webwatch.js
├─ webdump.js（实时抓）  deepseek_sweep.js（批量抓）
├─ SKILL.md  README.md  HANDOFF.md  state.json
├─ sessions\<日期>\<来源>\      ← 可读正文（来源 = 电脑会话 / 网页对话 / 网页文章）
└─ archive\<日期>\<来源>\ + index.jsonl   ← 归档 .md + .gz + 索引
```

来源名 = 路径里日期段的下一段，归档自动跟随，不同来源互不干扰。

## 3. 检索

```powershell
& $PY D:\4590-PC-文件\AI会话检索记录\ctxpack.py search 人格 --limit 5
& $PY D:\4590-PC-文件\AI会话检索记录\ctxpack.py search J1900 --source 网页对话 --sort time
& $PY D:\4590-PC-文件\AI会话检索记录\ctxpack.py status D:\4590-PC-文件\AI会话检索记录\sessions
```

`--source` 可选：`电脑会话`（另一台电脑的 DSH 工作区）、`网页对话`、`网页文章`。
输出只给**命中片段**，把这几行贴进上下文即可，不要整篇读。

### 3.1 混合检索（默认）：`zsearch.py`

`ctxpack.py search` 是**子串**检索——你知道要查哪个词才行。整句提问请用 `zsearch.py`：
关键词路（IDF + 回声降权）与向量路（bge-m3）并行，RRF 融合，默认 K=100。

```powershell
& $PY D:\4590-PC-文件\AI会话检索记录\zsearch.py "防止舔狗化的具体惩罚规则" --k 100
& $PY D:\4590-PC-文件\AI会话检索记录\zsearch.py "960 显卡" --no-vector   # 模型没起时降级
```

实测（20 题跨文件、去回声）：关键词 20/20、向量 19/20、合并 **20/20**。
默认 K 必须是 **100**——「备份保留几份」这类题的正确块在 78–85 名，K=30 会漏。

### 3.2 自测：`eval.py`

```powershell
& $PY D:\4590-PC-文件\AI会话检索记录\eval.py                  # 内置 20 题，报三条路 recall
& $PY D:\4590-PC-文件\AI会话检索记录\eval.py --no-vector      # 只测关键词
```

改动检索相关代码后**必须跑一次**，别凭感觉。当前基线：关键词 20/20、向量 19/20、合并 20/20。

> 三条路怎么选由使用者决定：合并最稳、关键词零依赖、`--hybrid` 是折中。
> 无论选哪种，**模型挂了都不能导致"查不到"**——`--hybrid` 会自动退回关键词。

### 3.3 索引维护：`index_inc.py`

抓取每分钟在写，全量重建跑完即旧。所以按文件 `sha256` 记账，只重嵌变过的文件：

```powershell
& $PY D:\4590-PC-文件\AI会话检索记录\index_inc.py --dry-run   # 看要嵌多少块
& $PY D:\4590-PC-文件\AI会话检索记录\index_inc.py             # 增量，通常几十秒
```

产物在 `index\`：`vectors.npy` + `meta.jsonl` + `manifest.json`；写入原子替换，重建期间旧索引照常可查。
索引是云端免费 bge-m3 建的、查询走本地量化版，属混合索引——实测损失可接受，别指望完全同分布。

### 3.4 长检索怎么办：两条路，任选

`--deep`（重排池 100）单次要 20–30 秒。**不要在前台干等**，下面两种都能做到"先回话、后出结果"。

**A. 代理层后台作业（零代码，适合 AI / 脚本）**

把检索丢进宿主的后台任务，自己继续对话，跑完再取。规则：**凡预计超过 3 秒的检索，一律后台化**。
缺点是只有能开子进程的调用方能用，纯命令行用户没有这个能力。

**B. 工具内 `--async`（适合任何调用方，包括裸 CLI）**

```powershell
# 立刻返回 job id（实测 0.3 秒）
& $PY zsearch.py "拒绝权 内核" --k 3 --deep --async
# 20261108-144151-3432

# 过一会儿取结果；没跑完会打印 RUNNING
& $PY zsearch.py --result 20261108-144151-3432
```

结果落在 `index\jobs\<id>.json`，跑完才出现——**看到 RUNNING 就说明还在跑，不是失败**。
缺点是每次都会留一个子进程和一份结果文件，需要偶尔清理 `index\jobs\`。

> **两种都保留，用哪个取决于使用者。** A 不占工具进程，B 不依赖宿主；
> 唯一必须守住的底线是：**不能让"等结果"把对话卡死**。

## 4. 归档 + 出摘要

```powershell
# 日常（推荐）：未变的不重新归档（按内容哈希幂等），摘要重写成全量版
& $PY D:\4590-PC-文件\AI会话检索记录\ctxpack.py digest D:\4590-PC-文件\AI会话检索记录\sessions `
     -o D:\4590-PC-文件\AI会话检索记录\HANDOFF.md --all --max-chars 14000
```

- `--all`：忽略水位，把所有来源汇成一份总摘要（唤醒用）；
- `--max-chars`：摘要总字数上限，超出的只列文件名（正文全在归档里，可 search）；
- 不带 `--all` 时只写本批，摘要会变短——**给新窗口用请带 `--all`**。

## 5. 抓 DeepSeek 网页对话（需要浏览器开调试端口）

浏览器必须带参数启动，**否则读不到页面**：

```powershell
Stop-Process -Name msedge -Force
Start-Process "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  -ArgumentList '--remote-debugging-port=9222','--remote-allow-origins=*',`
                '--restore-last-session',"--user-data-dir=$env:LOCALAPPDATA\Microsoft\Edge\User Data"
```

### 巡检范围：默认只看最近 10 个

会话是**只追加**的，旧会话上下文满了就不再变。所以：

```powershell
# 日常：最近 10 个，约 1 分钟
& $NODE D:\4590-PC-文件\AI会话检索记录\deepseek_sweep.js D:\4590-PC-文件\AI会话检索记录\sessions\<今天>\网页对话

# 周期全量：--full；连续 15 个未变即提前收工
& $NODE D:\4590-PC-文件\AI会话检索记录\deepseek_sweep.js <目录> --full --stop-after 15

# 只看前几个 / 只看不变动
... --limit 3        ... --dry-run
```

判定：每个会话只读**最下方那条消息**，指纹存在 `_tail.json`；末条一致 → 整段没变，直接下一个。
**当前打开的会话**（可能多个标签）自动排在**最后**，最多重抓 4 轮直到稳定，不会被跳过也不会抓到半截。

### 实时模式（只盯当前会话）

```powershell
& $NODE D:\4590-PC-文件\AI会话检索记录\deepseek_sweep.js <目录> --active
```

**不跳转**，只读当前标签的 DOM；只看**最近 4 条**尾部指纹，没变 0.3 秒退出，变了才临时撑高视口抓全量，抓完还原视口与滚动位置。
页面开久了会停止加载更早的消息（比过一次重载后 132→163 条），所以加 `--reload`：抓之前先重载页面。

周期任务（都已注册，浏览器没开调试端口时静默跳过）：

| 任务名 | 频率 | 行为 |
|---|---|---|
| `SysPersona-DeepSeek-LiveWatch` | 每 1 分钟 | `--active`，只抓当前会话，日志 `%LOCALAPPDATA%\SysPersona\live.log` |
| `SysPersona-DeepSeek-FullSweep` | 每周日 21:00 | `--full --stop-after 15` → `digest --all`，日志 `sweep_full.log` |

## 6. 去重规则（跑 N 次结果一致）

| 层 | 规则 |
|---|---|
| 抓取 | 文件名 = 会话 ID；末条指纹一致 → 跳过；否则全文比对，逐字相同也不写 |
| 水位 | `state.json` 按 mtime+size，未变整条跳过 |
| 归档 | 按内容 sha256：哈希没变（哪怕 mtime 变了）只更新水位，不重复归档、不追加索引 |
| 内容真变了 | 新归档**替换**旧归档，索引删旧行——一个来源只留最新快照 |

## 7. 踩过的坑（照做即可，别重试错）

1. Edge 的**启动加速**会吞掉调试参数：必须显式传 `--user-data-dir`。
2. DeepSeek 登录 cookie 是**会话级**的：重启浏览器就要重新登录。
3. 页面**不可见就不渲染消息**：CDP 里先 `Page.bringToFront` + `Emulation.setFocusEmulationEnabled`。
4. 消息是**虚拟列表**（首屏约 213 条）：要滚到顶把历史全部触发加载；视口撑到 30000px 可一次挂载，快得多。
5. 消息块选择器：`.ds-message`（用户带 `d29f3d7d`，助手含 `.ds-markdown`，思考在 `.ds-think-content`）。
6. node 跑 UNC 会崩 → 用 `Z:` 盘。
7. `powershell.exe` 读**无 BOM 的 UTF-8 脚本**会把中文当 ANSI 而报“字符串缺少终止符”→ 存成带 BOM。

## 8. 导出本机 DSH 会话

```powershell
& $NODE D:\4590-PC-文件\AI会话检索记录\dsh_export.js `
  "$env:USERPROFILE\.dsh\sessions\<工作区哈希>\<会话ID>" `
  D:\4590-PC-文件\AI会话检索记录\sessions\<日期>\电脑会话\<日期>.md
```

原始文件在 `...\sessions\<工作区哈希>\<会话ID>\session.v4.jsonl.zstd`（多帧 zstd 的 JSONL）；
`_raw\` 里的原始副本**绝对别删**，它不可再生。

## 9. 边界

- 这里是**私人聊天记录**，别提交 git；
- 归档删了检索就没了，`_raw` 建议再备份一份到网盘；
- 脚本是仓库 `tools/ctxpack/` 的副本，改了要双向同步（当前 ctxpack v2.6）。
