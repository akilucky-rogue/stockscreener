"""
Market Regime Engine — classifier + Markov + temporal stabilizer.

Ported into QSDE from StockTrack/stocktrack/models/regime_engine.py (Phase 0.3
consolidation). qsde/CLAUDE.md references a 5-state regime engine; this is it.
Framework-agnostic: only numpy/pandas at import time; sklearn (KMeans / PCA /
HistGradientBoosting) is imported lazily inside fit/predict, and xgboost / torch
/ hmmlearn are optional with graceful fallbacks.

Used by the inference/live layers to condition entries on the market system
(e.g. demand cleaner setups in RISK_OFF / VOLATILE_TREND). White-box: the
risk-conditional overrides are explicit, auditable rules (SEBI 2026).

Pipeline (per bar)
------------------
1. Feature extraction from the index series (^NSEI close, optional ^INDIAVIX,
   optional breadth panel): realised vol (20d/60d), trend strength, drawdown
   pressure, correlation stress, shock intensity.
2. Instantaneous classifier P(state | features): XGBoost multiclass, soft-fail
   to sklearn HistGradientBoosting. Initial labels seeded by KMeans.
3. Markov transition matrix P(s_{t+1}|s_t), Dirichlet-smoothed.
4. Temporal stabilizer: forward-pass HMM-style smoothing + EMA polish.
5. Risk-conditional overrides (hard, auditable rules).
6. Phase-space PCA projection + vector field (for the UI).

States: 0 CALM_TREND, 1 VOLATILE_TREND, 2 CHOP, 3 RISK_OFF, 4 BREAKOUT.

Public API
----------
    RegimeEngine.fit(features)            # build classifier + transition mat
    RegimeEngine.predict(features)        # DataFrame with state + probs
    extract_regime_features(index_close, breadth_close=, vix=)
    fit_predict(index_close, breadth_close=, vix=)
    phase_space_projection(prob_df) / phase_space_vector_field(xy)
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

STATE_NAMES = ["CALM_TREND", "VOLATILE_TREND", "CHOP", "RISK_OFF", "BREAKOUT"]
N_STATES = len(STATE_NAMES)


# ============================================================
# 1. Feature extraction
# ============================================================
def _rolling_adx(close: pd.Series, window: int = 14) -> pd.Series:
    """Simplified ADX-like trend-strength from close-only input."""
    delta = close.diff()
    up = delta.clip(lower=0)
    dn = (-delta).clip(lower=0)
    atr = delta.abs().rolling(window).mean()
    plus = up.rolling(window).mean() / atr
    minus = dn.rolling(window).mean() / atr
    dx = (plus - minus).abs() / (plus + minus + 1e-9)
    return dx.rolling(window).mean().fillna(0.0)


def extract_regime_features(
    index_close: pd.Series,
    *,
    breadth_close: Optional[pd.DataFrame] = None,
    vix: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Build the 5-group regime feature matrix (see module docstring)."""
    idx = index_close.astype(float)
    ret = idx.pct_change()
    out = pd.DataFrame(index=idx.index)

    # 1) realised vol
    out["feat_vol_20"] = ret.rolling(20).std() * np.sqrt(252)
    out["feat_vol_60"] = ret.rolling(60).std() * np.sqrt(252)
    if vix is not None and len(vix) > 0:
        out["feat_vix"] = vix.reindex(idx.index).ffill() / 100.0
    else:
        out["feat_vix"] = out["feat_vol_60"]

    # 2) trend strength
    out["feat_trend"] = _rolling_adx(idx, 14)

    # 3) drawdown pressure
    roll_peak = idx.rolling(120, min_periods=30).max()
    out["feat_dd"] = -(idx / roll_peak - 1.0).clip(upper=0)

    # 4) correlation stress
    if breadth_close is not None and not breadth_close.empty:
        pct = breadth_close.pct_change()
        roll = pct.rolling(20).corr()
        mean_corr = (
            roll.groupby(level=0).mean().mean(axis=1)
            if isinstance(roll.index, pd.MultiIndex)
            else pct.rolling(20).corr().mean(axis=1)
        )
        out["feat_corr_stress"] = mean_corr.reindex(idx.index).ffill().fillna(0.0)
    else:
        out["feat_corr_stress"] = ret.rolling(20).apply(
            lambda x: np.corrcoef(x[:-1], x[1:])[0, 1] if len(x) > 5 else 0.0, raw=True
        ).fillna(0.0)

    # 5) shock intensity
    vol60 = ret.rolling(60).std().replace(0, np.nan)
    out["feat_shock"] = (ret.abs() / vol60).fillna(0.0).clip(upper=6.0)

    return out.dropna().astype(float)


