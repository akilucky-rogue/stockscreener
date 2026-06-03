"""Tests for the ported candlestick pattern factors (qsde/factors/patterns.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.factors.patterns import compute_patterns


def _ohlcv(n: int = 200, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-06-01", periods=n)
    close = 800 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.005, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = rng.integers(1e5, 1e6, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )


def test_patterns_shape_and_signing():
    df = _ohlcv()
    out = compute_patterns(df)
    assert not out.empty
    assert len(out) == len(df)
    # engulfing exists on both pandas-ta and fallback paths
    assert "pattern_engulfing" in out.columns
    assert set(pd.unique(out["pattern_engulfing"])).issubset({-1, 0, 1})
    # aggregate net column present and integer
    assert "pattern_net" in out.columns
    assert out["pattern_net"].dtype.kind in ("i", "u")


def test_empty_input():
    assert compute_patterns(pd.DataFrame()).empty
