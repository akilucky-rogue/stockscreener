<#
.SYNOPSIS
    QSDE end-to-end smoke tests.

.DESCRIPTION
    Hits every critical endpoint against a running stack. Prints a PASS/FAIL
    table at the end. Exits with non-zero if any test fails.

    Run after .\scripts\start.ps1 has services up.
#>

$ApiBase  = "http://127.0.0.1:8000/api"
$WebBase  = "http://localhost:3000"
$TestSymbol = "HINDPETRO"   # change if not in your seeded universe

$Results = @()
function Add-Result($name, $passed, $detail) {
    $script:Results += [PSCustomObject]@{
        Test   = $name
        Result = if ($passed) { "PASS" } else { "FAIL" }
        Detail = $detail
    }
}

function Try-Endpoint($name, $url, [ScriptBlock]$validate) {
    try {
        $r = Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop
        $msg = & $validate $r
        if ($msg -eq $null) {
            Add-Result $name $true "200 OK"
        } else {
            Add-Result $name $false $msg
        }
    } catch {
        Add-Result $name $false $_.Exception.Message.Substring(0, [Math]::Min(80, $_.Exception.Message.Length))
    }
}

Write-Host "Running smoke tests against $ApiBase ..." -ForegroundColor Cyan
Write-Host ""

# -- Infra ----------------------------------------------------------------
Try-Endpoint "API /health" "$ApiBase/health" {
    param($r)
    if ($r.status -ne "healthy") { return "status=$($r.status)" }
    if ($r.checks.database -ne "ok") { return "database=$($r.checks.database)" }
    if ($r.checks.redis -ne "ok") { return "redis=$($r.checks.redis)" }
    return $null
}

# -- Universe -------------------------------------------------------------
Try-Endpoint "Universe count" "$ApiBase/universe" {
    param($r)
    if ($r.count -lt 100) { return "only $($r.count) symbols; expected >=100" }
    return $null
}

# -- Signals --------------------------------------------------------------
Try-Endpoint "Signals (swing)" "$ApiBase/signals?horizon=swing&limit=10" {
    param($r)
    if ($r.signals.Count -lt 1) { return "0 swing signals; run scripts\seed.ps1" }
    return $null
}

Try-Endpoint "Signals (long)" "$ApiBase/signals?horizon=long&limit=10" {
    param($r)
    if ($r.signals.Count -lt 1) { return "0 long signals" }
    return $null
}

# -- Research -------------------------------------------------------------
Try-Endpoint "Research / comps / $TestSymbol" "$ApiBase/research/comps/$TestSymbol" {
    param($r)
    if ($r.error) { return $r.error }
    if (-not $r.peers -or $r.peers.Count -lt 1) { return "0 peers found" }
    return $null
}

Try-Endpoint "Research / dcf / $TestSymbol" "$ApiBase/research/dcf/$TestSymbol" {
    param($r)
    if (-not $r.wacc) { return "no wacc in response" }
    if (-not $r.scenarios) { return "no scenarios in response" }
    return $null
}

Try-Endpoint "Research / earnings / $TestSymbol" "$ApiBase/research/earnings/$TestSymbol" {
    param($r)
    # Earnings may not be available for every symbol; only fail on hard error
    if ($r.error) { return $r.error }
    return $null
}

Try-Endpoint "Research / screen (value preset)" "$ApiBase/research/screen?preset=value&limit=10" {
    param($r)
    if (-not $r.results) { return "no results array" }
    return $null
}

Try-Endpoint "Research / sectors" "$ApiBase/research/sectors" {
    param($r)
    if (-not $r.sectors -or $r.sectors.Count -lt 1) { return "0 sectors" }
    return $null
}

# -- Factors API ----------------------------------------------------------
Try-Endpoint "Factors / importance" "$ApiBase/factors/importance?horizon=swing" {
    param($r)
    if (-not $r.features -or $r.features.Count -lt 1) { return "0 features" }
    return $null
}

Try-Endpoint "Factors / categories" "$ApiBase/factors/categories" {
    param($r)
    if ($r.total -lt 1) { return "0 factors known" }
    return $null
}

# -- Backtest API ---------------------------------------------------------
Try-Endpoint "Backtest / runs" "$ApiBase/backtest/runs?horizon=swing&limit=5" {
    param($r)
    if (-not $r.runs -or $r.runs.Count -lt 1) { return "no model runs logged" }
    return $null
}

Try-Endpoint "Backtest / latest" "$ApiBase/backtest/latest" {
    param($r)
    if (-not $r.runs -or $r.runs.Count -lt 1) { return "no latest run" }
    return $null
}

# -- Watchlist API --------------------------------------------------------
Try-Endpoint "Watchlist (GET)" "$ApiBase/watchlist" {
    param($r)
    # Empty watchlist is OK; just verify the shape is right.
    if ($null -eq $r.watchlist) { return "missing watchlist key" }
    return $null
}

# -- Analyze API (on-demand single-stock fetch) ---------------------------
# Uses a known liquid NSE symbol so the yfinance probe is fast.
# Skip if yfinance is rate-limited / slow on a given run.
try {
    $r = Invoke-RestMethod -Uri "$ApiBase/analyze/RELIANCE" -TimeoutSec 30 -ErrorAction Stop
    if (-not $r.signals) {
        Add-Result "Analyze RELIANCE" $false "missing signals key"
    } elseif (-not $r.signals.swing) {
        Add-Result "Analyze RELIANCE" $false "missing swing signal"
    } else {
        Add-Result "Analyze RELIANCE" $true "swing pred=$([math]::Round($r.signals.swing.predicted_return * 100, 2))% n_factors=$($r.n_factors)"
    }
} catch {
    Add-Result "Analyze RELIANCE" $false "unreachable or timeout (yfinance latency?)"
}

# -- Frontend reachability ------------------------------------------------
$frontendRoutes = @(
    "/", "/analyze", "/screener", "/signals", "/factors", "/backtest", "/watchlist",
    "/research", "/research/$TestSymbol"
)
foreach ($route in $frontendRoutes) {
    try {
        $null = Invoke-WebRequest -Uri "$WebBase$route" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        Add-Result "Frontend $route" $true "200 OK"
    } catch {
        Add-Result "Frontend $route" $false "unreachable"
    }
}

# -- Report ---------------------------------------------------------------
Write-Host ""
$Results | Format-Table -AutoSize Test, Result, Detail

$failed = ($Results | Where-Object { $_.Result -eq "FAIL" }).Count
$total  = $Results.Count
$passed = $total - $failed

Write-Host ""
if ($failed -eq 0) {
    Write-Host "[$passed/$total] All smoke tests passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "[$passed/$total] $failed test(s) failed." -ForegroundColor Red
    exit 1
}
