"""
Finnhub company-news client (REAL data — no mock).

Free-tier Finnhub `company-news` returns real headlines but NO sentiment score
(that endpoint is premium). So we fetch the real articles and compute a
lightweight, deterministic headline polarity from a finance lexicon. Honest
limitations: (1) Finnhub's NSE/BSE coverage is thinner than US names, so many
Indian tickers return few/no articles; (2) lexicon polarity is a coarse proxy
— swap in FinBERT or Finnhub's premium /news-sentiment for production-grade
scoring. The schema + factor don't change when you upgrade the scorer.

Pipeline: fetch_company_news -> aggregate daily (count, mean polarity) ->
upsert into `news_sentiment`. qsde/factors/sentiment.py reads from there.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Optional

import httpx
import pandas as pd

from qsde.config import settings
from qsde.db import upsert_dataframe

log = logging.getLogger(__name__)

# Tiny finance polarity lexicon. Deterministic; good enough as a v1 proxy.
_POS = {
    "surge", "surges", "surged", "jump", "jumps", "beat", "beats", "profit",
    "profits", "upgrade", "upgraded", "growth", "record", "strong", "gain",
    "gains", "rally", "rallies", "win", "wins", "soar", "soars", "rises",
    "rise", "boost", "outperform", "bullish", "high", "expansion", "approval",
}
_NEG = {
    "fall", "falls", "fell", "loss", "losses", "miss", "misses", "downgrade",
    "downgraded", "fraud", "probe", "weak", "cut", "cuts", "decline", "declines",
    "plunge", "plunges", "slump", "slumps", "default", "lawsuit", "ban", "fine",
    "bearish", "low", "warning", "recall", "scam", "crash", "drops", "drop",
}


def score_headline(text: Optional[str]) -> float:
    """Headline polarity in [-1, 1] from the finance lexicon. 0 if neutral/empty."""
    if not text:
        return 0.0
    toks = [t.strip(".,!?:;\"'()").lower() for t in str(text).split()]
    pos = sum(1 for t in toks if t in _POS)
    neg = sum(1 for t in toks if t in _NEG)
    if pos + neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg)


def fetch_company_news(
    symbol: str,
    from_date: date,
    to_date: date,
    exchange_suffix: str = ".NS",
) -> pd.DataFrame:
    """Fetch real Finnhub company-news for one symbol. Returns daily aggregates
    (date, news_count, polarity). Empty if no key / no coverage / error.
    """
    if not settings.finnhub_api_key:
        log.warning("FINNHUB_API_KEY not configured")
        return pd.DataFrame()

    fh_symbol = f"{symbol.upper()}{exchange_suffix}"
    url = f"{settings.finnhub_base_url}/company-news"
    params = {
        "symbol": fh_symbol,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "token": settings.finnhub_api_key,
    }
    try:
        resp = httpx.get(url, params=params, timeout=20.0)
        if resp.status_code != 200:
            log.warning("Finnhub %d for %s", resp.status_code, fh_symbol)
            return pd.DataFrame()
        articles = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Finnhub error for %s: %s", fh_symbol, e)
        return pd.DataFrame()

    if not isinstance(articles, list) or not articles:
        return pd.DataFrame()

    rows = []
    for a in articles:
        ts = a.get("datetime")
        if not ts:
            continue
        d = pd.to_datetime(int(ts), unit="s").date()
        headline = f"{a.get('headline', '')} {a.get('summary', '')}"
        rows.append({"date": d, "polarity": score_headline(headline)})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    daily = df.groupby("date").agg(
        news_count=("polarity", "size"),
        avg_polarity=("polarity", "mean"),
    ).reset_index()
    daily["symbol"] = symbol.upper()
    daily["source"] = "finnhub"
    return daily


def sync_sentiment_to_db(
    symbols: list[str],
    days: int = 365,
    exchange_suffix: str = ".NS",
) -> int:
    """Fetch + aggregate + upsert daily news sentiment for each symbol.

    Rate-limited to settings.finnhub_rps. Returns total rows upserted.
    """
    to_d = date.today()
    from_d = to_d - timedelta(days=days)
    sleep_s = 1.0 / max(settings.finnhub_rps, 0.1)
    total = 0
    for i, sym in enumerate(symbols, 1):
        daily = fetch_company_news(sym, from_d, to_d, exchange_suffix=exchange_suffix)
        if not daily.empty:
            total += upsert_dataframe(
                daily[["symbol", "date", "news_count", "avg_polarity", "source"]],
                table="news_sentiment",
                conflict_columns=["symbol", "date"],
                update_columns=["news_count", "avg_polarity", "source"],
            )
        if i % 25 == 0:
            log.info("Sentiment sync: %d/%d symbols (%d rows)", i, len(symbols), total)
        time.sleep(sleep_s)
    log.info("Sentiment sync complete: %d rows across %d symbols", total, len(symbols))
    return total


__all__ = ["score_headline", "fetch_company_news", "sync_sentiment_to_db"]
