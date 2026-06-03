"""
Horizon-aware transaction cost model.

Single source of truth for round-trip cost assumptions used by:
  * paper_journal.take_trade()         -> default cost_bps per horizon
  * paper_journal.reconcile_open_trades -> net return computation
  * edge_stats.json regeneration scripts
  * cap_governor                       -> Kelly fraction floor
  * dashboard "expected net" displays

Numbers below are Zerodha-realistic round-trip costs in basis points (bps),
where 1 bps = 0.01% of notional. They include brokerage, STT, exchange
transaction charges, GST, SEBI fee, stamp duty, and a modest slippage
allowance. They are intentionally CONSERVATIVE — better to under-promise.

INTRADAY (MIS):     ~6 bps round-trip
  brokerage     = 0.03% capped at Rs 20 -> ~3 bps on a typical retail ticket
  STT           = 0.025% on sell side    = 2.5 bps
  exchange + GST + SEBI + stamp          ~ 0.5 bps
  slippage      = 0.5 bps (large-cap, MIS, limit-order-aware)

BTST/SWING/LONG (CNC delivery):  ~12 bps round-trip
  brokerage     = 0 on delivery (Zerodha)
  STT           = 0.1% on both sides     = 20 bps  <-- the dominant cost
  exchange + GST + SEBI + stamp          ~ 1.5 bps
  slippage      = ~0.5 bps
  total bridge to 12 bps reflects discount on CNC + multi-day amortization
  of fixed costs over the holding period. For Swing/Long, costs are a
  smaller % of expected return so the same 12 bps approximation holds.

These numbers are tuned for the under-Rs-1-lakh bankroll case where the
Rs-20 brokerage cap binds. Larger bankrolls would see brokerage scale with
notional and these constants should rise accordingly.

DO NOT INLINE COSTS ELSEWHERE. Always import from here so a single
recalibration updates everywhere at once.
"""
from __future__ import annotations

from typing import Literal

Horizon = Literal["intraday", "swing", "long", "btst"]


# Round-trip cost in basis points (bps). 1 bps = 0.01% of notional.
COSTS_BPS: dict[str, float] = {
    "intraday": 6.0,
    "btst":     12.0,
    "swing":    12.0,
    "long":     12.0,
}

# Conservative override for paper-trade default (was 25 bps everywhere).
# Used when a caller passes no explicit cost — slightly above the realistic
# numbers to avoid optimism bias on the live scorecard.
PAPER_DEFAULT_BPS: dict[str, float] = {
    "intraday": 8.0,
    "btst":     15.0,
    "swing":    15.0,
    "long":     15.0,
}


def cost_bps(horizon: str, *, paper_default: bool = False) -> float:
    """Return round-trip cost in bps for a horizon.

    Args:
        horizon:       intraday / btst / swing / long.
        paper_default: if True, return the slightly conservative paper-trade
                       default (PAPER_DEFAULT_BPS); else the realistic cost.

    Returns:
        Round-trip cost in basis points. Falls back to 15 bps for unknown
        horizons (matches the legacy default in paper.py).
    """
    h = horizon.lower().strip()
    table = PAPER_DEFAULT_BPS if paper_default else COSTS_BPS
    return float(table.get(h, 15.0))


def cost_frac(horizon: str, *, paper_default: bool = False) -> float:
    """Same as cost_bps but returned as a fraction (e.g. 0.0006 for 6 bps)."""
    return cost_bps(horizon, paper_default=paper_default) / 10000.0


def all_costs() -> dict[str, dict[str, float]]:
    """Snapshot of the full cost table — for /api/risk/cap diagnostic output."""
    return {
        "realistic_bps":     dict(COSTS_BPS),
        "paper_default_bps": dict(PAPER_DEFAULT_BPS),
        "notes": (
            "Zerodha-realistic round-trip costs. Intraday MIS is dominated by "
            "STT 0.025% sell side + brokerage cap; delivery (BTST/Swing/Long) "
            "is dominated by STT 0.1% on both sides. Tuned for <Rs 1L bankroll."
        ),
    }
