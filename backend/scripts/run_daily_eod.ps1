# QSDE Daily EOD wrapper -- activates the venv python, runs the orchestrator,
# and tees all output to a timestamped log under backend\logs\.
#
# Invoked by the Windows Scheduled Task (see register_daily_task.ps1), or run
# by hand any time after close:
#     powershell -ExecutionPolicy Bypass -File scripts\run_daily_eod.ps1
#
# Extra args are forwarded to daily_eod.py, e.g.:
#     ... run_daily_eod.ps1 --skip-ohlcv

$ErrorActionPreference = "Continue"

$scriptsDir = $PSScriptRoot
$backend    = Split-Path $scriptsDir -Parent
$qsde       = Split-Path $backend -Parent
$py         = Join-Path $qsde ".venv\Scripts\python.exe"
$logsDir    = Join-Path $backend "logs"

if (-not (Test-Path $py)) {
    Write-Error "venv python not found at $py"
    exit 1
}
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}

$stamp   = Get-Date -Format "yyyy-MM-dd_HHmmss"
$logFile = Join-Path $logsDir "daily_eod_$stamp.log"

Set-Location $backend
"=== QSDE Daily EOD  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Tee-Object -FilePath $logFile
& $py "scripts\daily_eod.py" @args 2>&1 | Tee-Object -FilePath $logFile -Append
$code = $LASTEXITCODE
"=== exit code: $code ===" | Tee-Object -FilePath $logFile -Append

# Prune logs older than 30 days.
Get-ChildItem $logsDir -Filter "daily_eod_*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
