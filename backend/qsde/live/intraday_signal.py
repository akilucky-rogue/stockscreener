"""
Intraday signal decision core (Phase 2).

Pure, white-box function: given session-to-date 1-minute bars for one symbol,
produce an actionable signal -- direction, a 0..1 confidence, entry / stop /
target / risk-reward, and a human-readable list of reasons.

Design principles (from qsde + StockTrack CLAUDE.md):
  * "ML ranks, rules decide." This core is rule-based and fully auditable
    (SEBI 2026 white-box requirement). An ML alpha score, when available, is
    blended in as ONE additional, optional term -- never the sole driver.
  * Every contribution to the score is recorded in `reasons`, so the live
    dashboard / Telegram alert can explain exactly why a setup fired.
  * Levels reuse qsde/risk/trade_levels.compute_trade_levels so intraday,
    swing, and long horizons share one, tested entry/stop/target engine.

Score construction (bias ∈ roughly [-1, +1])
--------------------------------------------
    0.30 * sign(close - AVWAP)            anchored-VWAP position
    0.30 * OFI                            order-flow imbalance (continuous)
    0.25 * (sweep_low_reclaim - sweep_high_reject)   liquidity-sweep reversal
    0.15 * sign(close - POC)              volume-profile position
  (+ optional) alpha_weight * (2*alpha_pct - 1)   ML cross-sectional alpha

    direction = +1 if bias >=  buy_threshold
                -1 if bias <= -buy_threshold
                 0 otherwise
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from qsde.factors.intraday_microstructure import compute_intraday_microstructure
from qsde.factors.technical import atr_pct as _atr_pct
from qsde.risk.trade_levels import compute_trade_levels


# Weights for the white-box bias score.
_W_AVWAP = 0.30
_W_OFI = 0.30
_W_SWEEP = 0.25
_W_POC = 0.15

_BUY_THRESHOLD = 0.35   # |bias| to fire a directional signal
_WATCH_THRESHOLD = 0.20  # |bias| to put on the watchlist


@dataclass
class IntradaySignal:
    symbol: str
    ts: str
    horizon: str
    price: float
    direction: int          # -1 / 0 / +1
    action: str             # BUY / SELL / WATCH / SKIP
    bias: float             # white-box score in ~[-1, 1]
    confidence: float       # 0..1
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    risk_reward: Optional[float]
    quality: Optional[str]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _safe(row: pd.Series, key: str, default: float = np.nan) -> float:
    try:
        v = float(row.get(key, default))
    except (TypeError, ValueError):
        return default
    return v


def generate_intraday_signal(
    bars: pd.DataFrame,
    *,
    symbol: str = "",
    horizon: str = "intraday",
    micro: Optional[pd.DataFrame] = None,
    alpha_pct: Optional[float] = None,
    alpha_weight: float = 0.0,
    buy_threshold: float = _BUY_THRESHOLD,
) -> IntradaySignal:
    """
    Build an IntradaySignal from session-to-date minute bars.

    Args:
        bars:        single-symbol minute OHLCV, DatetimeIndex ascending.
        symbol:      label for the output.
        horizon:     "intraday" | "swing" | "long" (sets ATR multipliers).
        micro:       precomputed microstructure frame (else computed here).
        alpha_pct:   optional ML cross-sectional alpha percentile (0..1).
        alpha_weight: weight on the ML term (0 = pure rules; e.g. 0.3 to blend).
        buy_threshold: |bias| needed to fire a directional signal.
    """
    if bars is None or bars.empty:
        raise ValueError("generate_intraday_signal: empty bars")
    bars = bars.rename(columns=str.lower)
    if micro is None:
        micro = compute_intraday_microstructure(bars)

    last = bars.iloc[-1]
    m = micro.iloc[-1]
    close = float(last["close"])
    ts = bars.index[-1]
    ts_str = ts.isoformat() if isinstance(ts, (pd.Timestamp, datetime)) else str(ts)

    bias = 0.0
    reasons: list[str] = []

    # 1. Anchored-VWAP position
    avwap = _safe(m, "intraday_avwap")
    if np.isfinite(avwap) and avwap > 0:
        if close > avwap:
            bias += _W_AVWAP
            reasons.append("holding above anchored VWAP")
        else:
            bias -= _W_AVWAP
            reasons.append("trading below anchored VWAP")
        upper = _safe(m, "intraday_avwap_upper")
        lower = _safe(m, "intraday_avwap_lower")
        if np.isfinite(upper) and close > upper:
            reasons.append("extended above upper VWAP band (stretched)")
        elif np.isfinite(lower) and close < lower:
            reasons.append("below lower VWAP band (stretched)")

    # 2. Order-flow imbalance (continuous)
    ofi = _safe(m, "intraday_ofi", 0.0)
    if np.isfinite(ofi) and abs(ofi) > 1e-6:
        bias += _W_OFI * float(np.clip(ofi, -1, 1))
        if ofi > 0.15:
            reasons.append(f"buy-side order-flow imbalance ({ofi:+.2f})")
        elif ofi < -0.15:
            reasons.append(f"sell-side order-flow imbalance ({ofi:+.2f})")

    # 3. Liquidity-sweep reversal (the user's headline setup)
    sweep_reclaim = _safe(m, "intraday_sweep_low_reclaim", 0.0)
    sweep_reject = _safe(m, "intraday_sweep_high_reject", 0.0)
    sweep_term = (1.0 if sweep_reclaim >= 1 else 0.0) - (1.0 if sweep_reject >= 1 else 0.0)
    if sweep_term > 0:
        bias += _W_SWEEP
        reasons.append("liquidity sweep of lows + VWAP reclaim (bullish trap)")
    elif sweep_term < 0:
        bias -= _W_SWEEP
        reasons.append("liquidity sweep of highs + VWAP rejection (bearish trap)")

    # 4. Volume-profile position
    poc = _safe(m, "intraday_vp_poc")
    if np.isfinite(poc) and poc > 0:
        if close > poc:
            bias += _W_POC
            reasons.append("above session volume POC")
        else:
            bias -= _W_POC
            reasons.append("below session volume POC")

    # 5. Optional ML alpha blend (kept as one input, never the sole driver)
    if alpha_pct is not None and np.isfinite(alpha_pct) and alpha_weight > 0:
        contrib = alpha_weight * (2.0 * float(alpha_pct) - 1.0)
        bias += contrib
        reasons.append(f"ML alpha percentile {alpha_pct:.0%}")

    bias = float(np.clip(bias, -1.5, 1.5))

    if bias >= buy_threshold:
        direction = 1
    elif bias <= -buy_threshold:
        direction = -1
    else:
        direction = 0

    # Entry / stop / target via the shared, tested level engine.
    atr_series = _atr_pct(bars, 14)
    atr_val = float(atr_series.iloc[-1]) if len(atr_series) and np.isfinite(atr_series.iloc[-1]) else None
    # technical.atr_pct() returns ATR as a PERCENT of price (e.g. 0.17 == 0.17%).
    # Convert to a fraction UNCONDITIONALLY. (compute_trade_levels' ">1 means
    # percent" heuristic is correct for daily ATR but misfires intraday, where
    # ATR% is < 1 and would be misread as a whole fraction -> 100x-too-wide
    # stops.) Floor the intraday stop basis at 0.3% so a 1-minute ATR doesn't
    # yield a stop so tight that microstructure noise trips it instantly.
    atr_frac = (atr_val or 0.0) / 100.0
    if horizon == "intraday":
        atr_frac = max(atr_frac, 0.003)
    # Conviction-scaled expected move: stronger bias -> target beyond the
    # volatility floor, so high-conviction setups clear the R:R quality bar
    # while marginal ones stay WATCH.
    pred_ret = direction * abs(bias) * 2.0 * atr_frac if direction != 0 else None
    levels = compute_trade_levels(
        price=close,
        atr_pct=atr_frac,            # pass an unambiguous FRACTION
        predicted_return=pred_ret,
        direction=direction,
        horizon=horizon,
    )

    confidence = float(min(1.0, abs(bias)))
    if direction != 0 and levels.get("quality") == "good":
        action = "BUY" if direction > 0 else "SELL"
    elif abs(bias) >= _WATCH_THRESHOLD:
        action = "WATCH"
    else:
        action = "SKIP"

    return IntradaySignal(
        symbol=symbol,
        ts=ts_str,
        horizon=horizon,
        price=round(close, 2),
        direction=direction,
        action=action,
        bias=round(bias, 3),
        confidence=round(confidence, 3),
        entry=levels.get("entry"),
        stop=levels.get("stop"),
        target=levels.get("target"),
        risk_reward=levels.get("risk_reward"),
        quality=levels.get("quality"),
        reasons=reasons,
    )
