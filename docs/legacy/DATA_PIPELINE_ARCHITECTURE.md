# Data Pipeline Architecture — Multi-Factor Stock Signal Engine

**Version**: 1.0
**Date**: 2026-03-25
**Author**: Akshat
**Status**: Architecture Decision — Ready for Review

---

## 1. Current State

### What exists today
```
Browser (React + Recharts)
    ↓ fetch()
Finnhub API / Alpha Vantage API
    ↓ JSON
Single-component JSX
    → Parse candle data
    → Compute 14 technical indicators (pure JS)
    → Score -14 to +14 consensus
    → Compute quant stats (Sharpe, Sortino, VaR, etc.)
    → Render charts (Recharts)
```

### Current limitations
| Issue | Detail |
|-------|--------|
| Single-stock analysis | No cross-sectional universe ranking |
| Browser-only compute | 150 factors × 500 stocks = memory/CPU wall |
| No persistence | Every page load re-fetches from API |
| No ML | All rule-based scoring, no trained models |
| One data source at a time | No factor composition from multiple APIs |
| No backtesting | Forward-only analysis, no historical walk-forward |
| Rate limits hit fast | Finnhub 60/min, FMP 250/day — serial browser calls are slow |

---

## 2. Architecture Decision: Python Backend + React Frontend

### Decision
**Add a Python (FastAPI) backend** for data ingestion, factor computation, ML training, and signal generation. Keep React frontend for interactive dashboard and visualization.

### Why not stay JS-only?

| Requirement | JS/Browser | Python Backend |
|-------------|-----------|----------------|
| Fetch 5 APIs × 500 stocks daily | Slow, CORS issues, rate-limit pain | Server-side, scheduled, parallel |
| Compute 150 factors per stock | V8 is fast but no pandas/numpy | pandas + numpy = 10-100x faster for matrix ops |
| Train LightGBM/XGBoost ranking model | No mature ML libraries in browser | scikit-learn, lightgbm, xgboost native |
| Walk-forward backtest over 10 years | Memory limit (~1-2GB in browser tab) | No limit, can process on disk |
| Store historical factor matrix | IndexedDB is slow and size-limited | SQLite/PostgreSQL, unlimited |
| Schedule daily batch runs | User must have browser open | Cron job or scheduled task |
| Serve signals to mobile later | N/A | REST API serves any client |

**Verdict**: Python backend is necessary. The browser cannot handle 150 factors × 500 stocks × 10 years of history with ML training.

### What stays in the browser?
- Interactive dashboard (React + Recharts/D3)
- Real-time single-stock deep dive (keep existing Finnhub live fetch)
- User preferences, watchlist, passphrase/auth
- Signal consumption (read from API, display)

### What moves to Python?
- All data ingestion (5 APIs + NSE scraping)
- Factor computation engine (150 factors)
- ML model training and inference
- Backtesting and evaluation
- Signal generation (BUY/HOLD/SELL + scores)
- Scheduled daily batch processing

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     REACT FRONTEND                          │
│                                                             │
│  Dashboard  │  Stock Deep Dive  │  Signal Feed  │  Backtest │
│  (Recharts) │  (Live Finnhub)   │  (API poll)   │  Results  │
│                                                             │
│              fetch() → FastAPI backend                      │
└──────────────────────────┬──────────────────────────────────┘
                           │ REST API (JSON)
┌──────────────────────────▼──────────────────────────────────┐
│                    FASTAPI BACKEND                           │
│                                                             │
│  /api/signals     → Latest BUY/HOLD/SELL per stock          │
│  /api/factors     → Factor values per stock per date        │
│  /api/backtest    → Backtest results (equity curve, metrics)│
│  /api/universe    → Stock universe list + metadata          │
│  /api/health      → System status, last run, data freshness │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │   INGEST    │  │   COMPUTE    │  │     MODEL         │  │
│  │             │  │              │  │                   │  │
│  │ Finnhub     │  │ 150 factors  │  │ LightGBM Ranker   │  │
│  │ FMP         │→ │ per stock    │→ │ Score 0-100       │  │
│  │ FRED        │  │ per date     │  │ Map to 5 labels   │  │
│  │ NSE India   │  │              │  │ Confidence + Risk  │  │
│  │ Twelve Data │  │ pandas +     │  │                   │  │
│  │             │  │ numpy        │  │ Walk-forward      │  │
│  └──────┬──────┘  └──────┬───────┘  │ retrain monthly   │  │
│         │                │          └─────────┬─────────┘  │
│         ▼                ▼                    ▼             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                   SQLite / PostgreSQL                │   │
│  │                                                     │   │
│  │  raw_prices    │ daily OHLCV per stock              │   │
│  │  factors       │ 150 columns × stock × date         │   │
│  │  signals       │ latest signal per stock per horizon │   │
│  │  macro         │ macro time series (FRED, RBI)      │   │
│  │  flow          │ FII/DII, delivery %, OI data       │   │
│  │  model_runs    │ model version, params, metrics     │   │
│  │  backtest_runs │ equity curves, period metrics      │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  SCHEDULER: Daily batch at market close + 1 hour            │
│  (cron / APScheduler / Windows Task Scheduler)              │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Data Ingestion Layer

