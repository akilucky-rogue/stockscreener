"""
Budget-aware screener & position sizing (Phase 3).

Answers the user's core workflow question: "given my budget, which stocks can I
actually trade, at what size, and in what priority order?"

Two pure functions (no DB, no I/O — fully unit-tested):

  size_position(price, entry, stop, budget, risk_per_trade, lot_size)
      Combines the two binding constraints every retail trade faces:
        budgeted_qty = floor(budget / price)            (can you afford it?)
        max_qty      = floor(risk_per_trade / |entry-stop|)  (risk cap per trade)
        final_qty    = min(budgeted_qty, max_qty), floored to a lot multiple
      A name with final_qty == 0 is NOT tradeable in this budget and is dropped.

  screen_budget(candidates, budget, risk_per_trade, ...)
      Sizes every directional candidate, drops the untradeable ones, ranks the
      survivors by  strength × confidence ÷ risk, then greedily fits them into
      the budget (respecting max_positions and an optional per-position cap).

Ranking score (transparent / white-box)
    strength = alpha_pct  (if an ML alpha percentile is provided)
             | |bias|     (else the rule-based intraday bias)
             | confidence (else)
    risk     = risk_score (if provided, 0..1)
             | min(stop_distance% / 5%, 1.0)   (wider stop ⇒ riskier)
    rank_score = strength × confidence ÷ max(risk, ε)     (higher = better)
"""

from __future__ import annotations

import math
from typing import Optional

_EPS = 1e-9


def size_position(
    price: float,
    entry: Optional[float],
    stop: Optional[float],
    *,
    budget: float,
    risk_per_trade: float,
    lot_size: int = 1,
) -> dict:
    """Position size from budget + per-trade risk. See module docstring."""
    out = {
        "budgeted_qty": 0,
        "max_qty": 0,
        "final_qty": 0,
        "lot_size": int(lot_size),
        "capital_required": 0.0,
        "risk_at_stop": None,
        "affordable": False,
    }
    if price is None or price <= 0 or budget is None or budget <= 0:
        return out
    lot = max(int(lot_size), 1)

    budgeted_qty = math.floor(budget / price)

    entry_eff = entry if (entry and entry > 0) else price
    if stop is not None and abs(entry_eff - stop) > _EPS and risk_per_trade and risk_per_trade > 0:
        max_qty = math.floor(risk_per_trade / abs(entry_eff - stop))
    else:
        # No stop / no risk cap supplied → budget is the only constraint.
        max_qty = budgeted_qty

    raw = max(min(budgeted_qty, max_qty), 0)
    final_qty = (raw // lot) * lot  # floor to whole lots

    capital_required = round(final_qty * price, 2)
    risk_at_stop = (
        round(final_qty * abs(entry_eff - stop), 2) if stop is not None else None
    )

    out.update(
        budgeted_qty=int(budgeted_qty),
        max_qty=int(max_qty),
        final_qty=int(final_qty),
        capital_required=capital_required,
        risk_at_stop=risk_at_stop,
        affordable=bool(final_qty > 0 and capital_required <= budget + _EPS),
    )
    return out


def _rank_score(c: dict, entry: Optional[float], stop: Optional[float]) -> float:
    conf = c.get("confidence")
    conf = float(conf) if conf is not None else 0.5

    alpha = c.get("alpha_pct", c.get("alpha"))
    bias = c.get("bias")
    if alpha is not None:
        strength = abs(float(alpha))
    elif bias is not None:
        strength = abs(float(bias))
    else:
        strength = conf

    risk = c.get("risk_score")
    if risk is None:
        if stop is not None and entry and entry > 0:
            stop_pct = abs(entry - stop) / entry
            risk = min(max(stop_pct / 0.05, 0.05), 1.0)  # 5% stop ⇒ risk 1.0
        else:
            risk = 0.5
    risk = max(float(risk), 1e-6)

    return float(strength) * float(conf) / risk


def screen_budget(
    candidates: list[dict],
    *,
    budget: float,
    risk_per_trade: float,
    max_positions: int = 10,
    lot_size: int = 1,
    lot_size_map: Optional[dict] = None,
    max_position_weight: float = 1.0,
    allow_short: bool = True,
) -> dict:
    """
    Rank + size + budget-fit a list of candidate signals.

    Each candidate is a dict with at least: symbol, direction, and price (or
    entry). Optional: stop, target, confidence, alpha_pct/alpha, bias,
    risk_score, risk_reward, quality.

    Returns {"selected": [...sized+ranked...], "summary": {...}}.
    """
    lot_size_map = lot_size_map or {}
    per_position_cap = max(min(max_position_weight, 1.0), 0.0) * budget if budget else 0.0

    sized: list[dict] = []
    for c in candidates:
        direction = int(c.get("direction", 0) or 0)
        if direction == 0:
            continue
        if not allow_short and direction < 0:
            continue
        price = c.get("price") or c.get("entry")
        if not price or price <= 0:
            continue
        entry = c.get("entry", price)
        stop = c.get("stop")
        lot = int(lot_size_map.get(c.get("symbol"), lot_size))

        s = size_position(
            price, entry, stop,
            budget=per_position_cap, risk_per_trade=risk_per_trade, lot_size=lot,
        )
        if not s["affordable"]:
            continue

        row = {**c, **s, "rank_score": round(_rank_score(c, entry, stop), 6)}
        sized.append(row)

    sized.sort(key=lambda x: x["rank_score"], reverse=True)

    selected: list[dict] = []
    deployed = 0.0
    total_risk = 0.0
    for row in sized:
        if len(selected) >= max_positions:
            break
        if deployed + row["capital_required"] <= budget + _EPS:
            selected.append(row)
            deployed += row["capital_required"]
            total_risk += row["risk_at_stop"] or 0.0

    summary = {
        "budget": round(float(budget), 2),
        "risk_per_trade": round(float(risk_per_trade), 2),
        "n_candidates": len(candidates),
        "n_tradeable": len(sized),
        "n_selected": len(selected),
        "capital_deployed": round(deployed, 2),
        "cash_remaining": round(budget - deployed, 2),
        "total_risk_at_stop": round(total_risk, 2),
        "max_positions": max_positions,
        "max_position_weight": max_position_weight,
    }
    return {"selected": selected, "summary": summary}


__all__ = ["size_position", "screen_budget"]
