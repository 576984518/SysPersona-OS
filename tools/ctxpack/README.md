# ctxpack — 上下文提取与归档（MVP）

> 解决两个具体问题：**换会话接不上**，和**平台清会话之后找不回**。

零依赖，单文件，离线。

---

## 三个动作

```bash
# 1) 归档：原文一份（可读 .md）+ 压缩一份（.md.gz）+ 索引一行
python ctxpack.py archive chat.md --title "v2.4 讨论" --tags 规范,提取器

# 2) 提取：抽出「状态 / 定下的 / 否决的 / 下一步」，几百字
python ctxpack.py digest chat.md -o HANDOFF-session.md

# 3) 检索：只取命中片段，不把整包塞进上下文
python ctxpack.py search 分身 --limit 5 --sort time   # 按时间
python ctxpack.py search 分身 --limit 5 --sort freq   # 按命中次数（默认）
python ctxpack.py status ./notes                      # 哪些还没处理
```

---

## 交接与索引（2026-11-08 更新）

| 工具 | 做什么 | 用法 |
|---|---|---|
| `ctxguard.py` | 水位 + **接续包 v2.1**（锁定段原样抽取、自动段限长、3600 字硬顶、指针必留）；`--canonical` 写固定文件 | `python ctxguard.py --prepare` |
| `index_l1.py` | **一级索引**：每源一行（日期 / 来源 / 文件 / 消息数 / 首句），74 源 0.5 秒 | `python index_l1.py` |
| `read_watch.py` | 读不到新内容时**后台重试** 60 秒（抓取落盘有延迟，读太快会误判） | `python read_watch.py --seconds 60` |
| `from_phone.ps1` | 手机端导出的 md 一条命令进库（拷入 → 归档 → 刷新一级索引） | `.\from_phone.ps1 -Path x.md` |

级联关系：**交接只读一次一级索引**；要细节再走 `zsearch.py`（混合检索）或 `HANDOFF.md`（二级）。
接续包分三类——**锁定段**（0.0 / 哲学 / 5.3 / 5.4 / 5.6，只人改）、**自动段**（待办 / 上一轮收尾 / 现状 / 一级索引，限长自动更新）、
**固定格式**（章节顺序 / 首句 / 3600 字上限，改需人确认）。

---

## 为什么不是"让 AI 总结一下"

因为**总结也要占新会话的上下文**。你把三万字的总结塞进去，新会话立刻就掉了三成寿命——这正是"拆东墙补西墙"。

所以这里分成两件事：

| 动作 | 谁做 | 占不占上下文 |
|---|---|---|
| 归档 | 工具（确定性） | 不占 |
| 提取 | 工具（规则抽取） | 不占 |
| 精炼 | 模型（可选） | 占一点，但产物可复用 |
| 检索 | 工具，只回片段 | 只占命中的那几行 |

---

## 会话从哪来（DSH）

DSH 把每次会话存在：

```
%USERPROFILE%\.dsh\sessions\<工作区哈希>\<会话ID>\session.v4.jsonl.zstd
```

格式是**多帧 zstd 压缩的 JSONL 事件流**（`session` / `user/message` / `assistant/message` / `tool/call` / `tool/result` …）。

先导出成 Markdown（Node 自带 zstd，无需装包）：

```bash
node tools/ctxpack/dsh_export.js <会话目录或 .zstd 文件> sessions/2026-11-07.md
```

再提取：

```bash
python tools/ctxpack/ctxpack.py digest sessions -o sessions/HANDOFF.md
```

实测：一次 410 KB 的会话导出后，digest 得到 2.4 KB 的交接摘要——**约为原文的 0.6%**。

> 注意：导出的是你自己的聊天记录，别提交进仓库。`sessions/` 建议加进 `.gitignore`。

## 后台自动抓（网页版）

> 抓**网络层**，不抓网页。所以滚没滚到、虚拟列表渲染了多少，都不影响。

一次性准备：给浏览器快捷方式加参数

```
msedge.exe --remote-debugging-port=9222
```

然后开着这个脚本，正常聊天即可：

```bash
node tools/ctxpack/webwatch.js --port 9222 --out sessions/web.jsonl --match "/api/"
node tools/ctxpack/webwatch.js --check          # 只检查端口开没开
```

它会附着到每个新页面，监听 JSON 响应，抽出文本逐条追加到 `sessions/web.jsonl`；之后照常 `digest`。

**代价要说清楚**：调试端口等于把浏览器控制权暴露在本机 9222 上，任何本地程序都能连。用完就关浏览器（或去掉参数）。

## 只处理"变过的文件"

每次提取都在 `state.json` 里记一行水位（`mtime` + `size`）。下次再跑：

```
待处理 0 个，已跳过 2 个（未变更）
没有新内容，不生成摘要。
```

