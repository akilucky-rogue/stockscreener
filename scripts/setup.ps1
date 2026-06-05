<#
.SYNOPSIS
    QSDE first-time bootstrap (run ONCE on a fresh clone).

.DESCRIPTION
    Bootstraps everything `scripts\start.ps1` assumes already exists:
      1. Python venv + backend deps (pip install -e ".[dev]")
      2. .env from .env.example (you then paste real, ROTATED keys)
      3. Docker stack (TimescaleDB + Redis) up + healthy
      4. ALL DB migrations applied (infra/migrations/*.sql, in order)
      5. Frontend node deps (npm install)
      6. Windows Task Scheduler jobs registered:
           - QSDE_Daily_EOD     (weekdays 15:45 IST)
           - QSDE_Weekly_Drift  (Sundays  18:00 IST)
    After this, use scripts\start.ps1 to launch and scripts\seed.ps1 (or
    the runbook) to ingest real data + train.

.EXAMPLE
    cd qsde
    .\scripts\setup.ps1

.EXAMPLE
    # Skip frontend npm install (CI / API-only setups):
    .\scripts\setup.ps1 -SkipFrontend

.EXAMPLE
    # Skip Task Scheduler registration (manual cron / non-Windows hosts):
    .\scripts\setup.ps1 -SkipScheduledTasks
#>

[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipScheduledTasks,
    [string]$DriftWebhookUrl = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$BackendDir  = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$MigrationsDir = Join-Path $ProjectRoot "infra\migrations"

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  ok " -ForegroundColor Green -NoNewline; Write-Host $m }
function Warn($m) { Write-Host "  -- " -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Die($m)  { Write-Host "  !! " -ForegroundColor Red -NoNewline; Write-Host $m; exit 1 }

# 1. Python venv + deps
Step "Python venv + backend deps"
if (-not (Test-Path $VenvPython)) {
    $sys = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $sys) { Die "Python 3.11+ not found on PATH." }
    & python -m venv (Join-Path $ProjectRoot ".venv")
}
Push-Location $ProjectRoot
try {
    & $VenvPython -m pip install -U pip
    & $VenvPython -m pip install -e ".[dev]"     # runtime deps + pytest/ruff/mypy
    if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
} finally { Pop-Location }
Ok "deps installed"

# 2. .env
Step ".env"
$envPath = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $ProjectRoot ".env.example") $envPath
    Warn "created .env from template - PASTE YOUR (ROTATED) KEYS into it before ingesting."
} else { Ok ".env present" }

# 3. Docker stack
Step "Docker stack (TimescaleDB + Redis)"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $dd = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) {
        Warn "Docker daemon down - starting Docker Desktop..."
        Start-Process $dd
        for ($i = 0; $i -lt 90; $i++) { Start-Sleep 2; docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
    }
    if ($LASTEXITCODE -ne 0) { Die "Docker daemon not reachable. Start Docker Desktop, then re-run." }
}
Push-Location $ProjectRoot
try { docker compose up -d --wait } finally { Pop-Location }
Ok "containers healthy"

# 4. Migrations (idempotent). psql prints NOTICEs to stderr; under EAP=Stop those
#    would abort the loop, so relax the error preference locally (like start.ps1).
Step "Applying DB migrations (001..NNN)"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
Get-ChildItem -Path $MigrationsDir -Filter "*.sql" | Sort-Object Name | ForEach-Object {
    Get-Content -Raw $_.FullName | docker exec -i qsde_timescaledb psql -U qsde -d qsde -v ON_ERROR_STOP=1 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok $_.Name } else { Warn "$($_.Name) reported notices/errors (idempotent - safe if already applied)" }
}
$ErrorActionPreference = $prevEAP

# 5. Frontend deps
if (-not $SkipFrontend) {
    Step "Frontend deps (npm install)"
    $npm = "C:\Program Files\nodejs\npm.cmd"
    if (Test-Path $npm) {
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"   # npm writes progress/warnings to stderr
        Push-Location $FrontendDir
        try { & $npm install 2>&1 | Out-Host } finally { Pop-Location }
        $ErrorActionPreference = $prevEAP
        Ok "frontend deps installed"
    } else { Warn "Node not found - skipping. Install Node LTS then run 'npm install' in frontend/." }
}

# 6. Windows Task Scheduler -- daily EOD + weekly drift report.
# Both scripts already exist in scripts/. We just register them so the
# pipeline runs without manual intervention. Skipping is fine for CI or
# non-Windows hosts; you can run the wrappers by hand.
if (-not $SkipScheduledTasks) {
    Step "Registering Windows Scheduled Tasks"
    $regDaily  = Join-Path $ScriptDir "..\backend\scripts\register_daily_task.ps1"
    $regWeekly = Join-Path $ScriptDir "..\backend\scripts\register_weekly_drift_task.ps1"

    if (Test-Path $regDaily) {
        & powershell -ExecutionPolicy Bypass -File $regDaily 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) { Ok "QSDE_Daily_EOD registered (weekdays 15:45 IST)" }
        else { Warn "Daily EOD task registration returned non-zero; check the output above" }
    } else { Warn "register_daily_task.ps1 not found; skipping daily EOD" }

    if (Test-Path $regWeekly) {
        $weeklyArgs = @("-ExecutionPolicy", "Bypass", "-File", $regWeekly)
        if ($DriftWebhookUrl -ne "") { $weeklyArgs += @("-Webhook", $DriftWebhookUrl) }
        & powershell @weeklyArgs 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) { Ok "QSDE_Weekly_Drift registered (Sundays 18:00 IST)" }
        else { Warn "Weekly drift task registration returned non-zero; check the output above" }
    } else { Warn "register_weekly_drift_task.ps1 not found; skipping weekly drift" }
} else {
    Warn "Skipping Task Scheduler registration (-SkipScheduledTasks). Run by hand later:"
    Warn "    powershell -ExecutionPolicy Bypass -File backend\scripts\register_daily_task.ps1"
    Warn "    powershell -ExecutionPolicy Bypass -File backend\scripts\register_weekly_drift_task.ps1"
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Stoxy setup complete." -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Next:"
Write-Host "    1. Paste ROTATED API keys into qsde\.env"
Write-Host "       (KITE_API_KEY, KITE_API_SECRET, KITE_REDIRECT_URL)"
Write-Host "    2. Launch:        .\scripts\start.ps1"
Write-Host "                      backend :8000 + frontend :3000 + Kite live stream"
Write-Host "    3. Kite login:    open http://127.0.0.1:8000/api/kite/login_url"
Write-Host "                      (Zerodha access tokens expire daily at ~06:00 IST)"
Write-Host "    4. First seed:    .\scripts\seed.ps1"
Write-Host "                      universe -> OHLCV -> factors -> train -> signals"
Write-Host "    5. Tests:         .\.venv\Scripts\python -m pytest backend\tests -q"
Write-Host ""
Write-Host "  Pages once running:"
Write-Host "    Dashboard         http://localhost:3000"
Write-Host "    Paper journal     http://localhost:3000/paper"
Write-Host "    Research          http://localhost:3000/research/<SYMBOL>"
Write-Host "    Backtest          http://localhost:3000/backtest"
Write-Host "    API docs          http://localhost:8000/docs"
Write-Host ""
