# Register (or update) the Windows Scheduled Task that runs the QSDE daily EOD
# refresh on weekdays after NSE close.
#
# Run ONCE:
#     powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1
#
# Defaults to 15:45 local time (assumed IST), Mon-Fri. Override:
#     ... register_daily_task.ps1 -Time "16:00"
#
# Notes:
#   * Registers under the CURRENT user; runs only while you're logged on
#     (no stored password needed). For a 24/7 box, re-create with
#     "run whether logged on or not" via Task Scheduler GUI.
#   * The task does NOT retrain models — it refreshes data + signals off the
#     already-promoted models. Retrain (run_pipeline.py) stays manual/weekly.
#   * Requires an active Kite token at run time (you log in during the day);
#     if absent, the OHLCV step is skipped and signals refresh on existing data.

param(
    [string]$Time     = "15:45",
    [string]$TaskName = "QSDE_Daily_EOD"
)

$scriptsDir = $PSScriptRoot
$wrapper    = Join-Path $scriptsDir "run_daily_eod.ps1"

if (-not (Test-Path $wrapper)) {
    Write-Error "wrapper not found at $wrapper"
    exit 1
}

$action  = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`""

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Description "QSDE post-close refresh: OHLCV, factors, signals, liquidity." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' for weekdays at $Time (local time)."
Write-Host "Inspect/trigger:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Run now once:     Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:           Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
