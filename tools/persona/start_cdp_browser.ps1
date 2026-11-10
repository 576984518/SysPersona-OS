# start_cdp_browser.ps1 — 抓取用的调试浏览器（默认无头），幂等启动 / 停止
#
# 为什么：抓取链路要连 CDP（127.0.0.1:9222）。以前每次抓取都得人手开一个带
#         --remote-debugging-port=9222 的浏览器；现在这个脚本负责「没有就拉起」，
#         并固定用独立 profile（cdp-profile），不碰日常浏览器。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File start_cdp_browser.ps1
#       默认：探测 9222，没跑就无头拉起，最多等 20 秒。
#   ... -Login
#       首次登录：开可见窗口（不无头）。登录完关掉窗口，之后按无头跑。
#   ... -Headful
#       回落：无头抓不到时显式开可见窗口（日志记「本次回落到可见窗口」）。
#   ... -Stop
#       停掉本 profile 的调试浏览器（不影响日常浏览器）。
#   ... -Port 9223
#       换端口（排障 / 自测用；默认 9222，抓取脚本仍连 9222）。
#
# 退出码：0 = 已在跑 / 已就绪 / 已停止；1 = 起不来。
# 日志：同目录 cdp-browser.log
#
[CmdletBinding()]
param(
    [switch]$Login,
    [switch]$Headful,
    [switch]$Stop,
    [int]$Port = 9222
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

$ScriptDir  = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$ProfileDir = Join-Path $ScriptDir 'cdp-profile'
$LogFile    = Join-Path $ScriptDir 'cdp-browser.log'
$StartUrl   = 'https://chat.deepseek.com'
$ProbeUrl   = 'http://127.0.0.1:{0}/json/version' -f $Port
$WaitSec    = 20

function Write-Log([string]$Text) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    try { [IO.File]::AppendAllText($LogFile, $line + "`r`n", (New-Object Text.UTF8Encoding($false))) } catch { }
}

function Test-Cdp {
    try {
        $r = Invoke-WebRequest -Uri $ProbeUrl -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -eq 200 -and ("$($r.Content)" -match 'webSocketDebuggerUrl'))
    } catch { return $false }
}

function Get-BrowserExe {
    $cands = @(
        'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
    )
    foreach ($c in $cands) { if (Test-Path -LiteralPath $c) { return $c } }
    return $null
}

function Get-ProfileProcs {
    @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe' or Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$ProfileDir*") })
}

# ---------- -Stop：只停本 profile 的浏览器 ----------
if ($Stop) {
    $killed = 0
    foreach ($proc in (Get-ProfileProcs)) {
        try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop; $killed++ } catch { }
    }
    $deadline = (Get-Date).AddSeconds(5)
    while (((Get-ProfileProcs).Count -gt 0) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 300
    }
    $left = (Get-ProfileProcs).Count
    if ($left -gt 0) {
        Write-Host "停不掉：还有 $left 个进程在用 profile：$ProfileDir"
        Write-Log "停止失败：仍有 $left 个进程（profile: $ProfileDir）"
        exit 1
    }
    if ($killed -gt 0) {
        Write-Host "已停止调试浏览器（$killed 个进程，profile: $ProfileDir）"
        Write-Log "已停止调试浏览器：$killed 个进程（profile: $ProfileDir）"
    } else {
        Write-Host "没有在跑的调试浏览器（profile: $ProfileDir）"
        Write-Log "停止：没有找到本 profile 的浏览器进程"
    }
    exit 0
}

$mode = 'headless'
if ($Login) { $mode = 'login' } elseif ($Headful) { $mode = 'headful' }

# ---------- 幂等锁：两个抓取同时来，也只拉起一个 ----------
$owned = $true
try {
    $mutex = New-Object System.Threading.Mutex($false, 'Local\SysPersona-CDP-Launch')
    try { $owned = $mutex.WaitOne(30000) }
    catch [System.Threading.AbandonedMutexException] { $owned = $true }
} catch { $owned = $true }

