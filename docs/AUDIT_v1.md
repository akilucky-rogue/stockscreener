# QSDE Audit Report — v1

**Date:** 2026-05-10
**Auditor:** Claude (Cowork)
**Scope:** Full read of every file written so far in `qsde/`. Static analysis, schema cross-check, formula validation. No live container access from sandbox.

---

## TL;DR

The other tool's "Phase 0 Day 1-2 Complete" claim is roughly half true. The infrastructure layer and the dashboard shell are real and well-engineered. The factor engine and DSR module are real but each has one material correctness bug. The five research engines (~1,200 lines) are non-functional dead code due to a schema mismatch — they will throw `psycopg2.ProgrammingError` the moment any endpoint is called. The frontend has six broken nav links because the corresponding pages were claimed-but-never-written. No data has been ingested. Tests directories exist but contain zero test files.

The system is closer to "Day 1 scaffold" than "Phase 0 Day 1-2 complete."

---

## What works

### Infrastructure (solid)
- `docker-compose.yml` brings up TimescaleDB (pg16) and Redis 7 with healthchecks. Volumes persistent. Init SQL mounted correctly.
- `.env` populated with FMP, Finnhub, FRED keys, Choice Equity client ID, and the @Stoxsybot Telegram token. `chat_id` is empty (expected — user has to message the bot once to get it).
- `infra/init.sql` (245 lines) creates 12 tables. Two are TimescaleDB hypertables (`ohlcv`, `factor_pit`) with 1-year chunks. Indexes are sensible. Schema is well thought through.
- `pyproject.toml`, `.gitignore`, `CLAUDE.md` present and reasonable.

### Config + DB layer (solid)
- `qsde/config/settings.py` — clean Pydantic BaseSettings with .env loading, sensible defaults, properties for FRED series and LightGBM DART params from blueprint.
- `qsde/db/connection.py` — `get_sync_engine` (cached, pooled), `get_sync_conn` context manager (commits/rolls back correctly), `upsert_dataframe` (proper ON CONFLICT batching), and `pit_query` correctly implementing the blueprint §17.2 lookahead-free pattern.
- 16/16 modules import cleanly (verified statically).

### Factor engine (mostly solid, one count discrepancy)
- `qsde/factors/technical.py` — 33 factors actually computed (not "50" as claimed).  Math is correct: Wilder's RSI, MACD, Bollinger, ADX with EWMA smoothing, ATR, 12-1 momentum, etc. No lookahead bugs found in any rolling computation.
- `qsde/factors/engine.py` — orchestrates batch computation, has a `compute_rolling_ic` helper. **Gap:** never writes computed factors to the `factor_pit` table. There is no PIT writer. Computed factors are returned but not persisted — the whole PIT architecture is currently bypassed.

### Models (one bug)
- `qsde/models/deflated_sharpe.py` — `sharpe_ratio`, `sharpe_std_error` (Lo 2002), and `probabilistic_sharpe_ratio` are correct. The `deflated_sharpe_ratio` function has a units bug (see Bugs §1).

### Notifications
- `qsde/notifications/telegram.py` — uses python-telegram-bot, async `send_message` and a `send_message_sync` wrapper. Bot token wired. Will no-op until `TELEGRAM_CHAT_ID` is filled in.

