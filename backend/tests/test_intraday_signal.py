"""
Tests for qsde/live/intraday_signal.generate_intraday_signal (Phase 2 core).

Synthetic single-session minute bars with controlled drift to assert that a
clean uptrend -> long, downtrend -> short, levels are internally consistent,
and the optional ML-alpha term nudges the bias.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.live.intraday_signal import generate_intraday_signal


def _session(drift: float, bars: int = 90, seed: int = 3, vol_noise: float = 0.0006) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-03-01 09:15", periods=bars, freq="1min")
    rets = rng.normal(drift, 0.0004, bars)
    close = 1000.0 * np.exp(np.cumsum(rets))
    open_ = np.r_[close[0], close[:-1]]
    noise = np.abs(rng.normal(0, vol_noise, bars)) * close
    high = np.maximum(open_, close) + noise
    low = np.minimum(open_, close) - noise
    vol = rng.integers(2000, 30000, bars).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_bullish_session_goes_long():
    sig = generate_intraday_signal(_session(drift=0.0015), symbol="TEST")
    assert sig.direction == 1
    assert sig.action in ("BUY", "WATCH")
    assert sig.bias > 0
    # Long levels: stop below entry, target above entry.
    assert sig.entry is not None and sig.stop is not None and sig.target is not None
    assert sig.stop < sig.entry < sig.target
    assert sig.risk_reward is not None and sig.risk_reward > 0
    assert sig.reasons  # non-empty rationale


def test_strong_bullish_clears_quality_bar():
    """A high-conviction uptrend should fire an actual BUY (R:R >= 1.5)."""
    sig = generate_intraday_signal(_session(drift=0.0020, vol_noise=0.0003), symbol="TEST")
    assert sig.direction == 1
    assert sig.risk_reward >= 1.5
    assert sig.action == "BUY"
    assert sig.quality == "good"


def test_bearish_session_goes_short():
    sig = generate_intraday_signal(_session(drift=-0.0015), symbol="TEST")
    assert sig.direction == -1
    assert sig.action in ("SELL", "WATCH")
    assert sig.bias < 0
    # Short levels: stop above entry, target below entry.
    assert sig.stop > sig.entry > sig.target


def test_alpha_blend_raises_bias():
    bars = _session(drift=0.0003)  # mild
    base = generate_intraday_signal(bars, symbol="TEST", alpha_weight=0.0)
    blended = generate_intraday_signal(
        bars, symbol="TEST", alpha_pct=0.95, alpha_weight=0.30
    )
    assert blended.bias > base.bias


def test_serializable():
    sig = generate_intraday_signal(_session(drift=0.0015), symbol="KEI")
    d = sig.to_dict()
    assert d["symbol"] == "KEI"
    assert set(["direction", "action", "entry", "stop", "target", "risk_reward", "reasons"]).issubset(d)
    import json
    json.dumps(d)  # must be JSON-serializable for SSE / Telegram
