-- 002_signal_trade_levels.sql
--
-- Adds entry / target / stop / risk_reward / atr_pct columns to the signals
-- table so every direction call carries an actionable trade plan instead of
-- just a buy/sell/hold flag. Per-horizon ATR multipliers live in
-- qsde/risk/trade_levels.py; this migration only provisions storage.
--
-- All columns are nullable -- HOLD signals legitimately have no plan, and
-- older rows produced before this migration won't have values either.
--
-- Idempotent: safe to re-run.

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS entry_price   FLOAT8,
    ADD COLUMN IF NOT EXISTS target_price  FLOAT8,
    ADD COLUMN IF NOT EXISTS stop_price    FLOAT8,
    ADD COLUMN IF NOT EXISTS risk_reward   FLOAT8,
    ADD COLUMN IF NOT EXISTS atr_pct       FLOAT8,
    ADD COLUMN IF NOT EXISTS trade_quality VARCHAR(10);  -- 'good' / 'low' / NULL
