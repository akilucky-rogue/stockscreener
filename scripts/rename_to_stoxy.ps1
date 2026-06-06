# Stoxsy rename + cleanup orchestrator.
#
# ONE-SHOT script that:
#   1. Stops backend, frontend, Kite streamer
#   2. Moves StockTrack and legacy OUT of the stockscreener folder
#   3. Deletes the cruft (node_modules, .claude*, .swarm, .mcp.json, ruvector.db,
#      root package.json/lock, qsde/vendor/financial-services/, *.misaligned)
#   4. Moves loose .md spec docs into qsde/docs/legacy/
#   5. Moves backend root debug scripts into qsde/backend/scripts/_archive/
#   6. Renames C:\Users\NEW\Documents\stockscreener\qsde -> C:\Users\NEW\Documents\stockscreener\Stoxsy
#   7. Re-registers the Windows Scheduled Tasks with the new paths
#   8. Updates the local Git remote URL (you must rename the repo on GitHub
#      manually -- click Settings on https://github.com/Akilucky-rogue/StockScreener
#      and change name to Stoxsy BEFORE running step 8)
#   9. Force-pushes the current commit to the renamed remote
#
# RUN ONCE:
#     powershell -ExecutionPolicy Bypass -File scripts\rename_to_Stoxsy.ps1
#
# DRY-RUN (no destructive operations):
#     powershell -ExecutionPolicy Bypass -File scripts\rename_to_Stoxsy.ps1 -DryRun

param(
    [switch]$DryRun,
    [switch]$SkipGitPush
)

$ErrorActionPreference = "Stop"

$StockscreenerRoot = "C:\Users\NEW\Documents\stockscreener"
$QsdeRoot          = Join-Path $StockscreenerRoot "qsde"
$StoxsyRoot         = Join-Path $StockscreenerRoot "Stoxsy"
$DocumentsRoot     = "C:\Users\NEW\Documents"