- 变更的文件才提取，提取过的**先归档、再记水位**——顺序反过来，中途失败就会丢；
- `digest <目录> --dry-run` 先看会处理哪些，不写任何东西；
- `status <目录>` 看哪些还没被处理。

这样"每次会话满了再提取一次"就变成了增量动作，不会把老文件重炒一遍。

## 归档长什么样

```
archive/
  2026-11-07/
    v2-4-讨论-eb8ca7c2.md        ← 原文，可直接读
    v2-4-讨论-eb8ca7c2.md.gz     ← 压缩，纯文本压缩率很高
  index.jsonl                    ← 日期 / 标题 / 标签 / 字数 / sha256
```

- **可读**：Markdown，任何编辑器打开；
- **可压缩**：`.gz`，几万字通常只剩几十 KB；
- **独立**：不在任何平台的数据库里，平台清会话不影响它。

---

## digest 的边界

它按关键词抽行（`必须` / `决定` / `否决` / `下一步` …），**不是语义总结**。

好处：确定、快、不花 token、不会编。
坏处：会漏掉不含这些词的判断，也会把无关的行捞进来。

所以最佳用法是：**工具出骨架，模型补血肉**——把 `digest` 的结果给模型，让它补两三句"为什么"，再存回去。

---

## 语义检索：两路并起来才算召回

纯子串检索（`ctxpack.py search`）只在**你已经知道要查哪个词**时有效；整句提问会掉一半。
`zsearch.py` 把两路并起来：

| 路 | 适合 | 实现 |
|---|---|---|
| 关键词 | 代号、文件名、数字（`C118F500`、`_tail.json`、`500GB`） | IDF 加权字面命中 + **回声降权** |
| 向量 | 换个说法（"显卡怎么救活的" ↔ "满血复活"） | bge-m3，1024 维 |
| 融合 | 两路互补 | RRF，默认 K=100 |

```bash
python zsearch.py "防止舔狗化的具体惩罚规则" --k 100
python zsearch.py "960 显卡" --no-vector      # 没起模型时的降级
```

**实测**（20 题跨文件、去回声、判别词必须命中）：关键词 **20/20**、向量 **19/20**、合并 **20/20**。
单路都不过关——漏的题正好互补：一道只有向量捞得到，一道只有关键词捞得到。

> 「回声降权」是必须的：问题本身常常就躺在语料里，搜索引擎会把"你刚问的这句话"排到答案前面。
> 判定命中时也要逐行剔除引用提问的行，否则测的是"能不能找到自己刚说的话"。

## 自测：别听我说，自己测

`eval.py` 是这套东西唯一的"证据生产线"：

```bash
python eval.py                        # 内置 20 题（跨文件）
python eval.py --set my.jsonl --k 50  # 自己的题库
python eval.py --no-vector            # 只测关键词路
```

题库一行一题：`{"q": "问题", "want": ["答案里必须出现的字面"]}`。
默认会逐行剔除"引用了提问"的片段再判命中——否则测的是"能不能找到自己刚说的话"。

报告三条路的 recall：**关键词 / 向量 / 合并**。

> **三条路怎么选，是你的事，不是我们的。** 默认已经按「记忆优先」配好，其余任你改：
> - **默认**：`ctxpack.py search` 走合并、K=100，慢一点但漏得少；模型没起、索引缺失，
>   它自己退回关键词——**不会把"环境问题"变成"查不到"**；
> - **要零依赖、可预期**：加 `--no-vector`，永不受模型影响；
> - **要自己调**：`zsearch.py --k N` 改召回池，`eval.py` 随时复测，数字自己看。

---

## 零模型检索：SQLite FTS5（persona_fts.py，2026-11-10）

模型没起、断网、`vectors.npy` 缺失、没装 jieba——这些都不该让检索变成 0 条。
`persona_fts.py` 只用标准库（`sqlite3` / `json` / `os` / `re` / `time` / `argparse`），
把 `index\meta.jsonl` 建成 `index\fts.db`，查询走 FTS5 的 `bm25()`。

### 怎么建

```bash
python persona_fts.py --build                    # 全量或增量（按 file + sha256(text) 记账）
python persona_fts.py --build --rebuild          # 推倒重建
python persona_fts.py --build --tokenizer char   # 强制单字+双字（无 jieba 时的模式）
python persona_fts.py --build --with-l1          # 把 L1.jsonl 的首句也收进来
python persona_fts.py --stats                    # 行数/库大小/建库耗时/20 条查询 p50·p95
```

