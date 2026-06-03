"""
Tests for the ported regime engine (qsde/models/regime_engine.py).

Skips gracefully if scikit-learn is not installed in the venv (the engine's
KMeans/classifier need it). Synthetic index series with a calm stretch and a
volatile/drawdown stretch so multiple regimes are plausible.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.models.regime_engine import STATE_NAMES, extract_regime_features, fit_predict


def _sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except Exception:
        return False


def _synth_index(n: int = 900, seed: int = 17) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    # calm first half (low vol drift up), turbulent second half (high vol, drawdown)
    vol = np.where(np.arange(n) < n // 2, 0.008, 0.022)
    drift = np.where(np.arange(n) < n // 2, 0.0005, -0.0008)
    rets = rng.normal(drift, vol)
    close = 15000 * np.exp(np.cumsum(rets))
    vix = pd.Series(12 + 40 * pd.Series(rets, index=idx).rolling(20).std().fillna(0), index=idx)
    return pd.Series(close, index=idx, name="close"), vix


def test_feature_extraction():
    close, vix = _synth_index()
    feats = extract_regime_features(close, vix=vix)
    assert not feats.empty
    for c in ("feat_vol_20", "feat_vol_60", "feat_trend", "feat_dd", "feat_shock"):
        assert c in feats.columns
    assert np.isfinite(feats.to_numpy()).all()


def test_fit_predict_states_and_probs():
    if not _sklearn_available():
        print("SKIP test_fit_predict_states_and_probs (scikit-learn not installed)")
        return
    close, vix = _synth_index()
    result, engine = fit_predict(close, vix=vix)
    assert not result.empty
    assert "regime_state" in result.columns
    assert set(result["regime_state"].dropna().unique()).issubset(set(STATE_NAMES))

    prob_cols = [f"regime_prob_{s}" for s in STATE_NAMES]
    sums = result[prob_cols].sum(axis=1)
    assert np.allclose(sums.to_numpy(), 1.0, atol=1e-6)

    # one-step forecast distribution also present and normalized
    next_cols = [f"regime_next_prob_{s}" for s in STATE_NAMES]
    assert np.allclose(result[next_cols].sum(axis=1).to_numpy(), 1.0, atol=1e-6)
