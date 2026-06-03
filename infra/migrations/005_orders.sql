-- 005_orders.sql
--
-- Semi-auto order ticket lifecycle (Phase 5). QSDE never auto-fires: a ticket
-- is SUGGESTED, a human CONFIRMS it (echoing a confirm_token), and only then —
-- if live orders are explicitly enabled and the kill-switch is off — does it go
-- to the broker. dry_run tickets record a simulated placement for audit.
--
-- Status flow: SUGGESTED -> CONFIRMED -> (DRYRUN | PLACED) | REJECTED | FAILED
--
-- Append-only-ish: rows are updated in place as status advances, with
-- created_at / updated_at timestamps for the SEBI audit trail.

CREATE TABLE IF NOT EXISTS orders (
    ticket_id        UUID PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol           VARCHAR(20) NOT NULL,
    side             VARCHAR(4)  NOT NULL,        -- BUY / SELL
    qty              INTEGER     NOT NULL,
    order_type       VARCHAR(10) NOT NULL,        -- MARKET / LIMIT
    product          VARCHAR(10) NOT NULL,        -- MIS / CNC / NRML
    entry_price      FLOAT8,
    limit_price      FLOAT8,
    stop_price       FLOAT8,
    target_price     FLOAT8,
    risk_reward      FLOAT8,
    horizon          VARCHAR(20),
    bias             FLOAT8,
    confidence       FLOAT8,
    capital_required FLOAT8,
    risk_at_stop     FLOAT8,
    status           VARCHAR(16) NOT NULL DEFAULT 'SUGGESTED',
    confirm_token    VARCHAR(64) NOT NULL,
    dry_run          BOOLEAN     NOT NULL DEFAULT TRUE,
    broker_order_id  VARCHAR(64),
    reasons          JSONB,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol_created ON orders(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status        ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created       ON orders(created_at DESC);
