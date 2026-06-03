-- 010_risk_governor_and_baselines.sql
--
-- Two additions to make Phase 1 operational:
--
--   1. paper_trades.strategy : tag each paper trade with which strategy
--      produced it. Default 'model' for backwards compat. The drift report
--      compares 'model' against 'baseline_top_momentum', 'baseline_nifty',
--      and 'baseline_random' — if the ML doesn't beat all three on net
--      Sharpe net of cost over 30+ sessions, we don't have edge.
--
--   2. risk_governor_state : append-only audit log of every position-risk
--      tier change. Read latest row to know current tier; preserves who
--      escalated/de-escalated and why so the trail is reconstructable.
--
-- Idempotent: safe to run multiple times.


-- ── 1. Strategy tag on paper_trades ─────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'paper_trades' AND column_name = 'strategy'
    ) THEN
        ALTER TABLE paper_trades
            ADD COLUMN strategy VARCHAR(32) NOT NULL DEFAULT 'model';
    END IF;
END $$;

-- Drop & recreate the unique constraint to include strategy. Otherwise we
-- can't log the same (symbol, horizon, date) twice once for the model and
-- once for a baseline strategy.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'paper_trades_symbol_horizon_entry_date_key'
    ) THEN
        ALTER TABLE paper_trades
            DROP CONSTRAINT paper_trades_symbol_horizon_entry_date_key;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'paper_trades_strategy_symbol_horizon_entry_date_key'
    ) THEN
        ALTER TABLE paper_trades
            ADD CONSTRAINT paper_trades_strategy_symbol_horizon_entry_date_key
            UNIQUE (strategy, symbol, horizon, entry_date);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy
    ON paper_trades(strategy, horizon, entry_date DESC);


-- ── 2. Risk governor audit log ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS risk_governor_state (
    id              SERIAL       PRIMARY KEY,
    effective_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    tier_name       VARCHAR(8)   NOT NULL,                     -- T0 | T1 | T2 | T3
    reason          TEXT,
    changed_by      VARCHAR(16)  NOT NULL DEFAULT 'system'     -- 'system' | 'user'
);

CREATE INDEX IF NOT EXISTS idx_risk_governor_effective_at
    ON risk_governor_state(effective_at DESC);

-- Seed initial row (T0 default) only if the table is empty.
INSERT INTO risk_governor_state (tier_name, reason, changed_by)
SELECT 'T0', 'initial seed — pre-validation default cap 1%', 'system'
 WHERE NOT EXISTS (SELECT 1 FROM risk_governor_state);
