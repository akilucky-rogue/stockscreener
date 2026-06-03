"""
Paper-trade journal API — the live-validation loop.

  POST /api/paper/take?symbol=KEI&horizon=intraday   -> record a paper trade
  POST /api/paper/take-baselines?horizon=swing       -> record baseline trades
  POST /api/paper/reconcile                           -> resolve elapsed trades
  GET  /api/paper/track-record                        -> live scorecard (incl. baselines)
  GET  /api/paper/drift                               -> drift / edge-over-baselines report
  GET  /api/paper/trades?status=OPEN                  -> list trades

Long-only paper trades; reconciliation walks real OHLCV against triple
barriers and reports net-of-cost returns so the live record is directly
comparable to the backtested edge band.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query

from qsde.db.connection import read_sql
from qsde.execution.auto_taker import (
    DEFAULT_MIN_RANK,
    DEFAULT_TOP_K,
    take_top_model_signals,
    take_top_model_signals_all_horizons,
)
from qsde.execution.drift_report import drift_report
from qsde.execution.paper_journal import (
    reconcile_open_trades,
    take_baseline_trades,
    take_trade,
    track_record,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/paper/take")
def paper_take(
    symbol: str = Query(..., description="NSE symbol"),
    horizon: str = Query(default="intraday", description="intraday | swing | long"),
    cost_bps: Optional[float] = Query(default=None, ge=0, le=200,
        description="Round-trip cost bps. None -> horizon-aware default from qsde.risk.costs."),
    entry_price: Optional[float] = Query(default=None,
        description="Your actual decision-time price; defaults to signal entry."),
    strategy: str = Query(default="model",
        description="Strategy tag. 'model' for ML signal; baselines use /paper/take-baselines."),
):
    """Record a paper trade from the latest signal for this symbol+horizon."""
    return take_trade(symbol, horizon, cost_bps=cost_bps,
                      entry_price=entry_price, strategy=strategy)


@router.post("/paper/take-baselines")
def paper_take_baselines(
    horizon: str = Query(default="swing", description="intraday | swing | long"),
):
    """Record one paper trade per baseline strategy (top-momentum, nifty,
    random) for the chosen horizon. Idempotent per (strategy, symbol, day).
    Called by daily_eod.py automatically."""
    return take_baseline_trades(horizon=horizon)


@router.post("/paper/auto-take")
def paper_auto_take(
    horizon: Optional[str] = Query(default=None,
        description="intraday | swing | long. None = run for all three."),
    top_k: int = Query(default=DEFAULT_TOP_K, ge=1, le=10,
        description="How many top-ranked model signals to take per horizon."),
    min_rank: float = Query(default=DEFAULT_MIN_RANK, ge=0.0, le=1.0,
        description="Minimum cross-sectional ranking percentile (0..1)."),
    direction: int = Query(default=1, ge=-1, le=1,
        description="+1 long-only (default), -1 short-only, 0 = both."),
    min_predicted_return: float = Query(default=0.0, ge=-1.0, le=1.0,
        description="Per-pick floor on predicted_return (fraction). 0.0 = no floor."),
    skip_bearish_days: bool = Query(default=False,
        description="Skip all picks if top-K median predicted_return is negative."),
):
    """Auto-record top-K model paper trades for the latest signal date.

    Idempotent — re-running the same day adds nothing new. Called by
    daily_eod.py as step 6/7 between paper reconcile and baseline recording.
    """
    if horizon:
        return take_top_model_signals(
            horizon=horizon, top_k=top_k, min_rank=min_rank, direction=direction,
            min_predicted_return=min_predicted_return,
            skip_bearish_days=skip_bearish_days,
        )
    return take_top_model_signals_all_horizons(
        top_k=top_k, min_rank=min_rank, direction=direction,
        min_predicted_return=min_predicted_return,
        skip_bearish_days=skip_bearish_days,
    )


@router.post("/paper/reconcile")
def paper_reconcile():
    """Resolve all OPEN paper trades whose barrier window has elapsed."""
    return reconcile_open_trades()


@router.get("/paper/track-record")
def paper_track_record(
    horizon: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None,
        description="Filter to one strategy. None = breakout per strategy so model vs baselines is visible."),
):
    """Live scorecard: hit rate, avg net return, realized Sharpe vs backtest."""
    return track_record(horizon, strategy=strategy)


@router.get("/paper/drift")
def paper_drift():
    """Drift / edge-over-baselines report. Read this every week before
    deciding whether to escalate the risk cap."""
    return drift_report()


@router.get("/paper/trades")
def paper_trades(
    status: Optional[str] = Query(default=None, description="OPEN | WIN | LOSS | TIME"),
    strategy: Optional[str] = Query(default=None,
        description="model | baseline_top_momentum | baseline_nifty | baseline_random"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """List paper trades, newest first."""
    clauses: list[str] = []
    params: dict[str, object] = {"lim": limit}
    if status:
        clauses.append("status = :st")
        params["st"] = status.upper()
    if strategy:
        clauses.append("strategy = :strat")
        params["strat"] = strategy
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    df = read_sql(
        f"""SELECT id, symbol, horizon, strategy, entry_date, entry_price, direction,
                   target_price, stop_price, rank_pct, status, exit_date,
                   exit_price, realized_ret_net, taken_at
              FROM paper_trades {where}
          ORDER BY taken_at DESC LIMIT :lim""",
        params=params,
    )
    return {"trades": df.to_dict("records"), "count": len(df)}