function Step($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  ok " -ForegroundColor Green -NoNewline; Write-Host $m }
function Warn($m)  { Write-Host "  !! " -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Skip($m)  { Write-Host "  -- " -ForegroundColor DarkGray -NoNewline; Write-Host $m }

function Do-Move($src, $dst) {
    if (-not (Test-Path $src)) { Skip "$src (not present)"; return }
    if ($DryRun) { Warn "[DRY-RUN] would move $src -> $dst"; return }
    Move-Item -Path $src -Destination $dst -Force
    Ok "moved $src -> $dst"
}

function Do-Delete($path) {
    if (-not (Test-Path $path)) { Skip "$path (not present)"; return }
    if ($DryRun) { Warn "[DRY-RUN] would delete $path"; return }
    Remove-Item -Path $path -Recurse -Force
    Ok "deleted $path"
}

# -- 1. Stop running services ---------------------------------------------
Step "Stop running services"
$stopScript = Join-Path $QsdeRoot "scripts\stop.ps1"
if (Test-Path $stopScript) {
    if ($DryRun) { Warn "[DRY-RUN] would run scripts\stop.ps1" }
    else { & $stopScript; Ok "services stopped" }
} else {
    Warn "stop.ps1 not found, killing processes by port"
    if (-not $DryRun) {
        Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
        Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
}

# Kill any python.exe running kite_stream.py
if (-not $DryRun) {
    Get-WmiObject Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*kite_stream.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Ok "killed any running kite_stream.py"
}

# -- 2. Move StockTrack and legacy OUT of stockscreener -------------------
Step "Move StockTrack out (2.4 GB, keeps history but moves it elsewhere)"
Do-Move (Join-Path $StockscreenerRoot "StockTrack") `
        (Join-Path $DocumentsRoot "StockTrack_archive")

Step "Move legacy out (157 MB)"
Do-Move (Join-Path $StockscreenerRoot "legacy") `
        (Join-Path $DocumentsRoot "Stoxsy_legacy_archive")

# -- 3. Delete cruft ------------------------------------------------------
Step "Delete root-level cruft (node_modules, .claude*, .swarm, etc.)"
Do-Delete (Join-Path $StockscreenerRoot "node_modules")
Do-Delete (Join-Path $StockscreenerRoot "package.json")
Do-Delete (Join-Path $StockscreenerRoot "package-lock.json")
Do-Delete (Join-Path $StockscreenerRoot "ruvector.db")
Do-Delete (Join-Path $StockscreenerRoot ".claude")
Do-Delete (Join-Path $StockscreenerRoot ".claude-flow")
Do-Delete (Join-Path $StockscreenerRoot ".swarm")
Do-Delete (Join-Path $StockscreenerRoot ".mcp.json")

Step "Delete qsde/vendor/financial-services (accidental commit)"
Do-Delete (Join-Path $QsdeRoot "vendor\financial-services")
# remove the now-empty vendor folder too
Do-Delete (Join-Path $QsdeRoot "vendor")

Step "Delete *.misaligned model weight files"
Do-Delete (Join-Path $QsdeRoot "backend\qsde\models\weights\meta_long.txt.misaligned")
Do-Delete (Join-Path $QsdeRoot "backend\qsde\models\weights\meta_swing.txt.misaligned")

# -- 4. Consolidate docs ---------------------------------------------------
Step "Move loose spec docs into qsde\docs\legacy\"
$legacyDocs = Join-Path $QsdeRoot "docs\legacy"
if (-not $DryRun) { New-Item -ItemType Directory -Path $legacyDocs -Force | Out-Null }
Do-Move (Join-Path $StockscreenerRoot "India_Quant_Screener_Deployment_Options.md") `
        (Join-Path $legacyDocs "India_Quant_Screener_Deployment_Options.md")
Do-Move (Join-Path $StockscreenerRoot "India_Quant_Screener_Elite_Spec.md") `
        (Join-Path $legacyDocs "India_Quant_Screener_Elite_Spec.md")
Do-Move (Join-Path $StockscreenerRoot "India_Quant_Screener_Master_Spec.md") `
        (Join-Path $legacyDocs "India_Quant_Screener_Master_Spec.md")
Do-Move (Join-Path $StockscreenerRoot "India_Quant_Screener_Master_Spec_v1.1.md") `
        (Join-Path $legacyDocs "India_Quant_Screener_Master_Spec_v1.1.md")
Do-Move (Join-Path $StockscreenerRoot "CLAUDE.md") `
        (Join-Path $legacyDocs "CLAUDE_legacy_root.md")
# Old root docs/ directory -> qsde\docs\legacy\
$oldDocs = Join-Path $StockscreenerRoot "docs"
if (Test-Path $oldDocs) {
    Step "Merging old root docs\ into qsde\docs\legacy\"
    if (-not $DryRun) {
        Get-ChildItem -Path $oldDocs -Force | ForEach-Object {
            Move-Item -Path $_.FullName -Destination $legacyDocs -Force
        }
        Remove-Item -Path $oldDocs -Force -Recurse
        Ok "merged"
    }
}

# -- 5. Archive backend-root debug scripts --------------------------------
Step "Move backend\ root debug scripts into backend\scripts\_archive\"
$archive = Join-Path $QsdeRoot "backend\scripts\_archive"
if (-not $DryRun) { New-Item -ItemType Directory -Path $archive -Force | Out-Null }
foreach ($f in @("audit_query.py", "check_indexes.py", "create_active_index.py", "test_research_endpoints.py")) {
    Do-Move (Join-Path $QsdeRoot "backend\$f") (Join-Path $archive $f)
}

# -- 6. Rename qsde -> Stoxsy ----------------------------------------------
Step "Rename qsde -> Stoxsy"
if ($DryRun) {
    Warn "[DRY-RUN] would rename $QsdeRoot -> $StoxsyRoot"
} elseif (Test-Path $StoxsyRoot) {
    Warn "Stoxsy folder already exists; skipping rename"
} else {
    Rename-Item -Path $QsdeRoot -NewName "Stoxsy"
    Ok "renamed qsde -> Stoxsy"
}

# Everything after this references $StoxsyRoot, not $QsdeRoot.

# -- 7. Re-register Windows Scheduled Tasks with new paths ----------------
Step "Re-register Windows Scheduled Tasks"
if ($DryRun) {
    Warn "[DRY-RUN] would unregister and re-register QSDE_Daily_EOD and QSDE_Weekly_Drift"
} else {
    Get-ScheduledTask -TaskName "QSDE_Daily_EOD" -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue
    Get-ScheduledTask -TaskName "QSDE_Weekly_Drift" -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue

    $regDaily  = Join-Path $StoxsyRoot "backend\scripts\register_daily_task.ps1"
    $regWeekly = Join-Path $StoxsyRoot "backend\scripts\register_weekly_drift_task.ps1"
    if (Test-Path $regDaily) {
        & powershell -ExecutionPolicy Bypass -File $regDaily
        Ok "QSDE_Daily_EOD re-registered with new path"
    }
    if (Test-Path $regWeekly) {
        & powershell -ExecutionPolicy Bypass -File $regWeekly
        Ok "QSDE_Weekly_Drift re-registered with new path"
    }
}

# -- 8. Git remote URL + push ---------------------------------------------
if (-not $SkipGitPush) {
    Step "Git: update remote and push"
    if ($DryRun) {
        Warn "[DRY-RUN] would: git -C $StoxsyRoot remote set-url origin https://github.com/Akilucky-rogue/Stoxsy.git ; git push"
        Warn "         (you must rename the repo on GitHub from StockScreener to Stoxsy BEFORE this)"
    } else {
        Push-Location $StoxsyRoot
        try {
            git remote set-url origin "https://github.com/Akilucky-rogue/Stoxsy.git" 2>&1 | Out-Host
            git add -A 2>&1 | Out-Host
            git commit -m "rename: QSDE -> Stoxsy (display + folder)" 2>&1 | Out-Host
            git push -u origin main --force 2>&1 | Out-Host
            Ok "pushed to https://github.com/Akilucky-rogue/Stoxsy.git"
        } finally {
            Pop-Location
        }
    }
}

# -- 9. Summary -----------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Stoxsy rename complete." -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Project root: $StoxsyRoot"
Write-Host "  GitHub:       https://github.com/Akilucky-rogue/Stoxsy (you must rename the repo on GitHub manually)"
Write-Host "  StockTrack:   C:\Users\NEW\Documents\StockTrack_archive\"
Write-Host "  Legacy:       C:\Users\NEW\Documents\Stoxsy_legacy_archive\"
Write-Host ""
Write-Host "  To start:     cd $StoxsyRoot ; .\scripts\start.ps1"
Write-Host ""
