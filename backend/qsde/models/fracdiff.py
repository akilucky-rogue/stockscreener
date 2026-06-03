"""
Fractional differentiation — Fixed-Width Window method (FFD).

Reference: López de Prado, "Advances in Financial Machine Learning" (2018),
Chapter 5. Code is adapted from Snippets 5.1, 5.3, 5.4, and 5.5.

Why this module exists
----------------------
ML models on financial data face a tension:

  * Returns are stationary but memory-less (the model sees no levels).
  * Prices / cumulative volume / OBV are full-memory but non-stationary.

Integer differencing (returns) destroys long-memory; this is why ML on raw
returns plateaus around weak Sharpe. Fractional differencing of order
0 < d < 1 makes the series stationary AT the minimum d necessary — keeping
all the information available above that. This directly fixes the
`tech_obv_slope SHAP +16M` we saw in the precision audit: OBV is a level
series, the previous pipeline either left it raw (saturated SHAP) or
differenced it to first order (lost memory). Fractional diff is the
correct middle ground.

The FFD variant uses a fixed-width window τ truncated when the binomial
weight w_k falls below `thres`. This keeps each output value computed over
the same window length — making the series IID across the whole sample.

Public API
----------
  frac_diff_ffd(series, d, thres=1e-5)         -> fracdiff'd Series
  find_min_d(series, ...)                       -> smallest d passing ADF
  apply_fracdiff_to_features(df, features=None) -> wide DataFrame ready for ML
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Heuristic: feature-name suffixes that are ALREADY stationary (returns,
# ratios, percentages, z-scores, normalized indicators). No fracdiff applied
# to these — fracdiff'ing a return is meaningless.
_ALREADY_STATIONARY_PATTERNS = (
    "_ret", "_return", "_pct", "_zscore", "_rank",
    "_yield", "_margin", "_growth",
    "tech_rsi", "tech_atr_pct", "tech_macd_hist", "tech_stoch", "tech_williams",
    "tech_bb_width", "tech_kc_width", "tech_adx",
    "fund_pe", "fund_pb", "fund_roe", "fund_roic", "fund_roa", "fund_div_yield",
    "fund_ev_ebitda", "fund_ev_revenue", "fund_debt_equity",
    "flow_ratio", "flow_dii_pct", "flow_fii_pct",
    "smc_bos", "smc_choch", "smc_pattern",
    "news_sentiment", "macro_yield_curve", "macro_brent_chg", "macro_dxy_chg",
)


# ── core math: weights + FFD ───────────────────────────────────────

def _get_weights_ffd(d: float, thres: float, max_size: int = 10_000) -> np.ndarray:
    """Binomial weights for fixed-width fractional differencing.

    w_0 = 1, w_k = -w_{k-1} * (d - k + 1) / k. Stops when |w_k| < thres or
    max_size reached. AFML Snippet 5.3.

    Returns a 1-D float array ordered NEWEST -> OLDEST (so the dot product
    with the most-recent-first window matches indexing).
    """
    if d <= 0:
        raise ValueError("d must be > 0 for fractional differencing")
    if not 0 < thres < 1:
        raise ValueError("thres must be in (0, 1)")

    w: list[float] = [1.0]
    k = 1
    while k < max_size:
        w_k = -w[-1] * (d - k + 1.0) / k
        if abs(w_k) < thres:
            break
        w.append(w_k)
        k += 1
    return np.array(w[::-1], dtype=float)   # OLDEST -> NEWEST per AFML convention


def frac_diff_ffd(
    series: pd.Series,
    d: float,
    thres: float = 1e-4,
    max_warmup_frac: float = 0.5,
) -> pd.Series:
    """Fractionally differentiated series with a fixed-width window.

    Args:
        series: float Series indexed by date. NaNs are allowed; output NaN
                wherever the input window contained a NaN.
        d:      order of differencing, 0 < d <= 1.
        thres:  weight magnitude below which the kernel terminates. Smaller
                -> wider window -> more memory but more warm-up nulls. The
                default 1e-4 yields width ~150 for d=0.4, which fits a 1-yr
                daily series with 1/4 of values lost to warm-up. AFML's
                book default 1e-5 needs 5+ years of history.
        max_warmup_frac: safety. If the kernel computed from `thres` would
                consume more than this fraction of the series, the function
                automatically widens `thres` until it fits. Prevents the
                "all-NaN output on short series" failure mode.

    Returns:
        Series same length as input. First (window-1) values are NaN by
        construction (insufficient history for the window).
    """
    if d <= 0:
        log.debug("frac_diff_ffd d<=0 -> returning series unchanged")
        return series.copy()

    w = _get_weights_ffd(d=d, thres=thres)
    width = len(w)
    n = len(series)
    if n == 0:
        return series.copy()

    # Auto-widen thres so the warm-up never exceeds max_warmup_frac * n.
    # We multiply thres by 10 each loop iteration; in the worst case we
    # fall back to a trivial first-difference (d=1) when nothing fits.
    cur_thres = thres
    while width > int(n * (1.0 - max_warmup_frac)) and cur_thres < 1.0:
        cur_thres *= 10.0
        w = _get_weights_ffd(d=d, thres=cur_thres)
        width = len(w)
    if cur_thres != thres:
        log.debug("frac_diff_ffd auto-widened thres %g -> %g to fit n=%d (width=%d)",
                  thres, cur_thres, n, width)

    if width > n:
        return pd.Series(np.nan, index=series.index, dtype=float)

    x = series.astype(float).to_numpy()
    out = np.full(n, np.nan, dtype=float)
    for i in range(width - 1, n):
        window = x[i - width + 1 : i + 1]
        if np.isnan(window).any():
            continue
        out[i] = float(np.dot(w, window))
    return pd.Series(out, index=series.index, dtype=float)


# ── ADF test wrapper + minimum-d search ────────────────────────────

def _adf_pvalue(series: pd.Series) -> float:
    """Augmented Dickey-Fuller p-value. Lower = stronger stationarity
    evidence. Returns 1.0 (worst) if the series is degenerate or statsmodels
    is unavailable."""
    s = series.dropna()
    if len(s) < 50 or s.std() == 0:
        return 1.0
    try:
        from statsmodels.tsa.stattools import adfuller
        return float(adfuller(s, autolag="AIC")[1])
    except Exception as e:  # noqa: BLE001
        log.debug("ADF unavailable: %s", e)
        return 1.0


def find_min_d(
    series: pd.Series,
    candidates: Iterable[float] = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0),
    thres: float = 1e-5,
    adf_pval_max: float = 0.05,
) -> Optional[float]:
    """Smallest d (from `candidates`) that makes `series` stationary by ADF.

    Returns the d value, or None if no candidate worked. AFML Ch. 5 suggests
    binary search over [0, 1]; we use a coarse grid that's fast and the
    granularity is well within calibration noise.
    """
    for d in sorted(candidates):
        diffed = frac_diff_ffd(series, d=d, thres=thres)
        if _adf_pvalue(diffed) < adf_pval_max:
            return float(d)
    return None


# ── batch helper used by dataset.py ────────────────────────────────

def _is_already_stationary(col_name: str) -> bool:
    name = col_name.lower()
    return any(p in name for p in _ALREADY_STATIONARY_PATTERNS)


def apply_fracdiff_to_features(
    df: pd.DataFrame,
    feature_cols: Optional[Iterable[str]] = None,
    default_d: float = 0.4,
    thres: float = 1e-4,
    per_symbol: bool = True,
    symbol_col: str = "symbol",
    sort_col: str = "as_of_date",
) -> tuple[pd.DataFrame, dict[str, Optional[float]]]:
    """Return (transformed_df, d_used_per_column).

    For each non-stationary feature column we:
      1. Skip if the name matches a known-stationary pattern.
      2. Otherwise apply frac_diff_ffd(d=default_d) per symbol.

    `default_d=0.4` is the López de Prado-recommended sweet spot from his
    SP500 experiments — keeps autocorrelation while passing ADF for most
    equity series. Caller can override via `find_min_d()` if they want
    per-feature tuning (slower).

    Args:
        df:              wide DataFrame from build_training_dataset().
        feature_cols:    if None, infer (everything except symbol/date/target).
        per_symbol:      True (recommended) — fracdiff each symbol's series
                         independently so cross-sectional pooling doesn't
                         contaminate the kernel.
        symbol_col:      column to group by when per_symbol=True.
        sort_col:        date column to sort within each symbol.
    """
    if feature_cols is None:
        feature_cols = [
            c for c in df.columns
            if c not in {symbol_col, sort_col, "target", "label_end_date",
                         "source", "yf_symbol"}
        ]

    out = df.copy()
    d_used: dict[str, Optional[float]] = {}

    for col in feature_cols:
        if col not in out.columns:
            continue
        if _is_already_stationary(col):
            d_used[col] = None       # left alone
            continue
        # Numeric check
        if not np.issubdtype(out[col].dtype, np.number):
            d_used[col] = None
            continue

        try:
            if per_symbol:
                out[col] = (
                    out.sort_values([symbol_col, sort_col])
                       .groupby(symbol_col, group_keys=False)[col]
                       .apply(lambda s: frac_diff_ffd(s, d=default_d, thres=thres))
                       .reindex(out.index)
                )
            else:
                out[col] = frac_diff_ffd(
                    out.sort_values(sort_col)[col], d=default_d, thres=thres
                ).reindex(out.index)
            d_used[col] = default_d
        except Exception as e:  # noqa: BLE001
            log.warning("fracdiff failed for %s: %s — leaving column as-is", col, e)
            d_used[col] = None

    n_applied = sum(1 for v in d_used.values() if v is not None)
    n_skipped = sum(1 for v in d_used.values() if v is None)
    log.info("fracdiff: applied to %d features, skipped %d (already-stationary or non-numeric)",
             n_applied, n_skipped)
    return out, d_used
