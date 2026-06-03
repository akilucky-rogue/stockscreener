-- ========================================================================
-- QSDE Migration 001 — Phase 0 schema fixes
-- ========================================================================
-- Applies the schema corrections identified in docs/AUDIT_v1.md.
--
-- Fixes:
--   * Bug 4 — Make `fundamentals` PIT-correct: PK now includes filing_date
--                so that restated filings do not overwrite earlier ones.
--   * Bug 2/5 — Add columns the research engines + FMP client require
--                (market_cap, enterprise_value, ev_to_revenue, roic,
--                 fcf_per_share).
--   * Bug 3 — Add UNIQUE constraint to bulk_deals so re-ingesting does
--                not create duplicate rows.
--
-- Apply with:
--   psql "$DATABASE_URL" -f infra/migrations/001_phase0_fixes.sql
-- or:
--   docker compose exec -T timescaledb psql -U qsde -d qsde \
--       < infra/migrations/001_phase0_fixes.sql
--
-- Idempotent: safe to run multiple times.
-- ========================================================================

BEGIN;

-- ── 1. Add columns the research engines reference ───────────────────────
ALTER TABLE fundamentals
    ADD COLUMN IF NOT EXISTS market_cap        FLOAT8,
    ADD COLUMN IF NOT EXISTS enterprise_value  FLOAT8,
    ADD COLUMN IF NOT EXISTS ev_to_revenue     FLOAT8,
    ADD COLUMN IF NOT EXISTS roic              FLOAT8,
    ADD COLUMN IF NOT EXISTS fcf_per_share     FLOAT8;

-- ── 2. Promote filing_date to PIT key ───────────────────────────────────
-- For any rows with NULL filing_date, default to fiscal_date so the
-- migration succeeds on existing data. Production writes after this
-- migration MUST supply an explicit filing_date.
UPDATE fundamentals
   SET filing_date = fiscal_date
 WHERE filing_date IS NULL;

ALTER TABLE fundamentals
    ALTER COLUMN filing_date SET NOT NULL;

-- Drop the old (symbol, fiscal_date) primary key and rebuild on the
-- PIT-correct triple. Pull constraint name dynamically because
-- PostgreSQL auto-names PKs (e.g. fundamentals_pkey).
DO $$
DECLARE
    pk_name TEXT;
BEGIN
    SELECT conname INTO pk_name
    FROM pg_constraint
    WHERE conrelid = 'fundamentals'::regclass
      AND contype = 'p';

    IF pk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE fundamentals DROP CONSTRAINT %I', pk_name);
    END IF;
END $$;

ALTER TABLE fundamentals
    ADD CONSTRAINT fundamentals_pit_pkey
    PRIMARY KEY (symbol, fiscal_date, filing_date);

-- Helper index for the canonical PIT lookup pattern:
--   "give me the latest filing_date for (symbol, fiscal_date)
--    that was known on or before signal_date"
CREATE INDEX IF NOT EXISTS idx_fundamentals_pit_lookup
    ON fundamentals(symbol, fiscal_date, filing_date DESC);

-- ── 3. Deduplicate bulk_deals at the database level ─────────────────────
-- The natural key for a single bulk-deal record is the full tuple. NSE
-- occasionally republishes corrections, so we let identical rows be
-- swallowed silently rather than rejected. Wrapped in a DO block for
-- idempotency -- ALTER TABLE ADD CONSTRAINT has no IF NOT EXISTS form.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'bulk_deals_natural_key_uniq'
    ) THEN
        ALTER TABLE bulk_deals
            ADD CONSTRAINT bulk_deals_natural_key_uniq
            UNIQUE (symbol, date, client_name, deal_type, quantity, price);
    END IF;
END $$;

COMMIT;

-- ========================================================================
-- Verification queries (run manually after migration)
-- ========================================================================
--
-- \d fundamentals     -- should show NOT NULL filing_date and the new PK
-- \d bulk_deals       -- should show the bulk_deals_natural_key_uniq
-- SELECT column_name FROM information_schema.columns
--  WHERE table_name = 'fundamentals'
--    AND column_name IN ('market_cap', 'enterprise_value',
--                        'ev_to_revenue', 'roic', 'fcf_per_share');
-- ========================================================================
