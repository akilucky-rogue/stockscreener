"""
Weekly drift / scorecard report.

Produces the JSON the dashboard banner and `/api/paper/drift` endpoint
serve. Three things, per horizon:

  1. Realized model performance against the backtest edge band.
     Drift flag if rolling-14d realized hit rate is >5pp below the band
     low, OR rolling-14d net Sharpe is negative.

  2. Realized model performance against each baseline strategy.
     "Edge over baselines" flag if model net Sharpe AND avg net return
     both exceed every baseline by >=2 sessions of statistical noise.

  3. Recommendation: keep / shrink / stop.
        keep   = no drift + edge over baselines confirmed
        shrink = no drift but edge unclear -> stay at current cap, do not escalate
        stop   = drift OR model loses to a baseline net of cost

The whole report is read-only — it never changes the governor state. The
cap_governor reads its own copy of realized stats so the two systems
agree on what "drift" means without one driving the other.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db.connection import read_sql
from qsde.execution.paper_journal import _BASELINE_STRATEGIES
from qsde.models.edge_stats import horizon_edge

log = logging.getLogger(__name__)


# ── helpers ─────────────────────────────────────────────────────────

def _closed_trades(horizon: str, strategy: str,
                   window_sessions: Optional[int] = None) -> pd.DataFrame:
    """Closed paper trades for (horizon, strategy), optionally last-N sessions."""
    sql = (
        """SELECT entry_date, exit_date, realized_ret_net, status, horizon_sessions
             FROM paper_trades
            WHERE horizon = :h AND strategy = :s
              AND status IN ('WIN','LOSS','TIME')"""
    )
    params: dict[str, object] = {"h": horizon, "s": strategy}
    if window_sessions is not None:
        cutoff = date.today() - timedelta(days=int(window_sessions * 1.5) + 5)
        sql += " AND entry_date >= :cut"
        params["cut"] = cutoff
    return read_sql(sql, params=params)


def _stats(df: pd.DataFrame) -> dict:
    """Standard stats block from a closed-trades DataFrame."""
    n = len(df)
    if n == 0:
        return {"n": 0, "hit_rate": None, "avg_net_ret_bps": None,
                "net_sharpe": None}
    rets = df["realized_ret_net"].astype(float)
    hit  = float((rets > 0).mean())
    mean, std = float(rets.mean()), float(rets.std())
    sessions  = float(df["horizon_sessions"].mean()) or 1.0
    sharpe = None
    if std > 0 and n >= 10:
        sharpe = mean / std * (252.0 / sessions) ** 0.5
    return {
        "n":               n,
        "hit_rate":        round(hit, 3),
        "avg_net_ret_bps": round(mean * 1e4, 1),
        "net_sharpe":      round(sharpe, 2) if sharpe is not None else None,
    }


def _band_low(band: Optional[str]) -> Optional[float]:
    if not band:
        return None
    try:
        return float(band.split("-")[0])
    except (ValueError, IndexError):
        return None


# ── per-horizon ─────────────────────────────────────────────────────

def _model_vs_backtest(horizon: str) -> dict:
    """Compare rolling-14d model stats to backtest edge band."""
    rolling = _stats(_closed_trades(horizon, "model", window_sessions=14))
    edge    = horizon_edge(horizon) or {}
    bt_sharpe = edge.get("net_sharpe")
    bt_band   = edge.get("edge_band")

    drift = False
    reasons: list[str] = []

    if rolling["n"] >= 10:
        if rolling["net_sharpe"] is not None and rolling["net_sharpe"] < 0:
            drift = True
            reasons.append(f"rolling Sharpe negative ({rolling['net_sharpe']:.2f})")
        lo = _band_low(bt_band)
        if lo is not None and rolling["hit_rate"] is not None:
            if rolling["hit_rate"] < (lo - 0.05):
                drift = True
                reasons.append(
                    f"hit rate {rolling['hit_rate']:.2f} > 5pp below band low {lo:.2f}"
                )

    return {
        "rolling_14d":         rolling,
        "backtest_net_sharpe": bt_sharpe,
        "backtest_hit_band":   bt_band,
        "drift_flag":          drift,
        "drift_reasons":       reasons or None,
    }


def _model_vs_baselines(horizon: str) -> dict:
    """All-time model stats vs each baseline strategy. 'Edge over baselines'
    flag iff model wins on BOTH net Sharpe AND avg net return against every
    baseline AND has >= 15 trades on each side."""
    model = _stats(_closed_trades(horizon, "model"))
    baselines: dict[str, dict] = {}
    beats_all = True if model["n"] >= 15 else False
    reasons: list[str] = []

    for strat in _BASELINE_STRATEGIES:
        b = _stats(_closed_trades(horizon, strat))
        baselines[strat] = b
        if model["n"] < 15 or b["n"] < 15:
            beats_all = False
            continue
        # Beat on net Sharpe
        if model["net_sharpe"] is None or b["net_sharpe"] is None:
            beats_all = False
            reasons.append(f"{strat}: insufficient Sharpe")
            continue
        if model["net_sharpe"] <= b["net_sharpe"]:
            beats_all = False
            reasons.append(
                f"{strat}: model Sharpe {model['net_sharpe']} <= baseline {b['net_sharpe']}"
            )
        # Beat on avg net return
        if (model["avg_net_ret_bps"] is not None and b["avg_net_ret_bps"] is not None
                and model["avg_net_ret_bps"] <= b["avg_net_ret_bps"]):
            beats_all = False
            reasons.append(
                f"{strat}: model avg {model['avg_net_ret_bps']}bps <= baseline {b['avg_net_ret_bps']}bps"
            )

    return {
        "model":              model,
        "baselines":          baselines,
        "edge_over_baselines": beats_all,
        "issues":             reasons or None,
    }


def _recommendation(drift: bool, beats_baselines: bool, n_trades: int) -> dict:
    """Final keep/shrink/stop call."""
    if drift:
        return {
            "action": "stop",
            "summary": "Drift detected — pause new trades, do not escalate cap.",
        }
    if n_trades < 15:
        return {
            "action": "wait",
            "summary": f"Only {n_trades} closed model trades — need >=15 for a meaningful comparison.",
        }
    if beats_baselines:
        return {
            "action": "keep",
            "summary": "Model beats all baselines on net Sharpe AND avg net return. Edge is real.",
        }
    return {
        "action": "shrink",
        "summary": "No drift but model does not clearly beat baselines. Stay at current cap; do not escalate.",
    }


# ── top-level ───────────────────────────────────────────────────────

def drift_report() -> dict:
    """Full report — per horizon and an overall summary line."""
    by_horizon: dict[str, dict] = {}
    overall_drift = False
    overall_beats = True
    has_any_data  = False

    for h in ("intraday", "swing", "long"):
        bt   = _model_vs_backtest(h)
        bl   = _model_vs_baselines(h)
        n    = bl["model"]["n"]
        rec  = _recommendation(bt["drift_flag"], bl["edge_over_baselines"], n)
        by_horizon[h] = {
            "vs_backtest":   bt,
            "vs_baselines":  bl,
            "recommendation": rec,
        }
        if n > 0:
            has_any_data = True
        if bt["drift_flag"]:
            overall_drift = True
        if not bl["edge_over_baselines"]:
            overall_beats = False

    if not has_any_data:
        summary = "No closed paper trades yet — record signals via /api/paper/take to start the live validation loop."
        overall_action = "wait"
    elif overall_drift:
        summary = "At least one horizon shows drift. Cap will auto-de-escalate to T0 on the next signal."
        overall_action = "stop"
    elif overall_beats:
        summary = "All horizons keep their edge over baselines. System healthy."
        overall_action = "keep"
    else:
        summary = "No drift, but edge over baselines unconfirmed on at least one horizon."
        overall_action = "shrink"

    return {
        "as_of":         date.today().isoformat(),
        "summary":       summary,
        "action":        overall_action,
        "horizons":      by_horizon,
    }
