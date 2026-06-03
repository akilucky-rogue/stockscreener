"""
Fundamental factor computation.

Pulls quarterly fundamentals from the `fundamentals` table and forward-fills
them across the daily OHLCV index using a PIT-correct as-of merge on
`filing_date <= signal_date`. A factor's value on signal date `t` is the
value from the most recent filing whose `filing_date` is on or before `t`.

All factor columns prefixed `fund_` so the engine and PIT writer pick them
up via the existing naming convention (engine.py compute_rolling_ic filters
on tech_/fund_/flow_/macro_ prefixes).
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)


# Schema column -> factor name. Renaming makes the factor list explicit
# and avoids leaking schema details into the model layer.
_FACTOR_COLUMN_MAP = {
    "pe_ratio":           "fund_pe",
    "pb_ratio":           "fund_pb",
    "ev_ebitda":          "fund_ev_ebitda",
    "ev_to_revenue":      "fund_ev_revenue",
    "roe":                "fund_roe",
    "roce":               "fund_roce",
    "roic":               "fund_roic",
    "gross_margin":       "fund_gross_margin",
    "operating_margin":   "fund_op_margin",
    "net_margin":         "fund_net_margin",
    "debt_equity":        "fund_debt_equity",
    "dividend_yield":     "fund_div_yield",
    "fcf_yield":          "fund_fcf_yield",
    "revenue_growth_yoy": "fund_rev_growth",
    "eps_growth_yoy":     "fund_eps_growth",
}


def _load_fundamentals(symbol: str) -> pd.DataFrame:
    """Load all fundamental filings for a symbol, sorted by filing_date.

    Returns a DataFrame keyed on filing_date with one row per filing.
    Empty DataFrame if the symbol has no fundamentals.
    """
    sql = """
        SELECT filing_date, fiscal_date,
               pe_ratio, pb_ratio, ev_ebitda, ev_to_revenue,
               roe, roce, roic,
               gross_margin, operating_margin, net_margin,
               debt_equity, dividend_yield, fcf_yield,
               revenue_growth_yoy, eps_growth_yoy
          FROM fundamentals
         WHERE symbol = :symbol
         ORDER BY filing_date, fiscal_date
    """
    df = read_sql(sql, params={"symbol": symbol})
    if df.empty:
        return df

    df["filing_date"] = pd.to_datetime(df["filing_date"])
    # Some filings can land on the same day; keep the row from the
    # most recent fiscal quarter (an amended later filing supersedes
    # the older one for the same calendar day).
    df = (
        df.sort_values(["filing_date", "fiscal_date"])
          .drop_duplicates("filing_date", keep="last")
          .reset_index(drop=True)
    )
    return df


def compute_fundamental_factors_from_filings(
    filings: pd.DataFrame,
    ohlcv_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Pure in-memory function: filings + dates -> fundamental factor frame.

    Used by both the DB-backed `compute_all_fundamental` and the on-demand
    /analyze endpoint (which fetches filings from yfinance live).

    Args:
        filings: DataFrame with column `filing_date` plus any subset of the
            keys in _FACTOR_COLUMN_MAP. One row per quarterly filing.
        ohlcv_index: DatetimeIndex of trading days to align onto.

    Returns:
        DataFrame indexed by ohlcv_index with one column per fundamental
        factor that exists in `filings`. Pre-filing dates are NaN.
    """
    if len(ohlcv_index) == 0:
        return pd.DataFrame()
    if filings is None or filings.empty:
        return pd.DataFrame(index=ohlcv_index)

    filings = filings.copy()
    # Force ns precision on the join key; pandas 2.2+ rejects merge_asof
    # across mixed datetime64 precisions (yfinance returns [s], pandas
    # defaults to [us]).
    filings["filing_date"] = pd.to_datetime(filings["filing_date"]).astype("datetime64[ns]")
    filings = (
        filings.sort_values("filing_date")
               .drop_duplicates("filing_date", keep="last")
               .reset_index(drop=True)
    )

    # Kite returns a timezone-aware DatetimeIndex (UTC); yfinance returns
    # timezone-naive. merge_asof requires both keys at the same dtype, and
    # pandas refuses to cast tz-aware -> tz-naive via .astype() -- you have
    # to drop the tz explicitly first.
    idx_dt = pd.to_datetime(ohlcv_index)
    if getattr(idx_dt, "tz", None) is not None:
        idx_dt = idx_dt.tz_localize(None)
    normalized_index = idx_dt.astype("datetime64[ns]")
    left = pd.DataFrame({"date": normalized_index}).sort_values("date")
    right = filings.sort_values("filing_date")

    merged = pd.merge_asof(
        left=left,
        right=right,
        left_on="date",
        right_on="filing_date",
        direction="backward",
        allow_exact_matches=True,
    )

    out_cols = {}
    for src, dst in _FACTOR_COLUMN_MAP.items():
        if src in merged.columns:
            out_cols[dst] = merged[src].values

    return pd.DataFrame(out_cols, index=ohlcv_index)


def compute_all_fundamental(
    symbol: str,
    ohlcv_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """DB-backed entry point: load filings from `fundamentals` table, then
    compute factors via the pure in-memory helper.

    For every date in `ohlcv_index`, looks up the most recent filing where
    `filing_date <= date` and emits one factor row per fundamental field.
    """
    if len(ohlcv_index) == 0:
        return pd.DataFrame()
    fundamentals = _load_fundamentals(symbol)
    return compute_fundamental_factors_from_filings(fundamentals, ohlcv_index)
