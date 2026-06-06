-- 011_rule_signals.sql
--
-- Tier 1 rule-based factor engine integration.
--
-- Adds:
--   1. signals.strategy column so a single (symbol, date, horizon) tuple
--      can host multiple strategies side-by-side (ML, plus the 5 Tier 1
--      streams). Extends PRIMARY KEY to include strategy. Existing ML
--      signals are tagged 'ml' by default so the data carries through.
--   2. rule_factor_ic table for rolling per-factor Information Coefficient
--      and decile-hit-rate tracking. Composite weighting reads from here;
--      degrades to equal-weight when no data.
--
-- Idempotent. Safe to re-run.
--
-- Background:
--   * IC = Spearman correlation of factor rank vs realized forward return.
--     Per Grinold (1989), expected IR = IC * sqrt(breadth). We track IC
--     per (factor, horizon) over a rolling 60-session window so the
--     composite weight = max(IC, 0) (we don't short underperforming
--     factors, we just zero them).
--   * Storing IC in a separate table (not factor_pit) because IC is a
--     property of the strategy/horizon, not of a single symbol on a date.

BEGIN;

-- ── 1. Strategy tag on signals ─────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'signals' AND column_name = 'strategy'
    ) THEN
        ALTER TABLE signals
            ADD COLUMN strategy VARCHAR(32) NOT NULL DEFAULT 'ml';
    END IF;
END $$;

-- Drop the old PK (symbol, date, horizon) and recreate including strategy.
-- ALTER TABLE ... DROP CONSTRAINT errors if the constraint is missing, so we
-- guard. Postgres autogenerates the PK name as <table>_pkey.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'signals'::regclass AND conname = 'signals_pkey'
    ) THEN
        -- Only drop+recreate if the current PK doesn't already include strategy
        IF NOT EXISTS (
            SELECT 1
              FROM pg_index i
              JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = 'signals'::regclass
               AND i.indisprimary
               AND a.attname = 'strategy'
        ) THEN
            ALTER TABLE signals DROP CONSTRAINT signals_pkey;
            ALTER TABLE signals
                ADD PRIMARY KEY (strategy, symbol, date, horizon);
        END IF;
    END IF;
END $$;

-- Index for the common "give me today's signals for strategy X" query.
CREATE INDEX IF NOT EXISTS idx_signals_strategy_date
    ON signals(strategy, date DESC, horizon);

-- ── 2. Per-factor rolling IC tracking ──────────────────────────────────

CREATE TABLE IF NOT EXISTS rule_factor_ic (
    factor_name      VARCHAR(32)  NOT NULL,   -- jt | mop | bab | rsi2
    horizon          VARCHAR(10)  NOT NULL,   -- intraday | swing | long
    as_of_date       DATE         NOT NULL,
    -- Rolling 60-session Spearman IC of factor rank vs realized fwd return,
    -- net of horizon-appropriate cost (matches paper_trades cost_bps).
    ic_60d           FLOAT8,
    -- Decile hit rate: fraction of top-decile picks that closed positive.
    hit_rate_top     FLOAT8,
    -- Decile hit rate: fraction of bottom-decile picks that closed negative.
    hit_rate_bot     FLOAT8,
    -- Annualized Sharpe of factor's decile-spread portfolio (long top / short bot).
    sharpe_ann       FLOAT8,
    -- Number of paper trades the IC is computed from (breadth).
    n_observations   INT          NOT NULL DEFAULT 0,
    -- Weight the composite engine should use next session = max(ic_60d, 0).
    -- Materialized so the engine doesn't recompute on every load.
    composite_weight FLOAT8       NOT NULL DEFAULT 0.0,
    computed_at      TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (factor_name, horizon, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_rule_factor_ic_latest
    ON rule_factor_ic(factor_name, horizon, as_of_date DESC);

-- ── 3. View: latest IC per (factor, horizon) ──────────────────────────
-- Drop+create rather than CREATE OR REPLACE because column list may change.
DROP VIEW IF EXISTS rule_factor_ic_latest;
CREATE VIEW rule_factor_ic_latest AS
SELECT DISTINCT ON (factor_name, horizon)
       factor_name, horizon, as_of_date,
       ic_60d, hit_rate_top, hit_rate_bot, sharpe_ann,
       n_observations, composite_weight, computed_at
  FROM rule_factor_ic
 ORDER BY factor_name, horizon, as_of_date DESC;

COMMIT;