产物是 `<库根>\index\fts.db`，表 `chunks(rowid, file, day, source, role, text,
device, model, avatar)` + `chunks_fts(tokenize)`（后三列给设备/底座/分身过滤用）。**库本身不碰记忆正文**；`meta.jsonl` / `L1.jsonl` 只读。
`persona_search.py` 通过同目录的 `persona_fts.py` 调它；平铺部署时把这两个文件
一起放在库根即可，仓库版加 `SYSPERSONA_ROOT=<库根>` 也能直接读库。
分词模式记在 `build_meta` 里：`char`（单字+相邻双字，纯标准库）或 `jieba`（有 jieba 才用）。
同一个库只能用同一种模式查；换模式请 `--rebuild` 或 `--db` 另建一个库。

增量按 `file + sha256(text)` 复用块：文件没变就整份跳过，变了的文件只重写新块，
旧块从 `chunks` 与倒排里一起删。`--prune` 才会清掉输入里已消失的文件（默认不删，
免得拿 L1 建库时把 meta 的块误删）。

### 怎么查

```bash
python persona_fts.py --search "上下文压缩" --k 20
python persona_fts.py --search "DSH 水位 350K" --k 20 --rerank
```

输出固定四列：`分数<TAB>文件:行号<TAB>来源<TAB>前 80 字`。
查询本身以 `--` 开头时（如查 `--no-vector`）要写成 `--search="--no-vector"`，
否则 argparse 会把它当成选项。
`--rerank` 是**纯 Python 二路融合**：FTS 排名 + 查询词元命中密度（覆盖了几个词元、
每 500 字出现几次），再加“整句原样出现”的小加成。没有任何模型，也没有向量。
`--stats` 固定跑 20 条查询（精确标识符 / 中文短语 / 中英混合 / 模糊 / 长句 / 数字 /
代码 / 时间各若干），报 p50/p95；换机器可直接复测。

### 在 persona_search.py 里怎么切

`persona_search.py --engine` 保留全部路径，默认 `auto`：

```text
auto      有向量：原「关键词 + 向量」混合；向量不可用：优先 FTS；FTS 也没有：回落关键词
fts       默认 fts.db（库内记录实际分词模式）
fts-char  强制单字+双字库（PERSONA_FTS_DB_CHAR）
fts-jieba 强制 jieba 库（PERSONA_FTS_DB_JIEBA；没有 jieba 会明确报错，不静默换模式）
keyword   原来的 IDF 关键词路（--no-vector 的老行为）
vector    只走向量路（模型/网络不可用时返回空，不拿时间线兜底冒充命中）
hybrid    FTS + 关键词两路 RRF（不碰向量）
```

`auto` 在向量不可用、`fts.db` 不存在时会打印一句
`fts.db 不存在：...；先运行 python persona_fts.py --build`，然后照旧回落关键词——
**绝不把环境问题变成“查不到”**。设备/底座/分身过滤在建库时已写进 `chunks`，
FTS 路不需要先花十几秒读 archive 建块。默认输出格式（`文件:行号` 的样式）没有改。

### 按查询特征走哪条路

| 查询特征 | 首选 | 本机 40 题实测（Top3 相关/15） | 失效边界 |
|---|---|---|---|
| 精确标识符、代码片段、型号（`persona_search`、`--no-vector`、`B75`、`350K`） | `fts-char` | fts 14、keyword 6、vector 3、hybrid 13 | 拼错一个字符就查不到；下划线/大小写由 unicode61 兜底 |
| 已知写法的中文短语（`上下文压缩`、`记忆分身`） | `fts-char` | fts 14、keyword 12、vector 4、hybrid 11 | 同义改写（“压缩上下文”→“上下文压缩”）不一定命中 |
| 中英混合（`DSH 水位 350K`、`bge-m3 向量`） | `fts-char` | fts 15、keyword 15、vector 10、hybrid 14 | 英文缩写与全称不互通 |
| 含糊描述、口语（“那个不换窗口的方案”） | `fts-char` / `keyword`；向量只在“原文完全没有共同字面”时补位 | fts 9、keyword 9、vector 3、hybrid 8 | 向量只给近邻，不给因果；本机 bge-m3 对短否定句（“不换窗口”）明显跑偏 |
| 长句、多个条件（“我上次说那个检索慢的问题最后怎么处理的”） | `fts-char` / `keyword` | fts 8、keyword 8、vector 5、hybrid 7 | 关键词路冷启动要建 `blocks/idf`（本机约 18 秒），不适合一次性 CLI |
| 时间性提问（“11月7日的会话”“昨天改了什么”） | `fts-char`（先把相对时间换成日期） | fts 8、keyword 4、vector 5、hybrid 7 | 纯 FTS 不理解“昨天”；相对时间必须由调用方先归一成 `YYYY-MM-DD` |
| 纯语义改写（原文连“窗口/方案”这类词都不出现） | `vector`（若可用） | 本批题里没有这类“字面全不同”的样本；vector 只在“最近一次发版”一题拿到 1/3 | 语义近邻不等于答案；模型/网络不可用时此路直接没有 |
| 无模型、无网络、无 jieba | `fts-char` + `keyword` | fts 可跑，keyword 可跑 | 语义改写基本无解；索引体积约为 meta 的 5 倍 |

