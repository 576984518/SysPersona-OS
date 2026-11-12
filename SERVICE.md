# SERVICE.md · 常驻三件套（怎么装 / 怎么验 / 怎么停）

> 一句话：**托盘开机自启、守护每 15 分钟自愈一次、每天 23:50 出一张自检表。**
> 装一次，之后换机器照这份文档做一遍就行；装完不需要再手点任何界面。

## 一、三件套是什么

| 件 | 落在哪 | 干什么 |
|---|---|---|
| 托盘自启 | `%APPDATA%\…\Startup\SysPersona-Tray.lnk` | 开机就有图标；点菜单看状态、压缩、重载、换模型 |
| 守护自愈 | 计划任务 `SysPersona-Daemon`（每 15 分钟） | 拉起 `persona_daemon.py --daemon`；多实例忽略新的（IgnoreNew） |
| 每日自检 | 计划任务 `SysPersona-Digest`（每天 23:50） | 跑 `persona_digest.py --once` → `ops\daily-status.md` |

守护自己带着 51 个任务（抓取/索引/备份/交接/成本/自检…），所以**计划任务只留这两个**，
其余全在守护里排——这是 2026-11-11 定的「所有任务都放后台，计划任务只留拉起后台那一个」。

## 二、装：一条命令

前置只有三样东西（都有默认值，但缺了会直接报错而不是猜）：

| 变量 | 含义 | 不设时 |
|---|---|---|
| `SYSPERSONA_ROOT` | 记忆库根 | **必填**，缺了 exit 2 |
| `PERSONA_PY` | python.exe | 试 `SYSPERSONA_RUNTIME\python\python.exe` |
| `SYSPERSONA_REPO` | 代码仓库根 | 按 `tools\persona\` 往上两级推 |

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <仓库>\tools\persona\install-service.ps1 -DryRun   # 先看
powershell -NoProfile -ExecutionPolicy Bypass -File <仓库>\tools\persona\install-service.ps1           # 再装
```

装法是「先 Get 再判」：已存在且正确 → `[OK]` 跳过；不存在 → 建；存在但不对 → 改。
没有 `-Force` 那种一键覆盖，所以别人手调过的任务不会被悄悄抹掉。

**换机器时先用 `persona_machine.py new` 把本机配置生成好**（它会把上面三个变量写进用户环境），
再跑这条命令——顺序反了安装脚本会以 exit 2 提醒你。

## 三、验：四条命令

```powershell
# 1) 两个计划任务在不在、下次什么时候跑
Get-ScheduledTask -TaskName 'SysPersona-*' | Format-Table TaskName, State
Get-ScheduledTaskInfo -TaskName 'SysPersona-Daemon' | Select LastRunTime, NextRunTime

# 2) 守护进程在不在（父进程应是 WmiPrvSE 或计划任务宿主，不是某个 shell）
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'persona_daemon' } | Select ProcessId, CommandLine

# 3) 守护自己怎么说（51 个任务逐个 ok/耗时/消息）
python <库>\脚本\persona_daemon.py --status

# 4) 托盘怎么看（图标颜色 + 最后一行「提示」）
python <库>\脚本\persona_tray.py --status
```

判据：**`--status` 里没有 `FAIL` 行；托盘最后一行是「图标颜色=绿」；tooltip 第一行不带「（异常 N 项）」**。
只有「跳过（本机未配置）：…」是对的——那是这台机器没有那个客户端/密钥，不是故障。

## 四、停 / 恢复 / 卸

```powershell
# 临时停（重启也不跑；开关留痕）
python <库>\脚本\persona_switches.py off dr        # 容灾组一并关，会写 ops\dr-hold

# 只停守护进程，不动计划任务（15 分钟内会被拉回来）
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'persona_daemon' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 彻底停掉自愈（取消自启，不等于卸载）
Disable-ScheduledTask -TaskName 'SysPersona-Daemon'
Disable-ScheduledTask -TaskName 'SysPersona-Digest'
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SysPersona-Tray.lnk"
```

不删任何记忆、不动 `ops\` 里的历史记录；恢复就是把上面反过来（`Enable-ScheduledTask` + 重跑安装脚本）。

## 五、`%LOCALAPPDATA%\SysPersona\` 里的外部脚本（可缺）

守护有 5 个任务是用 `powershell -File` 跑的外部脚本，它们**不随仓库发布**，各机器自己放：

| 文件 | 归谁 | 缺了会怎样 |
|---|---|---|
| `ctxguard.ps1` | 壳侧水位守卫 | 跳过（`跳过：本机没有 ctxguard.ps1`） |
| `maint.ps1` | 记忆库维护 | 跳过 |
| `repo_backup.ps1` | 仓库备份 | 跳过 |
| `handoff_refresh.ps1` | 交接包定时刷新 | 跳过 |
| `start_cdp_browser.ps1` | 抓取用的调试浏览器 | 跳过（仓库 `tools\persona\start_cdp_browser.ps1` 可拷过来） |

**跳过 ≠ 没问题**：这几项本来就该有，只是新机器上还没放。等要用某个功能时再补，补完不用重装。

## 六、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 托盘红、`--status` 里有 `FAIL` | 某个任务真失败了 | 看 `ops\alerts.jsonl` 最后几条；`--once <任务名>` 单独跑一次 |
| `--status` 全是 `跳过（本机未配置）` | 这台机器没装对应客户端/没配密钥 | 正常；要用那功能再配 |
| 计划任务 LastTaskResult 非 0 | 多实例被忽略（`0x800710E0`） | 正常：上一轮还没跑完 |
| 图标不见了但进程在 | 资源管理器重启过 | 守护的 `tray` 任务会在 120 秒内拉回来 |
| 装完重启又没了 | 计划任务是 Interactive 主体，只在登录后跑 | 属于设计：不登录取不到桌面，托盘也画不出来 |
