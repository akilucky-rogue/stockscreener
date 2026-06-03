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

# Sanity-check token first; otherwise the streamer prints a confusing
# No active Kite access_token error and exits.
try {
    $st = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/kite/status" -TimeoutSec 5
    if (-not $st.authenticated) {
        Write-Host "No active Kite token. Open this URL to re-login:" -ForegroundColor Yellow
        Write-Host "  http://127.0.0.1:8000/api/kite/login_url"
        exit 1
    }
    Write-Host "  Token OK (expires $($st.expires_at))" -ForegroundColor DarkGray
} catch {
    Write-Host "Backend not responding on :8000 -- start it first via .\scripts\start.ps1 -NoFrontend" -ForegroundColor Red
    exit 1
}

$pyArgs = "scripts\kite_stream.py"
if ($Symbols -ne "") {
    $pyArgs = "$pyArgs --symbols ""$Symbols"""
}

# Spawn the child in a new window. Single inline command, no backticks,
# no string concatenation, no here-strings. Just the minimum.
$titleSet = '$Host.UI.RawUI.WindowTitle = ' + "'QSDE Live Stream'"
$inline = "$titleSet; & '$VenvPython' $pyArgs"

Start-Process -FilePath powershell -WorkingDirectory $BackendDir -ArgumentList @("-NoExit", "-Command", $inline)

Write-Host "Streamer launched in new window." -ForegroundColor Green
