"""
Intraday microstructure factors — the live, minute-bar analytics layer.

This is the *intraday* counterpart to the daily-bar SMC module
(qsde/factors/smc.py). It operates on 1-minute bars as produced by
qsde/ingestion/intraday_storage.py (the `ohlcv_intraday` table:
columns ts, open, high, low, close, volume, vwap, n_ticks) and emits a wide
DataFrame of `intraday_*` columns intended for the live signal loop
(qsde/live/engine.py, Phase 2) and the live charts (Phase 4).

Everything is **session-anchored** (re-anchored each trading day) and
**causal** (no use of future bars), so the same code is correct whether
called on a full backtest day or incrementally on the session-to-date frame
for the latest bar.

Feature groups & math
----------------------
1. Anchored VWAP + bands (per session, anchored at the session's first bar)
       tp_i      = (high_i + low_i + close_i) / 3                  (typical price)
       AVWAP_t   = Σ_{i=anchor..t} tp_i·v_i  /  Σ_{i=anchor..t} v_i
       var_t     = (Σ v_i·tp_i² / Σ v_i) − AVWAP_t²               (vol-weighted)
       band_t    = AVWAP_t ± k·sqrt(max(var_t, 0))
       dev_t     = (close_t − AVWAP_t) / AVWAP_t                  (signed %)

2. Order-flow imbalance (tick-rule proxy on bar closes) + cumulative delta
       sign_i        = sign(close_i − close_{i-1})                (per session)
       signed_vol_i  = sign_i · volume_i
       OFI_t         = Σ_{N} signed_vol / Σ_{N} volume   ∈ [−1, 1]
       CVD_t         = Σ_{anchor..t} signed_vol                   (running delta)
   NOTE: this is the bar-direction proxy. True executed-aggressor order flow
   needs per-tick classification (Kite MODE_FULL depth / ticks_raw); that is a
   later enhancement. Order-book pressure from total_buy/sell_quantity is a
   separate optional input.

3. Liquidity sweeps (intraday stop-hunts) + VWAP-reclaim confirmation
       prior_hi  = rolling max of prior `lookback` highs (this session)
       sweep_hi  = high_t > prior_hi AND close_t < prior_hi       (bull trap)
       sweep_lo  = low_t  < prior_lo AND close_t > prior_lo       (bear trap)
       sweep_lo_reclaim = sweep_lo AND close_t > AVWAP_t          (bullish)
       sweep_hi_reject  = sweep_hi AND close_t < AVWAP_t          (bearish)

4. Session volume profile (POC / VAH / VAL), expanding from the session open
       Vk        = Σ volume in price-bin k over bars [anchor..t]
       POC_t     = bin price with max Vk
       VA        = smallest [k1,k2] band s.t. ΣVk ≥ 0.70·V_total → VAL, VAH
   Expanding (not future-peeking): the profile at bar t uses only bars ≤ t.

All public functions take a single-symbol minute-bar DataFrame with a
DatetimeIndex and columns open/high/low/close/volume.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_IST = "Asia/Kolkata"


# ============================================================
# Session keys
# ============================================================
def _session_key(df: pd.DataFrame, tz: str = _IST) -> pd.Series:
    """Per-row session id (the local trading date), aligned to df.index.

    If the index is tz-aware it is converted to `tz` (default IST) before
    taking the date, so a UTC-stored bar at 03:50Z maps to the correct IST
    session. tz-naive indices (e.g. unit tests) are treated as already-local.
    Non-datetime indices collapse to a single session.
    """
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        local = idx.tz_convert(tz) if idx.tz is not None else idx
        return pd.Series(local.date, index=df.index, name="session")
    return pd.Series(np.zeros(len(df), dtype=int), index=df.index, name="session")


# ============================================================
# 1. Anchored VWAP + bands
# ============================================================
def anchored_vwap(df: pd.DataFrame, k: float = 2.0, tz: str = _IST) -> pd.DataFrame:
    """Session-anchored VWAP, ±k·σ bands, and signed deviation %."""
    key = _session_key(df, tz)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    v = df["volume"].astype(float)

    cum_v = v.groupby(key, sort=False).transform("cumsum")
    cum_pv = (tp * v).groupby(key, sort=False).transform("cumsum")
    cum_pv2 = (tp * tp * v).groupby(key, sort=False).transform("cumsum")

    safe_v = cum_v.replace(0, np.nan)
    avwap = cum_pv / safe_v
    var = (cum_pv2 / safe_v) - avwap ** 2
    std = np.sqrt(var.clip(lower=0))

    out = pd.DataFrame(index=df.index)
    out["intraday_avwap"] = avwap
    out["intraday_avwap_upper"] = avwap + k * std
    out["intraday_avwap_lower"] = avwap - k * std
    out["intraday_avwap_dev"] = (df["close"] - avwap) / avwap.replace(0, np.nan)
    return out


# ============================================================
# 2. Order-flow imbalance + cumulative volume delta
# ============================================================
def order_flow(df: pd.DataFrame, window: int = 14, tz: str = _IST) -> pd.DataFrame:
    """Bar-direction order-flow imbalance (OFI) in [-1,1] + session CVD."""
    key = _session_key(df, tz)
    v = df["volume"].astype(float)

    dclose = df["close"].groupby(key, sort=False).transform(lambda s: s.diff())
    sign = np.sign(dclose).fillna(0.0)
    signed_vol = sign * v

    num = signed_vol.groupby(key, sort=False).transform(
        lambda s: s.rolling(window, min_periods=1).sum()
    )
    den = v.groupby(key, sort=False).transform(
        lambda s: s.rolling(window, min_periods=1).sum()
    )
    ofi = (num / den.replace(0, np.nan)).clip(-1, 1)
    cvd = signed_vol.groupby(key, sort=False).transform("cumsum")

    out = pd.DataFrame(index=df.index)
    out["intraday_ofi"] = ofi.fillna(0.0)
    out["intraday_cvd"] = cvd.fillna(0.0)
    out["intraday_signed_vol"] = signed_vol
    return out


# ============================================================
# 3. Liquidity sweeps + VWAP-reclaim confirmation
# ============================================================
def liquidity_sweeps_intraday(
    df: pd.DataFrame,
    lookback: int = 20,
    avwap: pd.Series | None = None,
    tz: str = _IST,
) -> pd.DataFrame:
    """Intraday stop-hunt sweeps, session-aware, with optional VWAP reclaim.

    A sweep pierces a recent extreme and closes back inside it (a trap). When
    the closing price reclaims the anchored VWAP in the opposite direction, the
    reversal is higher-conviction -- those are the `*_reclaim` / `*_reject` cols.
    """
    key = _session_key(df, tz)

    prior_hi = df["high"].groupby(key, sort=False).transform(
        lambda s: s.shift(1).rolling(lookback, min_periods=3).max()
    )
    prior_lo = df["low"].groupby(key, sort=False).transform(
        lambda s: s.shift(1).rolling(lookback, min_periods=3).min()
    )

    sweep_hi = ((df["high"] > prior_hi) & (df["close"] < prior_hi)).astype(int)
    sweep_lo = ((df["low"] < prior_lo) & (df["close"] > prior_lo)).astype(int)

    out = pd.DataFrame(index=df.index)
    out["intraday_sweep_high"] = sweep_hi.fillna(0).astype(int)
    out["intraday_sweep_low"] = sweep_lo.fillna(0).astype(int)

    if avwap is not None:
        above = df["close"] > avwap
        out["intraday_sweep_low_reclaim"] = (
            (out["intraday_sweep_low"] == 1) & above
        ).astype(int)
        out["intraday_sweep_high_reject"] = (
            (out["intraday_sweep_high"] == 1) & ~above
        ).astype(int)
    else:
        out["intraday_sweep_low_reclaim"] = 0
        out["intraday_sweep_high_reject"] = 0
    return out


# ============================================================
# 4. Session volume profile (expanding POC / VAH / VAL)
# ============================================================
def _profile_levels(tp: np.ndarray, vol: np.ndarray, bins: int, va_frac: float):
    """POC / VAH / VAL for one expanding window of typical prices + volumes."""
    lo, hi = tp.min(), tp.max()
    if hi <= lo or vol.sum() <= 0:
        return np.nan, np.nan, np.nan
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.searchsorted(edges, tp, side="right") - 1, 0, bins - 1)
    counts = np.zeros(bins)
    np.add.at(counts, idx, vol)
    poc_bin = int(np.argmax(counts))
    poc = 0.5 * (edges[poc_bin] + edges[poc_bin + 1])
    target = va_frac * counts.sum()
    low_b = high_b = poc_bin
    acc = counts[poc_bin]
    while acc < target and (low_b > 0 or high_b < bins - 1):
        left = counts[low_b - 1] if low_b > 0 else -1.0
        right = counts[high_b + 1] if high_b < bins - 1 else -1.0
        if right >= left:
            high_b += 1
            acc += counts[high_b]
        else:
            low_b -= 1
            acc += counts[low_b]
    return poc, edges[high_b + 1], edges[low_b]


def session_volume_profile(
    df: pd.DataFrame,
    bins: int = 50,
    va_frac: float = 0.70,
    min_bars: int = 5,
    tz: str = _IST,
) -> pd.DataFrame:
    """Expanding-from-open session volume profile. POC/VAH/VAL per bar.

    For each bar t, the profile uses only bars from the session open up to and
    including t (causal). O(bars²) within a session; fine for one day or an
    incremental live call. The live loop computes only the latest bar.
    """
    key = _session_key(df, tz)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).to_numpy()
    vol = df["volume"].astype(float).to_numpy()

    n = len(df)
    poc = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)

    # iterate session by session over positional ranges
    sess = key.to_numpy()
    start = 0
    for i in range(1, n + 1):
        if i == n or sess[i] != sess[start]:
            # session is [start, i)
            for j in range(start, i):
                local = j - start + 1
                if local < min_bars:
                    continue
                p, h, l = _profile_levels(
                    tp[start : j + 1], vol[start : j + 1], bins, va_frac
                )
                poc[j], vah[j], val[j] = p, h, l
            start = i

    out = pd.DataFrame(index=df.index)
    out["intraday_vp_poc"] = poc
    out["intraday_vp_vah"] = vah
    out["intraday_vp_val"] = val
    out["intraday_vp_poc_dist"] = (df["close"] - out["intraday_vp_poc"]) / df["close"]
    out["intraday_vp_in_value"] = (
        (df["close"] >= out["intraday_vp_val"])
        & (df["close"] <= out["intraday_vp_vah"])
    ).astype(int)
    return out


# ============================================================
# Top-level aggregator
# ============================================================
def compute_intraday_microstructure(
    df: pd.DataFrame,
    *,
    k_bands: float = 2.0,
    ofi_window: int = 14,
    sweep_lookback: int = 20,
    vp_bins: int = 50,
    tz: str = _IST,
) -> pd.DataFrame:
    """
    Given a single-symbol minute-bar OHLCV DataFrame (DatetimeIndex; cols
    open/high/low/close/volume), return the wide `intraday_*` feature frame:
    anchored VWAP + bands + dev, order-flow imbalance + CVD, liquidity sweeps
    (+ VWAP reclaim/reject), and session volume profile (POC/VAH/VAL).
    """
    required = {"open", "high", "low", "close", "volume"}
    cols = set(c.lower() for c in df.columns)
    missing = required - cols
    if missing:
        raise ValueError(f"compute_intraday_microstructure missing cols: {missing}")
    df = df.rename(columns=str.lower).copy()
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    avwap_df = anchored_vwap(df, k=k_bands, tz=tz)
    of_df = order_flow(df, window=ofi_window, tz=tz)
    sweeps_df = liquidity_sweeps_intraday(
        df, lookback=sweep_lookback, avwap=avwap_df["intraday_avwap"], tz=tz
    )
    vp_df = session_volume_profile(df, bins=vp_bins, tz=tz)

    return pd.concat([avwap_df, of_df, sweeps_df, vp_df], axis=1)


__all__ = [
    "anchored_vwap",
    "order_flow",
    "liquidity_sweeps_intraday",
    "session_volume_profile",
    "compute_intraday_microstructure",
]