### 4.1 API Client Design

Each API gets a dedicated client module with:
- Rate limiting (token bucket per API)
- Retry with exponential backoff (max 3 retries)
- Response caching (don't re-fetch same day's data twice)
- Error classification (rate_limit vs auth_fail vs data_missing vs network)

```
src/
  ingestion/
    base_client.py       # Abstract base with rate limiting + retry
    finnhub_client.py    # Candles, recommendations, insider, earnings
    fmp_client.py        # Financials, ratios, key metrics, estimates
    fred_client.py       # Macro series (US + India)
    nse_client.py        # India VIX, FII/DII, options, delivery
    twelve_client.py     # Fallback prices + pre-computed technicals
```

### 4.2 Ingestion Schedule

| Source | What | Frequency | When | Budget |
|--------|------|-----------|------|--------|
| Finnhub | Daily candles (500 stocks) | Daily | 17:00 IST (after India close) | ~500 calls |
| Finnhub | Recommendations, insider, earnings | Weekly | Sunday 10:00 | ~1500 calls |
| FMP | Ratios, financials, key metrics | Weekly | Sunday 12:00 | ~200 calls |
| FMP | Earnings surprises, analyst estimates | Post-earnings | Event-driven | ~50 calls |
| FRED | Macro series (10 series) | Daily | 06:00 IST | 10 calls |
| NSE India | FII/DII, India VIX, delivery % | Daily | 17:30 IST | Scraping (~20 req) |
| NSE India | Options chain, futures OI | Daily | 16:00 IST | Scraping (~10 req) |
| NSE India | Bulk/block deals, market breadth | Daily | 18:00 IST | Scraping (~5 req) |

### 4.3 Rate Limit Management

| API | Limit | Strategy |
|-----|-------|----------|
| Finnhub | 60 calls/min | 1 call/sec with burst queue |
| FMP | 250 calls/day | Batch on Sunday, cache all week |
| FRED | Unlimited | No throttle needed |
| NSE India | ~5 req/sec (unofficial) | 1 req/2sec with session cookies |
| Twelve Data | 800 calls/day (8/min) | Fallback only, 1 call/8sec |

---

## 5. Storage Layer

### 5.1 MVP: SQLite

For MVP (150 stocks, daily data), SQLite is sufficient:
- Single file, zero config, portable
- 10 years × 500 stocks × 150 factors ≈ 2GB — fits easily
- Read performance is excellent for sequential scan
- Write is single-threaded but batch inserts are fast

### 5.2 Scale: PostgreSQL + TimescaleDB

When you need:
- Concurrent reads (multi-user dashboard)
- Real-time streaming inserts
- Time-series optimizations (compression, continuous aggregates)
- More than 1000 stocks

### 5.3 Schema Design

