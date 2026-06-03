"""
Risk governor API — position-risk cap diagnostics + manual tier control.

  GET  /api/risk/cap                       -> current tier, per-horizon cap,
                                              realized stats, next-tier readiness
  GET  /api/risk/costs                     -> horizon-aware cost table
  POST /api/risk/escalate?to=T1&reason=... -> request escalation (validates)
  POST /api/risk/deescalate?to=T0&reason=. -> manual de-escalation (always OK)
  GET  /api/risk/history                   -> tier-change audit trail

The governor only de-escalates AUTOMATICALLY on drift. Escalation requires
explicit user action through these endpoints so accidental risk increase
isn't possible.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query

from qsde.db.connection import read_sql
from qsde.risk.cap_governor import set_tier_user, state
from qsde.risk.costs import all_costs

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/risk/cap")
def risk_cap():
    """Current per-horizon cap fraction + governor diagnostic state.

    Returns enough information for the dashboard banner to render:
        current_tier, paper_sessions, per-horizon (realized stats, cap_fraction,
        drift_flag), next_tier readiness.
    """
    return state()


@router.get("/risk/costs")
def risk_costs():
    """Horizon-aware round-trip cost assumptions (single source of truth)."""
    return all_costs()


@router.post("/risk/escalate")
def risk_escalate(
    to: str = Query(..., description="Target tier: T1 | T2 | T3"),
    reason: str = Query(default="", description="Audit-log note"),
):
    """Request an escalation to a higher risk tier.

    Validated against minimum paper-session count + edge-confirmation per
    horizon. Returns {"ok": False, "error": ...} if the tier is not yet
    earned, so the user can't accidentally promote.
    """
    if not to.upper().startswith("T"):
        return {"ok": False, "error": "tier must be T1, T2, or T3"}
    return set_tier_user(to.upper(), reason=reason)


@router.post("/risk/deescalate")
def risk_deescalate(
    to: str = Query(..., description="Target tier: T0 | T1 | T2"),
    reason: str = Query(default="", description="Audit-log note"),
):
    """Manual de-escalation. Always permitted — you can always reduce risk."""
    if not to.upper().startswith("T"):
        return {"ok": False, "error": "tier must be T0, T1, or T2"}
    return set_tier_user(to.upper(), reason=reason or "manual de-escalation")


@router.get("/risk/history")
def risk_history(limit: int = Query(default=50, ge=1, le=200)):
    """Full audit trail of tier changes — who, when, why."""
    df = read_sql(
        """SELECT id, effective_at, tier_name, reason, changed_by
             FROM risk_governor_state
         ORDER BY effective_at DESC LIMIT :n""",
        params={"n": limit},
    )
    return {"history": df.to_dict("records"), "count": len(df)}
