"""
Flow factor computation -- bulk-deal institutional accumulation per stock.

The flagship factor here is `flow_bulk_net_qty_20d`: signed net quantity
(BUY - SELL shares) traded by institutional clients over the trailing 20
sessions. Blueprint Part 5.2 calls this out as a 0.06-0.10 IC factor on
5-day forward returns, and it's the headline factor for the Week 4
Kill Condition #1 IC gate.

Lookahead safety: bulk deals are reported AFTER market close on day t.
For signal date `t` we use windows ending at t-1 (i.e., .shift(1) on the
rolling sums) so the factor reflects only data available before t's open.

All factor columns prefixed `flow_` so engine.py and the PIT writer
recognize them.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)


def _load_bulk_deals(symbol: str) -> pd.DataFrame:
    """Load all bulk deals for a symbol, one row per deal."""
    sql = """
        SELECT date, deal_type, quantity, client_name
          FROM bulk_deals
         WHERE symbol = :symbol
         ORDER BY date
    """
    df = read_sql(sql, params={"symbol": symbol})
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"])
    # Normalize deal_type just in case the CSV had whitespace or mixed case.
    df["deal_type"] = df["deal_type"].astype(str).str.strip().str.upper()
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0).astype("int64")
    return df


def _daily_aggregates(deals: pd.DataFrame) -> pd.DataFrame:
    """Collapse one-row-per-deal into one-row-per-calendar-day stats.

    Output columns:
        net_qty      = sum(BUY qty) - sum(SELL qty)
        had_buy      = 1 if any BUY deal that day else 0
        unique_buyers= count of distinct BUY client names that day
    """
    if deals.empty:
        return pd.DataFrame(
            columns=["date", "net_qty", "had_buy", "unique_buyers"]
        )

    is_buy = deals["deal_type"].eq("BUY")
    signed = deals["quantity"].where(is_buy, -deals["quantity"])

    g = deals.assign(signed=signed, is_buy=is_buy.astype(int)).groupby("date")
    daily = pd.DataFrame({
        "net_qty":       g["signed"].sum(),
        "had_buy":       g["is_buy"].max(),  # 0 or 1
        "unique_buyers": deals[is_buy].groupby("date")["client_name"].nunique(),
    })
    # Days with only sells will have NaN unique_buyers -> 0.
    daily["unique_buyers"] = daily["unique_buyers"].fillna(0).astype("int64")
    daily = daily.reset_index()
    return daily


def compute_all_flow(
    symbol: str,
    ohlcv_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Build daily flow factor frame for one symbol.

    Args:
        symbol: Stock ticker.
        ohlcv_index: DatetimeIndex of trading days. Output is aligned to
            this index so callers can concat with technical / fundamental
            factors.

    Returns:
        DataFrame indexed by ohlcv_index with columns:
            flow_bulk_net_qty_5d
            flow_bulk_net_qty_20d
            flow_bulk_buy_days_20d
            flow_bulk_unique_buyers_20d
        Symbols with no bulk-deal history get an all-NaN frame; the PIT
        writer drops NaN rows before persisting.
    """
    if len(ohlcv_index) == 0:
        return pd.DataFrame()

    deals = _load_bulk_deals(symbol)
    if deals.empty:
        return pd.DataFrame(index=ohlcv_index)

    daily = _daily_aggregates(deals)
    if daily.empty:
        return pd.DataFrame(index=ohlcv_index)

    # Reindex to the OHLCV trading days. Days with no bulk-deal activity
    # are zero (not NaN -- the absence of a deal is informative).
    by_date = daily.set_index("date").reindex(ohlcv_index, fill_value=0)

    # Rolling windows over trading days. .shift(1) prevents same-day
    # lookahead: the factor for date t reflects [t-window, t-1].
    net_5d  = by_date["net_qty"].rolling(5,  min_periods=1).sum().shift(1)
    net_20d = by_date["net_qty"].rolling(20, min_periods=1).sum().shift(1)
    buy_days_20d = by_date["had_buy"].rolling(20, min_periods=1).sum().shift(1)
    uniq_20d     = by_date["unique_buyers"].rolling(20, min_periods=1).sum().shift(1)

    out = pd.DataFrame({
        "flow_bulk_net_qty_5d":         net_5d,
        "flow_bulk_net_qty_20d":        net_20d,
        "flow_bulk_buy_days_20d":       buy_days_20d,
        "flow_bulk_unique_buyers_20d":  uniq_20d,
    }, index=ohlcv_index)

    return out