# ============================================================
# 2. Instantaneous classifier (XGB → soft-fail HGBT)
# ============================================================
def _init_classifier():
    try:
        from xgboost import XGBClassifier

        return XGBClassifier(
            n_estimators=300,
            max_depth=4,
            learning_rate=0.05,
            objective="multi:softprob",
            num_class=N_STATES,
            subsample=0.8,
            colsample_bytree=0.8,
            eval_metric="mlogloss",
            verbosity=0,
            tree_method="hist",
        )
    except Exception:
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05)


# ============================================================
# 3. Markov transition matrix
# ============================================================
def _fit_transition_matrix(states: np.ndarray, n: int = N_STATES, alpha: float = 1.0) -> np.ndarray:
    """Dirichlet-smoothed transition count matrix."""
    mat = np.full((n, n), alpha, dtype=float)
    for a, b in zip(states[:-1], states[1:]):
        mat[int(a), int(b)] += 1.0
    mat = mat / mat.sum(axis=1, keepdims=True)
    return mat


# ============================================================
# 4. Temporal stabilizer (forward-pass + EMA)
# ============================================================
def _smooth_probs(probs: np.ndarray, trans: np.ndarray, halflife: int = 5) -> np.ndarray:
    """Approximate forward-pass HMM smoothing + EMA polish."""
    T, K = probs.shape
    alpha = np.zeros_like(probs)
    alpha[0] = probs[0] / probs[0].sum()
    for t in range(1, T):
        pred = alpha[t - 1] @ trans
        obs = probs[t]
        alpha[t] = pred * obs
        s = alpha[t].sum()
        if s > 0:
            alpha[t] /= s
        else:
            alpha[t] = pred

    lam = 0.5 ** (1 / halflife)
    smoothed = np.zeros_like(alpha)
    smoothed[0] = alpha[0]
    for t in range(1, T):
        smoothed[t] = lam * smoothed[t - 1] + (1 - lam) * alpha[t]
        s = smoothed[t].sum()
        if s > 0:
            smoothed[t] /= s
    return smoothed


# ============================================================
# 5. Risk-conditional overrides
# ============================================================
def _apply_risk_rules(df: pd.DataFrame, state_col: str = "regime_state_id") -> pd.DataFrame:
    """Hard, auditable overrides on top of the stabilized state."""
    vol60 = df["feat_vol_60"]
    vol_median = vol60.rolling(504, min_periods=60).median()
    dd = df["feat_dd"]

    state = df[state_col].copy()

    # Rule 1: RISK_OFF requires confirmed stress
    risk_off_mask = state == 3
    confirm = (vol60 > 1.5 * vol_median) & (dd > 0.08)
    demote = risk_off_mask & ~confirm
    state.loc[demote] = 1  # → VOLATILE_TREND

    # Rule 2: dampen CHOP when BOS/CHoCH is firing elsewhere
    if "bos_fire" in df.columns:
        chop_mask = state == 2
        breakout_nearby = df["bos_fire"].rolling(3).sum() > 0
        state.loc[chop_mask & breakout_nearby] = 4  # → BREAKOUT

    # Rule 3: BREAKOUT overextended → flag + rebalance to VOLATILE_TREND
    close_mu = df["feat_trend"].rolling(50).mean()
    close_sd = df["feat_trend"].rolling(50).std().replace(0, np.nan)
    z = (df["feat_trend"] - close_mu) / close_sd
    overext = (state == 4) & (z > 3)
    state.loc[overext] = 1

    df["regime_overextended"] = overext.astype(int)
    df["regime_state_id_final"] = state
    df["regime_state"] = state.map(lambda i: STATE_NAMES[int(i)] if pd.notna(i) else None)
    return df


# ============================================================
# 6. Phase-space projection + vector field
# ============================================================
def phase_space_projection(prob_df: pd.DataFrame) -> pd.DataFrame:
    """Project stabilized probability vectors onto 2-D via PCA."""
    from sklearn.decomposition import PCA

    P = prob_df[[f"regime_prob_{s}" for s in STATE_NAMES]].values
    if len(P) < 3:
        return pd.DataFrame({"x": np.zeros(len(P)), "y": np.zeros(len(P))}, index=prob_df.index)
    coords = PCA(n_components=2, random_state=42).fit_transform(P)
    return pd.DataFrame({"x": coords[:, 0], "y": coords[:, 1]}, index=prob_df.index)


