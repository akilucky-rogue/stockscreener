"""Tier 1 rule-based factor primitives — Stoxsy.

Pure, stateless, deterministic factor scores. No DB calls, no I/O, no side
effects. Each factor implements a peer-reviewed cross-sectional anomaly
that has been validated out-of-sample for decades, including on Indian
equities.

Why rule-based first
--------------------
At our data scale (~500-2000 symbols, ~5-20 years of daily bars) ML models
overfit unless validation is bulletproof. Rule-based factors are
deterministic, interpretable, hand-verifiable, and have a multi-decade
out-of-sample track record. They form Tier 1 of the signal stack;
ML re-enters as a Tier 2 confirmation layer only after it demonstrably
beats Tier 1 on DSR over 60+ sessions.

The four factors
----------------
1. Jegadeesh & Titman (1993), cross-sectional momentum
   "Returns to Buying Winners and Selling Losers". J. Finance 48(1).
   12-1 formation: rank stocks by trailing 11-month return, skipping the
   most recent month to avoid short-term reversal contamination. The
   skip-month is what makes this work in practice.

2. Moskowitz, Ooi, Pedersen (2012), time-series momentum
   "Time Series Momentum". J. Financial Economics 104(2).
   Each stock vs its OWN history: sign of trailing return, volatility-
   scaled so cross-asset signals are comparable. Diversifies cross-
   sectional momentum because it captures trend strength independent
   of peer ranking.

3. Frazzini & Pedersen (2014), betting against beta (BAB)
   "Betting Against Beta". J. Financial Economics 111(1).
   Rolling-window beta vs the market index; low-beta names earn higher
   risk-adjusted returns. We use beta rank as a tilt, NOT a leveraged
   long-short portfolio — retail can't safely run BAB at book size.

4. Connors & Alvarez (2009), RSI(2) mean reversion
   "Short-Term Trading Strategies That Work" (Trading Markets Books).
   2-period RSI as an aggressive overbought/oversold oscillator. Works
   for short-horizon mean reversion AFTER a confirmed uptrend — the
   prerequisite trend filter is the SMA(200) test from the book.

Composite
---------
composite_rank_ic_weighted() blends the four factor ranks using rolling
60-day Spearman IC as weights (Grinold 1989, "Fundamental Law of Active
Management"). Negative-IC factors are zeroed (not shorted) per a robust
implementation — we don't trust the precision of a negative IC enough
to bet against it.

All scoring conventions
-----------------------
Higher score = stronger long signal.
Lower (or negative) score = weaker / short candidate.
Cross-sectional rank is left to the engine; these primitives return
raw scores per symbol.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

# NSE trading days per year (used for annualizing volatility / Sharpe).
TRADING_DAYS_PER_YEAR = 252

# Default formation/skip windows per the original Jegadeesh-Titman paper.
JT_FORMATION_DAYS = 252   # 12 months
JT_SKIP_DAYS = 21         # 1 month

# Time-series momentum lookback per Moskowitz-Ooi-Pedersen.
MOP_LOOKBACK_DAYS = 252

# Beta computation window per Frazzini-Pedersen (12 months is the canonical
# choice; 252 trading days approximates that).
BAB_BETA_WINDOW = 252

# Connors-Alvarez RSI(2): 2-period RSI, decisions vs SMA(200) trend filter.
CONNORS_RSI_PERIOD = 2
CONNORS_TREND_FILTER = 200


# ──────────────────────────────────────────────────────────────────────
# 1. Jegadeesh-Titman cross-sectional momentum
# ──────────────────────────────────────────────────────────────────────

def jegadeesh_titman_score(
    close: pd.Series,
    formation: int = JT_FORMATION_DAYS,
    skip: int = JT_SKIP_DAYS,
) -> pd.Series:
    """Per-symbol 12-1 momentum score.

    Formation return = close[t-skip] / close[t-formation] - 1.
    The "skip" month is the critical step — last month's return shows
    short-term reversal, NOT momentum continuation. Skipping it is what
    separates the J-T effect from look-ahead-style trend chasing.

    Parameters
    ----------
    close : pd.Series
        Daily close prices, indexed by date (ascending).
    formation : int
        Formation window in trading days (default 252 = 12mo).
    skip : int
        Skip window in trading days (default 21 = 1mo).

    Returns
    -------
    pd.Series
        Score per date. NaN for first `formation` days.

    Notes
    -----
    Score is the raw return; the engine cross-sectionally ranks across
    the universe on each date. Higher = stronger winner.
    """
    if not isinstance(close, pd.Series):
        raise TypeError("close must be a pandas Series")
    if formation <= skip:
        raise ValueError(f"formation ({formation}) must exceed skip ({skip})")
    if len(close) < formation + 1:
        # Not enough history; return all-NaN aligned to input.
        return pd.Series(np.nan, index=close.index, name="jt_score")

    # close[t-skip] / close[t-formation] - 1
    numerator = close.shift(skip)
    denominator = close.shift(formation)
    score = (numerator / denominator) - 1.0
    return score.rename("jt_score")


# ──────────────────────────────────────────────────────────────────────
# 2. Moskowitz-Ooi-Pedersen time-series momentum
# ──────────────────────────────────────────────────────────────────────

def mop_tsmom_score(
    close: pd.Series,
    lookback: int = MOP_LOOKBACK_DAYS,
    vol_window: int = 60,
) -> pd.Series:
    """Per-symbol time-series momentum score, vol-scaled.

    score(t) = sign(R_lookback(t)) * R_lookback(t) / sigma_60d(t)

    where R_lookback = close[t] / close[t-lookback] - 1
    and sigma_60d = annualized rolling std of daily log returns.

    The vol-scaling is what makes signals comparable across symbols of
    different volatilities, per MOP §3. Without it, high-vol names
    dominate the composite.

    Parameters
    ----------
    close : pd.Series
        Daily close prices.
    lookback : int
        Momentum window (default 252 days).
    vol_window : int
        Volatility estimation window (default 60 days).

    Returns
    -------
    pd.Series
        Vol-scaled signed momentum per date.

    Notes
    -----
    Sign-only variant (just np.sign of return) is the original MOP
    binary signal. We use the vol-scaled magnitude because it gives a
    smoother score for cross-sectional ranking — but the sign carries
    most of the alpha per the paper.
    """
    if len(close) < max(lookback, vol_window) + 1:
        return pd.Series(np.nan, index=close.index, name="mop_score")

    lookback_return = (close / close.shift(lookback)) - 1.0
    daily_log_ret = np.log(close / close.shift(1))
    # Annualized rolling std of log returns.
    daily_vol = daily_log_ret.rolling(window=vol_window, min_periods=vol_window // 2).std()
    ann_vol = daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR)
    # Avoid divide-by-zero on completely flat series.
    ann_vol = ann_vol.replace(0.0, np.nan)
    score = np.sign(lookback_return) * (lookback_return.abs() / ann_vol)
    return score.rename("mop_score")


# ──────────────────────────────────────────────────────────────────────
# 3. Frazzini-Pedersen betting-against-beta
# ──────────────────────────────────────────────────────────────────────

def rolling_beta(
    stock_returns: pd.Series,
    market_returns: pd.Series,
    window: int = BAB_BETA_WINDOW,
) -> pd.Series:
    """Rolling OLS beta of stock returns vs market returns.

    beta_t = Cov(R_stock, R_market) / Var(R_market) over trailing `window`.

    Parameters
    ----------
    stock_returns, market_returns : pd.Series
        Daily simple returns, aligned on the same index.
    window : int
        Rolling window in trading days.

    Returns
    -------
    pd.Series
        Beta time series, NaN for warmup.
    """
    aligned = pd.concat({"s": stock_returns, "m": market_returns}, axis=1).dropna()
    if len(aligned) < window:
        return pd.Series(np.nan, index=stock_returns.index, name="beta")
    cov = aligned["s"].rolling(window=window).cov(aligned["m"])
    var = aligned["m"].rolling(window=window).var()
    var = var.replace(0.0, np.nan)
    beta = cov / var
    return beta.reindex(stock_returns.index).rename("beta")


def bab_score(
    close: pd.Series,
    market_close: pd.Series,
    window: int = BAB_BETA_WINDOW,
) -> pd.Series:
    """Betting-against-beta tilt score.

    Per Frazzini-Pedersen, low-beta names earn higher Sharpe ratios.
    The full BAB strategy is long low-beta / short high-beta with
    leverage; for a long-only retail tilt we just rank by -beta and
    let the engine compose with other signals.

    Score = -beta (so higher score = lower beta = stronger long tilt).

    Parameters
    ----------
    close : pd.Series
        Daily close of the stock.
    market_close : pd.Series
        Daily close of the benchmark (NIFTY 50 or NIFTY 500).
    window : int
        Beta estimation window.

    Returns
    -------
    pd.Series
        Negative beta per date (higher = better BAB long candidate).
    """
    if len(close) < window + 1 or len(market_close) < window + 1:
        return pd.Series(np.nan, index=close.index, name="bab_score")

    stock_ret = close.pct_change()
    mkt_ret = market_close.reindex(close.index).pct_change()
    beta = rolling_beta(stock_ret, mkt_ret, window=window)
    # Higher score = lower beta = stronger long tilt under BAB.
    return (-beta).rename("bab_score")


# ──────────────────────────────────────────────────────────────────────
# 4. Connors-Alvarez RSI(2) mean reversion
# ──────────────────────────────────────────────────────────────────────

def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing RSI (the conventional implementation)."""
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    # Wilder's smoothing = EMA with alpha = 1/period.
    avg_gain = gains.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss == 0 and avg_gain > 0, RSI is 100 by convention.
    rsi = rsi.where(~((avg_loss == 0.0) & (avg_gain > 0.0)), 100.0)
    return rsi


