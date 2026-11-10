# install-service.ps1 — SysPersona 常驻程序三件套：托盘自启 + 守护自愈 + 每日自检
#
# 编码：本文件含中文，必须存成 **UTF-8 带 BOM**。Windows PowerShell 5.1 对无 BOM 的
# UTF-8 会按系统 ANSI（这里是 GBK）解码，中文注释和字符串会变成乱码 —— 2026-11-10 踩过。
# 维护时用：
#   $s = [IO.File]::ReadAllText($p, [Text.UTF8Encoding]::new($false))
#   [IO.File]::WriteAllText($p, $s, (New-Object Text.UTF8Encoding($true)))
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-service.ps1 -DryRun
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-service.ps1
#
# 原则：先 Get 再判。已存在且正确 → [OK]；不存在 → 创建；存在但不对 → 修正。
# 不用 -Force 无脑覆盖（那会把别人手调过的任务也抹掉，且看不出改了什么）。
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'

# 控制台/管道按 UTF-8 输出，免得中文在捕获时变问号；不改任何系统设置。
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
} catch { }

$Repo    = 'D:\文档\SysPersona-OS'
$Mem     = 'D:\4590-PC-文件\AI会话检索记录'
$Py      = 'D:\ProgramData\SysPersona-Runtime\python\python.exe'
$Pyw     = 'D:\ProgramData\SysPersona-Runtime\python\pythonw.exe'
$Tray    = Join-Path $Repo 'tools\tray\persona_tray.py'
$DaemonPy = Join-Path $Mem 'persona_daemon.py'
$DigestPy = Join-Path $Mem 'persona_digest.py'
$Startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$Lnk     = Join-Path $Startup 'SysPersona-Tray.lnk'
$DaemonTask = 'SysPersona-Daemon'
$DigestTask = 'SysPersona-Digest'

$script:Failed = $false

function Normalize([string]$s) {
    if ($null -eq $s) { return '' }
    return (($s -replace '"', '') -replace '\s+', ' ').Trim().ToLowerInvariant()
}

function Get-ShortcutInfo([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($Path)
    return [pscustomobject]@{
        TargetPath       = $s.TargetPath
        Arguments        = $s.Arguments
        WorkingDirectory = $s.WorkingDirectory
    }
}

function Test-TrayLnk($Info) {
    if ($null -eq $Info) { return $false }
    if ((Normalize $Info.TargetPath) -ne (Normalize $Pyw)) { return $false }
    if ((Normalize $Info.Arguments) -ne (Normalize ('"' + $Tray + '"'))) { return $false }
    return $true
}

function Get-Task([string]$Name) {
    return Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
}

function Test-DaemonTask($t) {
    if ($null -eq $t) { return $false }
    if ($t.State -eq 'Disabled') { return $false }
    $a = @($t.Actions)
    if ($a.Count -ne 1) { return $false }
    if ((Normalize $a[0].Execute) -ne (Normalize $Pyw)) { return $false }
    if ((Normalize $a[0].Arguments) -notlike '*persona_daemon.py*') { return $false }
    $tr = @($t.Triggers)
    if ($tr.Count -lt 1) { return $false }
    if ((Normalize $tr[0].Repetition.Interval) -ne 'pt15m') { return $false }
    if ((Normalize $t.Settings.MultipleInstances) -ne 'ignorenew') { return $false }
    if (-not $t.Settings.StartWhenAvailable) { return $false }
    $logon = Normalize $t.Principal.LogonType
    if (@('interactive', 'interactivetoken') -notcontains $logon) { return $false }
    if ((Normalize $t.Principal.RunLevel) -ne 'limited') { return $false }
    return $true
}

function Test-DigestTask($t) {
    if ($null -eq $t) { return $false }
    if ($t.State -eq 'Disabled') { return $false }
    $a = @($t.Actions)
    if ($a.Count -ne 1) { return $false }
    if ((Normalize $a[0].Execute) -ne (Normalize $Pyw)) { return $false }
    $argText = Normalize $a[0].Arguments
    if ($argText -notlike '*persona_digest.py*') { return $false }
    if ($argText -notlike '*--once*') { return $false }
    $tr = @($t.Triggers)
    if ($tr.Count -lt 1) { return $false }
    try {
        $start = [datetime]$tr[0].StartBoundary
        if ($start.ToString('HH:mm') -ne '23:50') { return $false }
    } catch { return $false }
    if ((Normalize $tr[0].DaysInterval) -ne '1') { return $false }
    $logon = Normalize $t.Principal.LogonType
    if (@('interactive', 'interactivetoken') -notcontains $logon) { return $false }
    if ((Normalize $t.Principal.RunLevel) -ne 'limited') { return $false }
    return $true
}

function New-DaemonAction {
    return New-ScheduledTaskAction -Execute $Pyw `
        -Argument ('"' + $DaemonPy + '" --daemon') -WorkingDirectory $Mem
}

function New-DigestAction {
    return New-ScheduledTaskAction -Execute $Pyw `
        -Argument ('"' + $DigestPy + '" --once') -WorkingDirectory $Mem
}

function New-DaemonTrigger {
    return New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 15) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
}

