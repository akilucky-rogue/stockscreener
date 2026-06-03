"""
Comparable Company Analysis Engine.

Adapted from Anthropic financial-services: comps-analysis SKILL.md
Builds institutional-grade peer valuation spreads with operating metrics,
valuation multiples, and quartile statistics.

Data sources: QSDE universe table + FMP fundamentals + OHLCV.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)

# ── Metric definitions per the Anthropic comps skill ──────────
OPERATING_METRICS = [
    "revenue", "revenue_growth_yoy", "gross_margin", "ebitda_margin",
    "net_margin", "roe", "roic", "fcf_margin",
]

VALUATION_METRICS = [
    "market_cap", "enterprise_value", "ev_revenue", "ev_ebitda",
    "pe_ratio", "pb_ratio", "dividend_yield",
]

SECTOR_SPECIFIC = {
    "Technology": ["rule_of_40"],
    "Financial Services": ["roe", "roa", "net_interest_margin"],
    "Consumer Cyclical": ["inventory_turnover"],
    "Healthcare": ["rd_to_revenue"],
}


def _yf_sector_industry(symbol: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort sector/industry/company_name lookup via yfinance.

    Tries .NS first, then .BO. Returns (sector, industry, company_name).
    """
    try:
        import yfinance as yf
    except ImportError:
        return None, None, None
    for suffix in (".NS", ".BO"):
        try:
            info = yf.Ticker(symbol + suffix).info or {}
            sec = info.get("sector")
            ind = info.get("industry")
            name = info.get("longName") or info.get("shortName")
            if sec or ind or name:
                return sec, ind, name
        except Exception as e:
            log.debug("yf info probe %s%s failed: %s", symbol, suffix, e)
    return None, None, None