### Dashboard (works, but isolated)
- `frontend/src/app/globals.css` — 478 lines of polished Bloomberg-aesthetic CSS. Design tokens, dark theme, neon green/amber, JetBrains Mono + Inter.
- `frontend/src/app/page.tsx` — Dashboard page with live clock, market-open indicator, health check fetch from `/api/health`. Renders correctly (per the screenshot in the user's transcript).
- `frontend/src/app/layout.tsx` — Sidebar with 7 nav items (`/`, `/screener`, `/research`, `/signals`, `/watchlist`, `/factors`, `/backtest`).

### FastAPI app (works, returns empty)
- `api/main.py` — Clean. Registers 4 routers with CORS for localhost:3000.
- `api/routes/health.py` — Honest health check (DB + Redis + api).
- `api/routes/signals.py` — Three endpoints. SQL is correct but tables are empty so always returns `{"signals": [], "count": 0}`.
- `api/routes/universe.py` — Same — correct SQL, empty tables.

---

## Bugs

### Bug 1 — Deflated Sharpe Ratio units mismatch (CRITICAL math bug)

**File:** `qsde/models/deflated_sharpe.py` lines 110-169
**Severity:** Critical — silently invalidates any DSR-based promotion gate.

The code computes `expected_max_sr` from the standard-normal Z-distribution (Euler-Mascheroni approximation), which has units of **standard deviations of the SR estimator**, not annualized Sharpe. Then it plugs that Z-score directly into PSR against an annualized observed SR.

Empirical confirmation:
- Input: observed SR ≈ 1.54 (annualized), n_trials = 100, n = 2500 daily returns
- Code returns: **DSR = 0.0000**
- Correct DSR (after rescaling expected_max_sr by SE(SR)): **DSR = 1.0000**
- The docstring explicitly claims "DSR ~0.39" — neither value matches.

**Fix:** Multiply `expected_max_sr` by `sharpe_std_error(observed_sharpe, n_obs, ...)` before passing as benchmark to PSR. Equivalently:
```python
expected_max_sr_rescaled = expected_max_z * sharpe_std_error(observed_sharpe, n_obs, skew, kurtosis)
dsr = probabilistic_sharpe_ratio(observed_sharpe, expected_max_sr_rescaled, n_obs, skew, kurtosis)
```

**Side note:** the convention chosen here (DSR-as-probability, threshold 0.95) is one valid Bailey-Lopez de Prado interpretation. The blueprint's `DSR > 1.0` threshold uses a different convention (DSR-as-deflated-Sharpe-value). Pick one and align both code and blueprint.

### Bug 2 — Research engines reference columns that do not exist (CRITICAL schema bug)

**Files:** `qsde/research/{comps,dcf,earnings,screener,sector}_engine.py`
**Severity:** Critical — every research API endpoint will throw on first call.

The engines query columns like `f.market_cap`, `f.enterprise_value`, `f.ev_to_revenue`, `f.ev_to_ebitda`, `f.roic`, `f.fcf_per_share`, `f.debt_to_equity`, `f.revenue_growth`. None of these exist in the `fundamentals` table.

The schema names are different (`debt_equity`, `ev_ebitda`, `revenue_growth_yoy`, `roe`/`roa`/`roce` not `roic`) or absent entirely (`market_cap` is on `universe` not `fundamentals`; `enterprise_value`, `ev_to_revenue`, `fcf_per_share` are nowhere).

Per-engine missing columns (verified by static cross-check):
- `comps_engine`: `debt_to_equity, enterprise_value, ev_to_ebitda, ev_to_revenue, fcf_per_share, market_cap, revenue_growth, roic`
- `screener_engine`: `debt_to_equity, fcf_per_share, market_cap, revenue_growth, roic`
- `dcf_engine`: `debt_to_equity, enterprise_value, fcf_per_share, market_cap, revenue_growth, roic`
- `sector_engine`: same 8 missing as comps
- `earnings_engine`: `fcf_per_share, revenue_growth`

**Fix options:**
- (a) Rename column references in the engines to match schema (`debt_equity`, `ev_ebitda`, `revenue_growth_yoy`).
- (b) Extend the schema to add the missing columns and update FMP ingestion to populate them (`market_cap` should probably come from `universe` already; the rest need FMP profile/ratios endpoint).
- (c) Drop these engines entirely until the data exists to back them.

### Bug 3 — `bulk_deals` ingestion cannot backfill history (CRITICAL data gap)

**File:** `qsde/ingestion/nse_bulk_deals.py` line 25
**Severity:** Critical for Week 4 kill condition validation.

`BULK_DEALS_URL = "https://archives.nseindia.com/content/equities/bulk.csv"` is a today-only endpoint. There is no historical backfill function. To compute the Week 4 kill condition (252-day rolling IC of the 20-day net institutional accumulation factor), you need ~1.5 years of bulk deal history.

Also: the `bulk_deals` table has only `id` as primary key with no UNIQUE constraint, so re-running `sync_bulk_deals_to_db()` will create duplicate rows.

**Fix:** Use NSE's date-range historical endpoints or scrape the daily CSV for each historical trading day. Add a `(symbol, date, client_name, deal_type, quantity, price)` UNIQUE constraint to prevent dupes. (The StockTrack repo's `nsepython_client.py` has working scrapers worth referencing.)

