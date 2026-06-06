# Stoxsy -- operations scripts

PowerShell scripts to bootstrap, start, seed, test, and stop the local stack. Each script is idempotent: re-running is safe and won't double-up data.

All commands are run from the `qsde/` root directory.

## First-time bootstrap (run ONCE per machine)

```powershell
cd C:\Users\NEW\Documents\stockscreener\qsde

# 1. Copy .env template and paste your real (rotated) Kite keys
copy .env.example .env
notepad .env

# 2. Bootstrap: venv, deps, Docker, migrations, frontend, Task Scheduler
.\scripts\setup.ps1
```

`setup.ps1` does:

1. Creates the Python venv (`.venv/`) if absent and installs backend deps (`pip install -e ".[dev]"`).
2. Copies `.env.example` → `.env` if `.env` is missing (you fill in the keys).
3. Starts Docker Desktop if it isn't running; waits up to 90s for daemon.
4. Brings up TimescaleDB + Redis (`docker compose up -d --wait`) and waits for both healthchecks.
5. Applies all migrations under `infra/migrations/` in order (idempotent).
6. Installs frontend deps (`npm install`) unless `-SkipFrontend`.
7. Registers two Windows Scheduled Tasks unless `-SkipScheduledTasks`:
   - `QSDE_Daily_EOD` — weekdays 15:45 IST
   - `QSDE_Weekly_Drift` — Sundays 18:00 IST
8. Prints a "next steps" summary.

Optional flags:

```powershell
.\scripts\setup.ps1 -SkipFrontend                     # CI / API-only
.\scripts\setup.ps1 -SkipScheduledTasks               # non-Windows hosts
.\scripts\setup.ps1 -DriftWebhookUrl "https://..."    # Slack/Discord drift alerts
```

## First-time seed (run ONCE after setup)

```powershell
.\scripts\seed.ps1
```

Scrapes the Nifty 200/500 universe, ingests 5+ years of daily OHLCV via Kite/yfinance, computes the 120-factor library and persists PIT-correctly to `factor_pit`, trains LightGBM models for all 3 horizons with purged CV + cost-aware target, and generates today's signals. Takes 15–30 minutes.

## Daily launch

```powershell
.\scripts\start.ps1
```

This:

1. Validates prerequisites.
2. Re-applies migrations (no-op if already applied).
3. Starts FastAPI backend in a new window on `http://127.0.0.1:8000`, waits for `/api/health`.
4. Launches the Kite live tick streamer in its own window (skipped if no active Kite token).
5. Starts Next.js frontend on `http://localhost:3000` (omit with `-NoFrontend`).
6. Runs smoke tests if `-Test`.

After daily Kite re-login, restart just the streamer:

```powershell
.\scripts\start_live_stream.ps1
.\scripts\start_live_stream.ps1 -Symbols "RELIANCE,TCS,INFY"   # subset
```

## Re-seed (rebuild signals after code change)

When you've edited the factor engine or model code and want to regenerate signals against existing OHLCV/fundamentals data, run seed alone (containers should already be up):

```powershell
.\scripts\seed.ps1
```

## Smoke tests on demand

Whenever you want to verify the stack against every critical endpoint:

```powershell
.\scripts\smoke_test.ps1
```

Exit code 0 = all pass, non-zero = something broke. Useful before a commit or after a refactor.

## Stop

```powershell
.\scripts\stop.ps1                 # stop backend + frontend, keep Docker
.\scripts\stop.ps1 -DownDocker     # also docker compose down (data preserved)
.\scripts\stop.ps1 -Wipe           # destroys volumes — requires typing WIPE
```

## Useful flags

`start.ps1` accepts:

- `-Seed` — run the full data seed before starting servers
- `-NoFrontend` — backend only, useful when iterating against the API directly via `/docs`
- `-Test` — run smoke tests after startup completes
- `-SkipMigrations` — assume migrations already applied
- `-SkipDocker` — assume containers are already running

Combine freely: `.\scripts\start.ps1 -NoFrontend -Test` brings up only the backend and runs the API portion of the smoke tests.

## Scheduled tasks (Windows Task Scheduler)

Registered by `setup.ps1`. Re-register at any time if you change schedules:

```powershell
# Daily EOD pipeline (weekdays at 15:45 IST by default)
powershell -ExecutionPolicy Bypass -File backend\scripts\register_daily_task.ps1
powershell -ExecutionPolicy Bypass -File backend\scripts\register_daily_task.ps1 -Time "16:00"

# Weekly drift report (Sundays at 18:00 IST by default)
powershell -ExecutionPolicy Bypass -File backend\scripts\register_weekly_drift_task.ps1
powershell -ExecutionPolicy Bypass -File backend\scripts\register_weekly_drift_task.ps1 -Webhook "https://hooks.slack.com/..."

# Inspect / trigger / remove
Get-ScheduledTask -TaskName "QSDE_Daily_EOD"
Start-ScheduledTask -TaskName "QSDE_Weekly_Drift"
Unregister-ScheduledTask -TaskName "QSDE_Daily_EOD" -Confirm:$false
```

