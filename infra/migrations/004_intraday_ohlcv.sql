-- 004_intraday_ohlcv.sql
--
-- Minute-bar OHLCV storage for the Kite WebSocket live-tick feed.
--
-- Design notes:
--   * 1-minute resolution as the base bucket; finer aggregations can be
--     derived later via TimescaleDB continuous aggregates.
--   * `ts` is the *bar START* (e.g. 09:15:00 = bar [09:15, 09:16)).
--   * `n_ticks` is the number of raw ticks that fed the bar -- helps
--     identify low-quality bars (e.g. illiquid names with 1 tick/min).
--   * `vwap` is the volume-weighted average price computed across ticks
--     within the bar -- this is the *actual* intraday VWAP, not a daily
--     approximation. Compounds upward across the day if needed.
--   * Hypertable partitioned by 1-day chunks -- standard TimescaleDB
--     pattern for high-frequency time-series.

CREATE TABLE IF NOT EXISTS ohlcv_intraday (
    symbol      VARCHAR(20)  NOT NULL,
    ts          TIMESTAMPTZ  NOT NULL,
    open        FLOAT8       NOT NULL,
    high        FLOAT8       NOT NULL,
    low         FLOAT8       NOT NULL,
    close       FLOAT8       NOT NULL,
    volume      BIGINT       NOT NULL DEFAULT 0,
    vwap        FLOAT8,
    n_ticks     INTEGER      NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);

-- Make it a hypertable; 1-day chunks balance ingestion speed and query speed.
DO $$
BEGIN
    PERFORM create_hypertable(
        'ohlcv_intraday',
        'ts',
        chunk_time_interval => INTERVAL '1 day',
        if_not_exists => TRUE
    );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'create_hypertable skipped (already a hypertable or extension missing)';
END $$;

CREATE INDEX IF NOT EXISTS idx_ohlcv_intraday_symbol_ts
    ON ohlcv_intraday(symbol, ts DESC);

-- Live tick log -- raw ticks land here briefly before being aggregated.
-- Useful for replay/debugging and as a safety net if the aggregator crashes
-- mid-bar. Retention is short (24h) to keep this table small.
CREATE TABLE IF NOT EXISTS ticks_raw (
    instrument_token BIGINT       NOT NULL,
    symbol           VARCHAR(20),
    ts               TIMESTAMPTZ  NOT NULL,
    last_price       FLOAT8       NOT NULL,
    volume_traded    BIGINT,
    buy_quantity     BIGINT,
    sell_quantity    BIGINT,
    received_at      TIMESTAMPTZ  DEFAULT NOW()
);

DO $$
BEGIN
    PERFORM create_hypertable(
        'ticks_raw',
        'ts',
        chunk_time_interval => INTERVAL '6 hours',
        if_not_exists => TRUE
    );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'create_hypertable for ticks_raw skipped';
END $$;

CREATE INDEX IF NOT EXISTS idx_ticks_raw_token_ts
    ON ticks_raw(instrument_token, ts DESC);

-- Optional: 24h retention policy on raw ticks. Comment out if you want
-- to keep them longer for backtest data.
DO $$
BEGIN
    PERFORM add_retention_policy('ticks_raw', INTERVAL '24 hours', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'retention policy on ticks_raw not added (extension support varies)';
END $$;
