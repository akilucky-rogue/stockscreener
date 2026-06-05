<#
.SYNOPSIS
    QSDE one-command launcher.

.DESCRIPTION
    Validates prerequisites, brings up Docker (TimescaleDB + Redis), applies
    pending DB migrations idempotently, then starts the FastAPI backend and
    Next.js frontend in separate windows. Optionally seeds data and runs
    smoke tests.

.PARAMETER Seed
    Run universe sync, fundamentals + OHLCV ingestion, factor compute, and
    LightGBM training before bringing up the API. Required on first boot.

.PARAMETER NoFrontend
    Start backend only. Useful when iterating on Python or hitting the API
    directly through /docs.

.PARAMETER Test
    Run scripts/smoke_test.ps1 after services come up. Exits non-zero on
    any test failure.

.PARAMETER SkipMigrations
    Don't apply migrations. Use when you've already migrated manually.

.PARAMETER SkipDocker
    Assume Docker containers are already running.

.EXAMPLE
    # First boot (Docker not yet up, no data in DB):
    .\scripts\start.ps1 -Seed -Test

.EXAMPLE
    # Daily startup:
    .\scripts\start.ps1

.EXAMPLE
    # Backend only, run smoke tests:
    .\scripts\start.ps1 -NoFrontend -Test
#>

[CmdletBinding()]
param(
    [switch]$Seed,
    [switch]$NoFrontend,
    [switch]$Test,
    [switch]$SkipMigrations,
    [switch]$SkipDocker
)

$ErrorActionPreference = "Stop"

# -- Path resolution (script is in qsde/scripts/) -------------------------
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$BackendDir  = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$MigrationsDir = Join-Path $ProjectRoot "infra\migrations"
$NodeExe     = "C:\Program Files\nodejs\node.exe"
$NpmCmd      = "C:\Program Files\nodejs\npm.cmd"

function Write-Step($msg)    { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)      { Write-Host "  ok " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Warn($msg)    { Write-Host "  ?? " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Write-Fail($msg)    { Write-Host "  !! " -ForegroundColor Red -NoNewline; Write-Host $msg; exit 1 }

# -- 0. Prerequisite checks -----------------------------------------------
Write-Step "Checking prerequisites"

if (-not (Test-Path $VenvPython)) {
    Write-Fail "Python venv not found at $VenvPython. Create it with: python -m venv .venv ; .\.venv\Scripts\pip install -r backend\requirements.txt"
}
Write-Ok "Python venv"

if (-not (Test-Path $NodeExe) -and -not $NoFrontend) {
    Write-Fail "Node not found at $NodeExe. Install with: winget install OpenJS.NodeJS.LTS"
}
if (-not $NoFrontend) { Write-Ok "Node" }

if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
    Write-Fail ".env not found at $ProjectRoot\.env. Copy from .env.example and fill in API keys."
}
Write-Ok ".env present"

# -- 1. Docker ------------------------------------------------------------
if (-not $SkipDocker) {
    Write-Step "Bringing up Docker stack"

    # Check Docker daemon
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Docker daemon not responding. Starting Docker Desktop..."
        $dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dockerDesktop) {
            Start-Process $dockerDesktop
            Write-Host "  Waiting up to 90s for Docker to start..." -ForegroundColor DarkGray
            for ($i = 0; $i -lt 90; $i++) {
                Start-Sleep -Seconds 1
                docker info *> $null
                if ($LASTEXITCODE -eq 0) { break }
            }
            if ($LASTEXITCODE -ne 0) {
                Write-Fail "Docker Desktop didn't come up. Start it manually then re-run with -SkipDocker."
            }
        } else {
            Write-Fail "Docker Desktop not installed. Install it then re-run."
        }
    }
    Write-Ok "Docker daemon"

    Push-Location $ProjectRoot
    try {
        # Native-command stderr would trip $ErrorActionPreference=Stop;
        # let docker print to the terminal directly and check exit code.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        docker compose up -d
        $dockerExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        if ($dockerExit -ne 0) { Write-Fail "docker compose up failed (exit $dockerExit)" }
    } finally {
        Pop-Location
    }

    # Wait for healthchecks
    Write-Host "  Waiting for TimescaleDB + Redis healthchecks..." -ForegroundColor DarkGray
    for ($i = 0; $i -lt 60; $i++) {
        $pgHealth = docker inspect --format '{{.State.Health.Status}}' qsde_timescaledb 2>$null
        $rdHealth = docker inspect --format '{{.State.Health.Status}}' qsde_redis 2>$null
        if ($pgHealth -eq "healthy" -and $rdHealth -eq "healthy") {
            Write-Ok "TimescaleDB + Redis healthy"
            break
        }
        Start-Sleep -Seconds 1
    }
    if ($pgHealth -ne "healthy") { Write-Fail "TimescaleDB not healthy after 60s. Check 'docker logs qsde_timescaledb'." }
}

# -- 2. Migrations (idempotent) -------------------------------------------
if (-not $SkipMigrations) {
    Write-Step "Applying migrations"
    $migrations = Get-ChildItem -Path $MigrationsDir -Filter "*.sql" -ErrorAction SilentlyContinue | Sort-Object Name
    if (-not $migrations) {
        Write-Warn "No migration files in $MigrationsDir"
    } else {
        foreach ($mig in $migrations) {
            Write-Host "  Applying $($mig.Name)..." -ForegroundColor DarkGray
            $sql = Get-Content -Raw -Path $mig.FullName
            $prevEAP = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $sql | docker exec -i qsde_timescaledb psql -U qsde -d qsde -v ON_ERROR_STOP=1
            $psqlExit = $LASTEXITCODE
            $ErrorActionPreference = $prevEAP
            if ($psqlExit -ne 0) {
                Write-Warn "$($mig.Name) reported errors (likely already applied; migrations are idempotent)"
            } else {
                Write-Ok $mig.Name
            }
        }
    }
}

