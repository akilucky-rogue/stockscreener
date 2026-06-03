# CLAUDE.md — Context for AI-assisted development

## What this project is

**QSDE** (Quantitative Stock Decision Engine) — Multi-factor, multi-horizon,
ML-driven equity signal generation for Indian markets (NSE/BSE). Signal-only
system (no order execution). Nifty 200 universe.

## Architecture

```
yfinance / FMP / FRED / NSE Bhavcopy / NSE Bulk Deals
         │
         ▼
   qsde/ingestion/*     ← data clients, Celery Beat scheduled
         │
         ▼
   PostgreSQL+TimescaleDB  ← PIT schema (valid_from/valid_to)
         │
         ▼
   qsde/factors/*       ← 80+ factors → wide DataFrame
         │
         ├──► qsde/models/lightgbm_signal.py → Direction + Confidence
         │
         ▼
   qsde/models/regime_engine.py  (5-state HMM)
         │
         ▼
   qsde/risk/*  (Kelly sizing, portfolio constraints)
         │
         ▼
   FastAPI /api/*  +  Next.js frontend/  +  Telegram @Stoxybot
```

## Conventions

- **Python 3.11+**, PEP 8, 100-char lines, type hints everywhere.
- All NSE tickers: `RELIANCE`, `TCS`, `HDFCBANK` (no .NS suffix in DB).
- Factor columns: `snake_case`, prefix with category (`tech_rsi_14`, `fund_roe`, `flow_fii_20d`).
- All monetary values in INR (crore/lakh).
- Timezone: tz-naive IST (Asia/Kolkata implicit).
- Never commit `.env`, `data/cache/`, `logs/`, or ML artifacts.

## Critical Rules

1. **No lookahead.** All factor retrievals go through `factor_pit` table with
   `valid_from <= as_of_date AND valid_to > as_of_date`. Direct raw table
   queries are PROHIBITED in production code paths.

2. **Deflated Sharpe is the only promotion metric.** Raw Sharpe is diagnostic.
   DSR is for go/no-go decisions.

3. **Purged CV for any ML training on returns.** Use `qsde/models/purged_cv.py`
   (embargo = 5 days). Plain KFold on return series is banned.

4. **Small modules, clear docstrings.** Every file has single responsibility.
   Every function has a docstring.

5. **Transaction costs are non-optional.** Backtest applies 5-8bps large-cap,
   12-20bps mid-cap round-trip.

## Running

```bash
# Infrastructure
docker compose up -d

# Backend
cd backend && uvicorn api.main:app --reload --port 8000

# Frontend
cd frontend && npm run dev

# Tests
pytest -q
```

## Secrets

Live in `.env` (never commit). See `.env.example` for template.
