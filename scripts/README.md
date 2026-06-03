# QSDE — operations scripts

PowerShell scripts to start, seed, test, and stop the local stack. Each script is idempotent: re-running is safe and won't double-up data.

All commands are run from the `qsde/` root directory.

## First-time boot

You did the install (Docker Desktop, Python venv, Node) already. The only first-boot extra step is seeding the database.

```powershell
cd C:\Users\NEW\Documents\stockscreener\qsde
.\scripts\start.ps1 -Seed -Test
```

That single command:

1. Validates that `.venv`, `node`, and `.env` exist.
2. Starts Docker Desktop if it isn't running.
3. Brings up TimescaleDB and Redis via `docker compose up -d` and waits for both healthchecks.
4. Applies any pending migrations under `infra/migrations/` (idempotent — already-applied ones are skipped).
5. Runs `scripts/seed.ps1` to scrape the Nifty 200 universe, ingest fundamentals and 20-year OHLCV from yfinance, compute the 33 technical factors and persist them PIT-correctly, train both LightGBM models, and generate today's signals.
6. Launches the FastAPI backend in a new PowerShell window on `http://127.0.0.1:8000` and waits for `/api/health` to return `healthy`.
7. Launches the Next.js dashboard in a new cmd window on `http://localhost:3000` and waits for it to respond.
8. Runs `scripts/smoke_test.ps1` against every critical endpoint and prints a PASS/FAIL table.

Expect 5–15 minutes total on the first run — yfinance ingestion is the long pole.

## Daily startup

After the first seed, your data is persisted in Docker volumes. Just:

```powershell
.\scripts\start.ps1
```

This skips the seed step but reapplies migrations (no-op if already applied), brings up the stack, and starts both servers. Add `-Test` if you want the smoke tests too.

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