### Bug 4 — `fundamentals` PIT correctness broken

**File:** `infra/init.sql` lines 58-87
**Severity:** Major — will cause silent lookahead in any historical fundamental backtest.

The `fundamentals` table has `PRIMARY KEY (symbol, fiscal_date)`. Restated filings (the entire reason point-in-time exists for fundamentals) will overwrite the original row. The blueprint §17.2 explicitly requires `valid_from`/`valid_to` semantics for fundamentals.

**Fix:** Change PK to `(symbol, fiscal_date, filing_date)` or add `valid_from`/`valid_to` columns and route fundamental writes through the same PIT pattern as `factor_pit`.

### Bug 5 — FMP client doesn't populate margin columns

**File:** `qsde/ingestion/fmp_client.py` `fetch_fundamentals_batch`
**Severity:** Major — even if FMP returns Indian data, half the schema columns stay NULL.

The client only writes: `pe_ratio, pb_ratio, ps_ratio, ev_ebitda, roe, roa, debt_equity, dividend_yield, fcf_yield, eps, revenue`. It never writes `gross_margin, operating_margin, net_margin, roce, current_ratio, interest_coverage, revenue_growth_yoy, eps_growth_yoy, earnings_surprise, net_income, free_cash_flow, fiscal_date.filing_date`.

Combined with Bug 2, the screener/comps/sector engines have nothing to compute on even after a successful FMP run.

### Bug 6 — Factor engine never writes to `factor_pit`

**File:** `qsde/factors/engine.py`
**Severity:** Major — the entire PIT architecture is theoretical right now.

`compute_factors_for_symbol` returns a wide DataFrame; nothing inserts those values into the `factor_pit` table with `valid_from = signal_date, valid_to = infinity`. Without this writer, `pit_query` will always return empty, and the model layer can never train on PIT-correct factors.

**Fix:** Add `qsde/factors/pit_writer.py` that takes a wide factor DataFrame and writes long-format rows to `factor_pit`.

### Bug 7 — `validate_strategy` calls broken DSR with `pass_dsr: dsr > 0.95`

**File:** `qsde/models/deflated_sharpe.py` line 201
**Severity:** Operational — every strategy fails the gate due to Bug 1.

Once Bug 1 is fixed, this becomes valid (DSR-as-probability with 0.95 threshold).

### Bug 8 — Telegram `send_message_sync` race condition

**File:** `qsde/notifications/telegram.py` lines 50-63
**Severity:** Minor — works but ugly.

If called from a running event loop (e.g., a FastAPI request), it spawns a thread to run `asyncio.run`. Use `asyncio.run_coroutine_threadsafe` against a dedicated background loop, or just keep the API endpoints async.

---

## Gaps vs blueprint Phase 0 spec

Blueprint §13 Phase 0 (Weeks 1-3) deliverables:

| Deliverable | Status |
|---|---|
| Hetzner VPS, TimescaleDB, Redis, Celery | TimescaleDB + Redis ✓. Hetzner deferred (local-first, agreed). **Celery: not present.** No `qsde/scheduler/` exists. |
| Nifty 200 historical data ingested 2006-2026 | **Not ingested.** Code exists, never run. |
| First 80 factors covering technicals + fundamentals | 33 technical computed, 0 fundamental. ~41% of target. |
| LightGBM trained, signals on Next.js dashboard | **Not built.** No LightGBM training script, no PIT writer, no signal generator. The dashboard reads from an empty `signals` table. |