```sql
-- Raw price data (source of truth)
CREATE TABLE raw_prices (
    symbol      TEXT NOT NULL,
    date        DATE NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    source      TEXT DEFAULT 'finnhub',
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date)
);

-- Computed factor matrix (output of factor engine)
CREATE TABLE factors (
    symbol      TEXT NOT NULL,
    date        DATE NOT NULL,
    horizon     TEXT NOT NULL,        -- 'intraday', 'swing', 'long'
    -- Technical (stored as JSON or individual columns)
    rsi_14      REAL,
    macd_line   REAL,
    macd_hist   REAL,
    bb_pctb     REAL,
    atr_14      REAL,
    atr_pct     REAL,
    adx_14      REAL,
    obv_slope   REAL,
    vol_regime  REAL,
    price_sma50_pct REAL,
    -- ... (150 columns total, or use JSON blob for flexibility)
    factor_json TEXT,                 -- JSON blob for non-core factors
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date, horizon)
);

-- Fundamental data (quarterly, sparse)
CREATE TABLE fundamentals (
    symbol      TEXT NOT NULL,
    fiscal_date DATE NOT NULL,
    pe_ratio    REAL,
    pb_ratio    REAL,
    roe         REAL,
    debt_equity REAL,
    revenue_growth_yoy REAL,
    eps_growth_yoy     REAL,
    earnings_surprise  REAL,
    piotroski_score    INTEGER,
    -- ... other fundamental factors
    source      TEXT DEFAULT 'fmp',
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, fiscal_date)
);

-- Macro time series
CREATE TABLE macro (
    series_id   TEXT NOT NULL,        -- e.g., 'DGS10', 'VIXCLS', 'india_vix'
    date        DATE NOT NULL,
    value       REAL,
    source      TEXT,
    PRIMARY KEY (series_id, date)
);

-- Flow data (FII/DII, delivery, OI)
CREATE TABLE flow (
    date        DATE NOT NULL,
    metric      TEXT NOT NULL,        -- 'fii_net', 'dii_net', 'india_vix', etc.
    value       REAL,
    symbol      TEXT,                 -- NULL for market-wide, symbol for stock-specific
    source      TEXT,
    PRIMARY KEY (date, metric, symbol)
);

-- Generated signals
CREATE TABLE signals (
    symbol      TEXT NOT NULL,
    date        DATE NOT NULL,
    horizon     TEXT NOT NULL,
    score       REAL,                 -- 0-100 percentile rank
    label       TEXT,                 -- 'STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'
    confidence  REAL,                 -- 0-1
    risk_score  REAL,                 -- 0-1
    predicted_return REAL,            -- regression supplement
    top_factors TEXT,                 -- JSON: top 5 contributing factors
    model_version TEXT,
    PRIMARY KEY (symbol, date, horizon)
);

-- Model metadata
CREATE TABLE model_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    horizon     TEXT,
    model_type  TEXT,                 -- 'lgbm_ranker', 'lgbm_classifier'
    train_start DATE,
    train_end   DATE,
    test_start  DATE,
    test_end    DATE,
    ic_mean     REAL,
    ic_ir       REAL,
    sharpe      REAL,
    accuracy    REAL,
    params_json TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 5.4 Factor Storage Strategy

**Hybrid approach** for the 150 factors:
- **Core 40 factors** (P0/MVP): Individual columns in `factors` table for fast queries
- **Remaining 110 factors** (P1/P2): JSON blob in `factor_json` column for flexibility
- **Migration path**: Promote frequently-used JSON factors to dedicated columns as needed

---

## 6. Factor Computation Engine

### 6.1 Module Structure

```
src/
  factors/
    __init__.py
    base.py              # Abstract FactorComputer with common interface
    technical.py         # 40 technical factors (port from existing JS)
    fundamental.py       # 30 fundamental factors (from FMP data)
    macro.py             # 25 macro factors (from FRED + RBI + derived)
    sentiment.py         # 20 sentiment factors (Finnhub + NSE)
    flow.py              # 20 flow factors (NSE FII/DII + options)
    risk.py              # 15 risk factors (VIX, correlation, regime)
    registry.py          # Factor registry: name → compute function mapping
    pipeline.py          # Orchestrator: runs all factors for a universe
```

### 6.2 Factor Compute Interface

Every factor function follows this contract:

```python
def compute_factor(
    prices: pd.DataFrame,       # OHLCV, indexed by date
    fundamentals: pd.DataFrame, # quarterly financials (if needed)
    macro: pd.DataFrame,        # macro series (if needed)
    flow: pd.DataFrame,         # flow data (if needed)
    params: dict                # lookback, thresholds, etc.
) -> pd.Series:                 # factor values indexed by date
    """Returns a single factor time series for one stock."""
