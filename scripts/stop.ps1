<#
.SYNOPSIS
    Clean shutdown of QSDE services.

.PARAMETER DownDocker
    Also bring down docker compose. Keeps volumes (your data) intact.

.PARAMETER Wipe
    docker compose down -v -- destroys ALL data. Asks for confirmation.

.EXAMPLE
    .\scripts\stop.ps1
    .\scripts\stop.ps1 -DownDocker
    .\scripts\stop.ps1 -Wipe
#>

[CmdletBinding()]
param(
    [switch]$DownDocker,
    [switch]$Wipe
)

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  ok " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Warn($msg) { Write-Host "  ?? " -ForegroundColor Yellow -NoNewline; Write-Host $msg }

function Stop-PortOwner($port, $name) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) {
        Write-Warn "$name not running on :$port"
        return
    }
    try {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction Stop
        Stop-Process -Id $proc.Id -Force
        Write-Ok "$name stopped (PID $($proc.Id), $($proc.ProcessName))"
    } catch {
        Write-Warn "Could not stop process on :$port -- $($_.Exception.Message)"
    }
}

Write-Step "Stopping backend (uvicorn) on :8000"
Stop-PortOwner 8000 "Backend"

Write-Step "Stopping frontend (next) on :3000"
Stop-PortOwner 3000 "Frontend"

# Next.js sometimes spawns a child node.exe. Kill any remaining node procs
# running from the qsde frontend dir.
$ghostNode = Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*qsde\frontend*" }
foreach ($p in $ghostNode) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Write-Ok "Killed orphan node PID $($p.ProcessId)"
    } catch { }
}

if ($Wipe) {
    Write-Host ""
    Write-Host "WARNING: -Wipe will DELETE all TimescaleDB and Redis volumes." -ForegroundColor Red
    $confirm = Read-Host "Type 'WIPE' to confirm"
    if ($confirm -ne "WIPE") {
        Write-Warn "Aborted."
        exit 1
    }
    Push-Location $ProjectRoot
    try {
        docker compose down -v
        Write-Ok "Containers + volumes destroyed"
    } finally { Pop-Location }
} elseif ($DownDocker) {
    Push-Location $ProjectRoot
    try {
        docker compose down
        Write-Ok "Containers stopped (volumes preserved)"
    } finally { Pop-Location }
} else {
    Write-Host ""
    Write-Host "Docker containers left running. Stop them with:" -ForegroundColor DarkGray
    Write-Host "  .\scripts\stop.ps1 -DownDocker      # keeps your data"
    Write-Host "  .\scripts\stop.ps1 -Wipe             # destroys data"
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
