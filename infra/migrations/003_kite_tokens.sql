-- 003_kite_tokens.sql
--
-- Kite Connect access-token storage.
--
-- Kite's OAuth model issues a fresh `access_token` every day at 6am IST. The
-- daily handshake is: user opens login URL -> Zerodha redirects back with
-- a `request_token` -> backend exchanges (request_token, api_secret) for an
-- `access_token` which is valid until 6am next day.
--
-- We keep at most one current token, but retain old rows for an audit trail.
-- `is_active` flips to false when a fresh exchange happens.

CREATE TABLE IF NOT EXISTS kite_tokens (
    id              SERIAL PRIMARY KEY,
    access_token    VARCHAR(256) NOT NULL,
    public_token    VARCHAR(256),
    user_id         VARCHAR(50),               -- Zerodha client ID, e.g. KUW989
    user_name       VARCHAR(120),
    login_time      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,      -- next 06:00 IST after login
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kite_tokens_active
    ON kite_tokens(is_active, expires_at DESC);

-- Kite instrument_token map. The historical-data API takes an instrument_token
-- (an integer ID), NOT the trading symbol. We dump the full instrument list
-- once a day after market close and join against this table at ingestion time.
CREATE TABLE IF NOT EXISTS kite_instruments (
    instrument_token    BIGINT PRIMARY KEY,
    exchange_token      BIGINT,
    tradingsymbol       VARCHAR(50) NOT NULL,
    name                TEXT,
    last_price          FLOAT8,
    expiry              DATE,
    strike              FLOAT8,
    tick_size           FLOAT8,
    lot_size            INTEGER,
    instrument_type     VARCHAR(20),    -- EQ, FUT, CE, PE, ...
    segment             VARCHAR(20),    -- NSE, BSE, NFO-OPT, ...
    exchange            VARCHAR(10),    -- NSE / BSE
    refreshed_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kite_instruments_symbol
    ON kite_instruments(tradingsymbol, exchange)
    WHERE instrument_type = 'EQ';