if (-not $owned) {
    Write-Host '另一个启动器还在跑（等了 30 秒），这次不动。'
    Write-Log '另一个启动器仍在跑，跳过本次'
    exit 1
}

# ---------- 已在跑就直接退出（幂等） ----------
if (Test-Cdp) {
    Write-Host "CDP 已在跑：$ProbeUrl"
    Write-Log "已在跑（端口 $Port，模式 $mode，未启动新实例）"
    if ($Login) { Write-Host '（要重新登录：先停掉 9222 上的浏览器——本脚本的实例用 -Stop，日常/旧窗口直接关——再跑 -Login）' }
    exit 0
}

$exe = Get-BrowserExe
if (-not $exe) {
    Write-Host 'CDP 起不来：找不到 Edge 或 Chrome（msedge.exe / chrome.exe）'
    Write-Log '失败：找不到浏览器可执行文件'
    exit 1
}

# ---------- 组装参数 ----------
$bargs = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=`"$ProfileDir`"",
    '--no-first-run',
    '--no-default-browser-check',
    '--window-size=1440,900'
)
if ($mode -eq 'headless') { $bargs += '--headless=new' }
$bargs += $StartUrl

$p = $null
try {
    $p = Start-Process -FilePath $exe -ArgumentList $bargs -PassThru
} catch {
    Write-Host "CDP 起不来：启动浏览器失败（$($_.Exception.Message)）"
    Write-Log "失败：启动浏览器失败 $($_.Exception.Message)"
    exit 1
}

# ---------- 最多等 20 秒 ----------
$ready = $false
$deadline = (Get-Date).AddSeconds($WaitSec)
while ((Get-Date) -lt $deadline) {
    if (Test-Cdp) { $ready = $true; break }
    if ($p -and $p.HasExited) { break }
    Start-Sleep -Milliseconds 400
}

if ($ready) {
    $ver = ''
    try {
        $ver = ((Invoke-WebRequest -Uri $ProbeUrl -TimeoutSec 3 -UseBasicParsing).Content | ConvertFrom-Json).Browser
    } catch { }
    switch ($mode) {
        'headless' {
            Write-Host "CDP 已就绪（无头）：$ProbeUrl"
            if ($ver) { Write-Host "浏览器：$ver" }
            Write-Log "已就绪（无头）：$ProbeUrl，浏览器 $ver，profile $ProfileDir"
        }
        'login' {
            Write-Host "CDP 已就绪（可见窗口 / -Login）：$ProbeUrl"
            Write-Host '登录后关掉这个窗口，以后走无头（不用再手动开）。'
            Write-Log "已就绪（可见窗口 / -Login）：$ProbeUrl；登录后请关闭该窗口"
        }
        default {
            Write-Host "CDP 已就绪（可见窗口 / -Headful 回落）：$ProbeUrl"
            Write-Host '本次回落到可见窗口：抓取完成后可以关掉窗口。'
            Write-Log "已就绪（可见窗口）：本次回落到可见窗口（-Headful），$ProbeUrl"
        }
    }
    exit 0
}

# ---------- 失败：说清楚为什么 ----------
if ($p -and $p.HasExited -and $p.ExitCode -eq 0) {
    $why = "浏览器退出码 0：这次启动被交给了已有实例（profile 正被另一个窗口占着），端口 $Port 没拿到 CDP。"
} elseif ($p -and $p.HasExited) {
    $why = "浏览器进程已退出（退出码 $($p.ExitCode)）：端口 $Port 可能被占用，或 profile 正被另一个窗口锁着。"
} else {
    $why = "等了 $WaitSec 秒，$ProbeUrl 仍没响应。"
}
Write-Host "CDP 起不来：$why"
Write-Host "排查：curl.exe -s --noproxy '*' $ProbeUrl ；日志：$LogFile"
Write-Host '如果无头确实抓不到（页面检测 / 不渲染），用 -Headful 回落重跑。'
Write-Log "失败：$why（模式 $mode，exe $exe，profile $ProfileDir）"
exit 1
