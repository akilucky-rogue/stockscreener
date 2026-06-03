"""
Sentiment factor computation — company-news buzz + headline polarity per stock,
from the `news_sentiment` table (real Finnhub data; see
qsde.ingestion.finnhub_client).

Lookahead safety: news for date t is only fully known after t's close, so every
feature is `.shift(1)` — the factor on date t reflects news up to t-1.

All columns prefixed `sentiment_` (added to engine.py's IC filter).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)


def compute_sentiment_features(daily: pd.DataFrame, ohlcv_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Pure: a daily news frame (cols: date, news_count, avg_polarity) + a target
    index -> `sentiment_*` factor frame aligned to that index. No-news days are 0.
    """
    if len(ohlcv_index) == 0:
        return pd.DataFrame()
    if daily is None or daily.empty:
        return pd.DataFrame(index=ohlcv_index)

    d = daily.copy()
    if "date" in d.columns:
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date")
    d = d.sort_index()

    idx = pd.to_datetime(ohlcv_index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)

    news = pd.to_numeric(d.get("news_count"), errors="coerce")
    pol = pd.to_numeric(d.get("avg_polarity"), errors="coerce")
    news = news.reindex(idx).fillna(0.0)
    pol = pol.reindex(idx).fillna(0.0)
    news.index = ohlcv_index
    pol.index = ohlcv_index

    news_5d = news.rolling(5, min_periods=1).sum().shift(1)
    news_20d = news.rolling(20, min_periods=1).sum().shift(1)
    pol_5d = pol.rolling(5, min_periods=1).mean().shift(1)
    pol_20d = pol.rolling(20, min_periods=1).mean().shift(1)
    # Relative buzz: 5d news vs its 20d-implied daily baseline.
    spike = news_5d / ((news_20d / 4.0).replace(0, np.nan))

    out = pd.DataFrame({
        "sentiment_news_5d":      news_5d,
        "sentiment_news_20d":     news_20d,
        "sentiment_polarity_5d":  pol_5d,
        "sentiment_polarity_20d": pol_20d,
        "sentiment_news_spike":   spike,
    }, index=ohlcv_index)
    return out.replace([np.inf, -np.inf], np.nan)


def _load_news(symbol: str) -> pd.DataFrame:
    df = read_sql(
        "SELECT date, news_count, avg_polarity FROM news_sentiment "
        "WHERE symbol = :symbol ORDER BY date",
        params={"symbol": symbol.upper()},
    )
    return df


def compute_all_sentiment(symbol: str, ohlcv_index: pd.DatetimeIndex) -> pd.DataFrame:
    """DB-backed: load `news_sentiment` for symbol, align onto `ohlcv_index`."""
    if len(ohlcv_index) == 0:
        return pd.DataFrame()
    daily = _load_news(symbol)
    if daily is None or daily.empty:
        return pd.DataFrame(index=ohlcv_index)
    return compute_sentiment_features(daily, ohlcv_index)


__all__ = ["compute_sentiment_features", "compute_all_sentiment"]
