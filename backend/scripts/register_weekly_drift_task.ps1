# Register (or update) the Windows Scheduled Task that runs the QSDE weekly
# drift report — every Sunday at 18:00 IST by default.
#
# Run ONCE:
#     powershell -ExecutionPolicy Bypass -File scripts\register_weekly_drift_task.ps1
#
# Defaults: Sunday 18:00 local time. Override:
#     ... register_weekly_drift_task.ps1 -Time "20:00"
#     ... register_weekly_drift_task.ps1 -Webhook "https://hooks.slack.com/..."
#
# Notes:
#   * Registers under the CURRENT user; runs only while you're logged on.
#   * Exit code from weekly_drift.py = 0/1/2 for keep/shrink/stop. Task
#     Scheduler History will surface non-zero exits as warnings — that's
#     intentional, so the OS itself nudges you when drift fires.
#   * If you pass -Webhook, the URL is stored in the task action; otherwise
#     the script reads QSDE_DRIFT_WEBHOOK_URL from the env at run time.

param(
    [string]$Time     = "18:00",
    [string]$TaskName = "QSDE_Weekly_Drift",
    [string]$Webhook  = ""
)

$scriptsDir = $PSScriptRoot
$wrapper    = Join-Path $scriptsDir "run_weekly_drift.ps1"

if (-not (Test-Path $wrapper)) {
    Write-Error "wrapper not found at $wrapper"
    exit 1
}

$argString = "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`""
if ($Webhook) {
    # Quote the webhook URL since it can contain & characters.
    $argString += " --webhook `"$Webhook`""
}

$action  = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $argString

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Sunday `
    -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Description "QSDE weekly drift report: model vs baselines + edge-vs-backtest scorecard." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' for Sundays at $Time (local time)."
if ($Webhook) {
    Write-Host "Webhook configured: $Webhook"
} else {
    Write-Host "No webhook configured (set QSDE_DRIFT_WEBHOOK_URL env var or re-run with -Webhook)."
}
Write-Host "Inspect/trigger:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Run now once:     Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:           Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
