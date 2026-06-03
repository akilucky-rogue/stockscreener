-- QSDE — PostgreSQL + TimescaleDB Schema
-- Version: 1.0 (Phase 0)
-- Implements Point-in-Time (PIT) architecture from Master Blueprint §17.2

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- 1. OHLCV Price Data (TimescaleDB hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol          VARCHAR(20) NOT NULL,
    date            DATE NOT NULL,
    open            FLOAT8,
    high            FLOAT8,
    low             FLOAT8,
    close           FLOAT8,
    adj_close       FLOAT8,
    volume          BIGINT,
    delivery_pct    FLOAT8,            -- from NSE legacy bhavcopy
    source          VARCHAR(20) DEFAULT 'yfinance',
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, date)
);

SELECT create_hypertable('ohlcv', 'date',
    chunk_time_interval => INTERVAL '1 year',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- ============================================================
-- 2. Point-in-Time Factor Table (Blueprint §17.2)
-- ============================================================
-- This is the core PIT table. Every factor retrieval MUST go through
-- the pattern: valid_from <= as_of_date AND valid_to > as_of_date
-- Direct queries against raw tables are PROHIBITED in production.
CREATE TABLE IF NOT EXISTS factor_pit (
    symbol          VARCHAR(20) NOT NULL,
    as_of_date      DATE NOT NULL,
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ NOT NULL DEFAULT 'infinity',
    factor_name     VARCHAR(100) NOT NULL,
    factor_value    FLOAT8,
    data_source     VARCHAR(50),
    PRIMARY KEY (symbol, as_of_date, factor_name, valid_from)
);

SELECT create_hypertable('factor_pit', 'as_of_date',
    chunk_time_interval => INTERVAL '1 year',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- ============================================================
-- 3. Fundamentals (quarterly filings, PIT-aware)
-- ============================================================
-- PIT note: filing_date is part of the PK so restated filings (which
-- arrive with the same fiscal_date but a later filing_date) coexist
-- with the originals. Backtests must filter
--     WHERE filing_date <= signal_date
-- to avoid lookahead.
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol              VARCHAR(20) NOT NULL,
    fiscal_date         DATE NOT NULL,          -- quarter end date
    filing_date         DATE NOT NULL,          -- when it became public (PIT key)
    pe_ratio            FLOAT8,
    pb_ratio            FLOAT8,
    ps_ratio            FLOAT8,
    ev_ebitda           FLOAT8,
    ev_to_revenue       FLOAT8,
    roe                 FLOAT8,
    roce                FLOAT8,
    roa                 FLOAT8,
    roic                FLOAT8,
    gross_margin        FLOAT8,
    operating_margin    FLOAT8,
    net_margin          FLOAT8,
    debt_equity         FLOAT8,
    interest_coverage   FLOAT8,
    current_ratio       FLOAT8,
    revenue_growth_yoy  FLOAT8,
    eps_growth_yoy      FLOAT8,
    fcf_yield           FLOAT8,
    fcf_per_share       FLOAT8,
    earnings_surprise   FLOAT8,
    dividend_yield      FLOAT8,
    market_cap          FLOAT8,
    enterprise_value    FLOAT8,
    revenue             FLOAT8,
    net_income          FLOAT8,
    eps                 FLOAT8,
    free_cash_flow      FLOAT8,
    source              VARCHAR(20) DEFAULT 'fmp',
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, fiscal_date, filing_date)
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_pit_lookup
    ON fundamentals(symbol, fiscal_date, filing_date DESC);

-- ============================================================
-- 4. Macro Data (FRED + RBI)
-- ============================================================
CREATE TABLE IF NOT EXISTS macro (
    series_id       VARCHAR(50) NOT NULL,
    date            DATE NOT NULL,
    value           FLOAT8,
    source          VARCHAR(20),
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (series_id, date)
);

-- ============================================================
-- 5. Institutional Flows (FII/DII daily)
-- ============================================================
CREATE TABLE IF NOT EXISTS institutional_flows (
    date            DATE NOT NULL,
    category        VARCHAR(10) NOT NULL,   -- FII, DII
    buy_value       FLOAT8,                 -- in crores
    sell_value      FLOAT8,
    net_value       FLOAT8,
    source          VARCHAR(20) DEFAULT 'nse',
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (date, category)
);

-- ============================================================
-- 6. Bulk & Block Deals (NSE)
-- ============================================================
CREATE TABLE IF NOT EXISTS bulk_deals (
    id              SERIAL,
    symbol          VARCHAR(20) NOT NULL,
    date            DATE NOT NULL,
    client_name     TEXT,
    deal_type       VARCHAR(10),            -- BUY / SELL
    quantity        BIGINT,
    price           FLOAT8,
    source          VARCHAR(20) DEFAULT 'nse',
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (id),
    -- Natural key — re-running ingestion is a no-op rather than a dupe.
    CONSTRAINT bulk_deals_natural_key_uniq
        UNIQUE (symbol, date, client_name, deal_type, quantity, price)
);
CREATE INDEX IF NOT EXISTS idx_bulk_deals_symbol_date ON bulk_deals(symbol, date);

-- ============================================================
-- 7. Signals (Model Output)
-- ============================================================
CREATE TABLE IF NOT EXISTS signals (
    symbol              VARCHAR(20) NOT NULL,
    date                DATE NOT NULL,
    horizon             VARCHAR(20) NOT NULL,   -- intraday, swing, long
    direction           SMALLINT,               -- -1, 0, +1
    confidence          FLOAT8,
    predicted_return    FLOAT8,
    ranking_score       FLOAT8,
    factor_attribution  JSONB,                  -- SHAP values
    top_factors         JSONB,                  -- top 5 contributing factors
    model_version       VARCHAR(50),
    model_hash          VARCHAR(64),            -- SHA-256 for SEBI audit
    -- Trade plan (computed by qsde.risk.trade_levels at signal-write time).
    entry_price         FLOAT8,                 -- = latest close at signal time
    target_price        FLOAT8,                 -- max(model-implied, vol-floor) move
    stop_price          FLOAT8,                 -- ATR-based stop, horizon-scaled
    risk_reward         FLOAT8,                 -- |target-entry|/|entry-stop|
    atr_pct             FLOAT8,                 -- ATR / close (snapshot)
    trade_quality       VARCHAR(10),            -- 'good' / 'low' / NULL for HOLD
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, date, horizon)
);

