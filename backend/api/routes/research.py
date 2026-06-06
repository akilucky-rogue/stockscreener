"""
Research API endpoints — Comps, DCF, Earnings, Screener, Sector.

Adapted from Anthropic financial-services skill templates.
"""

from fastapi import APIRouter, Query
from typing import Optional

router = APIRouter()


@router.get("/research/comps/{symbol}")
def get_comps_analysis(symbol: str):
    """Comparable company analysis — peer valuation with quartile stats."""
    from qsde.research.comps_engine import build_comps_analysis
    return build_comps_analysis(symbol.upper())


@router.get("/research/dcf/{symbol}")
def get_dcf_valuation(
    symbol: str,
    scenario: str = Query(default="base", description="bear, base, or bull"),
):
    """DCF valuation model — 5-year projections, WACC, sensitivity grid."""
    from qsde.research.dcf_engine import build_dcf_valuation
    return build_dcf_valuation(symbol.upper(), scenario)


@router.get("/research/earnings/{symbol}")
def get_earnings_snapshot(symbol: str):
    """Earnings snapshot — beat/miss analysis, margin trends."""
    from qsde.research.earnings_engine import build_earnings_snapshot
    return build_earnings_snapshot(symbol.upper())


@router.get("/research/screen")
def run_stock_screen(
    preset: Optional[str] = Query(default=None, description="value, growth, quality, momentum, dividend"),
    sector: Optional[str] = Query(default=None),
    sort_by: Optional[str] = Query(default=None),
    sort_asc: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    include_momentum: bool = Query(default=False),
):
    """Multi-criteria stock screener with preset filters."""
    from qsde.research.screener_engine import run_screen
    return run_screen(
        preset=preset,
        sector=sector,
        sort_by=sort_by,
        sort_asc=sort_asc,
        limit=limit,
        include_momentum=include_momentum,
    )


@router.get("/research/sectors")
def get_sectors():
    """List all sectors with company counts."""
    from qsde.research.sector_engine import get_sector_list
    return {"sectors": get_sector_list()}


@router.get("/research/sector/{sector}")
def get_sector_overview(sector: str):
    """Sector overview — aggregate stats, leaders, competitive positioning."""
    from qsde.research.sector_engine import build_sector_overview
    return build_sector_overview(sector)


# ──────────────────────────────────────────────────────────────────────
# Tier 1 rule-based engine diagnostics
# ──────────────────────────────────────────────────────────────────────

