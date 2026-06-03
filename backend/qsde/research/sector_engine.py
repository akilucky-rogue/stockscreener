"""
Sector Overview Engine.

Adapted from Anthropic financial-services: sector-overview + competitive-analysis skills.
Produces sector-level aggregate statistics, peer rankings, and competitive positioning.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)


def get_sector_list() -> list[dict]:
    """Get all sectors with company counts."""
    df = read_sql(
        "SELECT sector, COUNT(*) as count "
        "FROM universe WHERE is_active = TRUE AND sector IS NOT NULL "
        "GROUP BY sector ORDER BY count DESC",
    )
    return df.to_dict("records") if not df.empty else []


def build_sector_overview(sector: str) -> dict:
    """
    Build comprehensive sector overview.

    Returns:
    - companies: all companies in the sector with metrics
    - aggregate_stats: sector-level median/quartile statistics
    - leaders: top performers by key metrics
    - competitive_map: positioning of companies on key dimensions
    """
    # Step 1: Get companies in sector with fundamentals
    df = read_sql(
        "SELECT u.symbol, u.company_name, u.index_membership, "
        "f.revenue, f.net_income, f.market_cap, f.enterprise_value, "
        "f.pe_ratio, f.pb_ratio, f.ev_to_revenue, f.ev_ebitda as ev_to_ebitda, "
        "f.gross_margin, f.operating_margin, f.net_margin, "
        "f.roe, f.roic, f.dividend_yield, f.revenue_growth_yoy as revenue_growth, "
        "f.debt_equity as debt_to_equity, f.fcf_per_share "
        "FROM universe u "
        "LEFT JOIN LATERAL ("
        "  SELECT * FROM fundamentals WHERE symbol = u.symbol ORDER BY fiscal_date DESC LIMIT 1"
        ") f ON TRUE "
        "WHERE u.is_active = TRUE AND u.sector = :sector "
        "ORDER BY f.market_cap DESC NULLS LAST",
        params={"sector": sector},
    )

    if df.empty:
        return {"sector": sector, "available": False, "note": "No companies found in this sector."}

    # Step 2: Aggregate statistics
    stat_cols = ["pe_ratio", "pb_ratio", "gross_margin", "operating_margin",
                 "net_margin", "roe", "roic", "dividend_yield", "revenue_growth",
                 "debt_to_equity", "ev_to_revenue", "ev_to_ebitda"]

    aggregate = {}
    for col in stat_cols:
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if series.empty:
            continue
        aggregate[col] = {
            "median": round(float(series.median()), 2),
            "mean": round(float(series.mean()), 2),
            "p75": round(float(series.quantile(0.75)), 2),
            "p25": round(float(series.quantile(0.25)), 2),
            "min": round(float(series.min()), 2),
            "max": round(float(series.max()), 2),
        }

    # Step 3: Leaders by key metrics
    leaders = {}
    for metric, label, ascending in [
        ("market_cap", "Largest by Market Cap", False),
        ("revenue_growth", "Fastest Revenue Growth", False),
        ("roe", "Highest ROE", False),
        ("dividend_yield", "Highest Dividend Yield", False),
        ("pe_ratio", "Most Attractively Valued (P/E)", True),
    ]:
        if metric in df.columns:
            sorted_df = df.dropna(subset=[metric]).sort_values(metric, ascending=ascending)
            top3 = sorted_df.head(3)
            leaders[label] = [
                {"symbol": r["symbol"], "company_name": r["company_name"],
                 "value": round(float(r[metric]), 2)}
                for _, r in top3.iterrows()
            ]

    # Step 4: Total market cap and revenue
    total_market_cap = df["market_cap"].sum() if "market_cap" in df.columns else 0
    total_revenue = df["revenue"].sum() if "revenue" in df.columns else 0

    return {
        "sector": sector,
        "available": True,
        "company_count": len(df),
        "total_market_cap": round(float(total_market_cap), 0) if total_market_cap else None,
        "total_revenue": round(float(total_revenue), 0) if total_revenue else None,
        "companies": df.where(df.notna(), None).to_dict("records"),
        "aggregate_stats": aggregate,
        "leaders": leaders,
        "methodology": {
            "adapted_from": "Anthropic financial-services sector-overview + competitive-analysis skills",
        },
    }