-- ============================================================
-- 8. Model Run Log (MLOps audit trail)
-- ============================================================
CREATE TABLE IF NOT EXISTS model_runs (
    run_id          SERIAL PRIMARY KEY,
    horizon         VARCHAR(20),
    model_type      VARCHAR(50),
    train_start     DATE,
    train_end       DATE,
    test_start      DATE,
    test_end        DATE,
    n_features      INTEGER,
    n_samples       INTEGER,
    ic_mean         FLOAT8,
    ic_ir           FLOAT8,
    sharpe          FLOAT8,
    deflated_sharpe FLOAT8,
    psr             FLOAT8,                 -- Probabilistic Sharpe Ratio
    direction_accuracy FLOAT8,
    params_json     JSONB,
    feature_importance JSONB,
    model_hash      VARCHAR(64),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 9. Universe (Nifty 200 constituents)
-- ============================================================
CREATE TABLE IF NOT EXISTS universe (
    symbol          VARCHAR(20) NOT NULL,
    company_name    TEXT,
    isin            VARCHAR(12),
    sector          VARCHAR(100),
    industry        VARCHAR(100),
    market_cap      FLOAT8,                 -- in crores
    index_membership JSONB,                 -- ["NIFTY 50", "NIFTY 200"]
    is_active       BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol)
);

-- ============================================================
-- 10. Watchlist
-- ============================================================
CREATE TABLE IF NOT EXISTS watchlist (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    source          VARCHAR(20) DEFAULT 'manual',  -- manual / auto
    notes           TEXT,
    UNIQUE (symbol)
);

-- ============================================================
-- 11. Factor Registry (metadata about each factor)
-- ============================================================
CREATE TABLE IF NOT EXISTS factor_registry (
    factor_name     VARCHAR(100) PRIMARY KEY,
    category        VARCHAR(50),            -- technical, fundamental, flow, macro, options, regulatory, alt
    description     TEXT,
    expected_ic_low FLOAT8,
    expected_ic_high FLOAT8,
    data_source     VARCHAR(50),
    compute_frequency VARCHAR(20),          -- daily, weekly, quarterly
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 12. Signal Audit Trail (SEBI compliance, append-only)
-- ============================================================
CREATE TABLE IF NOT EXISTS signal_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    symbol          VARCHAR(20) NOT NULL,
    horizon         VARCHAR(20) NOT NULL,
    signal_value    SMALLINT,
    confidence      FLOAT8,
    model_hash      VARCHAR(64),
    input_snapshot  JSONB,                  -- factor values at signal time
    output_snapshot JSONB                   -- full signal output
);

-- ============================================================
-- Indices for common query patterns
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON ohlcv(symbol);
CREATE INDEX IF NOT EXISTS idx_factor_pit_lookup
    ON factor_pit(symbol, as_of_date, factor_name)
    WHERE valid_to > NOW();
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date, horizon);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, horizon);
CREATE INDEX IF NOT EXISTS idx_macro_series ON macro(series_id, date);
CREATE INDEX IF NOT EXISTS idx_flows_date ON institutional_flows(date);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON signal_audit_log(timestamp);
