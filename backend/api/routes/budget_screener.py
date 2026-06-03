"""
Budget screener API.

  POST /api/screener/budget
    {
      "budget": 200000,
      "risk_per_trade": 5000,
      "max_positions": 10,
      "lot_size": 1,
      "max_position_weight": 0.25,
      "candidates": [ {signal dicts} ]    # optional; falls back to live loop
    }

Returns the ranked, risk-sized, budget-fitted selection. If `candidates` is
omitted, it uses the most recent per-symbol signals from the in-process live
loop (start it via POST /api/live/start first).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body

from qsde.risk.budget_screener import screen_budget
from qsde.live.engine import latest_signals

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/screener/budget")
def budget_screen(payload: dict = Body(default={})):
    budget = float(payload.get("budget", 0) or 0)
    risk_per_trade = float(payload.get("risk_per_trade", 0) or 0)
    if budget <= 0 or risk_per_trade <= 0:
        return {"error": "budget and risk_per_trade must both be > 0"}

    candidates = payload.get("candidates")
    source = "body"
    if not candidates:
        candidates = latest_signals()
        source = "live_loop"
    if not candidates:
        return {
            "selected": [],
            "summary": {"n_candidates": 0},
            "note": "No candidates. Pass 'candidates', or POST /api/live/start to begin the live loop.",
        }

    result = screen_budget(
        candidates,
        budget=budget,
        risk_per_trade=risk_per_trade,
        max_positions=int(payload.get("max_positions", 10)),
        lot_size=int(payload.get("lot_size", 1)),
        lot_size_map=payload.get("lot_size_map"),
        max_position_weight=float(payload.get("max_position_weight", 1.0)),
        allow_short=bool(payload.get("allow_short", True)),
    )
    result["candidate_source"] = source
    return result