function New-DigestTrigger {
    return New-ScheduledTaskTrigger -Daily -At '23:50'
}

function New-DaemonSettings {
    return New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
}

function New-DigestSettings {
    return New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
}

function New-UserPrincipal {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    return New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
}

function Show-Health {
    $info = Get-ShortcutInfo $Lnk
    if (Test-TrayLnk $info) {
        Write-Host ('[现状] 托盘自启：已存在，目标正确（' + $Lnk + '）')
    } elseif ($null -ne $info) {
        Write-Host ('[现状] 托盘自启：快捷方式存在但目标不对（' + $info.TargetPath + '）')
    } else {
        Write-Host ('[现状] 托盘自启：未安装（' + $Lnk + '）')
    }

    foreach ($name in @($DaemonTask, $DigestTask)) {
        $t = Get-Task $name
        if ($null -eq $t) {
            Write-Host ('[现状] ' + $name + '：未安装（State=-，NextRunTime=-）')
            continue
        }
        $i = Get-ScheduledTaskInfo -TaskName $name
        $next = if ($null -ne $i.NextRunTime) { $i.NextRunTime } else { '无' }
        Write-Host ('[现状] ' + $name + '：State=' + $t.State + '，NextRunTime=' + $next)
    }

    $n = @(Get-Process pythonw -ErrorAction SilentlyContinue).Count
    Write-Host ('[现状] daemon 进程数：' + $n + ' 个 pythonw（含托盘等，daemon 应在其中）')
}

# ── 0. 先看文件在不在：缺东西就别往下做 ─────────────────────────────
Write-Host ('[检查] 记忆库：' + $Mem)
foreach ($pair in @(
    @('Python', $Py),
    @('pythonw', $Pyw),
    @('托盘脚本', $Tray),
    @('守护脚本', $DaemonPy),
    @('自检脚本', $DigestPy)
)) {
    if (-not (Test-Path -LiteralPath $pair[1] -PathType Leaf)) {
        Write-Host ('[FAIL] 缺少' + $pair[0] + '：' + $pair[1])
        $script:Failed = $true
    }
}
if ($script:Failed) {
    Write-Host '[停止] 有文件缺失，未改动系统。'
    exit 1
}
if (-not (Test-Path -LiteralPath $Startup -PathType Container)) {
    if ($DryRun) {
        Write-Host ('[DRY] 将创建启动目录：' + $Startup)
    } else {
        New-Item -ItemType Directory -Path $Startup | Out-Null
        Write-Host ('[OK] 已创建启动目录：' + $Startup)
    }
}

# ── 1. 托盘自启：Startup\SysPersona-Tray.lnk ────────────────────────
Write-Host ''
Write-Host '── 1. 托盘自启 ──'
$trayInfo = Get-ShortcutInfo $Lnk
if (Test-TrayLnk $trayInfo) {
    Write-Host ('[OK] 已存在托盘自启：' + $Lnk)
} elseif ($DryRun) {
    if ($null -eq $trayInfo) {
        Write-Host ('[DRY] 将创建托盘自启：' + $Lnk + ' → ' + $Pyw + ' "' + $Tray + '"')
    } else {
        Write-Host ('[DRY] 将修正托盘自启（当前目标：' + $trayInfo.TargetPath + '）')
    }
} else {
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($Lnk)
    $s.TargetPath = $Pyw
    $s.Arguments = '"' + $Tray + '"'
    $s.WorkingDirectory = Split-Path -Parent $Tray
    $s.WindowStyle = 7
    $s.Description = 'SysPersona 托盘状态灯'
    $s.Save()
    if ($null -eq $trayInfo) {
        Write-Host ('[OK] 已创建托盘自启：' + $Lnk)
    } else {
        Write-Host ('[OK] 已修正托盘自启：' + $Lnk)
    }
}

