-- 009_paper_trades.sql
--
-- Paper-trade journal. The bridge from "validated in backtest" to "validated
-- live": you record the signals you would actually take, and a reconciliation
-- job walks forward through real OHLCV (daily) / ohlcv_intraday (minute) to
-- mark each against its triple barriers — WIN (target hit first), LOSS (stop
-- hit first), or TIME (neither, exit at horizon close). Realized returns are
-- computed net of an assumed round-trip cost so the live track record is
-- directly comparable to the stress-tested net Sharpe.
--
-- This is how you measure your REAL edge + slippage over weeks, instead of
-- trusting the backtest.

CREATE TABLE IF NOT EXISTS paper_trades (
    id                SERIAL PRIMARY KEY,
    symbol            VARCHAR(20)  NOT NULL,
    horizon           VARCHAR(10)  NOT NULL,        -- intraday | swing | long
    taken_at          TIMESTAMPTZ  DEFAULT NOW(),
    entry_date        DATE         NOT NULL,
    entry_price       FLOAT8       NOT NULL,
    direction         INT          NOT NULL,        -- +1 long, -1 short
    target_price      FLOAT8,
    stop_price        FLOAT8,
    rank_pct          FLOAT8,                        -- model cross-sectional rank at take time
    horizon_sessions  INT          NOT NULL,         -- barrier window in NSE sessions
    cost_bps          FLOAT8       DEFAULT 25,        -- assumed round-trip cost for net return

    status            VARCHAR(10)  DEFAULT 'OPEN',   -- OPEN | WIN | LOSS | TIME
    exit_date         DATE,
    exit_price        FLOAT8,
    realized_ret      FLOAT8,                         -- gross, direction-adjusted
    realized_ret_net  FLOAT8,                         -- after cost_bps
    notes             TEXT,

    -- One open paper trade per (symbol, horizon, entry_date) — prevents
    -- accidentally taking the same signal twice in a session.
    UNIQUE (symbol, horizon, entry_date)
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_status
    ON paper_trades(status, horizon);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol
    ON paper_trades(symbol, entry_date DESC);