一句话：**字面特征强 → FTS；连字面都不同、且模型可用 → 向量；两者都有 → hybrid。
但这批真库上 vector 没有一类超过 FTS，hybrid 只增加召回、Top3 精度没超过 FTS——
“语义描述”不等于“向量一定更准”，先看查询里有没有可命中的字面锚点。**

### 代价与极限（本机实测，56,685 块 / 67.1 MiB meta）

| 项 | fts-char | fts-jieba（桩，非真实 jieba） | 说明 |
|---|---|---|---|
| 建库 | 47.8 s | 54.2 s | 全量；增量未变时 2.7 s（56,685 块全部复用） |
| 库大小 | 354.3 MiB | 193.0 MiB | meta.jsonl 67.1 MiB，约 5.3× / 2.9× |
| 查询 p50 / p95 | 42–57 / 360–430 ms | 57.1 / 226.1 ms | 20 条固定查询，两次运行；冷启动首查含打开 354 MiB 库 |
| 依赖 | 标准库 | jieba（可选） | 没有 jieba 时 `--tokenizer jieba` 直接报错，不偷偷换模式 |

> `fts-jieba` 这列是**临时桩**测的：本机没装 jieba，用 `km-api` 里现成的
> `dict.txt` 做最长匹配，只验证“jieba 分支能建库/能查”这条代码路径，
> 不代表真实 jieba 的准确率。真实 jieba 装好后 `--tokenizer auto` 会选它，
> 但必须 `--rebuild` 或另建 `--db`（分词模式变了，旧库查询会错位）。

极限写清楚：

- **FTS 是字面检索，不是语义检索**。换个说法、跨语言、指代（“那个方案”）都靠词元碰运气；
  `--rerank` 只是把“命中更多查询词元”的块往前挪，不会理解同义。
- **单字+双字索引很大**。354 MiB 是拿空间换“纯标准库也能查中文”；磁盘紧张时用 jieba 模式，
  或给 `--db` 指到别的盘。
- **行号是回原文里找块首行**，块从半行开始时可能定位到该行行首；找不到返回 `:0`，不是伪造行号。
- **`contentless_delete=1` 需要 SQLite ≥ 3.43**。更老的 SQLite 会在建库时报错；
  升级 SQLite 或用 `--rebuild` 换库，不会悄悄写坏旧库。
- **hybrid 不是“永远比纯 FTS 好”**。本机 40 题实测：精确标识符 13/15 相关
  （`fts-char` 14/15），混合中英 14/15（15/15），含糊描述 8/15（9/15），
  只在代码片段上和 FTS 持平（10/15）。原因是 RRF 会把关键词的深位候选和 FTS 的
  中位候选相加，可能把 FTS 第 1 名挤到第 2。查询里有精确标识符/型号时，
  直接用 `fts-char` 更稳。
- 建库读的是 `meta.jsonl` 的快照。抓取还在写时，先跑一次增量（`--build`）再查；
  未变的块不会重写，成本约 2–3 秒。

## 索引要跟得上抓取（index_inc.py）

全量重建永远是过期件——抓取每分钟在写，建完就旧。所以按 `sha256` 记账，只重嵌变过的文件：

```bash
python index_inc.py             # 增量（第一次等于全量）
python index_inc.py --dry-run   # 只看要嵌多少块
```

三个必须记住的实现细节，都是踩过的：

1. **长消息要滑窗，不能截断**。`text[:1200]` 会把结论段切掉；`600 字 / 80 重叠` 才留得住。
2. **文档型归档要单独切块**。`README` / `SKILL` / `DECISIONS` 没有 `## 用户 / ## 助手` 结构，
   只认对话块的分块器会把它们**整份丢掉**——决策台账因此曾经完全检索不到。
3. **`split('\n---\n', 1)[1]` 只对会话导出成立**。对文档用，会把它第一个 `---` 之前的正文
   （往往正是表格与结论）切掉。

产物：`vectors.npy`（float32, N×1024）、`meta.jsonl`、`manifest.json`。
写入先落临时文件再原子替换，**重建期间旧索引始终可查**。

> 嵌入可以在云端做（bge-m3 有免费额度，全量约 4 分钟 / 390 万 tokens），也可以在本地 CPU 做（慢 20–40 倍）。
> 建库与查询最好用同一个模型实现；混用会带来轻微漂移，实测仍在可接受范围。

---

## 与 SysPersona 的关系

这是规范里「压缩记忆」和「DataSource」的最小实现：

- 归档 = `memory.db` 的冷层；
- digest = 唤醒时的恢复指针；
- search = 检索注入，只带命中片段。

先跑通这个，再谈 Runtime。
