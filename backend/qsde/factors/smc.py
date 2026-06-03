"""
Smart Money Concepts (SMC) / ICT / Price-Action factor module.

Ported into QSDE from StockTrack/stocktrack/factors/smc.py (consolidation,
Phase 0.3). Computes per-bar structural and liquidity features that complement
the classical technical factors in qsde/factors/technical.py. Every output is a
snake_case column prefixed `smc_`, lookback-safe, and free of forward-looking
peeks. Wired into qsde/factors/engine.py:compute_factors_for_symbol.

Feature groups
--------------
1. Market structure         : BOS / CHoCH / swing highs-lows
2. Fair Value Gaps (FVG)    : bullish / bearish gaps, mitigation flag
3. Order blocks             : last down-candle before up-impulse & vice-versa
4. Liquidity pools & sweeps : equal highs/lows + sweep-and-reverse pattern
5. Supply / demand zones    : consolidation ranges before directional moves
6. Volume profile           : POC, VAH, VAL over a rolling window
7. EMA stack                : 9 / 20 / 50 / 200 alignment and distance-from-EMA
8. Trendline break          : pivot-connected descending / ascending lines
9. Candle primitives        : engulfing, hammer, shooting-star, doji, pinbar

All public helpers return a pandas.DataFrame indexed by the original
DatetimeIndex. The top-level `compute_smc_features()` is what the factor
engine calls and returns a wide DataFrame.

NOTE: This is the *daily-bar* SMC implementation. The live intraday versions
(anchored VWAP, order-flow imbalance, live sweep detection on minute bars) live
in qsde/factors/intraday_microstructure.py (Phase 1).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ============================================================
# 0. Helpers
# ============================================================
def _swing_highs(high: pd.Series, left: int = 2, right: int = 2) -> pd.Series:
    """Binary series: 1 where `high` is the max within [-left, +right]."""
    roll = high.rolling(left + right + 1, center=True).max()
    piv = (high == roll).astype(int)
    # forbid lookahead: a pivot is only "known" `right` bars later
    return piv.shift(right).fillna(0).astype(int)


def _swing_lows(low: pd.Series, left: int = 2, right: int = 2) -> pd.Series:
    roll = low.rolling(left + right + 1, center=True).min()
    piv = (low == roll).astype(int)
    return piv.shift(right).fillna(0).astype(int)


def _last_value(series: pd.Series, mask: pd.Series) -> pd.Series:
    """Forward-fill the value of `series` at positions where mask==1."""
    s = series.where(mask.astype(bool)).ffill()
    return s


# ============================================================
# 1. BOS / CHoCH — market structure
# ============================================================
def bos_choch(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """
    Break of Structure / Change of Character.

    • BOS_up  : close breaks the most recent swing high (continuation)
    • BOS_dn  : close breaks the most recent swing low  (continuation)
    • CHoCH_up: BOS_up that flips a prior down-trend (trend change)
    • CHoCH_dn: BOS_dn that flips a prior up-trend
    """
    high, low, close = df["high"], df["low"], df["close"]
    sh = _swing_highs(high, left, right)
    sl = _swing_lows(low, left, right)
    last_sh = _last_value(high, sh)
    last_sl = _last_value(low, sl)

    bos_up = (close > last_sh.shift(1)).astype(int)
    bos_dn = (close < last_sl.shift(1)).astype(int)

    # CHoCH = first BOS in the opposite direction of the prior trend
    prev_trend = np.sign(
        (close.rolling(20).mean() - close.rolling(50).mean()).shift(1).fillna(0)
    )
    choch_up = ((bos_up == 1) & (prev_trend < 0)).astype(int)
    choch_dn = ((bos_dn == 1) & (prev_trend > 0)).astype(int)

    return pd.DataFrame(
        {
            "smc_swing_high": sh,
            "smc_swing_low": sl,
            "smc_bos_up": bos_up,
            "smc_bos_dn": bos_dn,
            "smc_choch_up": choch_up,
            "smc_choch_dn": choch_dn,
        },
        index=df.index,
    )


# ============================================================
# 2. Fair Value Gap (FVG / ICT imbalance)
# ============================================================
def fair_value_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """
    3-bar imbalance:
      Bullish FVG: low[t] > high[t-2]      → gap between bars t-2 and t
      Bearish FVG: high[t] < low[t-2]
    Outputs both the gap size (as % of close) and a "mitigated" flag
    flipped 1 when a later bar trades back into the gap.
    """
    high, low, close = df["high"], df["low"], df["close"]
    bull_fvg = (low > high.shift(2)).astype(int)
    bear_fvg = (high < low.shift(2)).astype(int)

    bull_size = ((low - high.shift(2)) / close).where(bull_fvg == 1)
    bear_size = ((low.shift(2) - high) / close).where(bear_fvg == 1)

    # Mitigation: bullish FVG low is pierced from above
    bull_top = high.shift(2).where(bull_fvg == 1).ffill()
    bull_mit = ((low <= bull_top) & bull_top.notna()).astype(int)

    bear_top = low.shift(2).where(bear_fvg == 1).ffill()
    bear_mit = ((high >= bear_top) & bear_top.notna()).astype(int)

    return pd.DataFrame(
        {
            "smc_fvg_bull": bull_fvg,
            "smc_fvg_bear": bear_fvg,
            "smc_fvg_bull_pct": bull_size.fillna(0.0),
            "smc_fvg_bear_pct": bear_size.fillna(0.0),
            "smc_fvg_bull_mit": bull_mit,
            "smc_fvg_bear_mit": bear_mit,
        },
        index=df.index,
    )


# ============================================================
# 3. Order blocks
# ============================================================
def order_blocks(df: pd.DataFrame, impulse_bars: int = 3, impulse_ret: float = 0.015) -> pd.DataFrame:
    """
    Bullish OB : last bearish candle before an `impulse_bars`-bar up-move >= impulse_ret.
    Bearish OB : last bullish candle before an `impulse_bars`-bar down-move.
    """
    close = df["close"]
    ret_fwd = close.pct_change(impulse_bars)

    bull_candle = (df["close"] > df["open"]).astype(int)
    bear_candle = (df["close"] < df["open"]).astype(int)

    up_impulse = (ret_fwd > impulse_ret).astype(int)
    dn_impulse = (ret_fwd < -impulse_ret).astype(int)

    # look back impulse_bars for the last candle of the OPPOSITE colour.
    # Cast to int explicitly: .fillna(0) upcasts to float, and bitwise & requires
    # matching integer dtypes on both sides.
    bull_ob = (bear_candle.shift(impulse_bars).fillna(0).astype(int) & up_impulse).astype(int)
    bear_ob = (bull_candle.shift(impulse_bars).fillna(0).astype(int) & dn_impulse).astype(int)

    # distance of current close from the most recent bullish OB low (support)
    bull_ob_low = df["low"].where(bull_ob == 1).ffill()
    bear_ob_high = df["high"].where(bear_ob == 1).ffill()
    dist_bull = (close - bull_ob_low) / close
    dist_bear = (bear_ob_high - close) / close

    return pd.DataFrame(
        {
            "smc_order_block_bull": bull_ob,
            "smc_order_block_bear": bear_ob,
            "smc_dist_bull_ob": dist_bull.fillna(0.0),
            "smc_dist_bear_ob": dist_bear.fillna(0.0),
        },
        index=df.index,
    )


# ============================================================
# 4. Liquidity pools & sweeps
# ============================================================
def liquidity_sweeps(df: pd.DataFrame, lookback: int = 20, tol: float = 0.0015) -> pd.DataFrame:
    """
    Equal highs / lows + stop-hunt detection.

    Equal-high cluster  : rolling max within `tol` of several prior highs.
    Sweep-high          : bar pierces prior N-bar high and closes back below.
    Sweep-low mirror.
    """
    high, low, close = df["high"], df["low"], df["close"]
    roll_hi = high.shift(1).rolling(lookback).max()
    roll_lo = low.shift(1).rolling(lookback).min()

    equal_hi = ((high - roll_hi).abs() / close <= tol).astype(int)
    equal_lo = ((low - roll_lo).abs() / close <= tol).astype(int)

    sweep_hi = ((high > roll_hi) & (close < roll_hi)).astype(int)   # bull trap
    sweep_lo = ((low < roll_lo) & (close > roll_lo)).astype(int)    # bear trap

    return pd.DataFrame(
        {
            "smc_equal_high": equal_hi,
            "smc_equal_low": equal_lo,
            "smc_liq_sweep_high": sweep_hi,  # bullish reversal candidate
            "smc_liq_sweep_low": sweep_lo,   # bearish reversal candidate
        },
        index=df.index,
    )


# ============================================================
# 5. Supply / Demand zones
# ============================================================
def supply_demand_zones(df: pd.DataFrame, window: int = 10, range_bp: float = 0.01) -> pd.DataFrame:
    """
    A "base" is a `window`-bar range where (max-min)/close < range_bp.
    A demand zone is a base followed by an up-impulse; supply zone: down-impulse.
    """
    high, low, close = df["high"], df["low"], df["close"]
    rng = (high.rolling(window).max() - low.rolling(window).min()) / close
    base = (rng < range_bp).astype(int)

    impulse_up = (close.pct_change(3) > 0.02).astype(int)
    impulse_dn = (close.pct_change(3) < -0.02).astype(int)

    demand = (base.shift(3).fillna(0).astype(int) & impulse_up).astype(int)
    supply = (base.shift(3).fillna(0).astype(int) & impulse_dn).astype(int)

    return pd.DataFrame(
        {"smc_base": base, "smc_demand_zone": demand, "smc_supply_zone": supply},
        index=df.index,
    )


# ============================================================
# 6. Rolling volume profile (POC / VAH / VAL)
# ============================================================
def volume_profile(df: pd.DataFrame, lookback: int = 30, bins: int = 24) -> pd.DataFrame:
    """
    For each bar, build a histogram of the last `lookback` closes weighted
    by volume. Emits Point of Control (max-vol price), Value Area High/Low
    (top/bottom of the 70%-volume band), and distance of current close
    from POC as % of close.
    """
    close = df["close"].values
    vol = df["volume"].astype(float).values
    n = len(df)
    poc = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)

    for i in range(lookback, n):
        c = close[i - lookback : i]
        v = vol[i - lookback : i]
        lo, hi = c.min(), c.max()
        if hi <= lo or v.sum() <= 0:
            continue
        edges = np.linspace(lo, hi, bins + 1)
        counts = np.zeros(bins)
        idx = np.clip(np.searchsorted(edges, c, side="right") - 1, 0, bins - 1)
        for k in range(len(c)):
            counts[idx[k]] += v[k]
        # POC
        poc_bin = int(np.argmax(counts))
        poc[i] = 0.5 * (edges[poc_bin] + edges[poc_bin + 1])
        # Value area: expand outward from POC until we cover 70% of total vol
        target = 0.7 * counts.sum()
        low_b, high_b = poc_bin, poc_bin
        acc = counts[poc_bin]
        while acc < target and (low_b > 0 or high_b < bins - 1):
            left = counts[low_b - 1] if low_b > 0 else -1
            right = counts[high_b + 1] if high_b < bins - 1 else -1
            if right >= left:
                high_b += 1
                acc += counts[high_b]
            else:
                low_b -= 1
                acc += counts[low_b]
        vah[i] = edges[high_b + 1]
        val[i] = edges[low_b]

    out = pd.DataFrame(
        {"smc_vp_poc": poc, "smc_vp_vah": vah, "smc_vp_val": val}, index=df.index
    )
    out["smc_vp_poc_dist"] = (df["close"] - out["smc_vp_poc"]) / df["close"]
    out["smc_vp_in_value"] = (
        (df["close"] >= out["smc_vp_val"]) & (df["close"] <= out["smc_vp_vah"])
    ).astype(int)
    return out


# ============================================================
# 7. EMA stack & distances
# ============================================================
def ema_stack(df: pd.DataFrame, spans=(9, 20, 50, 200)) -> pd.DataFrame:
    close = df["close"]
    emas = {f"smc_ema_{s}": close.ewm(span=s, adjust=False).mean() for s in spans}
    out = pd.DataFrame(emas)

    # Stack alignment: +1 fully bullish, -1 fully bearish
    vals = [out[f"smc_ema_{s}"] for s in spans]
    bull_align = pd.concat([vals[i] > vals[i + 1] for i in range(len(vals) - 1)], axis=1).all(axis=1)
    bear_align = pd.concat([vals[i] < vals[i + 1] for i in range(len(vals) - 1)], axis=1).all(axis=1)
    out["smc_ema_stack"] = bull_align.astype(int) - bear_align.astype(int)

    # Distance from each EMA as % of close (mean-reversion signal)
    for s in spans:
        out[f"smc_ema_{s}_dist"] = (close - out[f"smc_ema_{s}"]) / close
    return out


# ============================================================
# 8. Trendline break (pivot-connected)
# ============================================================
def trendline_break(df: pd.DataFrame, lookback: int = 40) -> pd.DataFrame:
    """
    Fit a descending line through the two most-recent swing highs and
    an ascending line through the two most-recent swing lows in the
    last `lookback` bars. Emit 1 when close breaks through.
    """
    high, low, close = df["high"], df["low"], df["close"]
    sh = _swing_highs(high).astype(bool)
    sl = _swing_lows(low).astype(bool)
    n = len(df)
    break_up = np.zeros(n)
    break_dn = np.zeros(n)

    hi_pos = np.where(sh.values)[0]
    lo_pos = np.where(sl.values)[0]

    for i in range(lookback, n):
        recent_hi = hi_pos[(hi_pos < i) & (hi_pos >= i - lookback)]
        recent_lo = lo_pos[(lo_pos < i) & (lo_pos >= i - lookback)]
        if len(recent_hi) >= 2:
            x1, x2 = recent_hi[-2], recent_hi[-1]
            y1, y2 = high.iloc[x1], high.iloc[x2]
            slope = (y2 - y1) / max(x2 - x1, 1)
            y_line = y2 + slope * (i - x2)
            if slope < 0 and close.iloc[i] > y_line:
                break_up[i] = 1.0
        if len(recent_lo) >= 2:
            x1, x2 = recent_lo[-2], recent_lo[-1]
            y1, y2 = low.iloc[x1], low.iloc[x2]
            slope = (y2 - y1) / max(x2 - x1, 1)
            y_line = y2 + slope * (i - x2)
            if slope > 0 and close.iloc[i] < y_line:
                break_dn[i] = 1.0

    return pd.DataFrame(
        {"smc_trendline_break_up": break_up, "smc_trendline_break_dn": break_dn},
        index=df.index,
    )


# ============================================================
# 9. Candle primitives (complements qsde/factors/patterns.py)
# ============================================================
def candle_primitives(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - c.where(c >= o, o)
    lower = c.where(c <= o, o) - l

    doji = ((body / rng) < 0.1).astype(int)
    hammer = ((lower > 2 * body) & (upper < body) & (c > o)).astype(int)
    shooter = ((upper > 2 * body) & (lower < body) & (c < o)).astype(int)
    pinbar = (
        (body / rng < 0.35)
        & ((upper > 2 * body) | (lower > 2 * body))
    ).astype(int)

    bull_eng = (
        (c > o) & (c.shift(1) < o.shift(1)) & (c > o.shift(1)) & (o < c.shift(1))
    ).astype(int)
    bear_eng = (
        (c < o) & (c.shift(1) > o.shift(1)) & (c < o.shift(1)) & (o > c.shift(1))
    ).astype(int)

    return pd.DataFrame(
        {
            "smc_doji": doji.fillna(0),
            "smc_hammer": hammer.fillna(0),
            "smc_shooting_star": shooter.fillna(0),
            "smc_pinbar": pinbar.fillna(0),
            "smc_engulfing_bull": bull_eng.fillna(0),
            "smc_engulfing_bear": bear_eng.fillna(0),
        },
        index=df.index,
    ).astype(int)


# ============================================================
# Top-level aggregator
# ============================================================
def compute_smc_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Given a single-symbol OHLCV DataFrame (cols: open, high, low, close, volume),
    return a wide DataFrame of ~35 SMC/ICT/price-action features suitable for
    joining into the factor engine.
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns.str.lower())
    if missing:
        raise ValueError(f"compute_smc_features missing cols: {missing}")
    df = df.rename(columns=str.lower).copy()

    pieces = [
        bos_choch(df),
        fair_value_gaps(df),
        order_blocks(df),
        liquidity_sweeps(df),
        supply_demand_zones(df),
        volume_profile(df),
        ema_stack(df),
        trendline_break(df),
        candle_primitives(df),
    ]
    out = pd.concat(pieces, axis=1)
    return out


__all__ = [
    "bos_choch",
    "fair_value_gaps",
    "order_blocks",
    "liquidity_sweeps",
    "supply_demand_zones",
    "volume_profile",
    "ema_stack",
    "trendline_break",
    "candle_primitives",
    "compute_smc_features",
]