def connors_rsi2_score(
    close: pd.Series,
    rsi_period: int = CONNORS_RSI_PERIOD,
    trend_period: int = CONNORS_TREND_FILTER,
    oversold: float = 10.0,
    overbought: float = 90.0,
) -> pd.Series:
    """Connors-Alvarez RSI(2) mean-reversion score.

    The original rule (paraphrased from the book):
      LONG  if close > SMA(200) AND RSI(2) < 10
      EXIT  on close > SMA(5)
    SHORT mirrors with close < SMA(200) AND RSI(2) > 90.

    We return a continuous score so the composite can blend it:
      score = (oversold - rsi2) / oversold       when above trend
      score = -(rsi2 - overbought) / (100 - overbought)  when below trend
      score = 0  otherwise
    Magnitude in roughly [-1, +1].

    Parameters
    ----------
    close : pd.Series
        Daily close prices.
    rsi_period : int
        RSI lookback (default 2).
    trend_period : int
        SMA trend filter (default 200).
    oversold, overbought : float
        Thresholds (default 10, 90 per the book).

    Returns
    -------
    pd.Series
        Score per date. Higher = stronger BUY-the-dip in uptrend.
    """
    if len(close) < trend_period + rsi_period:
        return pd.Series(np.nan, index=close.index, name="rsi2_score")

    rsi = _wilder_rsi(close, rsi_period)
    sma_trend = close.rolling(window=trend_period, min_periods=trend_period).mean()
    above_trend = close > sma_trend
    below_trend = close < sma_trend

    long_signal = (oversold - rsi) / oversold      # positive when rsi < oversold
    short_signal = -(rsi - overbought) / (100.0 - overbought)  # negative when rsi > overbought

    score = pd.Series(0.0, index=close.index)
    score = score.where(~above_trend, long_signal)
    score = score.where(~below_trend, short_signal)
    # Carry NaN where inputs are NaN.
    score = score.where(~rsi.isna() & ~sma_trend.isna())
    return score.rename("rsi2_score")


