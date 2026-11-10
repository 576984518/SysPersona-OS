# 常驻程序三件套 — SERVICE

> B86 · 2026-11-10
> 目标：开机有托盘，挂了有人拉，每天有人把六项体检写下来。
> 这套东西是**用户级**的：不装 Windows 服务，不需要管理员。

文件位置：

| 文件 | 作用 |
|---|---|
| `tools/persona/install-service.ps1` | 安装 / 修正托盘自启与两个计划任务（幂等，支持 `-DryRun`） |
| `tools/tray/persona_tray.py` | 托盘状态灯（`pythonw` 启动，只显示不代办） |
| `persona_daemon.py`（记忆库根） | 常驻守护：抓取 / 索引 / 归档 / 保活 / 心跳 |
| `persona_digest.py`（记忆库根） | 每日自检：六项写进 `ops/daily-status.md` |
| `tools/ctxpack/persona_digest.py` | 自检脚本的仓库副本；运行时仍以记忆库根那份为准 |
| `docs/SERVICE.md` | 本文件：怎么装 / 怎么验 / 怎么停 |

---

## 一、怎么装

### 0. 先看路径（不用改脚本）

> 本文档里出现的 `$Repo` / `$Mem` / `$Py` / `$Pyw` 是**占位变量**，不是某台机器的固定路径：
> `$Repo` = 仓库目录，`$Mem` = 记忆库根（`SYSPERSONA_ROOT`），`$Py` / `$Pyw` = 你的 python / pythonw。
> PowerShell 里请照 §0 那三行设成环境变量或你自己的变量，别把示例字符串直接照抄。

`install-service.ps1` **不写死路径**：它从环境变量取，缺了就 `[FAIL]` 并 `exit 2`，不会拿别处的默认值硬跑。

```powershell
$env:SYSPERSONA_ROOT = '<记忆库目录>'          # 必填：库根（含 ops\ sessions\ index\）
$env:PERSONA_PY      = '<pythonw.exe 绝对路径>' # 可选：不给就用 PATH 里的 pythonw
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\persona\install-service.ps1 -DryRun
```

`-DryRun` 会先把你设的两个值、四条计划任务的目标、以及每个待装文件的实际解析路径全打印一遍；确认无误再去掉 `-DryRun` 真装。

确认这三个脚本都在：

```powershell
Test-Path "$Repo\tools\tray\persona_tray.py"
Test-Path "$Mem\persona_daemon.py"
Test-Path "$Mem\persona_digest.py"
```

三个都应为 `True`。

### 1. 先演练，不改系统

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$Repo\tools\persona\install-service.ps1" -DryRun
```

看到 `[DRY] 将创建计划任务 ...` 和最后四行 `[现状]` 才算读懂了它要做什么。
**第一次装、或者改过脚本之后，都先跑这一遍。**

### 2. 真装

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$Repo\tools\persona\install-service.ps1"
```

它会做四件事，每件都先 `Get` 再判：

1. **托盘自启**：在
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` 下建
   `SysPersona-Tray.lnk`，目标 `pythonw.exe`，参数
   `"$Repo\tools\tray\persona_tray.py"`，工作目录 `tools\tray`。
2. **守护自愈**：计划任务 `SysPersona-Daemon`，每 15 分钟运行
   `pythonw.exe "$Mem\persona_daemon.py" --daemon`；
   `IgnoreNew`（上一轮没退就不叠新的）、`StartWhenAvailable`、`RunLevel Limited`、
   `LogonType Interactive`。
3. **每日自检**：计划任务 `SysPersona-Digest`，每天 23:50 运行
   `pythonw.exe "$Mem\persona_digest.py" --once`。
4. **体检**：打印四行现状（托盘 lnk、两个任务的 State/NextRunTime、pythonw 进程数）。

重复跑不会重复建：已存在且正确 → `[OK] 已存在...`；存在但目标不对 → 修正；
不存在 → 创建。脚本里没有 `-Force`。

### 3. 现在就用起来（可选）

托盘本来要等下次登录才自启；想立刻看到状态灯：

```powershell
Start-Process "$Pyw" `
  -ArgumentList '"$Repo\tools\tray\persona_tray.py"'
```

守护不用手动开——15 分钟内计划任务会自己拉起；想立刻跑一轮：

```powershell
Start-ScheduledTask -TaskName SysPersona-Daemon
```

