# Restart just the Kite live tick streamer in a fresh window.
# Useful after a daily Kite re-login when the daemon was started during
# yesterday's start.ps1 and is now running with a stale token.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start_live_stream.ps1
#
# Optionally pass a comma-separated subset of symbols:
#   ... start_live_stream.ps1 -Symbols "RELIANCE,TCS,INFY"

param(
    [string]$Symbols = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$BackendDir  = Join-Path $ProjectRoot "backend"
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "venv python not found at $VenvPython" -ForegroundColor Red
    exit 1
}

# Sanity-check token first; otherwise the streamer prints "No active Kite
# access_token in DB" and exits, which is a confusing failure mode.
try {
    $st = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/kite/status" -TimeoutSec 5
    if (-not $st.authenticated) {
        Write-Host "No active Kite token. Open this URL to re-login:" -ForegroundColor Yellow
        Write-Host "  http://127.0.0.1:8000/api/kite/login_url"
        exit 1
    }
    Write-Host "  Token OK (expires $($st.expires_at))" -ForegroundColor DarkGray
} catch {
    Write-Host "Backend not responding on :8000 — start it first via .\scripts\start.ps1 -NoFrontend" -ForegroundColor Red
    exit 1
}

$pyArgs = "scripts\kite_stream.py"
if ($Symbols -ne "") {
    $pyArgs = "$pyArgs --symbols `"$Symbols`""
}

# Use a single-line -Command string instead of a here-string. PowerShell's
# here-string close marker ("@) must be at column 1 with no leading
# whitespace AND no trailing whitespace; if either drifts (often after
# a Save-As or CRLF normalization) the whole script blows up before this
# block even runs. The single-line form is uglier but bullet-proof.
$cmd = '$Host.UI.RawUI.WindowTitle = ''QSDE Live Stream''; ' +
       "Set-Location '$BackendDir'; " +
       "& '$VenvPython' $pyArgs"

Start-Process powershell -ArgumentList @("-NoExit", "-Command", $cmd)
Write-Host "Streamer launched in new window." -ForegroundColor Green
