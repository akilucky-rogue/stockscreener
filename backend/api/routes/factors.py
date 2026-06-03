"""
Factor API endpoints -- feature importance and rolling IC dashboards.

Exposes two things the /factors frontend needs:
  * /api/factors/importance  -- top-N features from the latest model run
                                per horizon, with importance values.
  * /api/factors/ic          -- per-factor Spearman IC vs forward returns
                                over a recent window. Computed on demand
                                (small enough for ~50 factors * 200 stocks
                                in a few hundred ms).
  * /api/factors/categories  -- factor count by prefix (tech_, fund_, flow_)
                                for the dashboard summary.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Query

from qsde.db import read_sql

log = logging.getLogger(__name__)
router = APIRouter()


def _category(name: str) -> str:
    """Map factor name to its category by prefix."""
    if name.startswith("tech_"):  return "technical"
    if name.startswith("fund_"):  return "fundamental"
    if name.startswith("flow_"):  return "flow"
    if name.startswith("macro_"): return "macro"
    return "other"


@router.get("/factors/importance")
def get_factor_importance(
    horizon: str = Query(default="swing"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Latest model run's feature_importance for the given horizon.

    Returns rows like {name, importance, category}, sorted desc by importance.
    """
    try:
        df = read_sql(
            """SELECT feature_importance, ic_mean, sharpe, deflated_sharpe,
                      n_features, n_samples, created_at
                 FROM model_runs
                WHERE horizon = :horizon
             ORDER BY created_at DESC
                LIMIT 1""",
            params={"horizon": horizon},
        )
        if df.empty:
            return {"horizon": horizon, "features": [], "note": "No model runs yet."}

        row = df.iloc[0]
        raw = row.get("feature_importance")
        if isinstance(raw, str):
            features = json.loads(raw)
        elif isinstance(raw, list):
            features = raw
        else:
            features = []

        features = [
            {
                "name":       f.get("name"),
                "importance": float(f.get("importance", 0.0)),
                "category":   _category(f.get("name", "")),
            }
            for f in features[:limit]
        ]

        return {
            "horizon":          horizon,
            "features":         features,
            "ic_mean":          float(row.get("ic_mean") or 0.0),
            "sharpe":           float(row.get("sharpe") or 0.0),
            "deflated_sharpe":  float(row.get("deflated_sharpe") or 0.0),
            "n_features":       int(row.get("n_features") or 0),
            "n_samples":        int(row.get("n_samples") or 0),
            "trained_at":       str(row.get("created_at")),
        }
    except Exception as e:
        return {"horizon": horizon, "features": [], "error": str(e)}


@router.get("/factors/ic")
def get_factor_ic(
    horizon: str = Query(default="swing"),
    lookback_days: int = Query(default=90, ge=20, le=504),
):
    """Per-factor Spearman IC over recent N days vs forward returns.

    Joins factor_pit (long-format) with prices to compute the forward
    return per (symbol, date), pivots factors wide, then Spearman per
    factor pooled across all (symbol, date) rows in the window.
    """
    try:
        forward_days = 5 if horizon == "swing" else 20

        # Forward returns per (symbol, date)
        prices = read_sql(
            """
            SELECT symbol, date, close
              FROM ohlcv
             WHERE date >= (CURRENT_DATE - (:lookback + 60) * INTERVAL '1 day')
             ORDER BY symbol, date
            """,
            params={"lookback": lookback_days},
        )
        if prices.empty:
            return {"horizon": horizon, "factors": [], "note": "No price history."}

        import pandas as pd
        pivot = prices.pivot(index="date", columns="symbol", values="close")
        fwd = (pivot.shift(-forward_days) / pivot) - 1.0
        targets = (
            fwd.melt(ignore_index=False, value_name="target")
               .reset_index()
               .dropna(subset=["target"])
               .rename(columns={"date": "as_of_date"})
        )

        # Factors in the same window
        factors = read_sql(
            """
            SELECT symbol, as_of_date, factor_name, factor_value
              FROM factor_pit
             WHERE as_of_date >= (CURRENT_DATE - :lookback * INTERVAL '1 day')
               AND valid_to = 'infinity'::timestamptz
            """,
            params={"lookback": lookback_days},
        )
        if factors.empty:
            return {"horizon": horizon, "factors": [], "note": "No factors in window."}

        wide = factors.pivot_table(
            index=["symbol", "as_of_date"],
            columns="factor_name",
            values="factor_value",
        ).reset_index()

        merged = wide.merge(targets, on=["symbol", "as_of_date"], how="inner")
        if merged.empty:
            return {"horizon": horizon, "factors": []}

        from scipy.stats import spearmanr
        rows = []
        for col in merged.columns:
            if col in ("symbol", "as_of_date", "target"): continue
            valid = merged[[col, "target"]].dropna()
            if len(valid) < 50: continue
            ic, _ = spearmanr(valid[col], valid["target"])
            if ic is None: continue
            rows.append({
                "name":     col,
                "ic":       float(ic),
                "n_obs":    int(len(valid)),
                "category": _category(col),
            })

        rows.sort(key=lambda r: abs(r["ic"]), reverse=True)
        return {
            "horizon":       horizon,
            "lookback_days": lookback_days,
            "forward_days":  forward_days,
            "factors":       rows,
        }
    except Exception as e:
        log.exception("factor IC compute failed")
        return {"horizon": horizon, "factors": [], "error": str(e)}


@router.get("/factors/categories")
def get_factor_categories():
    """Counts of distinct factor names per category in factor_pit."""
    try:
        df = read_sql(
            """SELECT factor_name FROM factor_pit
                 GROUP BY factor_name"""
        )
        counts = {"technical": 0, "fundamental": 0, "flow": 0, "macro": 0, "other": 0}
        for name in df["factor_name"]:
            counts[_category(name)] += 1
        return {
            "categories": [{"name": k, "count": v} for k, v in counts.items()],
            "total":      sum(counts.values()),
        }
    except Exception as e:
        return {"categories": [], "total": 0, "error": str(e)}
