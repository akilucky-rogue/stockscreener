"""
NSE Bulk & Block Deals parser — CRITICAL for Week 4 kill condition.

Parses NSE bulk/block deal CSV data published daily after market close.
Aggregates into 20-day rolling net institutional accumulation per stock.
IC 0.06–0.10 on 5-day forward returns (Blueprint §5.2).
"""

from __future__ import annotations

import io
import logging
import time
from datetime import date
from typing import Optional

import httpx
import pandas as pd

from qsde.config import settings
from qsde.db import read_sql, get_sync_conn

log = logging.getLogger(__name__)

BULK_DEALS_URL = "https://archives.nseindia.com/content/equities/bulk.csv"

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
}


def fetch_daily_bulk_deals() -> pd.DataFrame:
    """Fetch today's bulk deals from NSE archives."""
    client = httpx.Client(timeout=20.0, headers=NSE_HEADERS, follow_redirects=True)
    try:
        client.get(settings.nse_base_url)
        time.sleep(1.5)
        resp = client.get(BULK_DEALS_URL)
        if resp.status_code != 200:
            log.warning("Bulk deals fetch returned %d", resp.status_code)
            return pd.DataFrame()
        text = resp.text
        if not text.strip():
            return pd.DataFrame()
        df = pd.read_csv(io.StringIO(text))
        df.columns = df.columns.str.strip()
        return _normalize(df)
    except Exception as e:
        log.error("Failed to fetch bulk deals: %s", e)
        return pd.DataFrame()
    finally:
        client.close()


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize bulk deals CSV columns to standard schema."""
    if df.empty:
        return pd.DataFrame()
    col_map = {}
    for c in df.columns:
        cl = c.strip().upper()
        if cl in ("SYMBOL", "SCRIP"):
            col_map[c] = "symbol"
        elif "DATE" in cl:
            col_map[c] = "date"
        elif "CLIENT" in cl:
            col_map[c] = "client_name"
        elif "BUY" in cl and "SELL" in cl:
            col_map[c] = "deal_type"
        elif cl in ("QTY", "QUANTITY", "QUANTITY TRADED"):
            col_map[c] = "quantity"
        elif "PRICE" in cl:
            col_map[c] = "price"
    df = df.rename(columns=col_map)
    for col in ["symbol", "date", "client_name", "deal_type", "quantity", "price"]:
        if col not in df.columns:
            df[col] = None
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["deal_type"] = df["deal_type"].astype(str).str.strip().str.upper()
    df["deal_type"] = df["deal_type"].replace({"B": "BUY", "S": "SELL"})
    df["quantity"] = df["quantity"].astype(str).str.replace(",", "")
    df["price"] = df["price"].astype(str).str.replace(",", "")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["source"] = "nse"
    return df.dropna(subset=["symbol", "date"])[
        ["symbol", "date", "client_name", "deal_type", "quantity", "price", "source"]
    ]

def _upsert_bulk_deals(df: pd.DataFrame) -> int:
    """Helper to upsert a normalized dataframe of bulk deals."""
    if df.empty:
        return 0
        
    from qsde.db import upsert_dataframe
    # Remove exact duplicates that NSE sometimes provides
    df = df.drop_duplicates(subset=["symbol", "date", "client_name", "deal_type", "quantity", "price"])
    
    return upsert_dataframe(
        df,
        table="bulk_deals",
        conflict_columns=["symbol", "date", "client_name", "deal_type", "quantity", "price"],
        update_columns=["source"]
    )

def sync_bulk_deals_to_db() -> int:
    """Fetch today's bulk deals and insert into DB."""
    df = fetch_daily_bulk_deals()
    if df.empty:
        return 0
    
    inserted = _upsert_bulk_deals(df)
    log.info("Inserted %d bulk deal rows", inserted)
    return inserted

def backfill_bulk_deals_from_csv(file_paths: list[str]) -> int:
    """Load historical bulk deals from NSE CSV archives."""
    total_inserted = 0
    for path in file_paths:
        try:
            log.info("Loading bulk deals from %s", path)
            df = pd.read_csv(path)
            norm_df = _normalize(df)
            inserted = _upsert_bulk_deals(norm_df)
            total_inserted += inserted
            log.info("Loaded %d rows from %s", inserted, path)
        except Exception as e:
            log.error("Failed to load %s: %s", path, e)
            
    return total_inserted


def compute_net_accumulation(
    symbol: str, lookback_days: int = 20, as_of_date: Optional[str] = None,
) -> float:
    """
    Compute 20-day net institutional accumulation.
    Positive = net buying, Negative = net selling.
    Core factor for Week 4 kill condition gate.
    """
    if as_of_date is None:
        as_of_date = date.today().isoformat()
    df = read_sql(
        """SELECT deal_type, SUM(quantity) as total_qty FROM bulk_deals
           WHERE symbol = :symbol
             AND date BETWEEN (:as_of::date - :lb * INTERVAL '1 day') AND :as_of::date
           GROUP BY deal_type""",
        params={"symbol": symbol, "as_of": as_of_date, "lb": lookback_days},
    )
    if df.empty:
        return 0.0
    buy = df.loc[df["deal_type"] == "BUY", "total_qty"].sum()
    sell = df.loc[df["deal_type"] == "SELL", "total_qty"].sum()
    return float(buy - sell)


def compute_bulk_deal_factor_all(
    symbols: list[str], lookback_days: int = 20, as_of_date: Optional[str] = None,
) -> pd.DataFrame:
    """Compute 20-day net accumulation factor for all symbols."""
    if as_of_date is None:
        as_of_date = date.today().isoformat()
    df = read_sql(
        """SELECT symbol, deal_type, SUM(quantity) as total_qty FROM bulk_deals
           WHERE date BETWEEN (:as_of::date - :lb * INTERVAL '1 day') AND :as_of::date
           GROUP BY symbol, deal_type""",
        params={"as_of": as_of_date, "lb": lookback_days},
    )
    if df.empty:
        return pd.DataFrame({"symbol": symbols, "flow_bulk_deal_net_20d": 0.0})
    pivot = df.pivot_table(index="symbol", columns="deal_type", values="total_qty", fill_value=0)
    buy = pivot.get("BUY", pd.Series(0, index=pivot.index))
    sell = pivot.get("SELL", pd.Series(0, index=pivot.index))
    net = (buy - sell).reset_index()
    net.columns = ["symbol", "flow_bulk_deal_net_20d"]
    result = pd.DataFrame({"symbol": symbols}).merge(net, on="symbol", how="left")
    result["flow_bulk_deal_net_20d"] = result["flow_bulk_deal_net_20d"].fillna(0)
    return result