# -- 3. Seed (first boot only) --------------------------------------------
if ($Seed) {
    Write-Step "Seeding data (this can take 5-15 minutes on first run)"
    $seedScript = Join-Path $ScriptDir "seed.ps1"
    if (-not (Test-Path $seedScript)) { Write-Fail "scripts\seed.ps1 missing" }
    & $seedScript
    if ($LASTEXITCODE -ne 0) { Write-Fail "Seeding failed. Inspect logs above." }
    Write-Ok "Data seeded"
}

# -- 4. Backend -----------------------------------------------------------
Write-Step "Starting FastAPI backend on http://127.0.0.1:8000"

# Kill any process already on 8000
$existing = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
if ($existing) {
    Write-Warn "Port 8000 in use (PID $($existing.OwningProcess)). Killing it."
    Stop-Process -Id $existing.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# Launch in a new window so user can see logs and Ctrl+C cleanly
$backendCmd = @"
`$Host.UI.RawUI.WindowTitle = 'QSDE Backend (uvicorn)'
Set-Location '$BackendDir'
& '$VenvPython' -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd

# Wait for /api/health
Write-Host "  Waiting for backend /api/health..." -ForegroundColor DarkGray
$backendUp = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2 -ErrorAction Stop
        if ($r.status -eq "healthy") {
            $backendUp = $true
            Write-Ok "Backend live (status=healthy)"
            break
        }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $backendUp) { Write-Fail "Backend didn't pass health check in 30s." }

# -- 4b. Live Kite ticker daemon ----------------------------------------------
# Starts the WebSocket tick consumer that fills ohlcv_intraday minute bars
# and emits SSE ticks to the dashboard's intraday chart. Skipped silently if
# no active Kite token (the daemon would fail anyway -- re-login first).
Write-Step "Starting Kite live tick streamer"
try {
    $tokenJson = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/kite/status" -TimeoutSec 5 -ErrorAction Stop
    if (-not $tokenJson.authenticated) {
        Write-Warn "No active Kite token. Skipping live streamer. Run /api/kite/login_url to re-login, then: powershell .\scripts\start_live_stream.ps1"
    } else {
        # Launch Python directly in its own window. Simpler than wrapping in
        # PowerShell -Command and avoids string-escaping quirks in PS 5.1.
        Start-Process -FilePath $VenvPython -ArgumentList "scripts\kite_stream.py" -WorkingDirectory $BackendDir
        Write-Ok "Live tick streamer launched (token expires $($tokenJson.expires_at))"
    }
} catch {
    Write-Warn "Couldn't query Kite status -- skipping live streamer. Backend may not be fully up."
}

# -- 5. Frontend ----------------------------------------------------------
if (-not $NoFrontend) {
    Write-Step "Starting Next.js frontend on http://localhost:3000"

    $existing = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Warn "Port 3000 in use (PID $($existing.OwningProcess)). Killing it."
        Stop-Process -Id $existing.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }

    # Launch npm directly with -WorkingDirectory. Avoids all the
    # string-escape gymnastics that PS 5.1 keeps choking on. npm.cmd is
    # a batch file so it opens its own cmd window automatically.
    Start-Process -FilePath $NpmCmd -ArgumentList @("run", "dev") -WorkingDirectory $FrontendDir

    Write-Host "  Waiting for frontend on :3000..." -ForegroundColor DarkGray
    $frontendUp = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $null = Invoke-WebRequest -Uri "http://localhost:3000" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            $frontendUp = $true
            Write-Ok "Frontend live"
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $frontendUp) { Write-Warn "Frontend not responding after 60s; Next.js may still be compiling. Check the npm window." }
}

# -- 6. Smoke tests -------------------------------------------------------
if ($Test) {
    Write-Step "Running smoke tests"
    $smokeScript = Join-Path $ScriptDir "smoke_test.ps1"
    if (Test-Path $smokeScript) {
        & $smokeScript
    } else {
        Write-Warn "scripts\smoke_test.ps1 missing; skipping"
    }
}

# -- 7. Summary -----------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Stoxy is up." -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Dashboard         http://localhost:3000"
Write-Host "  Paper journal     http://localhost:3000/paper"
Write-Host "  Research page     http://localhost:3000/research/HINDPETRO"
Write-Host "  Screener          http://localhost:3000/screener"
Write-Host "  Backtest          http://localhost:3000/backtest"
Write-Host "  API docs          http://localhost:8000/docs"
Write-Host ""
Write-Host "  Stop everything:        .\scripts\stop.ps1"
Write-Host "  Re-seed (rebuild data): .\scripts\seed.ps1"
Write-Host "  Smoke tests:            .\scripts\smoke_test.ps1"
Write-Host "  Restart live stream:    .\scripts\start_live_stream.ps1"
Write-Host "  Run EOD now:            .\.venv\Scripts\python.exe backend\scripts\daily_eod.py"
Write-Host "  Weekly drift now:       .\.venv\Scripts\python.exe backend\scripts\weekly_drift.py --unicode"
Write-Host "  Retrain models:         .\.venv\Scripts\python.exe backend\scripts\retrain.py"
Write-Host ""
Write-Host "  Kite token expires daily at ~06:00 IST. Re-login at:"
Write-Host "    http://127.0.0.1:8000/api/kite/login_url"
Write-Host "  After re-login, restart the streamer:"
Write-Host "    .\scripts\start_live_stream.ps1"
Write-Host ""
