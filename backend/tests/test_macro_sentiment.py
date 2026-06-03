"""
Tests for the macro (FRED) and sentiment (Finnhub) factor math + the headline
polarity scorer. Pure / DB-free — verifies the transforms; live ingest is
exercised separately against the real APIs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.factors.macro import compute_macro_features
from qsde.factors.sentiment import compute_sentiment_features
from qsde.ingestion.finnhub_client import score_headline


def _macro_wide(n=150, seed=2):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-01", periods=n)
    return pd.DataFrame({
        "DGS10": 4.0 + np.cumsum(rng.normal(0, 0.02, n)),
        "VIXCLS": 15 + np.abs(np.cumsum(rng.normal(0, 0.3, n))),
        "FEDFUNDS": np.full(n, 5.25),
        "DTWEXBGS": 100 + np.cumsum(rng.normal(0, 0.2, n)),
        "DCOILBRENTEU": 80 + np.cumsum(rng.normal(0, 0.5, n)),
    }, index=dates)


def test_macro_features_columns_and_alignment():
    wide = _macro_wide()
    idx = wide.index[-100:]
    out = compute_macro_features(wide, idx)
    assert len(out) == len(idx)
    for c in ["macro_us10y", "macro_us10y_chg20", "macro_vix", "macro_vix_chg5",
              "macro_fedfunds", "macro_yield_curve", "macro_dxy_chg20", "macro_brent_chg20"]:
        assert c in out.columns, c
    # yield curve = us10y - fedfunds, on the aligned/shifted series
    yc = (out["macro_us10y"] - out["macro_fedfunds"]).dropna()
    assert np.allclose(yc.values, out["macro_yield_curve"].dropna().values, atol=1e-9)
    # plenty of finite values (not all-NaN)
    assert out["macro_us10y"].notna().sum() > 50


def test_macro_empty_inputs():
    idx = pd.bdate_range("2024-01-01", periods=10)
    assert compute_macro_features(pd.DataFrame(), idx).shape[0] == len(idx)
    assert compute_macro_features(_macro_wide(), pd.DatetimeIndex([])).empty


def test_sentiment_features():
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2024-01-01", periods=60)
    daily = pd.DataFrame({
        "date": dates,
        "news_count": rng.integers(0, 5, 60),
        "avg_polarity": rng.uniform(-1, 1, 60),
    })
    out = compute_sentiment_features(daily, dates)
    assert len(out) == len(dates)
    for c in ["sentiment_news_5d", "sentiment_news_20d", "sentiment_polarity_5d",
              "sentiment_polarity_20d", "sentiment_news_spike"]:
        assert c in out.columns, c
    assert (out["sentiment_news_5d"].dropna() >= 0).all()
    assert out["sentiment_polarity_5d"].dropna().between(-1, 1).all()


def test_score_headline_polarity():
    assert score_headline("Profit surges to record, strong growth and upgrade") > 0
    assert score_headline("Fraud probe; losses mount as shares plunge, downgrade") < 0
    assert score_headline("The company held its annual general meeting today") == 0.0
    assert score_headline("") == 0.0
