# 项目备忘（只和本仓库有关）

> 这份不许并进全局交接包。全局的人格 / 记忆 / 交互规则在记忆库的 `DECISIONS.md`；
> 这里只记**项目事务**：发版规矩、今天踩的坑、常用命令、定时任务、当前状态。
> 最后更新：2026-11-08 16:0x

## 一、发版规则（2026-11-08 澄清）

- **已发布过的对象，只是修改，就不要发版**——不开新 tag、不建 Release、不动版本行。
- 文档修订一律不发版本；Release 只在**交付新的可运行产物**时创建。
- 今天踩过一次：把工具更新误打成 `v3.2.0` 并推了 tag。**已删除**本地与远端 tag，版本行回 `v3.1.9`，
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

## 三、常用命令

```powershell
$PY = "C:\Users\Huang\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$R  = "D:\4590-PC-文件\AI会话检索记录"

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
| `SysPersona-CtxGuard` | 10 分钟 | 测水位；≥450K 出包；≥80%/95% 告警 |
| `SysPersona-Handoff-Refresh` | 30 分钟 | **覆盖** `续接\接续包-最新.md`（防丢） |
| `SysPersona-DeepSeek-LiveWatch` | 每天 03:30 | 整页重载一次，防积压 |
| `SysPersona-DeepSeek-FullSweep` | 每周日 21:00 | 全量巡检 |
| `SysPersona-Memory-Backup` | 每天 23:00 | 打包 zip，留 8 份 |
| `SysPersona-Memory-Maint` | 每天 22:00 | 压缩 / 清理维护 |

## 五、当前状态

- 版本：**v3.1.9**（未发新版）；`main` 已推送。
- 工具：`ctxguard.py` / `index_l1.py` / `read_watch.py` / `zsearch.py` / `index_inc.py` / `eval.py` 均在 `tools/ctxpack/`。
- 待办：无阻塞项；B8–B11f 已关账，B24「满 100」定为每跨 100K 提醒一次。
- 交接文件：v2.1（分锁定段 / 自动段 / 固定格式），规格见记忆库 `续接\交接文件规范-v2.md`。

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
| 7 | 已发布的工具只是改了改，却发了 v3.2.0 | **已发布对象只修改就不发版**；3.1.7/8/9 那种 13 分钟连发三个是反例 | 本备忘 §一 |
| 8 | 改完没有自备份、没有备忘 | 建自备份（git bundle）+ 每次问题记这里 | 本节 + `repo_backup.ps1` |

## 七、自备份

- 脚本：`C:\Users\Huang\AppData\Local\SysPersona\repo_backup.ps1`
- 做法：`git bundle create 备份\SysPersona-OS-<日期>.bundle --all`（含全部分支与历史，单文件可克隆）+
  把本备忘复制一份明文；**只留最近 8 份**。
- 计划任务：`SysPersona-Repo-Backup`，每天 23:30（记忆库备份 23:00 之后）。
- 校验：`git bundle verify 备份\xxx.bundle`；恢复用 `git clone 备份\xxx.bundle 新目录`。

