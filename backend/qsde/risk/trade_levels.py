"""
Trade level computation: entry, target, stop-loss, and risk-reward ratio.

Design philosophy
-----------------
A direction-only buy/sell/hold signal is not actionable for a discretionary
trader -- it needs price levels to plan around. We attach three levels and
an R:R to every directional signal:

  * entry   -- the latest close (display price). In live trading this
               would be next-day open; for analysis screens we use the last
               available close.
  * target  -- profit-taking level. Combines two views:
                   (a) the model's predicted return -> price_target_model
                   (b) a volatility-based floor       -> price_target_vol
               We use the FARTHER of the two from entry, in the direction
               of the trade. This way the target is never tighter than the
               natural noise of the stock.
  * stop    -- volatility-based stop = N * ATR away from entry, on the
               losing side. ATR (Average True Range) is the most widely
               used volatility proxy for stop placement and scales
               naturally across high- and low-volatility names.

The multipliers (N for stop, M for target floor) scale with horizon -- a
1-day intraday trade needs tighter stops than a 20-day swing, otherwise
intraday noise will knock the trade out before the thesis plays out.

  intraday (1d):   0.75 * ATR stop, 1.0 * ATR target floor
  swing    (5d):   1.5  * ATR stop, 2.0 * ATR target floor
  long     (20d):  2.5  * ATR stop, 4.0 * ATR target floor

These multipliers are calibrated such that:
  * a stop = 1 * sigma_horizon, where sigma scales as sqrt(horizon_days)
  * 2:1 target/stop minimum baked in
  * intraday is wider relative to ATR_1 because daily ATR is a 14-day
    average, so a fresh 1d move can exceed the rolling-14 ATR by ~30%.

The R:R (risk:reward) is reported so the user can filter -- we flag any
trade with R:R < 1.5 as low quality. For HOLD signals (direction == 0) we
return None for all levels.
"""

from __future__ import annotations

from typing import Optional, TypedDict


# Per-horizon ATR multipliers. Recalibrated so vol-floor-only trades clear
# the _MIN_QUALITY_RR bar — otherwise swing/intraday vol-floor trades were
# permanently flagged "low quality" no matter what the model predicted.
#   intraday:  R:R = 1.5 / 0.75 = 2.00
#   swing:     R:R = 2.5 / 1.5  = 1.67
#   long:      R:R = 4.5 / 2.5  = 1.80
# All baseline R:Rs now >= 1.5; model-driven targets only IMPROVE the R:R
# (target_distance = max(model_target, vol_floor)).
_HORIZON_MULTIPLIERS: dict[str, tuple[float, float]] = {
    # horizon : (stop_atr_mult, target_atr_floor_mult)
    "intraday": (0.75, 1.50),
    "swing":    (1.50, 2.50),
    "long":     (2.50, 4.50),
}

# R:R below this is flagged as low-quality (trader should probably skip).
_MIN_QUALITY_RR = 1.5

# Per-horizon ATR fallback when the factor_pit row is missing. Scales with
# sqrt(horizon_days) since stock vol scales as sqrt(time). A flat 2% (the
# old value) was right for swing but too wide for intraday and too narrow
# for long, producing the wrong stop distances on the two extreme horizons.
_ATR_FALLBACK: dict[str, float] = {
    "intraday": 0.010,   # ~1% sigma per 1-day
    "swing":    0.022,   # ~2.2% sigma per 5-day
    "long":     0.045,   # ~4.5% sigma per 20-day
}


class TradeLevels(TypedDict, total=False):
    entry: Optional[float]
    target: Optional[float]
    stop: Optional[float]
    risk_reward: Optional[float]
    atr_pct: Optional[float]
    quality: Optional[str]      # "good" | "low" | None for HOLD
    notes: Optional[str]


