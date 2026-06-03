"""
Smoke + correctness tests for the ported SMC factor module
(qsde/factors/smc.py, Phase 0.3 consolidation).

Pure pandas/numpy — no DB, no network, no live data. Validates that the
StockTrack port produces the expected wide frame, key liquidity/volume-profile
columns exist, shapes align, and there is no obvious forward-looking peek.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make the `qsde` package importable when pytest is invoked from anywhere.
BACKEND_ROOT = Path(__file__).resolve().parents[1]  # qsde/backend
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.factors.smc import compute_smc_features, liquidity_sweeps, volume_profile


def _synthetic_ohlcv(n: int = 320, seed: int = 7) -> pd.DataFrame:
    """Deterministic random-walk OHLCV with a clean DatetimeIndex."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n)
    rets = rng.normal(0.0004, 0.015, n)
    close = 1000.0 * np.exp(np.cumsum(rets))
    spread = np.abs(rng.normal(0, 0.004, n)) * close
    high = close + spread
    low = close - spread
    open_ = np.r_[close[0], close[:-1]]
    vol = rng.integers(50_000, 500_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_compute_smc_features_shape_and_columns():
    df = _synthetic_ohlcv()
    out = compute_smc_features(df)

    # Same number of rows, indexed identically.
    assert len(out) == len(df)
    assert out.index.equals(df.index)

    # The columns the user explicitly cares about must exist.
    must_have = {
        "smc_liq_sweep_high", "smc_liq_sweep_low",     # liquidity sweeps
        "smc_vp_poc", "smc_vp_vah", "smc_vp_val",       # volume profile
        "smc_vp_in_value", "smc_vp_poc_dist",
        "smc_bos_up", "smc_bos_dn", "smc_choch_up",     # market structure
        "smc_fvg_bull", "smc_fvg_bear",                  # fair value gaps
        "smc_order_block_bull", "smc_order_block_bear",  # order blocks
        "smc_ema_stack",                                  # trend alignment
    }
    missing = must_have - set(out.columns)
    assert not missing, f"missing SMC columns: {missing}"

    # ~35 features expected.
    assert out.shape[1] >= 30


def test_value_area_brackets_poc():
    """VAL <= POC <= VAH wherever the profile is defined."""
    df = _synthetic_ohlcv()
    vp = volume_profile(df, lookback=30, bins=24)
    defined = vp.dropna(subset=["smc_vp_poc", "smc_vp_vah", "smc_vp_val"])
    assert len(defined) > 0
    assert (defined["smc_vp_val"] <= defined["smc_vp_poc"] + 1e-9).all()
    assert (defined["smc_vp_poc"] <= defined["smc_vp_vah"] + 1e-9).all()


def test_sweeps_are_binary_and_sparse():
    df = _synthetic_ohlcv()
    sw = liquidity_sweeps(df)
    for col in ("smc_liq_sweep_high", "smc_liq_sweep_low"):
        vals = set(pd.unique(sw[col].dropna()))
        assert vals.issubset({0, 1}), f"{col} not binary: {vals}"
    # Sweeps are exceptional events, not the majority of bars.
    assert sw["smc_liq_sweep_high"].mean() < 0.5
    assert sw["smc_liq_sweep_low"].mean() < 0.5


def test_no_lookahead_on_truncation():
    """
    Values computed on the full series must match values computed on a prefix,
    up to the prefix end (a basic no-future-peek guarantee for causal columns).
    Volume-profile / trendline use centered or pivot logic, so we check the
    purely-causal sweep + structure columns.
    """
    df = _synthetic_ohlcv()
    causal_cols = ["smc_liq_sweep_high", "smc_liq_sweep_low", "smc_bos_up", "smc_bos_dn"]
    full = compute_smc_features(df)[causal_cols]
    cut = 250
    prefix = compute_smc_features(df.iloc[:cut])[causal_cols]
    # Compare the overlapping region (skip warmup head where rolling windows fill).
    a = full.iloc[50:cut].reset_index(drop=True)
    b = prefix.iloc[50:cut].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_dtype=False)
