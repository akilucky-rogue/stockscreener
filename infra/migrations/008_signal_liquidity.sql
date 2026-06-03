-- 008_signal_liquidity.sql
--
-- Liquidity annotation on signals. The intraday slippage+liquidity stress
-- test (scripts/stress_test_intraday.py) proved the model's edge only
-- survives execution costs when trades are restricted to liquid names
-- (trailing-20d average daily value traded, ADV, >= ~Rs 10 crore/day).
-- Below that, backtested fills are fiction.
--
-- We persist ADV + a liquidity flag on every generated signal so the serve
-- layer can default to surfacing only TRADEABLE signals, and the UI can
-- show the ADV honestly next to each name.

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS adv_20d    FLOAT8,   -- trailing 20d avg daily value traded (rupees)
    ADD COLUMN IF NOT EXISTS is_liquid  BOOLEAN;  -- adv_20d >= threshold at generation time

CREATE INDEX IF NOT EXISTS idx_signals_liquid
    ON signals(horizon, date, is_liquid);
