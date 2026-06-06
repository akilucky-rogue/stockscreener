# Stoxsy -- Quantitative Stock Decision Engine

*Internal codename: qsde. Folder, Python package, DB name, and Docker containers retain `qsde` for stability; user-facing name is Stoxsy.*

Multi-factor ML signal generation for Indian equities (NSE Nifty 200/500), with a live-validation loop that compares the model against three baseline strategies day by day. Real money never gets sized until paper-trade data proves the model has edge over baselines on net Sharpe.

## What it does

**Ingestion**
- Zerodha Kite Connect (REST + WebSocket) for OHLCV daily bars and live minute ticks
- Yahoo Finance fallback for symbols not on Kite
- Nifty 200/500 universe from official NSE feeds
- Macro data (VIX, DXY, yield curve), fundamentals, news sentiment, NSE bulk deals

**Factor library** (~120 PIT-correct features, all in `factor_pit` with bitemporal valid_from / valid_to)
- Technical: RSI, MACD, ATR, Bollinger %, Supertrend, ADX, OBV, CMF, VWAP, Williams %R, Donchian, Heikin-Ashi streak, etc.
- Microstructure: rolling-VWAP deviation, volume spikes, mean-reversion z-score
- Fundamental: PE/PB/ROE/D-E ratio (point-in-time joined)
- Macro: VIX 5/20-day changes, DXY trend, yield-curve slope
- Sentiment: News-headline polarity rolling mean
- Patterns: KDE support/resistance, double tops/bottoms, head & shoulders, triangles
- Volume Profile (VPVR): POC, VAH, VAL, HVN, LVN

**Models** (LightGBM DART, purged 5-fold CV, per-horizon embargo, Deflated Sharpe gate)
- Intraday (open-to-close next session)
- Swing (5-day forward return, triple-barrier labels)
- Long (20-day forward return, triple-barrier labels)
- Cost-aware target: horizon round-trip cost is subtracted from labels before training, so the model learns to predict NET returns

**Risk machinery**
- Kelly-aware position cap with auto-de-escalation on drift
- Per-trade risk cap auto-scales with realized paper-trade stats
- One-way auto: drift drops you to T0 immediately; escalation requires explicit user action
- Manual escalate/de-escalate buttons in the dashboard

**Live-validation loop**
- Paper journal records the trades the system would actually take
- Three baseline strategies (top-momentum / Nifty proxy / random) are tracked alongside the model
- Weekly drift report compares the model against all three on net Sharpe + avg net return
- Until the model beats all three baselines on 30+ paper sessions, the risk cap stays at 1% per trade
- Goal: never deploy real money on edge that isn't proven over baselines net of cost

**Dashboard** (Next.js, port 3000)
- Live Validation panel: tier, cap %, drift verdict, model vs baselines per horizon
- Paper journal: open trades, recent closures, full scorecard
- Per-symbol research with live minute-bar chart
- Backtest history with per-horizon DSR / IC / Sharpe
- Screener, watchlist, factor importance

## First-time setup

Prerequisites:
- Windows 11 / 10 (PowerShell 5.1+)
- Python 3.11+
- Node.js LTS (`winget install OpenJS.NodeJS.LTS`)
- Docker Desktop (TimescaleDB + Redis)
- Zerodha Kite Connect developer account (paid: ₹2,000/mo)

```powershell
git clone https://github.com/<your-user>/qsde.git
cd qsde

# 1. Copy .env template, then paste your real (rotated) API keys
copy .env.example .env
notepad .env
#   KITE_API_KEY=<your key>
#   KITE_API_SECRET=<your secret>
#   KITE_REDIRECT_URL=http://127.0.0.1:8000/api/kite/callback

# 2. Bootstrap everything: venv, deps, Docker, migrations, frontend, Task Scheduler
.\scripts\setup.ps1
#   ... or with a Slack webhook for weekly drift alerts:
.\scripts\setup.ps1 -DriftWebhookUrl "https://hooks.slack.com/services/..."

# 3. First-time seed: universe, OHLCV history, factors, train models, generate signals
.\scripts\seed.ps1
#   This takes 15-30 minutes on first run

# 4. Launch the stack
.\scripts\start.ps1
#   Opens FastAPI :8000, Next.js :3000, and the Kite live tick streamer
```

Open `http://127.0.0.1:8000/api/kite/login_url` once to authenticate with Zerodha. Token persists in the `kite_tokens` table and expires daily at 06:00 IST.

