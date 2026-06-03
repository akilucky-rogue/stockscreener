# QSDE Weekly Drift wrapper — activates the venv python, runs the report,
# and tees all output to a timestamped log under backend\logs\.
#
# Invoked by the Windows Scheduled Task (see register_weekly_drift_task.ps1),
# or run by hand any time:
#     powershell -ExecutionPolicy Bypass -File scripts\run_weekly_drift.ps1
#
# Extra args are forwarded to weekly_drift.py, e.g.:
#     ... run_weekly_drift.ps1 --webhook "https://hooks.slack.com/..."

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
$logFile = Join-Path $logsDir "weekly_drift_$stamp.log"

Set-Location $backend

# Pass --ascii by default. PowerShell's Tee-Object mangles non-cp1252 bytes
# regardless of PYTHONUTF8/PYTHONIOENCODING (it captures the pipe through
# the console's own encoding). ASCII glyphs render identically everywhere
# and the JSON snapshot in weekly_reports/ preserves all data anyway.
# Pass --unicode explicitly if you really want emoji in the log file.
$pyArgs = @("scripts\weekly_drift.py", "--ascii") + $args

"=== QSDE Weekly Drift  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Tee-Object -FilePath $logFile
& $py @pyArgs 2>&1 | Tee-Object -FilePath $logFile -Append
$code = $LASTEXITCODE
"=== exit code: $code  (0=keep/wait  1=shrink  2=stop) ===" | Tee-Object -FilePath $logFile -Append

# Prune logs older than 90 days (weekly cadence -> keep more history than daily).
Get-ChildItem $logsDir -Filter "weekly_drift_*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-90) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
