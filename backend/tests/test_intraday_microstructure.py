"""
Tests for qsde/factors/intraday_microstructure.py (Phase 1).

Pure pandas/numpy. Synthetic 2-session minute bars. Validates session
re-anchoring of VWAP, band ordering, order-flow bounds, sweep logic,
volume-profile value-area bracketing, and the causal (no-lookahead) property.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.factors.intraday_microstructure import (
    anchored_vwap,
    compute_intraday_microstructure,
    order_flow,
)


def _synth_intraday(sessions: int = 2, bars: int = 120, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    base = pd.Timestamp("2024-02-01 09:15")
    price0 = 500.0
    for d in range(sessions):
        idx = pd.date_range(base + pd.Timedelta(days=d), periods=bars, freq="1min")
        rets = rng.normal(0, 0.0008, bars)
        close = price0 * np.exp(np.cumsum(rets))
        open_ = np.r_[close[0], close[:-1]]
        noise = np.abs(rng.normal(0, 0.0006, bars)) * close
        high = np.maximum(open_, close) + noise
        low = np.minimum(open_, close) - noise
        vol = rng.integers(1000, 20000, bars).astype(float)
        frames.append(
            pd.DataFrame(
                {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
                index=idx,
            )
        )
        price0 = float(close[-1])
    return pd.concat(frames)


def test_columns_and_shape():
    df = _synth_intraday()
    out = compute_intraday_microstructure(df)
    assert len(out) == len(df)
    must = {
        "intraday_avwap", "intraday_avwap_upper", "intraday_avwap_lower",
        "intraday_avwap_dev", "intraday_ofi", "intraday_cvd",
        "intraday_sweep_high", "intraday_sweep_low",
        "intraday_sweep_low_reclaim", "intraday_sweep_high_reject",
        "intraday_vp_poc", "intraday_vp_vah", "intraday_vp_val",
        "intraday_vp_in_value",
    }
    assert not (must - set(out.columns)), must - set(out.columns)


def test_avwap_reanchors_each_session():
    """First bar of each session: AVWAP == that bar's typical price."""
    df = _synth_intraday()
    av = anchored_vwap(df)["intraday_avwap"]
    dates = pd.DatetimeIndex(df.index).date
    first_mask = np.r_[True, dates[1:] != dates[:-1]]
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    assert np.allclose(av[first_mask].to_numpy(), tp[first_mask].to_numpy(), rtol=1e-9)
    # at least 2 sessions detected
    assert first_mask.sum() == 2


def test_bands_bracket_avwap_and_dev_sign():
    df = _synth_intraday()
    av = anchored_vwap(df, k=2.0)
    assert (av["intraday_avwap_upper"] >= av["intraday_avwap"] - 1e-9).all()
    assert (av["intraday_avwap"] >= av["intraday_avwap_lower"] - 1e-9).all()
    # deviation sign matches close vs avwap
    above = df["close"] > av["intraday_avwap"]
    assert (av["intraday_avwap_dev"][above] > -1e-12).all()


def test_order_flow_bounded():
    df = _synth_intraday()
    of = order_flow(df, window=14)
    assert of["intraday_ofi"].between(-1.0, 1.0).all()
    assert np.isfinite(of["intraday_cvd"]).all()


def test_sweeps_binary_and_reclaim_consistent():
    df = _synth_intraday()
    out = compute_intraday_microstructure(df)
    for c in ("intraday_sweep_high", "intraday_sweep_low",
              "intraday_sweep_low_reclaim", "intraday_sweep_high_reject"):
        assert set(pd.unique(out[c])).issubset({0, 1}), c
    # reclaim implies a low-sweep occurred
    assert ((out["intraday_sweep_low_reclaim"] == 1) <= (out["intraday_sweep_low"] == 1)).all()
    assert ((out["intraday_sweep_high_reject"] == 1) <= (out["intraday_sweep_high"] == 1)).all()


def test_value_area_brackets_poc():
    df = _synth_intraday()
    out = compute_intraday_microstructure(df)
    d = out.dropna(subset=["intraday_vp_poc", "intraday_vp_vah", "intraday_vp_val"])
    assert len(d) > 0
    assert (d["intraday_vp_val"] <= d["intraday_vp_poc"] + 1e-9).all()
    assert (d["intraday_vp_poc"] <= d["intraday_vp_vah"] + 1e-9).all()
    assert set(pd.unique(out["intraday_vp_in_value"])).issubset({0, 1})


def test_no_lookahead_within_session():
    """Causal columns on a prefix match the full computation over the overlap."""
    df = _synth_intraday().iloc[:120]  # single session
    cols = ["intraday_avwap", "intraday_ofi", "intraday_sweep_high",
            "intraday_sweep_low", "intraday_vp_poc"]
    full = compute_intraday_microstructure(df)[cols]
    prefix = compute_intraday_microstructure(df.iloc[:100])[cols]
    a = full.iloc[10:100].reset_index(drop=True)
    b = prefix.iloc[10:100].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_dtype=False, rtol=1e-9)