def compute_trade_levels(
    price: Optional[float],
    atr_pct: Optional[float],
    predicted_return: Optional[float],
    direction: Optional[int],
    horizon: str,
) -> TradeLevels:
    """
    Compute entry / target / stop / R:R for a signal.

    Args:
        price:            Latest close (used as entry price).
        atr_pct:          ATR as a FRACTION of price (e.g. 0.018 for 1.8%).
                          Sourced from the `tech_atr_pct` factor. Note that
                          factors/technical.py stores this as a PERCENT
                          (3.89 means 3.89%); we auto-detect that scale and
                          divide by 100 -- see the normalization step below.
        predicted_return: Model-predicted forward return (e.g. 0.03 for +3%).
                          Sign should match `direction`; this is the magnitude
                          of expected move over the horizon.
        direction:        -1 / 0 / +1 from the signal classifier.
        horizon:          One of "intraday" / "swing" / "long".

    Returns:
        dict with entry / target / stop / risk_reward / atr_pct / quality / notes.
        For HOLD signals or missing data, levels are None and `notes` explains.
    """
    out: TradeLevels = {
        "entry": None,
        "target": None,
        "stop": None,
        "risk_reward": None,
        "atr_pct": atr_pct,
        "quality": None,
        "notes": None,
    }

    # Need a price to anchor.
    if price is None or price <= 0:
        out["notes"] = "no_price"
        return out

    # HOLD -> no trade plan.
    if not direction or direction == 0:
        out["entry"] = price
        out["notes"] = "hold"
        return out

    # Need ATR for stops. If it's missing, fall back to a horizon-scaled
    # default (sqrt(horizon_days) scaling — see _ATR_FALLBACK). The old
    # one-size-fits-all 2% was wrong for intraday and long.
    if atr_pct is None or atr_pct <= 0:
        atr_pct_effective = _ATR_FALLBACK.get(horizon, 0.022)
        out["notes"] = f"atr_fallback_{atr_pct_effective*100:.1f}pct"
    else:
        # Auto-detect storage scale. `factors/technical.py:atr_pct()` stores
        # this as PERCENT (3.89 means 3.89%), but the math here wants a
        # FRACTION (0.0389). Any plausible equity ATR/price ratio is well
        # under 100% per period, so anything > 1.0 is unambiguously the
        # percent-form -- divide by 100. This makes the function agnostic
        # to which convention upstream uses.
        atr_pct_effective = atr_pct / 100.0 if atr_pct > 1.0 else atr_pct
    # Surface the FRACTION form back to the caller so the UI's "ATR x.xx%"
    # display computes off the correct number.
    out["atr_pct"] = atr_pct_effective

    mults = _HORIZON_MULTIPLIERS.get(horizon)
    if mults is None:
        out["notes"] = f"unknown_horizon_{horizon}"
        return out
    stop_mult, target_floor_mult = mults

    atr_abs = atr_pct_effective * price

    # Volatility-based target floor -- guaranteed minimum profit target.
    vol_target_abs = target_floor_mult * atr_abs

    # Model-predicted target. Use absolute value -- direction supplies sign.
    pred = abs(predicted_return) if predicted_return is not None else 0.0
    model_target_abs = pred * price

    # Use the LARGER of model vs vol-floor for target distance.
    target_distance = max(vol_target_abs, model_target_abs)

    # Stop distance is purely volatility-based.
    stop_distance = stop_mult * atr_abs

    # Apply direction.
    if direction > 0:  # BUY
        target = price + target_distance
        stop = price - stop_distance
    else:              # SELL / SHORT
        target = price - target_distance
        stop = price + stop_distance

    # Risk-reward ratio = (reward) / (risk).
    if stop_distance > 0:
        rr = target_distance / stop_distance
    else:
        rr = None

    out["entry"] = round(price, 2)
    out["target"] = round(target, 2)
    out["stop"] = round(stop, 2)
    out["risk_reward"] = round(rr, 2) if rr is not None else None
    out["quality"] = (
        "good" if (rr is not None and rr >= _MIN_QUALITY_RR) else "low"
    )

    # Annotate which leg was the binding constraint -- useful for the UI.
    if model_target_abs > vol_target_abs:
        binding = "model"
    else:
        binding = "vol_floor"
    if out["notes"]:
        out["notes"] = f"{out['notes']};target={binding}"
    else:
        out["notes"] = f"target={binding}"

    return out
