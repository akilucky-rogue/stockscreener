"""
Earnings Snapshot Engine.

Adapted from Anthropic financial-services: earnings-analysis SKILL.md
Produces beat/miss analysis, margin trends, and updated estimates
for quarterly earnings results.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)


def get_earnings_history(symbol: str, quarters: int = 8) -> list[dict]:
    """
    Pull quarterly earnings history from fundamentals.

    Returns list of quarterly snapshots with key metrics.
    """
    df = read_sql(
        "SELECT fiscal_date as date, revenue, net_income, "
        "pe_ratio, gross_margin, operating_margin, net_margin, "
        "roe, revenue_growth_yoy as revenue_growth, fcf_per_share "
        "FROM fundamentals WHERE symbol = :symbol "
        "ORDER BY fiscal_date DESC LIMIT :limit",
        params={"symbol": symbol, "limit": quarters},
    )
    if df.empty:
        return []

    return df.where(df.notna(), None).to_dict("records")


def compute_margin_trends(history: list[dict]) -> dict:
    """
    Compute margin trend analysis over recent quarters.

    Per Anthropic earnings skill: Track gross, operating, net margin trends.
    """
    if not history:
        return {}

    metrics = ["gross_margin", "operating_margin", "net_margin"]
    trends = {}

    for metric in metrics:
        values = [h.get(metric) for h in history if h.get(metric) is not None]
        if len(values) >= 2:
            current = values[0]
            prior = values[1]
            change_bps = round((current - prior) * 100, 0) if current and prior else None

            trends[metric] = {
                "current": round(current, 2) if current else None,
                "prior": round(prior, 2) if prior else None,
                "change_bps": change_bps,
                "trend": "expanding" if change_bps and change_bps > 0 else
                         "contracting" if change_bps and change_bps < 0 else "flat",
                "history": [round(v, 2) for v in values[:4]],
            }

    return trends


def compute_beat_miss(history: list[dict]) -> dict:
    """
    Compute sequential beat/miss analysis.

    Compares latest quarter to prior quarter on key metrics.
    Per Anthropic earnings skill: Lead with beat/miss, quantify variances.
    """
    if len(history) < 2:
        return {"available": False}

    latest = history[0]
    prior = history[1]

    results = {}
    for metric in ["revenue", "net_income", "gross_margin", "operating_margin"]:
        curr = latest.get(metric)
        prev = prior.get(metric)
        if curr is not None and prev is not None and prev != 0:
            change = curr - prev
            change_pct = (change / abs(prev)) * 100
            results[metric] = {
                "current": round(curr, 2),
                "prior": round(prev, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 1),
                "verdict": "BEAT" if change > 0 else "MISS" if change < 0 else "IN-LINE",
            }

    return {"available": True, "results": results}


def build_earnings_snapshot(symbol: str) -> dict:
    """
    Build complete earnings snapshot for a symbol.

    Returns structured JSON with:
    - latest_quarter: most recent quarter data
    - beat_miss: sequential comparison
    - margin_trends: gross/operating/net margin progression
    - revenue_trajectory: quarterly revenue history
    """
    # Step 1: Get earnings history
    history = get_earnings_history(symbol)
    if not history:
        return {
            "symbol": symbol,
            "available": False,
            "note": "No earnings data available. Run FMP ingestion first.",
        }

    # Step 2: Latest quarter
    latest = history[0]

    # Step 3: Beat/miss analysis
    beat_miss = compute_beat_miss(history)

    # Step 4: Margin trends
    margin_trends = compute_margin_trends(history)

    # Step 5: Revenue trajectory
    rev_trajectory = []
    for h in history:
        if h.get("revenue") is not None:
            rev_trajectory.append({
                "date": str(h.get("date", "")),
                "revenue": round(h["revenue"], 2),
                "growth": round(h["revenue_growth"], 1) if h.get("revenue_growth") else None,
            })

    # Step 6: Key takeaways (automated)
    takeaways = []
    if beat_miss.get("available"):
        for metric, data in beat_miss.get("results", {}).items():
            if data["verdict"] == "BEAT":
                takeaways.append(
                    f"{metric.replace('_', ' ').title()} beat prior quarter by "
                    f"{data['change_pct']:+.1f}%"
                )
            elif data["verdict"] == "MISS":
                takeaways.append(
                    f"{metric.replace('_', ' ').title()} missed prior quarter by "
                    f"{data['change_pct']:+.1f}%"
                )

    if margin_trends:
        for metric, data in margin_trends.items():
            if data.get("trend") == "expanding":
                takeaways.append(
                    f"{metric.replace('_', ' ').title()} expanding: "
                    f"+{data['change_bps']:.0f}bps QoQ"
                )

    return {
        "symbol": symbol,
        "available": True,
        "latest_quarter": latest,
        "beat_miss": beat_miss,
        "margin_trends": margin_trends,
        "revenue_trajectory": rev_trajectory,
        "key_takeaways": takeaways,
        "methodology": {
            "comparison": "Sequential quarter-over-quarter",
            "adapted_from": "Anthropic financial-services earnings-analysis skill",
        },
    }