# ──────────────────────────────────────────────────────────────────────
# Composite — IC-weighted blend
# ──────────────────────────────────────────────────────────────────────

def cross_sectional_rank(scores_by_symbol: pd.DataFrame) -> pd.DataFrame:
    """Rank-transform each row (cross-section per date) to [-0.5, +0.5].

    Parameters
    ----------
    scores_by_symbol : pd.DataFrame
        Rows = dates, columns = symbols, values = raw factor scores.

    Returns
    -------
    pd.DataFrame
        Same shape, values in [-0.5, +0.5] within each row.
        NaN inputs preserved as NaN.
    """
    # rank(pct=True) -> [0, 1]; subtract 0.5 -> [-0.5, +0.5].
    return scores_by_symbol.rank(axis=1, pct=True) - 0.5


def composite_rank_ic_weighted(
    ranked_factors: dict[str, pd.DataFrame],
    ic_weights: dict[str, float],
) -> pd.DataFrame:
    """IC-weighted composite of cross-sectionally ranked factor scores.

    composite[date, symbol] = sum_f (w_f * rank_f[date, symbol])
    where w_f = max(IC_f, 0) / sum_g max(IC_g, 0).

    Negative-IC factors are zeroed (not shorted). This is the robust
    choice per a "least-trust your noisy signal" principle: a negative IC
    might be a regime artifact rather than a true sign flip, and we'd
    rather give up the signal than reverse it on small-sample evidence.

    Parameters
    ----------
    ranked_factors : dict[name, DataFrame]
        Each DataFrame has rows=dates, cols=symbols, values=cross-sectional ranks.
    ic_weights : dict[name, float]
        Rolling IC per factor (must include the same keys as `ranked_factors`).
        Missing keys default to 0 (factor is dropped). Empty / all-zero
        falls back to equal-weight across whatever was provided.

    Returns
    -------
    pd.DataFrame
        Composite cross-sectional score per (date, symbol).

    Raises
    ------
    ValueError if `ranked_factors` is empty.
    """
    if not ranked_factors:
        raise ValueError("ranked_factors is empty")

    positive_weights = {
        name: max(float(ic_weights.get(name, 0.0)), 0.0) for name in ranked_factors
    }
    total = sum(positive_weights.values())
    if total <= 0:
        # All IC zero or negative — equal-weight fallback.
        n = len(ranked_factors)
        positive_weights = {name: 1.0 / n for name in ranked_factors}
    else:
        positive_weights = {name: w / total for name, w in positive_weights.items()}

    composite: pd.DataFrame | None = None
    for name, df in ranked_factors.items():
        w = positive_weights[name]
        if w == 0:
            continue
        contribution = df * w
        composite = contribution if composite is None else composite.add(contribution, fill_value=0.0)

    if composite is None:
        raise RuntimeError("composite is None despite non-empty ranked_factors — logic bug")
    return composite