def phase_space_vector_field(xy: pd.DataFrame, grid: int = 12) -> pd.DataFrame:
    """Mean (dx, dy) per grid cell over the trajectory — arrows for the UI."""
    if len(xy) < 5:
        return pd.DataFrame(columns=["x", "y", "dx", "dy", "count"])
    x, y = xy["x"].values, xy["y"].values
    dx = np.diff(x, append=x[-1])
    dy = np.diff(y, append=y[-1])
    xb = np.linspace(x.min(), x.max(), grid + 1)
    yb = np.linspace(y.min(), y.max(), grid + 1)
    xi = np.clip(np.searchsorted(xb, x) - 1, 0, grid - 1)
    yi = np.clip(np.searchsorted(yb, y) - 1, 0, grid - 1)
    agg: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    for i in range(len(x)):
        agg.setdefault((xi[i], yi[i]), []).append((dx[i], dy[i]))
    rows = []
    for (gi, gj), deltas in agg.items():
        d = np.array(deltas)
        rows.append(
            {
                "x": 0.5 * (xb[gi] + xb[gi + 1]),
                "y": 0.5 * (yb[gj] + yb[gj + 1]),
                "dx": d[:, 0].mean(),
                "dy": d[:, 1].mean(),
                "count": len(d),
            }
        )
    return pd.DataFrame(rows)


# ============================================================
# 7. Engine class
# ============================================================
@dataclass
class RegimeEngine:
    """Fit-predict-stabilize regime inference engine."""

    n_states: int = N_STATES
    stabilizer_halflife: int = 5
    classifier: object = field(default=None)
    transition: Optional[np.ndarray] = None
    feature_cols: Optional[List[str]] = None
    kmeans_centers: Optional[np.ndarray] = None

    def _initial_labels(self, X: np.ndarray) -> np.ndarray:
        """KMeans over the feature matrix seeds initial regime labels."""
        from sklearn.cluster import KMeans

        km = KMeans(n_clusters=self.n_states, n_init=10, random_state=42).fit(X)
        self.kmeans_centers = km.cluster_centers_
        # Re-order clusters so 0 = low-vol/low-trend ... rank by (vol + trend).
        order = np.argsort(km.cluster_centers_[:, 0] + km.cluster_centers_[:, 3])
        remap = {old: new for new, old in enumerate(order)}
        return np.array([remap[l] for l in km.labels_])

    def fit(self, features: pd.DataFrame) -> "RegimeEngine":
        if features.empty:
            raise ValueError("RegimeEngine.fit: empty feature frame")
        self.feature_cols = [c for c in features.columns if c.startswith("feat_")]
        X = features[self.feature_cols].values
        y = self._initial_labels(X)
        self.classifier = _init_classifier()
        self.classifier.fit(X, y)
        self.transition = _fit_transition_matrix(y, n=self.n_states)
        return self

    def predict(self, features: pd.DataFrame) -> pd.DataFrame:
        if self.classifier is None or self.transition is None or self.feature_cols is None:
            raise RuntimeError("RegimeEngine.predict before fit()")
        X = features[self.feature_cols].values
        probs = self.classifier.predict_proba(X) if hasattr(self.classifier, "predict_proba") else None
        if probs is None:
            pred = self.classifier.predict(X)
            probs = np.zeros((len(pred), self.n_states))
            probs[np.arange(len(pred)), pred] = 1.0

        smoothed = _smooth_probs(probs, self.transition, halflife=self.stabilizer_halflife)
        state_id = smoothed.argmax(axis=1)

        out = features.copy()
        for i, name in enumerate(STATE_NAMES):
            out[f"regime_prob_{name}"] = smoothed[:, i]
        out["regime_state_id"] = state_id
        out = _apply_risk_rules(out)

        next_probs = smoothed @ self.transition
        for i, name in enumerate(STATE_NAMES):
            out[f"regime_next_prob_{name}"] = next_probs[:, i]
        return out


# ============================================================
# 8. Convenience entry-point
# ============================================================
def fit_predict(
    index_close: pd.Series,
    *,
    breadth_close: Optional[pd.DataFrame] = None,
    vix: Optional[pd.Series] = None,
) -> Tuple[pd.DataFrame, RegimeEngine]:
    feats = extract_regime_features(index_close, breadth_close=breadth_close, vix=vix)
    engine = RegimeEngine().fit(feats)
    result = engine.predict(feats)
    return result, engine


__all__ = [
    "STATE_NAMES",
    "N_STATES",
    "extract_regime_features",
    "RegimeEngine",
    "fit_predict",
    "phase_space_projection",
    "phase_space_vector_field",
]