```

### 6.3 Cross-Sectional Factors

Some factors require the full universe (not just one stock):
- **52-week range percentile**: Needs only own history (stock-level)
- **Sector rotation score**: Needs sector index data (universe-level)
- **Correlation to Nifty50**: Needs index data (universe-level)
- **Percentile rank within universe**: Needs all stocks' values on same date

Cross-sectional factors run **after** all stock-level factors are computed.

### 6.4 Pipeline Flow

```
Daily Pipeline (runs at 17:30 IST):

1. INGEST
   ├── Fetch Finnhub candles for 500 stocks (parallelized, rate-limited)
   ├── Fetch NSE India VIX, FII/DII, delivery data
   ├── Fetch FRED macro updates
   └── Store all raw data in SQLite

2. COMPUTE (per stock, parallelized across stocks)
   ├── Load raw_prices for stock (last 252 trading days)
   ├── Compute 40 technical factors
   ├── Load latest fundamentals (quarterly, cached)
   ├── Merge macro factors (market-wide, shared across stocks)
   ├── Merge flow factors (FII/DII market-wide + stock-specific delivery %)
   ├── Compute 15 risk factors
   └── Write to factors table

3. RANK (cross-sectional, all stocks together)
   ├── Load today's factor matrix (500 stocks × 150 factors)
   ├── Compute cross-sectional percentile ranks
   ├── Run LightGBM ranker → score 0-100 per stock
   ├── Map scores to labels (STRONG_BUY / BUY / HOLD / SELL / STRONG_SELL)
   ├── Run regression model → predicted return per stock
   └── Write to signals table

4. SERVE
   └── FastAPI serves latest signals to React frontend
```

---

## 7. Backtest Engine

### 7.1 Walk-Forward Design

```
Training Window (expanding)         Test Window (fixed 1 year)
├──────────────────────────────────┤├─────────────────────────┤
│         Train set                ││      Out-of-sample      │
│                                  ││                         │
│  Retrain model monthly           ││  Generate signals daily │
│  using all available history     ││  Track PnL, IC, metrics │
└──────────────────────────────────┘└─────────────────────────┘

Fold 1: Train 2014-2018 → Test 2019
Fold 2: Train 2014-2019 → Test 2020
Fold 3: Train 2014-2020 → Test 2021
Fold 4: Train 2014-2021 → Test 2022
Fold 5: Train 2014-2022 → Test 2023
Fold 6: Train 2014-2023 → Test 2024
Fold 7: Train 2014-2024 → Test 2025
```

### 7.2 Backtest Metrics Output

Per fold and aggregated:
- Spearman IC (daily/weekly) + IC_IR
- Top/bottom quintile returns
- Long-short portfolio Sharpe, Sortino, Calmar
- Max drawdown, win rate, profit factor
- Turnover (monthly)
- Factor importance (SHAP values)

---

## 8. Technology Stack

| Component | Technology | Version | Reason |
|-----------|-----------|---------|--------|
| Backend framework | FastAPI | 0.110+ | Async, fast, auto-docs, type hints |
| Data manipulation | pandas | 2.2+ | Factor computation, time series |
| Numerical compute | numpy | 1.26+ | Vectorized math for indicators |
| ML: Ranking | LightGBM | 4.3+ | LambdaRank objective, fast, SHAP support |
| ML: Classification | LightGBM | 4.3+ | Multi-class for label backup |
| Feature importance | SHAP | 0.45+ | Factor explanation ("why this signal?") |
| Database | SQLite → PostgreSQL | 3.45+ | Zero-config MVP → scale later |
| HTTP client | httpx | 0.27+ | Async API calls with rate limiting |
| Scheduling | APScheduler | 3.10+ | Daily batch job orchestration |
| Testing | pytest | 8.0+ | Factor round-trip + integration tests |
| Frontend | React + Recharts | 18+ | Keep existing dashboard code |
| API client (frontend) | fetch / axios | native | Call FastAPI endpoints |

### Python Dependencies (requirements.txt)

```
fastapi>=0.110.0
uvicorn>=0.29.0
pandas>=2.2.0
numpy>=1.26.0
lightgbm>=4.3.0
shap>=0.45.0
httpx>=0.27.0
apscheduler>=3.10.0
scikit-learn>=1.4.0
pytest>=8.0.0
```

---

## 9. Project Structure (Python Backend)

```
stockengine/
├── requirements.txt
├── config.py                    # API keys, DB path, schedule times
├── main.py                      # FastAPI app entry point
├── db/
│   ├── schema.sql               # Table definitions
│   ├── connection.py            # SQLite/PostgreSQL connection manager
│   └── queries.py               # Common queries (insert, upsert, select)
├── ingestion/
│   ├── base_client.py           # Rate limiter + retry + cache
│   ├── finnhub_client.py
│   ├── fmp_client.py
│   ├── fred_client.py
│   ├── nse_client.py
│   └── twelve_client.py
├── factors/
│   ├── base.py                  # Factor interface
│   ├── technical.py             # 40 technical factors
│   ├── fundamental.py           # 30 fundamental factors
│   ├── macro.py                 # 25 macro factors
│   ├── sentiment.py             # 20 sentiment factors
│   ├── flow.py                  # 20 flow factors
│   ├── risk.py                  # 15 risk factors
│   ├── registry.py              # Factor name → function map
│   └── pipeline.py              # Daily computation orchestrator
├── models/
│   ├── ranker.py                # LightGBM LambdaRank training + inference
│   ├── classifier.py            # Label generation from ranking scores
│   ├── evaluator.py             # IC, Sharpe, drawdown, factor analysis
│   └── explainer.py             # SHAP-based factor explanations
├── backtest/
│   ├── walk_forward.py          # Walk-forward engine
│   ├── portfolio.py             # Quintile portfolio construction
│   └── metrics.py               # All trading + classification metrics
├── api/
│   ├── routes.py                # FastAPI route definitions
│   └── schemas.py               # Pydantic response models
├── scheduler/
│   └── jobs.py                  # Daily ingest + compute + rank jobs
└── tests/
    ├── test_factors.py          # Factor computation tests
    ├── test_ingestion.py        # API client tests
    ├── test_models.py           # Model training/inference tests
    └── test_integration.py      # End-to-end pipeline tests
