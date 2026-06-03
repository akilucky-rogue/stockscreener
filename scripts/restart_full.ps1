# restart_full.ps1
# ==================================================================
# Full restart of the QSDE stack with the new trade-level + per-horizon
# embargo + executable-intraday changes. Runs end-to-end:
#
#   1.  Stop any running backend/frontend processes
#   2.  Bring up TimescaleDB + Redis containers
#   3.  Apply any pending migrations (002_signal_trade_levels.sql etc.)
#   4.  (Re)train all three horizons with the new code
#   5.  Start the FastAPI backend (uvicorn --reload)
#   6.  Start the Next.js frontend
#   7.  Smoke-test the API
#
# Run from anywhere -- the script computes its own paths.
# ASCII-only (no Unicode) to avoid Windows codepage corruption.
# ==================================================================

$ErrorActionPreference = "Stop"

# --- Paths --------------------------------------------------------
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = Resolve-Path (Join-Path $ScriptDir "..")
$BackendDir  = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$InfraDir    = Join-Path $RepoRoot "infra"
$Venv        = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Container   = "qsde_timescaledb"

if (-not (Test-Path $Venv)) {
    Write-Host "[FATAL] venv python not found at $Venv" -ForegroundColor Red
    Write-Host "        Run: python -m venv .venv  ; .\.venv\Scripts\pip install -r backend\requirements.txt"
    exit 1
}

Write-Host ""
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host " QSDE FULL RESTART" -ForegroundColor Cyan
Write-Host " Repo: $RepoRoot"
Write-Host "==================================================================" -ForegroundColor Cyan

# --- Step 1: Kill any old processes -------------------------------
Write-Host ""
Write-Host "[1/7] Stopping any existing backend/frontend processes..." -ForegroundColor Yellow
Get-Process -Name "python","node" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*qsde*" -or $_.Path -like "*stockscreener*" } |
    ForEach-Object {
        Write-Host "      killing PID $($_.Id) ($($_.ProcessName))"
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
# Also free ports 8000 / 3000 by killing anything bound there.
foreach ($port in 8000, 3000) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        try {
            Write-Host "      freeing port $port (PID $($c.OwningProcess))"
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        } catch {}
    }
}

# --- Step 2: Bring up Docker containers ---------------------------
Write-Host ""
Write-Host "[2/7] Starting Docker containers (postgres + redis)..." -ForegroundColor Yellow
Push-Location $RepoRoot
try {
    $ErrorActionPreference = "Continue"
    docker compose up -d 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FATAL] docker compose up failed (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    $ErrorActionPreference = "Stop"
} finally {
    Pop-Location
}

# Wait for postgres to accept connections.
Write-Host "      Waiting for postgres..."
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    $ErrorActionPreference = "Continue"
    docker exec $Container pg_isready -U qsde 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    $ErrorActionPreference = "Stop"
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Host "[FATAL] postgres did not become ready in 30s" -ForegroundColor Red
    exit 1
}
Write-Host "      Postgres is ready."

# --- Step 3: Apply any pending migrations -------------------------
Write-Host ""
Write-Host "[3/7] Applying migrations..." -ForegroundColor Yellow
$migrationDir = Join-Path $InfraDir "migrations"
if (Test-Path $migrationDir) {
    Get-ChildItem $migrationDir -Filter "*.sql" | Sort-Object Name | ForEach-Object {
        Write-Host "      $($_.Name)"
        $remotePath = "/tmp/$($_.Name)"
        $ErrorActionPreference = "Continue"
        docker cp $_.FullName "${Container}:$remotePath" 2>&1 | Out-Host
        docker exec $Container psql -U qsde -d qsde -f $remotePath 2>&1 | Out-Host
        $ErrorActionPreference = "Stop"
    }
} else {
    Write-Host "      (no migrations directory)"
}

# --- Step 4: Retrain all three horizons ---------------------------
Write-Host ""
Write-Host "[4/7] Retraining all 3 horizons (intraday/swing/long)..." -ForegroundColor Yellow
Write-Host "      This will take a few minutes."
Push-Location $BackendDir
try {
    & $Venv run_pipeline.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FATAL] run_pipeline.py failed (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

# --- Step 5: Start the FastAPI backend ----------------------------
Write-Host ""
Write-Host "[5/7] Starting FastAPI backend on http://localhost:8000..." -ForegroundColor Yellow
$backendCmd = "cd '$BackendDir'; & '$Venv' -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload"
Start-Process powershell -ArgumentList "-NoExit","-Command",$backendCmd | Out-Null
Start-Sleep -Seconds 4

# --- Step 6: Start the Next.js frontend ---------------------------
Write-Host ""
Write-Host "[6/7] Starting Next.js frontend on http://localhost:3000..." -ForegroundColor Yellow
$frontendCmd = "cd '$FrontendDir'; npm run dev"
Start-Process powershell -ArgumentList "-NoExit","-Command",$frontendCmd | Out-Null

# --- Step 7: Smoke-test the API -----------------------------------
Write-Host ""
Write-Host "[7/7] Smoke-testing API..." -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $h = Invoke-WebRequest -Uri "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 2
        if ($h.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}
if ($ready) {
    Write-Host "      /api/health OK" -ForegroundColor Green
    foreach ($h in "intraday","swing","long") {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:8000/api/signals?horizon=$h&limit=3" -UseBasicParsing
            $count = ((($r.Content | ConvertFrom-Json).signals).Count)
            Write-Host ("      /api/signals?horizon={0,-9} -> {1} rows" -f $h, $count) -ForegroundColor Green
        } catch {
            Write-Host "      /api/signals?horizon=$h FAILED" -ForegroundColor Red
        }
    }
} else {
    Write-Host "      backend did not respond on /api/health within 30s" -ForegroundColor Red
}

Write-Host ""
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host " READY" -ForegroundColor Green
Write-Host ""
Write-Host "   Backend:  http://localhost:8000/api/health"
Write-Host "   Frontend: http://localhost:3000"
Write-Host ""
Write-Host "   Useful checks:"
Write-Host "     - Dashboard:  http://localhost:3000/"
Write-Host "     - Analyze:    http://localhost:3000/analyze"
Write-Host "     - Signals:    http://localhost:3000/signals"
Write-Host "     - Factors:    http://localhost:3000/factors"
Write-Host "     - Backtest:   http://localhost:3000/backtest"
Write-Host ""
Write-Host "   To stop everything:"
Write-Host "     .\scripts\stop.ps1"
Write-Host "==================================================================" -ForegroundColor Cyan
