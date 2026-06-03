-- 006_model_promotion_bse.sql
--
-- (a) Model promotion gate: DSR is the only promotion metric (Blueprint #2).
--     A trained model is only PROMOTED to the live/active slot if its deflated
--     Sharpe clears the threshold (or an explicit dev force-promote). Record the
--     decision on each model_runs row for the audit trail.
-- (b) BSE universe support: tag each universe row with its exchange so NSE and
--     BSE equities can coexist (MCX deferred).

ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS promoted       BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS dsr_threshold  FLOAT8;
ALTER TABLE model_runs ADD COLUMN IF NOT EXISTS promotion_note TEXT;

ALTER TABLE universe   ADD COLUMN IF NOT EXISTS exchange VARCHAR(10) NOT NULL DEFAULT 'NSE';
CREATE INDEX IF NOT EXISTS idx_universe_exchange ON universe(exchange, is_active);
