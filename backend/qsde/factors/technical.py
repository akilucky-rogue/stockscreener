"""
Technical factor computation — 50 factors from OHLCV data.

Ported from StockTrack/stocktrack/factors/technical.py with additions
for momentum lookbacks and VWAP deviation.

All functions take a pandas DataFrame with columns:
  date, open, high, low, close, volume
and return a pandas Series or DataFrame of factor values.
"""

import numpy as np
import pandas as pd


# ── Helpers ────────────────────────────────────────────────────
def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def _sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()

def _true_range(df: pd.DataFrame) -> pd.Series:
    h_l = df["high"] - df["low"]
    h_pc = (df["high"] - df["close"].shift(1)).abs()
    l_pc = (df["low"] - df["close"].shift(1)).abs()
    return pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)


# ── Momentum ──────────────────────────────────────────────────
def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's RSI. Oversold < 30, Overbought > 70."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, and histogram."""
    ema_fast = _ema(df["close"], fast)
    ema_slow = _ema(df["close"], slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd_line": macd_line, "macd_signal": signal_line, "macd_hist": histogram})

def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    """Stochastic %K and %D."""
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return pd.DataFrame({"stoch_k": k, "stoch_d": d})

def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity Channel Index."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma_tp) / (0.015 * mad).replace(0, np.nan)

def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Williams %R."""
    high_max = df["high"].rolling(period).max()
    low_min = df["low"].rolling(period).min()
    return -100 * (high_max - df["close"]) / (high_max - low_min).replace(0, np.nan)

def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Money Flow Index — volume-weighted RSI."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mf = tp * df["volume"]
    pos_mf = mf.where(tp > tp.shift(1), 0).rolling(period).sum()
    neg_mf = mf.where(tp < tp.shift(1), 0).rolling(period).sum()
    ratio = pos_mf / neg_mf.replace(0, np.nan)
    return 100 - 100 / (1 + ratio)

def momentum_return(df: pd.DataFrame, period: int = 21) -> pd.Series:
    """N-day price return (momentum factor)."""
    return df["close"].pct_change(period) * 100


# ── Trend ─────────────────────────────────────────────────────
def adx(df: pd.DataFrame, period: int = 14):
    """Average Directional Index with DI+ and DI-."""
    high_diff = df["high"].diff()
    low_diff = -df["low"].diff()
    plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
    minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
    tr = _true_range(df)
    atr_vals = tr.ewm(alpha=1 / period, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period
    ).mean() / atr_vals.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period
    ).mean() / atr_vals.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / period, min_periods=period).mean()
    return pd.DataFrame({"adx": adx_val, "di_plus": plus_di, "di_minus": minus_di})

def price_vs_sma(df: pd.DataFrame, window: int) -> pd.Series:
    """Price deviation from SMA as percentage."""
    sma_val = _sma(df["close"], window)
    return (df["close"] - sma_val) / sma_val.replace(0, np.nan) * 100

def golden_death_ratio(df: pd.DataFrame) -> pd.Series:
    """SMA50 / SMA200 ratio. > 1 = golden cross zone."""
    return _sma(df["close"], 50) / _sma(df["close"], 200).replace(0, np.nan)

def donchian_breakout(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Position within Donchian channel. 1 = at high, 0 = at low."""
    high_max = df["high"].rolling(period).max()
    low_min = df["low"].rolling(period).min()
    return (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)


# ── Volatility ────────────────────────────────────────────────
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    return _true_range(df).ewm(alpha=1 / period, min_periods=period).mean()

def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR as percentage of price — normalized volatility."""
    return atr(df, period) / df["close"].replace(0, np.nan) * 100

def bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0):
    """Bollinger Bands: %B and bandwidth."""
    mid = _sma(df["close"], period)
    std = df["close"].rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    pct_b = (df["close"] - lower) / (upper - lower).replace(0, np.nan)
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame({"bb_pctb": pct_b, "bb_bandwidth": bandwidth})

def realized_volatility(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Annualized realized volatility."""
    log_ret = np.log(df["close"] / df["close"].shift(1))
    return log_ret.rolling(period).std() * np.sqrt(252)

def vol_regime_flag(df: pd.DataFrame) -> pd.Series:
    """Vol regime: 20d vol / 60d vol. > 1.2 = expanding."""
    vol_20 = realized_volatility(df, 20)
    vol_60 = realized_volatility(df, 60)
    return vol_20 / vol_60.replace(0, np.nan)


# ── Volume ────────────────────────────────────────────────────
def obv_slope(df: pd.DataFrame, period: int = 10) -> pd.Series:
    """Slope of On Balance Volume over N bars."""
    direction = np.sign(df["close"].diff())
    obv_vals = (direction * df["volume"]).cumsum()
    return obv_vals.diff(period) / period

def volume_sma_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Today's volume / SMA of volume. > 2 = unusual volume."""
    vol_sma = df["volume"].rolling(period).mean()
    return df["volume"] / vol_sma.replace(0, np.nan)

def cmf(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Chaikin Money Flow."""
    mfv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / \
          (df["high"] - df["low"]).replace(0, np.nan) * df["volume"]
    return mfv.rolling(period).sum() / df["volume"].rolling(period).sum().replace(0, np.nan)


# ── Other ─────────────────────────────────────────────────────
def range_pct_52w(df: pd.DataFrame) -> pd.Series:
    """Position in 52-week range. 0 = at low, 100 = at high."""
    high_252 = df["high"].rolling(252).max()
    low_252 = df["low"].rolling(252).min()
    return (df["close"] - low_252) / (high_252 - low_252).replace(0, np.nan) * 100

def consecutive_updown(df: pd.DataFrame) -> pd.Series:
    """Count of consecutive up/down days. Positive = up streak."""
    direction = np.sign(df["close"].diff())
    groups = (direction != direction.shift(1)).cumsum()
    return direction.groupby(groups).cumsum()


# ── New TA factors (mean reversion / trend / VWAP / volume) ──

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, min_periods=period, adjust=False).mean()


def rsi_2(df: pd.DataFrame) -> pd.Series:
    """RSI(2) -- aggressive mean-reversion oscillator.

    Different beast from RSI(14). Connors-style: oversold at <10, overbought
    at >90. Decoupled from RSI(14) by lookback length.
    """
    return rsi(df, 2)


def rolling_vwap(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Rolling N-day VWAP (typical price weighted by volume).

    Daily-bar approximation of intraday VWAP. The 20-day window roughly
    captures monthly fair-value. Real intraday VWAP requires minute bars
    -- that's a Phase 1 add once Kite WebSocket is wired.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"]
    return pv.rolling(period, min_periods=period).sum() / df["volume"].rolling(period, min_periods=period).sum()


def price_vs_vwap_pct(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """How far price is above/below rolling VWAP, in percent."""
    v = rolling_vwap(df, period)
    return (df["close"] / v - 1.0) * 100.0


def vwap_slope(df: pd.DataFrame, period: int = 20, lookback: int = 5) -> pd.Series:
    """5-day percent change of the 20-day VWAP -- VWAP trend direction."""
    v = rolling_vwap(df, period)
    return (v / v.shift(lookback) - 1.0) * 100.0


def ema_ratio(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    """20EMA / 50EMA - 1, in percent. >0 = bullish stacking, <0 = bearish."""
    return (ema(df["close"], fast) / ema(df["close"], slow) - 1.0) * 100.0


def supertrend_state(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
) -> pd.Series:
    """Supertrend (10, 3) regime state: +1 = bullish, -1 = bearish.

    The classic India intraday filter. Built from ATR-anchored upper/lower
    bands; sign flips when price crosses the active band.
    """
    atr_vals = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper = hl2 + multiplier * atr_vals
    lower = hl2 - multiplier * atr_vals

    # Carry-forward the band-walk that defines Supertrend.
    final_upper = upper.copy()
    final_lower = lower.copy()
    state = pd.Series(index=df.index, dtype="float64")
    state.iloc[:period] = np.nan

    prev_state = 1.0
    for i in range(period, len(df)):
        u_prev = final_upper.iloc[i-1] if not np.isnan(final_upper.iloc[i-1]) else upper.iloc[i]
        l_prev = final_lower.iloc[i-1] if not np.isnan(final_lower.iloc[i-1]) else lower.iloc[i]

        final_upper.iloc[i] = min(upper.iloc[i], u_prev) if df["close"].iloc[i-1] <= u_prev else upper.iloc[i]
        final_lower.iloc[i] = max(lower.iloc[i], l_prev) if df["close"].iloc[i-1] >= l_prev else lower.iloc[i]

        if prev_state > 0 and df["close"].iloc[i] < final_lower.iloc[i]:
            prev_state = -1.0
        elif prev_state < 0 and df["close"].iloc[i] > final_upper.iloc[i]:
            prev_state = 1.0
        state.iloc[i] = prev_state
    return state


def mean_reversion_zscore(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """(close - SMA(period)) / std(close, period). Connors-style mean-reversion.

    >+2  = stretched far above mean (potential short)
    <-2  = stretched far below mean (potential long)
    """
    sma = df["close"].rolling(period, min_periods=period).mean()
    std = df["close"].rolling(period, min_periods=period).std()
    return (df["close"] - sma) / std.replace(0, np.nan)


def volume_spike(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Today's volume / 20-day average volume. >2 = clear spike."""
    avg = df["volume"].rolling(period, min_periods=period).mean()
    return df["volume"] / avg.replace(0, np.nan)


def heikin_ashi_streak(df: pd.DataFrame) -> pd.Series:
    """Consecutive Heikin Ashi candle color streak. +ve = green streak, -ve = red.

    HA candles are smoothed averages -- a long color streak is the cleanest
    momentum-confirmation indicator in the technical toolkit.
    """
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = ((df["open"].shift(1) + df["close"].shift(1)) / 2.0)
    # First HA open seeds from raw open.
    ha_open = ha_open.fillna(df["open"])
    color = np.sign(ha_close - ha_open)
    groups = (color != color.shift(1)).cumsum()
    return color.groupby(groups).cumsum()


def support_dist_pct(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """(close / 20-day rolling low) - 1, in percent. Proxy for double-bottom strength.

    Small positive number = price is sitting just above a recent support
    floor (classic bounce setup).
    """
    floor = df["low"].rolling(period, min_periods=period).min()
    return (df["close"] / floor.replace(0, np.nan) - 1.0) * 100.0


def resistance_dist_pct(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """(close / 20-day rolling high) - 1. Negative = below recent resistance."""
    ceil_ = df["high"].rolling(period, min_periods=period).max()
    return (df["close"] / ceil_.replace(0, np.nan) - 1.0) * 100.0


# ── Master Compute Function ──────────────────────────────────
def compute_all_technical(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all ~50 technical factors from OHLCV data.

    Args:
        df: DataFrame with columns [date, open, high, low, close, volume]

    Returns:
        DataFrame with one column per factor, indexed same as input.
    """
    result = pd.DataFrame(index=df.index)

    # Momentum (16 factors)
    result["tech_rsi_14"] = rsi(df, 14)
    result["tech_rsi_7"] = rsi(df, 7)
    m = macd(df)
    result["tech_macd_line"] = m["macd_line"]
    result["tech_macd_hist"] = m["macd_hist"]
    s = stochastic(df)
    result["tech_stoch_k"] = s["stoch_k"]
    result["tech_stoch_d"] = s["stoch_d"]
    result["tech_cci_20"] = cci(df)
    result["tech_williams_r"] = williams_r(df)
    result["tech_mfi_14"] = mfi(df)
    # Momentum lookbacks (Blueprint §6.1)
    result["tech_mom_1m"] = momentum_return(df, 21)
    result["tech_mom_3m"] = momentum_return(df, 63)
    result["tech_mom_6m"] = momentum_return(df, 126)
    result["tech_mom_12m"] = momentum_return(df, 252)
    result["tech_mom_12_1m"] = momentum_return(df, 252) - momentum_return(df, 21)
    result["tech_ret_5d"] = df["close"].pct_change(5) * 100
    result["tech_ret_20d"] = df["close"].pct_change(20) * 100

    # Trend (6 factors)
    a = adx(df)
    result["tech_adx_14"] = a["adx"]
    result["tech_di_diff"] = a["di_plus"] - a["di_minus"]
    result["tech_price_vs_sma50"] = price_vs_sma(df, 50)
    result["tech_price_vs_sma200"] = price_vs_sma(df, 200)
    result["tech_golden_death"] = golden_death_ratio(df)
    result["tech_donchian_20"] = donchian_breakout(df)

    # Volatility (6 factors)
    result["tech_atr_14"] = atr(df)
    result["tech_atr_pct"] = atr_pct(df)
    bb = bollinger_bands(df)
    result["tech_bb_pctb"] = bb["bb_pctb"]
    result["tech_bb_bandwidth"] = bb["bb_bandwidth"]
    result["tech_vol_20d"] = realized_volatility(df, 20)
    result["tech_vol_regime"] = vol_regime_flag(df)

    # Volume (3 factors)
    result["tech_obv_slope"] = obv_slope(df)
    result["tech_vol_sma_ratio"] = volume_sma_ratio(df)
    result["tech_cmf_20"] = cmf(df)

    # Other (3 factors)
    result["tech_range_52w"] = range_pct_52w(df)
    result["tech_consec_updown"] = consecutive_updown(df)

    # New: mean-reversion / trend / VWAP / volume (11 factors)
    result["tech_rsi_2"]              = rsi_2(df)
    result["tech_mean_rev_z"]         = mean_reversion_zscore(df, 20)
    result["tech_price_vs_vwap20"]    = price_vs_vwap_pct(df, 20)
    result["tech_vwap20_slope_5d"]    = vwap_slope(df, 20, 5)
    result["tech_ema20_50_ratio"]     = ema_ratio(df, 20, 50)
    result["tech_supertrend_10_3"]    = supertrend_state(df, 10, 3.0)
    result["tech_volume_spike"]       = volume_spike(df, 20)
    result["tech_ha_streak"]          = heikin_ashi_streak(df)
    result["tech_support_dist_20d"]   = support_dist_pct(df, 20)
    result["tech_resistance_dist_20d"] = resistance_dist_pct(df, 20)
    result["tech_adx_strong"]         = (result["tech_adx_14"] > 25).astype(float)

    return result
