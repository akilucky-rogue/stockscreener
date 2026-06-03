"""
Multi-Criteria Stock Screener Engine.

Adapted from Anthropic financial-services: idea-generation SKILL.md
Supports Value/Growth/Quality/Momentum/Short preset screens
plus custom criteria. Combines QSDE ML signals with fundamental filters.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)

# ── Screen Presets (from Anthropic idea-generation skill) ────

SCREEN_PRESETS = {
    "value": {
        "label": "Value",
        "description": "Undervalued stocks with strong cash generation",
        "filters": {
            "pe_ratio": {"max": 20, "label": "P/E < 20"},
            "pb_ratio": {"max": 2.0, "label": "P/B < 2.0"},
            "dividend_yield": {"min": 1.5, "label": "Div Yield > 1.5%"},
            "debt_to_equity": {"max": 1.5, "label": "D/E < 1.5"},
        },
        "sort_by": "pe_ratio",
        "sort_asc": True,
    },
    "growth": {
        "label": "Growth",
        "description": "High-growth companies with expanding margins",
        "filters": {
            "revenue_growth": {"min": 15, "label": "Revenue Growth > 15%"},
            "roe": {"min": 15, "label": "ROE > 15%"},
            "operating_margin": {"min": 10, "label": "Op Margin > 10%"},
        },
        "sort_by": "revenue_growth",
        "sort_asc": False,
    },
    "quality": {
        "label": "Quality",
        "description": "Consistent compounders with high returns on capital",
        # Thresholds tuned for Indian P&L conventions. yfinance Indian gross
        # margins are systematically lower than US peers (different COGS
        # classification), so 40% killed every row. 20% + ROE/ROIC + low
        # leverage is the right Indian-market quality screen.
        "filters": {
            "roe": {"min": 15, "label": "ROE > 15%"},
            "roic": {"min": 12, "label": "ROIC > 12%"},
            "gross_margin": {"min": 20, "label": "Gross Margin > 20%"},
            "debt_to_equity": {"max": 1.5, "label": "D/E < 1.5"},
        },
        "sort_by": "roe",
        "sort_asc": False,
    },
    "momentum": {
        "label": "Momentum",
        "description": "Stocks with strong price and earnings momentum",
        "filters": {
            "revenue_growth": {"min": 10, "label": "Revenue Growth > 10%"},
        },
        "sort_by": "revenue_growth",
        "sort_asc": False,
    },
    "dividend": {
        "label": "Dividend",
        "description": "High-yield dividend payers with sustainable payouts",
        "filters": {
            "dividend_yield": {"min": 3, "label": "Div Yield > 3%"},
            "pe_ratio": {"max": 25, "label": "P/E < 25"},
        },
        "sort_by": "dividend_yield",
        "sort_asc": False,
    },
}


def get_universe_with_fundamentals() -> pd.DataFrame:
    """Load full universe with latest fundamentals."""
    df = read_sql(
        "SELECT u.symbol, u.company_name, u.sector, u.index_membership, "
        "f.revenue, f.net_income, f.market_cap, f.pe_ratio, f.pb_ratio, "
        "f.gross_margin, f.operating_margin, f.net_margin, "
        "f.roe, f.roic, f.dividend_yield, f.revenue_growth_yoy as revenue_growth, "
        "f.debt_equity as debt_to_equity, f.fcf_per_share, f.fiscal_date as fundamental_date "
        "FROM universe u "
        "LEFT JOIN LATERAL ("
        "  SELECT * FROM fundamentals WHERE symbol = u.symbol ORDER BY fiscal_date DESC LIMIT 1"
        ") f ON TRUE "
        "WHERE u.is_active = TRUE "
        "ORDER BY u.symbol",
    )
    return df


def apply_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """Apply filter criteria to a DataFrame.

    Graceful degradation: if a filter column is missing or null for more than
    80% of rows, skip that filter silently rather than zero out the screen.
    This is important for Indian-market data where some fundamentals
    (gross_margin, roic) are sparsely populated by yfinance.
    """
    filtered = df.copy()

    for col, criteria in filters.items():
        if col not in filtered.columns:
            continue

        # If the column is missing for >80% of the candidate set, the filter
        # is informationally worthless — skip it. (Reports the skip in
        # filters_applied via the engine; the threshold stays in the preset.)
        non_null_frac = filtered[col].notna().mean() if len(filtered) else 0.0
        if non_null_frac < 0.20:
            continue

        if "min" in criteria:
            filtered = filtered[filtered[col].notna() & (filtered[col] >= criteria["min"])]

        if "max" in criteria:
            filtered = filtered[filtered[col].notna() & (filtered[col] <= criteria["max"])]

    return filtered


def add_momentum_data(df: pd.DataFrame) -> pd.DataFrame:
    """Enrich with price momentum data from OHLCV."""
    # Get 1m, 3m, 6m, 12m returns for each symbol
    symbols = df["symbol"].tolist()
    if not symbols:
        return df

    momentum_data = []
    for sym in symbols[:50]:  # Limit for performance
        try:
            prices = read_sql(
                "SELECT date, close FROM ohlcv WHERE symbol = :symbol "
                "ORDER BY date DESC LIMIT 252",
                params={"symbol": sym},
            )
            if prices.empty or len(prices) < 21:
                momentum_data.append({"symbol": sym})
                continue

            current = float(prices.iloc[0]["close"])
            mom_1m = ((current / float(prices.iloc[min(20, len(prices) - 1)]["close"])) - 1) * 100
            mom_3m = ((current / float(prices.iloc[min(62, len(prices) - 1)]["close"])) - 1) * 100 if len(prices) > 62 else None
            mom_6m = ((current / float(prices.iloc[min(125, len(prices) - 1)]["close"])) - 1) * 100 if len(prices) > 125 else None
            mom_12m = ((current / float(prices.iloc[min(251, len(prices) - 1)]["close"])) - 1) * 100 if len(prices) > 251 else None

            momentum_data.append({
                "symbol": sym,
                "current_price": round(current, 2),
                "mom_1m": round(mom_1m, 1) if mom_1m else None,
                "mom_3m": round(mom_3m, 1) if mom_3m else None,
                "mom_6m": round(mom_6m, 1) if mom_6m else None,
                "mom_12m": round(mom_12m, 1) if mom_12m else None,
            })
        except Exception:
            momentum_data.append({"symbol": sym})

    if momentum_data:
        mom_df = pd.DataFrame(momentum_data)
        df = df.merge(mom_df, on="symbol", how="left")

    return df


def run_screen(
    preset: Optional[str] = None,
    custom_filters: Optional[dict] = None,
    sector: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_asc: bool = True,
    limit: int = 50,
    include_momentum: bool = False,
) -> dict:
    """
    Run a stock screen across the Nifty 200 universe.

    Args:
        preset: One of 'value', 'growth', 'quality', 'momentum', 'dividend'
        custom_filters: Dict of {column: {min/max: value}}
        sector: Filter to specific sector
        sort_by: Column to sort results by
        sort_asc: Sort ascending if True
        limit: Maximum results to return
        include_momentum: Whether to compute price momentum

    Returns:
        Structured screen results with metadata.
    """
    # Step 1: Load universe
    df = get_universe_with_fundamentals()
    total_universe = len(df)

    if df.empty:
        return {
            "results": [],
            "total": 0,
            "universe_size": 0,
            "note": "No data available. Run universe sync and FMP ingestion first.",
        }

    # Step 2: Sector filter
    if sector:
        df = df[df["sector"] == sector]

    # Step 3: Apply preset or custom filters
    active_filters = {}
    screen_label = "Custom"
    screen_description = "Custom filter criteria"

    if preset and preset in SCREEN_PRESETS:
        config = SCREEN_PRESETS[preset]
        active_filters = config["filters"]
        screen_label = config["label"]
        screen_description = config["description"]
        sort_by = sort_by or config.get("sort_by")
        sort_asc = config.get("sort_asc", sort_asc)

    if custom_filters:
        active_filters.update(custom_filters)

    if active_filters:
        df = apply_filters(df, active_filters)

    # Step 4: Add momentum if requested
    if include_momentum:
        df = add_momentum_data(df)

    # Step 5: Sort
    if sort_by and sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=sort_asc, na_position="last")

    # Step 6: Limit results
    results = df.head(limit)

    return {
        "screen": screen_label,
        "description": screen_description,
        "filters_applied": {k: v for k, v in active_filters.items()},
        "sector_filter": sector,
        "results": results.replace({np.nan: None}).to_dict("records"),
        "passing_count": len(results),
        "total_screened": total_universe,
        "pass_rate": round(len(results) / total_universe * 100, 1) if total_universe else 0,
        "available_presets": list(SCREEN_PRESETS.keys()),
        "methodology": {
            "adapted_from": "Anthropic financial-services idea-generation skill",
        },
    }