自检想立刻看结果：

```powershell
Start-ScheduledTask -TaskName SysPersona-Digest
```

（计划任务用 `pythonw`，没有控制台；结果看文件。想在终端里看表，用下面「验」里的
`python.exe` 手动跑法。）

---

## 二、怎么验

### 1. 验托盘快捷方式

```powershell
$lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SysPersona-Tray.lnk"
$sh  = New-Object -ComObject WScript.Shell
$s   = $sh.CreateShortcut($lnk)
$s | Select-Object TargetPath, Arguments, WorkingDirectory
```

应看到：

- `TargetPath` = `$Pyw`
- `Arguments` = `"$Repo\tools\tray\persona_tray.py"`
- `WorkingDirectory` = `$Repo\tools\tray`

### 2. 验两个计划任务

```powershell
Get-ScheduledTask -TaskName SysPersona-Daemon, SysPersona-Digest |
  Select-Object TaskName, State

Get-ScheduledTaskInfo -TaskName SysPersona-Daemon |
  Select-Object LastRunTime, LastTaskResult, NextRunTime

Get-ScheduledTaskInfo -TaskName SysPersona-Digest |
  Select-Object LastRunTime, LastTaskResult, NextRunTime
```

判读：

- `State` 应为 `Ready`（正在跑时是 `Running`；不要是 `Disabled`）；
- `SysPersona-Daemon` 的 `NextRunTime` 应在 15 分钟以内；
- `SysPersona-Digest` 的 `NextRunTime` 应是当天或次日 `23:50`；
- 跑过之后 `LastTaskResult` 应为 `0`（非 0 先看「事件查看器 → 任务计划程序」）。

### 3. 验进程

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue |
  Select-Object Id, StartTime, Path
```

正常至少有两个：守护和托盘。`Get-Process pythonw` 数的是**所有** pythonw，
包含托盘，不等于「daemon 进程数」；要看谁是谁，用命令行过滤：

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*persona_daemon.py*' } |
  Select-Object ProcessId, CommandLine
```

### 4. 验每日自检

手动跑一遍（用 `python.exe`，能在终端看到表）：

```powershell
& "$Py" `
  "$Mem\persona_digest.py" --once
$LASTEXITCODE
```

判读：

- 退出码 `0` = 六项全是 OK/WARN；
- 退出码 `1` = 至少一项 FAIL（表里 `判定` 列会写 `FAIL`）；
- 文件应落在 `$Mem\ops\daily-status.md`。

```powershell
Get-Content "$Mem\ops\daily-status.md" -Encoding UTF8
```

只想拿机器可读结果、不写文件：

```powershell
& "$Py" `
  "$Mem\persona_digest.py" --json
```

### 5. 验自愈（想验证时再做，会短暂停掉守护）

```powershell
# 杀掉守护（只杀命令行里带 persona_daemon.py 的 pythonw，不动托盘）
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*persona_daemon.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 等计划任务下一轮（最多 15 分钟），或者立刻触发
Start-ScheduledTask -TaskName SysPersona-Daemon
Start-Sleep -Seconds 5

# 应该又能看到守护进程
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*persona_daemon.py*' } |
  Select-Object ProcessId, CommandLine
```

---

## 三、怎么停

### 1. 临时停（保留配置，改天还能开）

```powershell
Disable-ScheduledTask -TaskName SysPersona-Daemon
Disable-ScheduledTask -TaskName SysPersona-Digest
```