## Daily operation

The system is designed to run on its own.

| When | What | Trigger |
|---|---|---|
| Daily 06:00 IST | Kite token expires | Re-login via `/api/kite/login_url` |
| Weekdays 15:45 IST | EOD pipeline | Windows Task Scheduler (`QSDE_Daily_EOD`) |
| Sundays 18:00 IST | Drift report | Windows Task Scheduler (`QSDE_Weekly_Drift`) |
| 09:15 – 15:30 IST | Live tick stream | Started by `start.ps1`, persists in `ohlcv_intraday` |

EOD pipeline does, in order:
1. Refresh OHLCV from Kite (last 7 days)
2. Recompute factors and persist to `factor_pit`
3. Generate signals for all 3 horizons (with ADV liquidity gate)
4. Universe hygiene (deactivate bonds/NCDs that leaked in)
5. Reconcile open paper trades against new OHLCV
6. Auto-take top-3 model signals per horizon (long-only, ranking-percentile gated)
7. Record 11 baseline paper trades per horizon (3 strategies × 3 picks + 5 Nifty proxy + 3 random)

Weekly drift report:
- Compares realized model stats vs backtest edge band
- Compares model vs all 3 baselines on net Sharpe + avg net return
- Emits `action: keep` / `shrink` / `stop` per horizon
- Persists JSON snapshot to `backend/weekly_reports/`
- Optional Slack/Discord webhook
- Exit code 0/1/2 = keep/shrink/stop so Task Scheduler surfaces drift

## Scripts

All under `scripts/`. Idempotent — re-running is safe.

| Script | Purpose |
|---|---|
| `setup.ps1` | First-time bootstrap (run ONCE per machine) |
| `start.ps1` | Daily launcher — Docker, migrations, backend, frontend, live stream |
| `start_live_stream.ps1` | Restart just the Kite tick daemon (after daily re-login) |
| `seed.ps1` | Full data seed — universe → OHLCV → factors → train → signals |
| `smoke_test.ps1` | Health check against every critical endpoint |
| `stop.ps1` | Stop backend + frontend (Docker stays up by default) |

Backend Python scripts (`backend/scripts/`):

| Script | Purpose |
|---|---|
| `daily_eod.py` | The EOD pipeline (steps 1-7 above) |
| `weekly_drift.py` | Generate the weekly drift report |
| `retrain.py` | Retrain all 3 horizons on the cost-aware target |
| `simulate_strategies.py` | Top-K backtest to refresh `edge_stats.json` |
| `stress_test_intraday.py` | Liquidity + slippage + concentration stress |
| `kite_stream.py` | Live tick consumer (started by `start_live_stream.ps1`) |
| `register_daily_task.ps1` | Register weekday EOD task in Task Scheduler |
| `register_weekly_drift_task.ps1` | Register Sunday drift task in Task Scheduler |

## Architecture

```
NSE/Kite  -->  ingestion/  -->  TimescaleDB     -->  models/      -->  signals
                                  (PIT factors)        (LightGBM)        (ranked)
                                                                            |
                                                                            v
Live Kite WebSocket -->  ohlcv_intraday  -->  /api/intraday/stream  -->  Dashboard
                          (TimescaleDB hypertable)
                                                                            |
                                                                            v
                          paper_trades  <--  auto_taker  <--  signals  +  baselines
                              |
                              v
                          drift_report  -->  /api/paper/drift  -->  weekly Slack alert
                                                                            |
                                                                            v
                          cap_governor  <--  realized stats  <--  paper_trades reconciled
                              |
                              v
                          /api/risk/cap  -->  position size on real orders
```

## Risk philosophy

You don't deploy real money until edge is proven on a strategy that beats:
- Yesterday's top-3 momentum picks (naive baseline)
- A static 5-stock Nifty proxy (passive baseline)
- Random picks from the liquid universe (luck baseline)

Until that happens (minimum 30 paper sessions with model winning all three on net Sharpe), the risk cap stays at 1% of bankroll per trade. After confirmation, you can manually escalate one tier at a time:

| Tier | Cap | Requires |
|---|---|---|
| T0 | 1.0% | default; auto-set on drift |
| T1 | 3.0% | ≥30 paper sessions, edge confirmed on ≥1 horizon |
| T2 | 5.0% | ≥60 sessions, edge persists |
| T3 | 7.0% | ≥90 sessions, manual half-Kelly opt-in |

