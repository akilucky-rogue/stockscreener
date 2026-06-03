"""
Backtest API endpoints -- reads from the model_runs audit table.

Every training run logs a row to model_runs with IC, Sharpe, DSR, train/test
date ranges, hyperparameters, and the feature importance JSON. The /backtest
frontend page reads from here.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query

from qsde.db import read_sql

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/backtest/runs")
def get_runs(
    horizon: str = Query(default="all"),
    limit: int = Query(default=50, ge=1, le=500),
):
    """Recent model runs, newest first."""
    try:
        clauses = []
        params = {"limit": limit}
        if horizon != "all":
            clauses.append("horizon = :horizon")
            params["horizon"] = horizon
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        df = read_sql(
            f"""SELECT run_id, horizon, model_type, train_start, train_end,
                       test_start, test_end, n_features, n_samples,
                       ic_mean, ic_ir, sharpe, deflated_sharpe, psr,
                       direction_accuracy, params_json, model_hash, created_at
                  FROM model_runs
                  {where}
              ORDER BY created_at DESC
                 LIMIT :limit""",
            params=params,
        )
        if df.empty:
            return {"runs": [], "count": 0}

        # Parse params_json so the frontend can show n_cv_splits, embargo, etc.
        def _parse(s):
            if not s: return {}
            if isinstance(s, dict): return s
            try: return json.loads(s)
            except Exception: return {}

        df["params"] = df["params_json"].apply(_parse)
        df = df.drop(columns=["params_json"])
        return {"runs": df.to_dict("records"), "count": len(df)}
    except Exception as e:
        return {"runs": [], "count": 0, "error": str(e)}


@router.get("/backtest/latest")
def get_latest_by_horizon():
    """One latest run per horizon -- compact view for cards on the dashboard."""
    try:
        df = read_sql(
            """SELECT DISTINCT ON (horizon)
                      run_id, horizon, model_type, ic_mean, sharpe,
                      deflated_sharpe, n_features, n_samples,
                      train_start, train_end, test_start, test_end, created_at
                 FROM model_runs
             ORDER BY horizon, created_at DESC"""
        )
        return {"runs": df.to_dict("records"), "count": len(df)}
    except Exception as e:
        return {"runs": [], "count": 0, "error": str(e)}
