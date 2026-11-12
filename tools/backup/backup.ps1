# backup.ps1 —— 把记忆库打成一个带日期的 zip（v6.9.0：先问"有没有变化"，再按日/周/月轮转）
# 用法：
#   powershell -ExecutionPolicy Bypass -File backup.ps1              # 有变化才打
#   ... -Force                                                       # 不管有没有变化都打一份
#   ... -NoRetention                                                 # 只打不轮转
param([int]$Keep = 8, [switch]$Force, [switch]$NoRetention)

$ErrorActionPreference = 'Continue'

# 库根：环境变量 > 本脚本所在目录（含 sessions/）> 报错退出。
# 过去这里写死过一台机器的路径，换机后必然失效——所以只认变量与实际目录，不猜。
$root = $env:SYSPERSONA_ROOT
if (-not $root -or -not (Test-Path (Join-Path $root 'sessions'))) {
    if (Test-Path (Join-Path $PSScriptRoot 'sessions')) { $root = $PSScriptRoot }
}
if (-not $root -or -not (Test-Path $root)) { Write-Host '找不到记忆库根（SYSPERSONA_ROOT 未设，脚本目录下也没有 sessions/），退出'; exit 1 }

$py = $env:PERSONA_PY
if (-not $py -or -not (Test-Path $py)) {
    $cand = Join-Path $env:SYSPERSONA_RUNTIME 'python\python.exe'
    if (Test-Path $cand) { $py = $cand }
}
$retain = Join-Path $root '脚本\persona_backup_retention.py'

# ① 去重闸：源目录指纹没变、且刚打过备份，就不再打一份全量（人 2026-11-12：「3 天快 2 个 G」）
if ($py -and (Test-Path $retain) -and -not $Force) {
    & $py $retain due | Out-Null
    if ($LASTEXITCODE -eq 1) {
        Write-Host '源目录没有变化，跳过本次备份（要强制打：加 -Force）'
        exit 0
    }
}

$dst = Join-Path $root '备份'
if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Force $dst | Out-Null }

$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$zip = Join-Path $dst ("记忆库-$stamp.zip")

$items = @()
$items += Get-ChildItem (Join-Path $root 'sessions') -Recurse -File -Include *.md,*.json,*.jsonl -ErrorAction SilentlyContinue
$items += Get-ChildItem (Join-Path $root 'archive') -Recurse -File -Include *.md,*.jsonl -ErrorAction SilentlyContinue
foreach ($f in '文档/HANDOFF.md', 'state.json', '文档/DECISIONS.md', 'README.md', '文档/SKILL.md', 'index\manifest.json') {
    $p = Join-Path $root $f
    if (Test-Path $p) { $items += Get-Item $p }
}
$items += Get-ChildItem (Join-Path $root '整理') -File -Include *.md -ErrorAction SilentlyContinue
$items += Get-ChildItem (Join-Path $root 'memory') -Recurse -File -Include *.md,*.json,*.jsonl,*.txt -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch '(?i)(secret|\.key$|\.dpapi|token)' }

if (-not $items) { Write-Host '没有可备份的文件'; exit 1 }

$tmp = Join-Path $env:TEMP ("mem-backup-" + $stamp)
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Force $tmp | Out-Null

foreach ($f in $items) {
    $rel = $f.FullName.Substring($root.Length).TrimStart('\')
    $out = Join-Path $tmp $rel
    $dir = Split-Path $out -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    Copy-Item $f.FullName $out -Force
}

Compress-Archive -Path (Join-Path $tmp '*') -DestinationPath $zip -CompressionLevel Optimal -Force
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ("已备份 → {0}  ({1} MB, {2} 个文件)" -f $zip, $size, $items.Count)

# ② 轮转：日 7 / 周 4 / 月 3，且**比 7 天新的一律不删**（v6.9.0）
if ($py -and (Test-Path $retain) -and -not $NoRetention) {
    & $py $retain apply
} else {
    # 兜底：没有轮转脚本时退回老规矩（留最近 N 份）
    $old = Get-ChildItem $dst -Filter '记忆库-*.zip' | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
    foreach ($o in $old) { Remove-Item $o.FullName -Force; Write-Host ("清理旧备份: " + $o.Name) }
}

# ③ 记账：把这次的指纹记下，下次才能判断"有没有变化"
if ($py -and (Test-Path $retain)) { & $py $retain record | Out-Null }
