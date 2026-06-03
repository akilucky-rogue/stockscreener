<#
.SYNOPSIS
    First-time data seeding for QSDE.

.DESCRIPTION
    Sequence: universe scrape -> fundamentals + OHLCV ingestion ->
    factor compute (PIT writer) -> LightGBM training + signal generation.

    Idempotent; safe to re-run. Existing rows upsert, new ones insert.

    Expects Docker stack to already be up (run scripts\start.ps1 first or
    bring up containers manually).
#>

$ErrorActionPreference = "Stop"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$BackendDir  = Join-Path $ProjectRoot "backend"
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  ok " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Fail($msg) { Write-Host "  !! " -ForegroundColor Red -NoNewline; Write-Host $msg; exit 1 }

if (-not (Test-Path $VenvPython)) {
    Write-Fail "Python venv missing at $VenvPython"
}

# Sanity: db reachable
$health = & $VenvPython -c "import sys; sys.path.insert(0,r'$BackendDir'); from qsde.db import check_connection; print('OK' if check_connection() else 'FAIL')"
if ($health -notmatch "OK") {
    Write-Fail "Database not reachable. Start the stack with: .\scripts\start.ps1"
}

Push-Location $BackendDir
try {
    $env:PYTHONPATH = $BackendDir

    # -- 1. Universe + Fundamentals + OHLCV -------------------------------
    Write-Step "Step 1/3: Universe scrape, fundamentals (yfinance), OHLCV (20yr)"
    if (-not (Test-Path "run_ingestion.py")) {
        Write-Fail "run_ingestion.py not found in $BackendDir"
    }
    & $VenvPython run_ingestion.py
    if ($LASTEXITCODE -ne 0) { Write-Fail "Ingestion script failed" }
    Write-Ok "Universe + fundamentals + OHLCV ingested"

    # -- 2. Factor compute (writes to factor_pit) -------------------------
    Write-Step "Step 2/3: Computing 33 technical factors across universe (writes to factor_pit)"
    & $VenvPython -c @"
import logging, sys
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
from qsde.factors.engine import compute_factors_batch
from qsde.db import read_sql

symbols = read_sql('SELECT symbol FROM universe WHERE is_active=TRUE ORDER BY symbol')['symbol'].tolist()
if not symbols:
    print('ERROR: universe is empty; re-run run_ingestion.py first', file=sys.stderr)
    sys.exit(1)
compute_factors_batch(symbols)
"@
    if ($LASTEXITCODE -ne 0) { Write-Fail "Factor computation failed" }
    Write-Ok "Factors persisted to factor_pit"

    # -- 3. LightGBM training + signal generation -------------------------
    Write-Step "Step 3/3: Training LightGBM (swing + long horizons) and generating signals"
    if (-not (Test-Path "run_pipeline.py")) {
        Write-Fail "run_pipeline.py not found"
    }
    & $VenvPython run_pipeline.py
    if ($LASTEXITCODE -ne 0) { Write-Fail "LightGBM pipeline failed" }
    Write-Ok "Models trained, signals generated"

} finally {
    Pop-Location
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Seeding complete." -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green

# Quick counts
$counts = & $VenvPython -c @"
import sys; sys.path.insert(0, r'$BackendDir')
from qsde.db import read_sql
for tbl in ('universe', 'fundamentals', 'ohlcv', 'factor_pit', 'signals', 'bulk_deals'):
    try:
        n = read_sql(f'SELECT COUNT(*) AS n FROM {tbl}').iloc[0]['n']
        print(f'  {tbl:20s} {n:>12,}')
    except Exception as e:
        print(f'  {tbl:20s} ERROR: {e}')
"@
Write-Host ""
Write-Host "Row counts:" -ForegroundColor DarkGray
Write-Host $counts
Write-Host ""