# ──────────────────────────────────────────────────────────────────────
# Module-level public API
# ──────────────────────────────────────────────────────────────────────

FACTOR_NAMES: tuple[str, ...] = ("jt", "mop", "bab", "rsi2")


# Which factors are appropriate per horizon, per the source papers.
# J-T is a 6-12 month effect, BAB is multi-month, MOP is multi-week-to-year,
# RSI(2) is days. Intraday is not in scope for any of these — intraday
# alpha needs microstructure factors, not daily-bar factors.
HORIZON_FACTORS: dict[str, tuple[str, ...]] = {
    "swing":    ("jt", "mop", "bab", "rsi2"),
    "long":     ("jt", "mop", "bab"),
    "intraday": (),   # Tier 1 deliberately does NOT trade intraday.
}


__all__ = [
    "jegadeesh_titman_score",
    "mop_tsmom_score",
    "bab_score",
    "connors_rsi2_score",
    "rolling_beta",
    "cross_sectional_rank",
    "composite_rank_ic_weighted",
    "FACTOR_NAMES",
    "HORIZON_FACTORS",
    "TRADING_DAYS_PER_YEAR",
    "JT_FORMATION_DAYS",
    "JT_SKIP_DAYS",
    "MOP_LOOKBACK_DAYS",
    "BAB_BETA_WINDOW",
    "CONNORS_RSI_PERIOD",
    "CONNORS_TREND_FILTER",
]