Logs land in `backend/logs/daily_eod_*.log` and `backend/logs/weekly_drift_*.log`.

JSON snapshots of weekly drift reports persist to `backend/weekly_reports/drift_YYYY-MM-DD.json`.

## Run things manually when needed

```powershell
# Run the full EOD pipeline right now (independent of scheduler)
.\.venv\Scripts\python.exe backend\scripts\daily_eod.py
.\.venv\Scripts\python.exe backend\scripts\daily_eod.py --skip-ohlcv   # data already fresh

# Print the weekly drift report
.\.venv\Scripts\python.exe backend\scripts\weekly_drift.py --unicode   # nice glyphs
.\.venv\Scripts\python.exe backend\scripts\weekly_drift.py --ascii     # PowerShell-safe

# Retrain all 3 horizons on the cost-aware target
.\.venv\Scripts\python.exe backend\scripts\retrain.py

# Auto-take top model signals across all 3 horizons (idempotent)
.\.venv\Scripts\python.exe -c "from qsde.execution.auto_taker import take_top_model_signals_all_horizons; import json; print(json.dumps(take_top_model_signals_all_horizons(), indent=2, default=str))"
```

## Troubleshooting

### Docker doesn't come up

`start.ps1` waits 90 seconds for Docker Desktop. If it times out, start Docker manually and re-run with `-SkipDocker`.

### Port 8000 or 3000 already in use

The launcher detects this and kills the owning process. If for some reason that fails:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Get-NetTCPConnection -LocalPort 3000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### `npm.ps1 cannot be loaded ... running scripts is disabled`

Known PowerShell execution-policy issue with npm. `start.ps1` works around it by launching frontend via `cmd.exe` instead of `npm.ps1`. If you need to run `npm` interactively yourself:

```powershell
cmd.exe /c "cd /d C:\Users\NEW\Documents\stockscreener\qsde\frontend && npm run dev"
```

Or relax the policy permanently for your user (do this once, never again):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Migration appears to fail with "constraint already exists"

The migration script is idempotent but PostgreSQL's `ADD CONSTRAINT` statement does not have `IF NOT EXISTS` for UNIQUE constraints. The launcher catches the non-zero exit and prints a warning, but the migration has already done its work. Safe to ignore on re-runs.

### Smoke test fails on `Research / comps / HINDPETRO`

If HINDPETRO isn't in your seeded universe (or has NULL sector in the `universe` table), edit `$TestSymbol` near the top of `smoke_test.ps1` to a known-good symbol like RELIANCE or ABB, or run a sector backfill:

```powershell
docker exec -i qsde_timescaledb psql -U qsde -d qsde -c "SELECT symbol, sector FROM universe WHERE symbol='HINDPETRO';"
```

If sector is NULL, the comps engine returns "No peers found" early because it filters on sector match. This is a known data-quality issue — yfinance's `industry` field maps to our `sector` column, but it's sometimes empty.

### Backend reload doesn't pick up code changes

`uvicorn --reload` watches the backend tree but not the frontend or migrations. If you changed Python and saw stale behavior, kill and restart the backend window manually (or run `.\scripts\stop.ps1` then `.\scripts\start.ps1` again).

### Kite token expired (`No active Kite access_token in DB`)

Zerodha access tokens expire daily at ~06:00 IST. After expiry, the daily EOD step 1 skips OHLCV refresh (other steps continue on existing data) and `start_live_stream.ps1` exits with the login URL printed. Re-login:

1. Open `http://127.0.0.1:8000/api/kite/login_url` in a browser, complete the Zerodha OAuth.
2. Restart the streamer: `.\scripts\start_live_stream.ps1`.

The daily EOD scheduled task tolerates an expired token — it logs the skip and proceeds; signals stay fresh on the existing OHLCV.

### Live chart shows "only N bars"

The live streamer hasn't accumulated enough minute bars yet. Wait until ~10 bars (10 minutes after market open at 09:15 IST). The chart auto-swaps from "1M historical context" to live view once the threshold is hit.

### Weekly drift script crashes with `UnicodeEncodeError`

The Task Scheduler wrapper passes `--ascii` automatically so this can't happen in scheduled runs. If you hit it interactively in a vanilla cmd.exe, either:
- Add `--unicode` if your terminal supports UTF-8 (PowerShell usually does)
- Add `--ascii` to force plain-text glyphs
- Set `chcp 65001` once per session to switch the codepage to UTF-8

### Paper page shows `n=0` everywhere

Expected on day 1 — paper trades are open but none have closed yet. Tomorrow's EOD step 5/7 runs `reconcile_open_trades` against fresh OHLCV and the numbers populate.

### "Take (paper)" button works but I can't find my trade

The button POSTs to `/api/paper/take`, shows "✓ taken" on success. To see the trade, click **Paper** in the sidebar — open trades appear in the "Open paper trades" table.