# ── 2. 守护自愈：SysPersona-Daemon，15 分钟一次 ─────────────────────
Write-Host ''
Write-Host '── 2. 守护自愈 ──'
$daemonObj = Get-Task $DaemonTask
if (Test-DaemonTask $daemonObj) {
    Write-Host ('[OK] 已存在计划任务 ' + $DaemonTask + '（15 分钟一次，IgnoreNew）')
} elseif ($DryRun) {
    $verb = if ($null -eq $daemonObj) { '将创建' } else { '将更新' }
    Write-Host ('[DRY] ' + $verb + '计划任务 ' + $DaemonTask + '：每 15 分钟运行 ' + $Pyw +
        ' "' + $DaemonPy + '" --daemon；IgnoreNew / StartWhenAvailable / Limited / Interactive')
} else {
    try {
        $act = New-DaemonAction
        $trg = New-DaemonTrigger
        $set = New-DaemonSettings
        $pri = New-UserPrincipal
        if ($null -eq $daemonObj) {
            Register-ScheduledTask -TaskName $DaemonTask -Action $act -Trigger $trg `
                -Settings $set -Principal $pri -Description 'SysPersona 守护：每 15 分钟检查并拉起守护进程' | Out-Null
            Write-Host ('[OK] 已创建计划任务 ' + $DaemonTask + '（15 分钟一次）')
        } else {
            Set-ScheduledTask -TaskName $DaemonTask -Action $act -Trigger $trg `
                -Settings $set -Principal $pri | Out-Null
            $cur = Get-Task $DaemonTask
            if ($null -ne $cur -and $cur.State -eq 'Disabled') {
                Enable-ScheduledTask -TaskName $DaemonTask | Out-Null
            }
            Write-Host ('[OK] 已更新计划任务 ' + $DaemonTask + '（15 分钟一次）')
        }
    } catch {
        Write-Host ('[FAIL] ' + $DaemonTask + '：' + $_.Exception.Message)
        $script:Failed = $true
    }
}

# ── 3. 每日自检：SysPersona-Digest，每天 23:50 ──────────────────────
Write-Host ''
Write-Host '── 3. 每日自检 ──'
$digestObj = Get-Task $DigestTask
if (Test-DigestTask $digestObj) {
    Write-Host ('[OK] 已存在计划任务 ' + $DigestTask + '（每天 23:50）')
} elseif ($DryRun) {
    $verb = if ($null -eq $digestObj) { '将创建' } else { '将更新' }
    Write-Host ('[DRY] ' + $verb + '计划任务 ' + $DigestTask + '：每天 23:50 运行 ' + $Pyw +
        ' "' + $DigestPy + '" --once')
} else {
    try {
        $act = New-DigestAction
        $trg = New-DigestTrigger
        $set = New-DigestSettings
        $pri = New-UserPrincipal
        if ($null -eq $digestObj) {
            Register-ScheduledTask -TaskName $DigestTask -Action $act -Trigger $trg `
                -Settings $set -Principal $pri -Description 'SysPersona 每日自检：六项写进 ops/daily-status.md' | Out-Null
            Write-Host ('[OK] 已创建计划任务 ' + $DigestTask + '（每天 23:50）')
        } else {
            Set-ScheduledTask -TaskName $DigestTask -Action $act -Trigger $trg `
                -Settings $set -Principal $pri | Out-Null
            $cur = Get-Task $DigestTask
            if ($null -ne $cur -and $cur.State -eq 'Disabled') {
                Enable-ScheduledTask -TaskName $DigestTask | Out-Null
            }
            Write-Host ('[OK] 已更新计划任务 ' + $DigestTask + '（每天 23:50）')
        }
    } catch {
        Write-Host ('[FAIL] ' + $DigestTask + '：' + $_.Exception.Message)
        $script:Failed = $true
    }
}

# ── 4. 体检：四行现状 ───────────────────────────────────────────────
Write-Host ''
Write-Host '── 4. 体检 ──'
if ($DryRun) {
    Write-Host '[DRY] 只演练，未改动系统。'
}
Show-Health

if ($script:Failed) { exit 1 }
exit 0