停进程（只停这两个脚本，不碰别的 pythonw）：

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object {
    $_.CommandLine -like '*persona_daemon.py*' -or
    $_.CommandLine -like '*persona_tray.py*'
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

重新开：

```powershell
Enable-ScheduledTask -TaskName SysPersona-Daemon
Enable-ScheduledTask -TaskName SysPersona-Digest
```

托盘下次登录会自启；想现在开，用「一、3」里的 `Start-Process`。

### 2. 去掉托盘自启

```powershell
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SysPersona-Tray.lnk"
```

这只删开机项，不杀当前正在跑的托盘。

### 3. 彻底卸载（计划任务也删）

```powershell
Disable-ScheduledTask  -TaskName SysPersona-Daemon -ErrorAction SilentlyContinue
Disable-ScheduledTask  -TaskName SysPersona-Digest -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName SysPersona-Daemon -Confirm:$false
Unregister-ScheduledTask -TaskName SysPersona-Digest -Confirm:$false
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SysPersona-Tray.lnk" `
  -ErrorAction SilentlyContinue
```

再按「1. 临时停」杀一次进程即可。重装就是重新跑 `install-service.ps1`。

---

## 四、为什么这么设计

**常驻程序不该靠人记得。** 人只会记得「今天要用」，不会记得「每 15 分钟看一眼守护还在不在」；
把这件事交给计划任务，人只负责看结果。

**自愈比自启重要。** 启动项只管开机那一下；进程在下午三点挂了，启动项不会知道，也不会再拉。
所以托盘靠 Startup，守护靠「每 15 分钟一次」的计划任务：**启动项负责开始，计划任务负责一直在**。

几个配套决定：

- **15 分钟 + `IgnoreNew`**：间隔够短，挂了最多一刻钟被发现；`IgnoreNew` 保证上一轮没退时
  不叠新的，不会越堆越多。
- **`persona_daemon.py` 自己还有单实例锁**：就算计划任务和手工同时拉起，第二个也会自己退出，
  不会出现两个守护抢同一份 `ops/`。
- **`LogonType Interactive` + `RunLevel Limited`**：只在这个用户登录后跑，不提权；
  所以它不碰系统目录，也不需要管理员。
- **每日自检只写一个文件**：`ops/daily-status.md` 一张表，第二天一眼看完；不新开数据库、
  不往聊天窗口里塞长文。
- **`-DryRun` 先演练**：装常驻程序最容易犯的错是「以为装上了，其实任务指错了脚本」；
  先看它要做什么，再让它做。

---

## 五、边界

- **不装 Windows 服务**：Windows service 需要管理员权限、要写服务控制管理器；我们只做
  当前用户的启动项 + 计划任务。代价是**用户没登录时不会跑**，这是有意接受的。
- **不动内核**：不修改 DSH / Hermes 等壳的程序文件，不改 `memory\设定\`，不改 `persona_*.py`
  里的判定逻辑；这里只负责「什么时候把哪个脚本拉起来」。
- **不删记忆**：安装、自检、卸载都不删除 `sessions\`、`archive\`、索引或备份；自检只读，
  最多写 `ops/daily-status.md`。
- **不装依赖、不联网**：假设 Python 运行时、`pystray` / `Pillow` 已就绪；脚本不执行 pip，
  也不从网络拉东西。
- **路径来自环境变量，不是这台机器的**：`SYSPERSONA_ROOT`（必填）与 `PERSONA_PY`（可选）；
  缺变量时脚本打 `[FAIL]` 并 `exit 2`，先 `-DryRun` 核对再真装。

---

## 六、抓取用的调试浏览器（默认无头）

> 2026-11-10 · 修掉「每次抓取都要人先开一个调试浏览器」。
> 抓取链路（`persona_readnew.py` → `persona_daemon.py --once capture`）连的是
> `http://127.0.0.1:9222`；没人开时**自动拉起**，平时是**无头**的，不弹窗口。

| 文件 | 作用 |
|---|---|
| `%LOCALAPPDATA%\SysPersona\start_cdp_browser.ps1` | 探测 / 拉起 / 停止（幂等） |
| `%LOCALAPPDATA%\SysPersona\cdp-profile\` | 它**专用**的浏览器 profile（登录态存这儿） |
| `%LOCALAPPDATA%\SysPersona\cdp-browser.log` | 每次启动的时间、模式、结果 |
| `Startup\SysPersona-CDP.lnk` | 开机自启：登录后无头拉起 |

### 为什么用独立 profile

- **不碰你日常浏览器的 profile**：日常窗口的标签、登录、扩展完全不受影响；刷新页面、
  跳会话只发生在 `cdp-profile` 这个进程里。
- 抓取要一个「登录着 DeepSeek、且随时能被 CDP 控制」的浏览器，把它和日常浏览分开：
  关掉调试浏览器不丢你正在看的页面；profile 坏了也只影响抓取。
- 两者可以同时开：两个进程、两个 `--user-data-dir`，互不抢锁。

### 首次：登录一次（只有这一次要开窗口）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\SysPersona\start_cdp_browser.ps1" -Login
```

- `-Login` **开一个可见窗口**（不无头），打开 `https://chat.deepseek.com`；
- 在里面扫码 / 登录；
- 看到会话列表后**直接关掉这个窗口**——登录态已写进 `cdp-profile`，以后无头复用。

> 如果 9222 还被旧的手工调试窗口占着，先关掉那个窗口再登录；
> 本脚本的 `-Stop` 只会停 `cdp-profile` 的实例，不会动旧 profile。

### 平时：不用管

- 开机：`SysPersona-CDP.lnk` 无头拉起；
- 每次抓取前：`persona_readnew.py` 先调 `ensure_cdp()`，CDP 不通就自己跑这个脚本
  （最多等 20 秒），输出里会有一行 `CDP：已在跑` 或 `CDP：已拉起`；起不来会如实报
  `CDP 起不来：…`，不会误报「没有新内容」；
- 无头确实抓不到（个别页面不渲染）时，手工回落一次可见窗口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\SysPersona\start_cdp_browser.ps1" -Headful
```

启动输出和 `cdp-browser.log` 会写明「本次回落到可见窗口」。

### 验证

```powershell
curl.exe -s --noproxy '*' http://127.0.0.1:9222/json/version
curl.exe -s --noproxy '*' http://127.0.0.1:9222/json
```

- 第一条返回 `"Browser": "Edg/…"` = CDP 活着；
- 第二条里找一个 `"type": "page"`、`"url"` 含 `chat.deepseek.com` 的项，看 `title`
  是否为 DeepSeek 页面——**页面在无头里也照样渲染，CDP 照样可用**。

### 怎么停

```powershell
# 停本 profile 的调试浏览器（不影响日常浏览器）
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\SysPersona\start_cdp_browser.ps1" -Stop

# 不想开机自启：删快捷方式（不杀当前进程）
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SysPersona-CDP.lnk"
```

`-Login` / `-Headful` 开的可见窗口直接关窗口即可；无头没有窗口，用 `-Stop`。

### 边界

- 只操作 `--user-data-dir=%LOCALAPPDATA%\SysPersona\cdp-profile` 的进程：不杀、也不复用
  日常浏览器的 profile；
- 日常浏览器若自己带着 `--remote-debugging-port=9222` 在跑，抓取会连它（和以前一样）；
  本脚本不会去开、改、关它；
- 端口默认 `9222`（`-Port` 只给排障 / 自测换口用，抓取脚本仍连 9222）；
- 登录过期时抓取会抓到登录页：重跑一次 `-Login` 扫码即可；
- 不做反检测、不伪造指纹：无头抓不到就 `-Headful` 回落，如实记录。

## 七、日志怎么看（治「打开全是乱码」）

> 现象：双击 `ops\*.jsonl` / `ops\persona.log`，满屏 `姘翠綅 322K`。**文件没坏**，
> 是记事本在没有 BOM 时按 GBK(ANSI) 去解 UTF-8 的字节。

**正确入口**：双击仓库里的 `tools\ctxpack\看日志.cmd`（记忆库里同名文件），或者托盘菜单「看日志」。
它自己解对编码，并把 JSONL 排成「时间 / 动作 / 说明」三栏：

```
2026-11-10 11:05:22 fired      已注入 867 字到「# 接续包 20261110-0709 · v2（极简 — DeepSeek H」
2026-11-10 11:05:25 skip       水位 56K < 150K
```

命令行等价物：

```powershell
python "$Mem\persona_logs.py" 重载 --tail 30
python ...\persona_logs.py 后台 --tail 80
python ...\persona_logs.py --scan        # 44 个日志的真实编码一次列清
python ...\persona_logs.py --fix         # 非 UTF-8 的就地转好（先存 .bak）
```

**两层修法**（都做了）：

| 层 | 做什么 | 为什么 |
|---|---|---|
| 文件 | `persona.log` / `keeper.log` / `keepalive.log` 补 **UTF-8 BOM**；写入端新建文件时先写 BOM | 记事本认 BOM 就不猜了；不补的话每次重建又回到乱码 |
| 工具 | `persona_logs.py` 自己按 BOM→UTF-8→UTF-16→GBK 顺序试解 | 不指望任何编辑器猜对，也不改机器读的 `.jsonl`（解析器对 BOM 敏感） |

**边界**：`.jsonl` 保持 UTF-8 **无 BOM**（`json.loads` 对首行 BOM 会报错，机器读的这一层不动）；
`--fix` 只动文本、只替换坏字节、只备份一次 `.bak`，不删任何日志。