```

---

## 10. Migration Plan from Current State

### Phase A: Setup (Week 1)
- [ ] Create `stockengine/` project with FastAPI skeleton
- [ ] Port Finnhub client from JSX to Python (reuse API key, same endpoints)
- [ ] Add FMP + FRED + NSE clients (already tested)
- [ ] Create SQLite database with schema
- [ ] Daily ingest pipeline for 50 stocks (Nifty 50)

### Phase B: Factor Port (Week 2-3)
- [ ] Port 14 existing technical indicators from JS → Python/pandas
- [ ] Add 26 new technical factors
- [ ] Add 8 P0 fundamental factors (from FMP)
- [ ] Add 4 P0 macro factors (from FRED)
- [ ] Add 5 P0 flow factors (from NSE)
- [ ] Add 5 P0 risk factors (computed)
- [ ] Total: ~40 P0 factors working

### Phase C: Model (Week 4)
- [ ] Build LightGBM ranker on historical factor matrix
- [ ] Walk-forward backtest (5 folds minimum)
- [ ] Evaluate IC, Sharpe, quintile spreads
- [ ] If metrics pass bars → proceed
- [ ] If not → iterate on feature engineering

### Phase D: API + Frontend (Week 5)
- [ ] FastAPI endpoints serving signals
- [ ] React dashboard consuming signal API
- [ ] Keep existing Finnhub live deep-dive as-is
- [ ] Add signal feed view, universe screener view

### Phase E: Production (Week 6)
- [ ] APScheduler daily batch job
- [ ] Error alerting (email or Telegram on failure)
- [ ] Model retraining schedule (monthly)
- [ ] Performance monitoring dashboard

---

## 11. Key Design Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Backend language | Python | pandas, numpy, lightgbm, scikit-learn ecosystem |
| Backend framework | FastAPI | Async, auto-docs, type-safe, production-ready |
| Database (MVP) | SQLite | Zero config, single file, sufficient for 500 stocks |
| Database (scale) | PostgreSQL + TimescaleDB | Multi-user, streaming, time-series optimization |
| Factor storage | Hybrid (columns + JSON) | Fast queries for core 40, flexibility for rest |
| Scheduling | APScheduler | Python-native, simple cron-like scheduling |
| ML framework | LightGBM | Fast training, LambdaRank for ranking, SHAP native |
| Backtest style | Walk-forward expanding window | Gold standard for time-series, no look-ahead bias |
| Frontend | Keep React + Recharts | Already built, works well for dashboards |
| Communication | REST JSON API | Simple, stateless, any client can consume |
