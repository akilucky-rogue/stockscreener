-- 007_news_sentiment.sql
--
-- Daily company-news buzz + headline polarity per symbol, aggregated from the
-- Finnhub company-news API (real data). Factors read from here (PIT-safe via
-- .shift(1) in the factor module). Macro factors reuse the existing `macro`
-- table (FRED), so no new table is needed for macro.

CREATE TABLE IF NOT EXISTS news_sentiment (
    symbol        VARCHAR(20) NOT NULL,
    date          DATE        NOT NULL,
    news_count    INTEGER     NOT NULL DEFAULT 0,
    avg_polarity  FLOAT8,                       -- [-1, 1] lexicon polarity of headlines
    source        VARCHAR(20) DEFAULT 'finnhub',
    fetched_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_news_sentiment_symbol ON news_sentiment(symbol, date DESC);
