-- 012_news_sentiment_multi_source.sql
--
-- Extend news_sentiment PK from (symbol, date) to (symbol, date, source)
-- so MoneyControl + Economic Times + (future: GDELT, Bright Data scrapes)
-- can co-exist per (symbol, date) without overwriting each other. The
-- sentiment factor reader aggregates across sources at read time.
--
-- Idempotent. Guarded so re-runs after the rebuild succeed.

DO $$
BEGIN
    -- Only restructure if the current PK is still the 2-column version.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'news_sentiment'::regclass
           AND conname = 'news_sentiment_pkey'
    ) AND NOT EXISTS (
        SELECT 1
          FROM pg_index i
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
         WHERE i.indrelid = 'news_sentiment'::regclass
           AND i.indisprimary
           AND a.attname = 'source'
    ) THEN
        -- Drop the old PK.
        ALTER TABLE news_sentiment DROP CONSTRAINT news_sentiment_pkey;
        -- Make sure source has no NULLs (PK members can't be null).
        UPDATE news_sentiment SET source = 'finnhub' WHERE source IS NULL;
        ALTER TABLE news_sentiment ALTER COLUMN source SET NOT NULL;
        ALTER TABLE news_sentiment ALTER COLUMN source SET DEFAULT 'finnhub';
        -- New 3-column PK.
        ALTER TABLE news_sentiment
            ADD CONSTRAINT news_sentiment_pkey
            PRIMARY KEY (symbol, date, source);
    END IF;
END $$;

-- Index for the common "all sources for this symbol over time" read path.
CREATE INDEX IF NOT EXISTS idx_news_sentiment_symbol_date
    ON news_sentiment(symbol, date DESC);
