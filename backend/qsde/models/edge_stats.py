"""
Honest edge statistics per horizon — the validated, cost-and-liquidity
stress-tested numbers, NOT the gross backtest fantasy.

These are read by the API so the dashboard/terminal can show the user the
REAL expected edge next to every signal: "intraday liquid-only net Sharpe
~0.9, concentration-driven" instead of implying every BUY is gold.

Source of truth is weights/edge_stats.json, written by:
  * scripts/simulate_strategies.py   (top-K long-only net Sharpe per horizon)
  * scripts/stress_test_intraday.py  (liquidity + slippage + concentration)

If the JSON is missing, we fall back to the SEED values below — the numbers
measured on 2026-06-01 from 5y of NSE data. Re-run the stress scripts after
each retrain and call write_edge_stats() to refresh.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def _path() -> str:
    return os.path.join(os.path.dirname(__file__), "weights", "edge_stats.json")


# Validated 2026-06-01 (5y NSE, AFML triple-barrier labels, fracdiff features,
# top-5 long-only, ADV>=10cr liquid universe, realistic friction).
# These are intentionally CONSERVATIVE — they're the stress-tested numbers,
# not the gross backtest.
_SEED: dict = {
    "as_of": "2026-06-01",
    "method": "top-5 long-only, ADV>=10cr, AFML triple-barrier, fracdiff",
    "horizons": {
        "intraday": {
            "net_sharpe": 0.93,           # @ 25bps all-in, ADV>=10cr
            "net_sharpe_gross_universe": 3.60,   # @ 15bps, all names (NOT tradeable — fill fiction)
            "win_rate": 0.495,
            "avg_trade_bps_net": 9,
            "concentration_top5pct_days": 0.88,  # 88% of return from best 5% of days
            "edge_band": "0.7-1.0",
            "tradeable": True,
            "caveats": [
                "Liquid names only (ADV>=10cr) — the gross 3.6 Sharpe was fill fiction.",
                "Concentration-driven: ~88% of return from best 5% of days. Most days flat/negative.",
                "Size small, survive flat stretches. The big days are also the least executable.",
            ],
        },
        "swing": {
            "net_sharpe": 0.30,           # @ 15bps, top-5
            "win_rate": 0.476,
            "avg_trade_bps_net": 10,
            "edge_band": "0.2-0.4",
            "tradeable": True,
            "caveats": [
                "Marginal edge. Borderline after costs.",
            ],
        },
        "long": {
            "net_sharpe": 0.43,           # @ 15bps, top-5, low cost-sensitivity
            "win_rate": 0.511,
            "avg_trade_bps_net": 67,
            "edge_band": "0.4-0.5",
            "tradeable": True,
            "caveats": [
                "Most robust / believable edge. Low turnover, not concentration-dependent.",
                "Modest in absolute terms (~0.43 net Sharpe) but steady.",
            ],
        },
    },
    "notes": (
        "L/S decile-spread benchmark is NEGATIVE net of costs — these long-only "
        "top-5 numbers are the realistic retail strategy, not market-neutral."
    ),
}


def read_edge_stats() -> dict:
    """Return the validated edge stats. JSON if present, else the seed."""
    p = _path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            log.warning("edge_stats.json unreadable (%s) — using seed", e)
    return _SEED


def write_edge_stats(stats: dict) -> str:
    """Persist edge stats to JSON (called by the stress-test scripts)."""
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    return p


def horizon_edge(horizon: str) -> Optional[dict]:
    """Convenience: the edge block for one horizon, or None."""
    return read_edge_stats().get("horizons", {}).get(horizon)