Additional blueprint items not present:
- No `qsde/backtest/` module — no CPCV, no walk-forward, no metrics integration with DSR
- No `qsde/risk/` module — no Kelly sizing, no CVaR portfolio constraint
- No MLflow registry stub for model hash logging (blueprint §11.3 SEBI requirement)
- No purged cross-validation implementation (the StockTrack repo has one — could be ported)

---

## Frontend gaps

The layout has 7 nav links. Only `/` is implemented. Six routes 404:
- `/screener` — backend exists (with bugs), no frontend page
- `/research` — backend exists (with bugs), no frontend page
- `/signals` — backend works (returns empty), no frontend page
- `/watchlist` — no backend, no frontend page
- `/factors` — no backend, no frontend page
- `/backtest` — no backend, no frontend page

---

## Tests

`backend/tests/{api,e2e,factors,ingestion,models}/` directories exist. **Zero test files.** No conftest, no smoke test, no PIT correctness test. The blueprint's §15.3 "point-in-time correctness is structural" cannot be enforced without at least one PIT regression test.

---

## State of data

12 tables, all empty. Specifically:
- `universe` empty → screener/comps/sector return `"No companies found"`
- `ohlcv` empty → factor engine returns `"Insufficient data"` for every symbol
- `fundamentals` empty → DCF returns `available: false`
- `factor_pit` empty → `pit_query` always returns empty
- `bulk_deals` empty → IC validation impossible
- `signals` empty → dashboard signal feed empty, top picks empty
- `macro` empty → DCF risk-free rate falls back to 7% default
- `model_runs`, `signal_audit_log`, `watchlist`, `institutional_flows`, `factor_registry` — all empty

---

## Recommended next actions, in priority order

1. **Fix Bug 1 (DSR units).** ~10 lines. Without this, every promotion gate fails.
2. **Fix Bug 2 (research engine schema).** Either rename to schema columns OR extend schema OR delete engines. The first option is fastest (~2 hours).
3. **Fix Bug 4 (fundamentals PIT key).** Single ALTER TABLE migration.
4. **Add PIT writer (Bug 6).** ~50 lines. This is the missing link between factor engine and the actual PIT store the blueprint depends on.
5. **Build the bulk deals historical backfill (Bug 3).** This is what unblocks the Week 4 kill condition validation, which the blueprint calls "the single most important action item."
6. **Run the universe sync + 20-year OHLCV backfill.** Once those land, the factor engine can actually produce something.
7. **Write tests.** At minimum: `test_pit_correctness.py` proving the PIT query rejects future-dated rows; `test_dsr.py` pinning the corrected DSR formula; `test_factors.py` with one regression value per indicator.
8. **Decide the research engines' fate.** They're a sidequest from the blueprint's Phase 0 critical path. Either commit time to fixing them properly (fix Bug 2 + extend FMP ingestion + build the frontend pages) or remove them and focus on the signal engine.

---

## Honest verdict

Architecture and intent: A.
Execution discipline: C.

The infrastructure is good. The core math (RSI, MACD, ADX, Wilder's, etc.) is right. The PIT schema is right. The Bloomberg theme is genuinely nice.

But the blueprint's principle 5 ("scope must be defended") was violated by spending ~1,200 lines on financial-services research engines that don't connect to the actual schema, while the Week 4 kill-condition validation — explicitly called out as the single most important Week 1 action — has not been started. And of the things that were claimed complete, several won't run when called.

The fix list above is small and concrete. None of these bugs are deep — they're all "this was written and never executed once" bugs. A focused day of fixing 1, 2, 3, 4, 6 plus running the universe + OHLCV ingestion would get the system to a real "Phase 0 working" state.