def _backfill_universe_sector(symbol: str, sector: Optional[str],
                              industry: Optional[str], company_name: Optional[str]) -> None:
    """Write the fetched sector/industry back so the next request is instant."""
    if not (sector or industry or company_name):
        return
    # execute_sql goes through raw psycopg2, so use %(name)s placeholders.
    try:
        from qsde.db import execute_sql
        execute_sql(
            """UPDATE universe SET
                 sector = COALESCE(NULLIF(sector,''), %(sec)s),
                 industry = COALESCE(NULLIF(industry,''), %(ind)s),
                 company_name = COALESCE(NULLIF(company_name,''), %(nm)s)
               WHERE symbol = %(sym)s""",
            params={"sec": sector, "ind": industry, "nm": company_name, "sym": symbol},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("backfill universe.sector for %s failed: %s", symbol, e)


def get_peer_group(symbol: str, max_peers: int = 12) -> list[dict]:
    """
    Identify peer companies from the same sector/industry in our universe.

    Returns list of dicts with symbol, company_name, sector.

    Robust path: when universe.sector is NULL (common for the headline Nifty
    200 names because the Kite instrument master doesn't carry GICS), this
    falls back to yfinance Ticker.info -> sector, persists it back into
    universe so the next call is instant, and broadens to index-membership
    peers as a last resort when too few same-sector rows exist.
    """
    target = read_sql(
        "SELECT symbol, company_name, sector, industry, index_membership "
        "FROM universe WHERE symbol = :symbol AND is_active = TRUE",
        params={"symbol": symbol},
    )

    sector: Optional[str] = None
    industry: Optional[str] = None
    company_name: Optional[str] = None
    idx_membership = None
    if not target.empty:
        sector       = target.iloc[0]["sector"] or None
        industry     = target.iloc[0]["industry"] or None
        company_name = target.iloc[0]["company_name"] or None
        idx_membership = target.iloc[0]["index_membership"]

    if not sector:
        yf_sec, yf_ind, yf_name = _yf_sector_industry(symbol)
        sector       = sector or yf_sec
        industry     = industry or yf_ind
        company_name = company_name or yf_name
        if sector or industry or company_name:
            _backfill_universe_sector(symbol, sector, industry, company_name)

    peers_df = None
    if sector:
        peers_df = read_sql(
            "SELECT symbol, company_name, sector, index_membership "
            "FROM universe WHERE sector = :sector AND is_active = TRUE "
            "ORDER BY symbol LIMIT :limit",
            params={"sector": sector, "limit": max_peers + 1},
        )

    # If sector matching produced <3 peers (or none), broaden to index members.
    # That's still a meaningful peer set for a large-cap headline name.
    need_broaden = peers_df is None or len(peers_df) < 3
    if need_broaden and idx_membership:
        try:
            import json as _json
            idx_list = idx_membership if isinstance(idx_membership, list) else _json.loads(idx_membership)
            if isinstance(idx_list, list) and idx_list:
                fallback = read_sql(
                    """SELECT symbol, company_name, sector, index_membership
                         FROM universe
                        WHERE is_active = TRUE
                          AND index_membership::jsonb ?| array[:idx]
                        ORDER BY symbol
                        LIMIT :limit""",
                    params={"idx": idx_list[:3], "limit": max_peers + 1},
                )
                if peers_df is None or peers_df.empty:
                    peers_df = fallback
                else:
                    peers_df = (
                        pd.concat([peers_df, fallback])
                        .drop_duplicates(subset="symbol")
                        .head(max_peers + 1)
                    )
        except Exception as e:  # noqa: BLE001
            log.debug("index-broaden fallback failed for %s: %s", symbol, e)

    if peers_df is None or peers_df.empty:
        return []
    return peers_df.to_dict("records")


def get_fundamentals_for_symbols(symbols: list[str]) -> pd.DataFrame:
    """Pull latest fundamentals from our database for a list of symbols."""
    if not symbols:
        return pd.DataFrame()

    placeholders = ", ".join(f"'{s}'" for s in symbols)
    df = read_sql(
        f"SELECT DISTINCT ON (symbol) symbol, fiscal_date as date, "
        f"revenue, net_income, market_cap, enterprise_value, "
        f"pe_ratio, pb_ratio, ev_to_revenue, ev_ebitda as ev_to_ebitda, "
        f"roe, roic, dividend_yield, gross_margin, operating_margin, "
        f"net_margin, revenue_growth_yoy as revenue_growth, fcf_per_share, debt_equity as debt_to_equity "
        f"FROM fundamentals "
        f"WHERE symbol IN ({placeholders}) "
        f"ORDER BY symbol, fiscal_date DESC",
    )
    return df


def compute_quartile_stats(df: pd.DataFrame, columns: list[str]) -> dict:
    """
    Compute Max, 75th, Median, 25th, Min for each column.
    Per the Anthropic comps skill: quartiles show distribution, not just average.
    """
    stats = {}
    for col in columns:
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if series.empty:
            continue
        stats[col] = {
            "maximum": round(float(series.max()), 4),
            "p75": round(float(series.quantile(0.75)), 4),
            "median": round(float(series.median()), 4),
            "p25": round(float(series.quantile(0.25)), 4),
            "minimum": round(float(series.min()), 4),
            "count": int(series.count()),
        }
    return stats


def build_comps_analysis(symbol: str) -> dict:
    """
    Build full comparable company analysis for a symbol.

    Returns structured JSON with:
    - target: the company being analyzed
    - peers: list of peer companies with metrics
    - operating_stats: quartile statistics for operating metrics
    - valuation_stats: quartile statistics for valuation multiples
    """
    # Step 1: Get peer group
    peer_list = get_peer_group(symbol)
    if not peer_list:
        return {"error": f"No peers found for {symbol}", "symbol": symbol}

    symbols = [p["symbol"] for p in peer_list]

    # Step 2: Pull fundamentals
    fundamentals = get_fundamentals_for_symbols(symbols)
    if fundamentals.empty:
        # Return peer list with placeholder data
        return {
            "symbol": symbol,
            "peers": peer_list,
            "operating_stats": {},
            "valuation_stats": {},
            "note": "No fundamental data available yet. Run FMP ingestion first.",
        }

    # Step 3: Merge peer info with fundamentals
    peer_df = pd.DataFrame(peer_list)
    merged = peer_df.merge(fundamentals, on="symbol", how="left")

    # Step 4: Compute quartile statistics
    op_cols = [c for c in ["gross_margin", "operating_margin", "net_margin",
                           "roe", "roic", "revenue_growth"] if c in merged.columns]
    val_cols = [c for c in ["pe_ratio", "pb_ratio", "ev_to_revenue",
                            "ev_to_ebitda", "dividend_yield"] if c in merged.columns]

    operating_stats = compute_quartile_stats(merged, op_cols)
    valuation_stats = compute_quartile_stats(merged, val_cols)

    # Step 5: Identify target company
    target_row = merged[merged["symbol"] == symbol]
    target_data = target_row.to_dict("records")[0] if not target_row.empty else {"symbol": symbol}
    # Clean target_data of any NaN values to avoid floating-point NaN issues
    target_data = {
        k: (None if not isinstance(v, (list, dict, np.ndarray, tuple)) and pd.isna(v) else v)
        for k, v in target_data.items()
    }

    # Step 6: Compute relative positioning
    # Where does the target sit vs peers on key multiples?
    positioning = {}
    for col in val_cols:
        if col in merged.columns and col in target_data:
            val = target_data.get(col)
            if val is not None and not pd.isna(val):
                median = merged[col].median()
                if median and median != 0:
                    positioning[col] = {
                        "value": round(float(val), 2),
                        "vs_median": round(float((val - median) / abs(median) * 100), 1),
                        "percentile_rank": round(float(
                            (merged[col].dropna() <= val).sum() / merged[col].dropna().count() * 100
                        ), 1),
                    }

    # Step 7: Determine sector
    sector = peer_list[0]["sector"] if peer_list else "Unknown"

    return {
        "symbol": symbol,
        "sector": sector,
        "target": target_data,
        "peers": merged.replace({np.nan: None}).to_dict("records"),
        "peer_count": len(peer_list),
        "operating_stats": operating_stats,
        "valuation_stats": valuation_stats,
        "positioning": positioning,
        "methodology": {
            "description": "Comparable company analysis using institutional methodology",
            "metrics": "Operating metrics + valuation multiples with quartile benchmarking",
            "source": "QSDE fundamentals database (FMP)",
            "adapted_from": "Anthropic financial-services comps-analysis skill",
        },
    }