@router.get("/research/tier1/diagnostics")
def get_tier1_diagnostics(
    lookback_days: int = Query(default=60, ge=10, le=365,
                               description="History window for IC sparkline."),
):
    """Tier 1 engine state: per-factor IC, hit rates, composite weights,
    plus realized-Sharpe comparison vs ML + baselines.

    Returns
    -------
    {
        "factors": {
            "jt":   {"swing": {...}, "long": {...}},
            "mop":  {"swing": {...}, "long": {...}},
            "bab":  {"swing": {...}, "long": {...}},
            "rsi2": {"swing": {...}, "long": {...}},
        },
        "composite_vs_baselines": {
            "tier1_composite":      {"swing": {...}, "long": {...}},
            "model":                {"swing": {...}, "long": {...}},
            "baseline_top_momentum":{"swing": {...}, "long": {...}},
            ...
        },
        "as_of": "2026-06-06",
        "note": "Cold start..." | None
    }

    Per-factor entry shape:
        {
            "ic_60d":          float | null,
            "hit_rate_top":    float | null,
            "hit_rate_bot":    float | null,
            "sharpe_ann":      float | null,
            "n_observations":  int,
            "composite_weight": float,
            "ic_history":      [{"date": "YYYY-MM-DD", "ic": 0.04}, ...],
        }
    """
    from datetime import date, timedelta
    from qsde.db.connection import read_sql

    today = date.today()
    cutoff = today - timedelta(days=lookback_days)

    # Latest per-factor row from the materialized-style view.
    latest = read_sql(
        "SELECT * FROM rule_factor_ic_latest ORDER BY factor_name, horizon"
    )

    # 60d IC history for sparklines.
    history = read_sql(
        """
        SELECT factor_name, horizon, as_of_date, ic_60d
          FROM rule_factor_ic
         WHERE as_of_date >= :since
         ORDER BY factor_name, horizon, as_of_date
        """,
        params={"since": cutoff},
    )

    factors: dict[str, dict[str, dict]] = {}
    cold_start = True
    for fname in ("jt", "mop", "bab", "rsi2"):
        factors[fname] = {}
        for hzn in ("swing", "long"):
            row_match = latest[(latest["factor_name"] == fname)
                               & (latest["horizon"] == hzn)] if not latest.empty else None
            row = row_match.iloc[0] if (row_match is not None and not row_match.empty) else None

            ic_hist = []
            if not history.empty:
                sub = history[(history["factor_name"] == fname)
                              & (history["horizon"] == hzn)]
                ic_hist = [
                    {"date": r["as_of_date"].isoformat() if hasattr(r["as_of_date"], "isoformat")
                                                       else str(r["as_of_date"]),
                     "ic": float(r["ic_60d"]) if r["ic_60d"] is not None else None}
                    for _, r in sub.iterrows()
                ]

            if row is None:
                factors[fname][hzn] = {
                    "ic_60d": None, "hit_rate_top": None, "hit_rate_bot": None,
                    "sharpe_ann": None, "n_observations": 0,
                    "composite_weight": 0.0, "ic_history": ic_hist,
                }
            else:
                n_obs = int(row["n_observations"]) if row["n_observations"] is not None else 0
                if n_obs >= 20:
                    cold_start = False
                factors[fname][hzn] = {
                    "ic_60d":          None if row["ic_60d"] is None else float(row["ic_60d"]),
                    "hit_rate_top":    None if row["hit_rate_top"] is None else float(row["hit_rate_top"]),
                    "hit_rate_bot":    None if row["hit_rate_bot"] is None else float(row["hit_rate_bot"]),
                    "sharpe_ann":      None if row["sharpe_ann"] is None else float(row["sharpe_ann"]),
                    "n_observations":  n_obs,
                    "composite_weight": float(row["composite_weight"]) if row["composite_weight"] is not None else 0.0,
                    "ic_history":      ic_hist,
                }

    # Realized stats per strategy from paper_trades (closed trades only).
    perf = read_sql(
        """
        SELECT strategy, horizon,
               COUNT(*) FILTER (WHERE status IN ('WIN','LOSS','TIME')) AS n_closed,
               COUNT(*) FILTER (WHERE status = 'WIN') AS n_wins,
               AVG(realized_ret_net) FILTER (WHERE status IN ('WIN','LOSS','TIME')) AS avg_net_ret,
               STDDEV_SAMP(realized_ret_net) FILTER (WHERE status IN ('WIN','LOSS','TIME')) AS std_net_ret
          FROM paper_trades
         WHERE strategy IN ('model',
                            'tier1_composite','tier1_jt','tier1_mop','tier1_bab','tier1_rsi2',
                            'baseline_top_momentum','baseline_nifty','baseline_random')
         GROUP BY strategy, horizon
        """
    )

    composite_vs_baselines: dict[str, dict[str, dict]] = {}
    if not perf.empty:
        import math
        for _, r in perf.iterrows():
            strat, hzn = str(r["strategy"]), str(r["horizon"])
            n_closed = int(r["n_closed"]) if r["n_closed"] is not None else 0
            n_wins = int(r["n_wins"]) if r["n_wins"] is not None else 0
            avg = float(r["avg_net_ret"]) if r["avg_net_ret"] is not None else None
            std = float(r["std_net_ret"]) if r["std_net_ret"] is not None else None
            sharpe = None
            if avg is not None and std is not None and std > 0 and math.isfinite(std):
                sharpe = (avg / std) * math.sqrt(252)
            composite_vs_baselines.setdefault(strat, {})[hzn] = {
                "n_closed":         n_closed,
                "hit_rate":         (n_wins / n_closed) if n_closed else None,
                "avg_net_ret_bps":  None if avg is None else round(avg * 1e4, 1),
                "realized_sharpe":  None if sharpe is None else round(sharpe, 2),
            }

    return {
        "as_of": today.isoformat(),
        "factors": factors,
        "composite_vs_baselines": composite_vs_baselines,
        "note": ("Cold start — need 20+ resolved signals per factor for "
                 "meaningful IC. Composite is running equal-weight 0.25 "
                 "until then.") if cold_start else None,
    }