Drift detection: rolling-14-session hit rate >5pp below the backtest band, or rolling net Sharpe negative → auto-drop to T0 on next signal fetch.

## Environment variables

In `.env`:

```ini
# Database (auto-set by docker-compose)
DATABASE_URL=postgresql://qsde:qsde@localhost:5432/qsde
REDIS_URL=redis://localhost:6379/0

# Zerodha Kite Connect
KITE_API_KEY=<from developers.kite.trade>
KITE_API_SECRET=<from developers.kite.trade>
KITE_REDIRECT_URL=http://127.0.0.1:8000/api/kite/callback
MARKET_DATA_SOURCE=kite   # or yfinance

# Auto-taker tuning (optional)
QSDE_AUTOTAKE_K=3                       # top-K signals per horizon
QSDE_AUTOTAKE_MIN_RANK=0.90             # min cross-sectional ranking percentile
QSDE_AUTOTAKE_MIN_PRED_RET=0.0          # per-pick predicted return floor
QSDE_AUTOTAKE_SKIP_BEARISH=false        # skip days when median predicted return < 0

# Weekly drift webhook (optional)
QSDE_DRIFT_WEBHOOK_URL=https://hooks.slack.com/services/...

# Model promotion (optional, only when calibrating)
QSDE_FORCE_PROMOTE=false                # bypass DSR gate during retraining
```

## Troubleshooting

**Kite token expired** (`No active Kite access_token in DB`)
- Open `http://127.0.0.1:8000/api/kite/login_url`, authenticate
- Restart the streamer: `.\scripts\start_live_stream.ps1`
- Tokens expire daily at 06:00 IST — script the re-login if you want to avoid the manual step

**Backend won't start: port 8000 in use**
- `start.ps1` auto-kills whatever owns the port. If that fails, manually: `Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess`

**EOD runs but signals stay zero**
- Check Kite token (step above)
- Check Docker is up: `docker ps` — both `qsde_timescaledb` and `qsde_redis` should be "Up"
- Check `backend/logs/daily_eod_*.log` — most recent file shows which step failed

**Weekly drift script crashes with encoding error**
- The wrapper passes `--ascii` automatically. For interactive use, run with `--unicode`:
  `.\.venv\Scripts\python.exe backend\scripts\weekly_drift.py --unicode`

**Live chart shows "only 5 bars"**
- Live streamer hasn't accumulated enough minute bars yet. Wait until ~10 bars (10 minutes after market open). Chart auto-swaps to live view once threshold hit.

**Frontend won't load**
- Next.js 16 + Turbopack takes 30-60s to compile on first page hit
- Check the cmd window labeled "QSDE Frontend (next)" for "Ready in Xms"

## License

Private — not for redistribution. Trading at your own risk. The system is a research engine, not financial advice; it can and will lose money.

## Project layout

```
qsde/
├── .env                       # secrets (gitignored)
├── .env.example               # template
├── docker-compose.yml         # TimescaleDB + Redis
├── pyproject.toml             # backend deps
├── scripts/
│   ├── setup.ps1              # first-time bootstrap
│   ├── start.ps1              # daily launcher
│   ├── start_live_stream.ps1  # restart Kite streamer after re-login
│   ├── seed.ps1               # ingest + train + signals
│   ├── smoke_test.ps1         # end-to-end health check
│   └── stop.ps1               # stop everything
├── backend/
│   ├── api/                   # FastAPI routes
│   ├── qsde/
│   │   ├── ingestion/         # Kite, NSE, yfinance, news, macro
│   │   ├── factors/           # 120+ PIT factor library
│   │   ├── models/            # LightGBM, purged CV, DSR, triple-barrier
│   │   ├── risk/              # cap governor, trade levels, cost model
│   │   ├── execution/         # paper journal, auto-taker, drift report
│   │   └── live/              # intraday signal engine
│   └── scripts/
│       ├── daily_eod.py       # the EOD pipeline
│       ├── weekly_drift.py    # weekly drift report
│       ├── retrain.py         # retrain on cost-aware target
│       └── kite_stream.py     # live tick consumer
├── frontend/
│   └── src/app/
│       ├── page.tsx           # dashboard (Live Validation panel)
│       ├── paper/page.tsx     # paper journal
│       ├── research/[symbol]/ # per-symbol deep dive + live chart
│       └── backtest/page.tsx  # model_runs history
└── infra/
    ├── init.sql               # initial schema
    └── migrations/            # 001-010 idempotent migrations
```
