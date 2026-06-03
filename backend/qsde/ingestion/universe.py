"""
Nifty 200 universe scraper — fetches current constituents from NSE.

Sources:
  - NSE index page: https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20200
  - Fallback: CSV download from NSE

Output: List of symbols + metadata (company name, sector, index membership).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
import pandas as pd

from qsde.config import settings
from qsde.db import upsert_dataframe

log = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Hardcoded Nifty 50 as guaranteed baseline
NIFTY_50_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "BHARTIARTL", "SBIN", "BAJFINANCE",
    "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI",
    "TITAN", "SUNPHARMA", "ULTRACEMCO", "WIPRO", "NESTLEIND",
    "NTPC", "POWERGRID", "ONGC", "TECHM", "HCLTECH",
    "DRREDDY", "BAJAJFINSV", "ADANIENT", "ADANIPORTS", "COALINDIA",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "INDUSINDBK", "GRASIM",
    "CIPLA", "DIVISLAB", "BPCL", "BRITANNIA", "EICHERMOT",
    "HEROMOTOCO", "APOLLOHOSP", "TATACONSUM", "HINDALCO", "M&M",
    "SBILIFE", "HDFCLIFE", "BAJAJ-AUTO", "LTIM", "SHREECEM",
]

# BSE SENSEX 30 baseline. Most are dual-listed (yfinance addresses BSE with the
# .BO suffix vs .NS for NSE). Used as the guaranteed BSE universe until a BSE
# index API is wired. NOTE: `universe` is keyed by `symbol` alone, so a symbol
# listed on both exchanges resolves to one row — proper multi-exchange needs a
# composite (symbol, exchange) key (tracked follow-up).
BSE_SENSEX_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "BAJFINANCE", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "ULTRACEMCO", "NESTLEIND",
    "NTPC", "POWERGRID", "TECHM", "HCLTECH", "BAJAJFINSV", "TATAMOTORS",
    "TATASTEEL", "INDUSINDBK", "M&M", "WIPRO", "JSWSTEEL",
]


def _create_nse_session() -> httpx.Client:
    """Create an httpx client with NSE session cookies."""
    client = httpx.Client(
        timeout=15.0,
        headers=NSE_HEADERS,
        follow_redirects=True,
    )
    # Hit homepage to establish cookies
    try:
        client.get(settings.nse_base_url)
        time.sleep(1)
    except Exception as e:
        log.warning("Failed to establish NSE session: %s", e)
    return client


def fetch_index_constituents(
    index_name: str = "NIFTY 200",
) -> pd.DataFrame:
    """
    Fetch current constituents of an NSE index.

    Args:
        index_name: Name of the index (e.g., 'NIFTY 200', 'NIFTY 50').

    Returns:
        DataFrame with columns: symbol, company_name, sector, industry.
    """
    client = _create_nse_session()
    encoded_name = index_name.replace(" ", "%20")
    url = f"{settings.nse_base_url}/api/equity-stockIndices?index={encoded_name}"

    try:
        resp = client.get(url)
        if resp.status_code != 200:
            log.warning("NSE index API returned %d for %s", resp.status_code, index_name)
            return pd.DataFrame()

        data = resp.json()
        records = data.get("data", [])
        if not records:
            log.warning("No data in NSE response for %s", index_name)
            return pd.DataFrame()

        rows = []
        for r in records:
            symbol = r.get("symbol", "").strip()
            if not symbol or symbol == index_name:
                continue
            rows.append({
                "symbol": symbol,
                "company_name": r.get("meta", {}).get("companyName", "")
                                if isinstance(r.get("meta"), dict)
                                else r.get("identifier", ""),
                "sector": r.get("meta", {}).get("industry", "")
                          if isinstance(r.get("meta"), dict) else "",
                "industry": r.get("meta", {}).get("industry", "")
                            if isinstance(r.get("meta"), dict) else "",
            })

        df = pd.DataFrame(rows)
        log.info("Fetched %d constituents from %s", len(df), index_name)
        return df

    except Exception as e:
        log.error("Failed to fetch %s constituents: %s", index_name, e)
        return pd.DataFrame()
    finally:
        client.close()


def build_universe(target_index: str = "NIFTY 500") -> pd.DataFrame:
    """
    Build the universe with index membership info, tagged with all the
    sub-index memberships (NIFTY 50 / NEXT 50 / 100 / 200 / 500) the
    symbol belongs to.

    `target_index` controls how wide the active universe is:
      "NIFTY 50"   ->  ~50 names
      "NIFTY 100"  ->  ~100 names
      "NIFTY 200"  ->  ~200 names
      "NIFTY 500"  ->  ~500 names

    Falls back to Nifty 50 baseline if every NSE API call fails.
    """
    log.info("Building universe (target=%s)...", target_index)
    df_target = fetch_index_constituents(target_index)

    df_50  = fetch_index_constituents("NIFTY 50")
    df_100 = fetch_index_constituents("NIFTY 100")
    df_200 = fetch_index_constituents("NIFTY 200")
    df_500 = fetch_index_constituents("NIFTY 500")
    df_next50 = fetch_index_constituents("NIFTY NEXT 50")

    nifty_50_set   = set(df_50["symbol"].tolist())  if not df_50.empty  else set(NIFTY_50_SYMBOLS)
    nifty_100_set  = set(df_100["symbol"].tolist()) if not df_100.empty else set()
    nifty_200_set  = set(df_200["symbol"].tolist()) if not df_200.empty else set()
    nifty_500_set  = set(df_500["symbol"].tolist()) if not df_500.empty else set()
    next_50_set    = set(df_next50["symbol"].tolist()) if not df_next50.empty else set()

    if df_target.empty:
        log.warning("%s API failed. Falling back to NIFTY 50 baseline.", target_index)
        df_target = pd.DataFrame({"symbol": NIFTY_50_SYMBOLS})
        df_target["company_name"] = ""
        df_target["sector"] = ""
        df_target["industry"] = ""

    import json
    def _memberships(sym: str) -> str:
        memberships = []
        if sym in nifty_50_set:   memberships.append("NIFTY 50")
        if sym in next_50_set:    memberships.append("NIFTY NEXT 50")
        if sym in nifty_100_set:  memberships.append("NIFTY 100")
        if sym in nifty_200_set:  memberships.append("NIFTY 200")
        if sym in nifty_500_set:  memberships.append("NIFTY 500")
        if target_index not in memberships:
            memberships.append(target_index)
        # de-dup while preserving order
        seen, dedup = set(), []
        for m in memberships:
            if m not in seen:
                seen.add(m); dedup.append(m)
        return json.dumps(dedup)

    df_target["index_membership"] = df_target["symbol"].apply(_memberships)
    df_target["is_active"] = True

    log.info("Built universe with %d stocks (target=%s)", len(df_target), target_index)
    return df_target


# Backward-compat alias so any existing caller keeps working.
def build_nifty200_universe() -> pd.DataFrame:
    return build_universe(target_index="NIFTY 200")


def sync_universe_to_db(target_index: str = "NIFTY 500") -> int:
    """
    Fetch the target index constituents from NSE and sync to `universe`.

    Idempotent. Symbols already present have their metadata refreshed but
    aren't deactivated -- that's important because if NSE momentarily 500s
    you don't want to wipe everything you'd already ingested.

    Returns:
        Number of stocks synced.
    """
    df = build_universe(target_index=target_index)
    if df.empty:
        log.error("Empty universe -- cannot sync")
        return 0
    df["exchange"] = "NSE"

    return upsert_dataframe(
        df,
        table="universe",
        conflict_columns=["symbol"],
        update_columns=["company_name", "sector", "industry", "index_membership", "is_active", "exchange"],
    )


def build_bse_universe() -> pd.DataFrame:
    """BSE baseline universe (SENSEX 30), tagged exchange='BSE'.

    No BSE index API is wired yet, so this uses the hardcoded SENSEX-30 baseline.
    Data ingestion for BSE needs the yfinance `.BO` suffix (vs `.NS`) — that
    wiring in yfinance_client is the remaining BSE step.
    """
    import json as _json
    df = pd.DataFrame({"symbol": BSE_SENSEX_SYMBOLS})
    df["company_name"] = ""
    df["sector"] = ""
    df["industry"] = ""
    df["index_membership"] = _json.dumps(["BSE SENSEX"])
    df["is_active"] = True
    df["exchange"] = "BSE"
    return df


def sync_bse_universe_to_db() -> int:
    """Upsert the BSE baseline into `universe` (exchange='BSE')."""
    df = build_bse_universe()
    return upsert_dataframe(
        df,
        table="universe",
        conflict_columns=["symbol"],
        update_columns=["company_name", "sector", "industry", "index_membership", "is_active", "exchange"],
    )


def get_universe_symbols(exchange: Optional[str] = None) -> list[str]:
    """
    Return active universe symbols from the database, optionally filtered by
    exchange ('NSE' / 'BSE').

    Falls back to the Nifty 50 hardcoded list if the database is empty/unreadable.
    """
    from qsde.db import read_sql
    try:
        if exchange:
            df = read_sql(
                "SELECT symbol FROM universe WHERE is_active = TRUE AND exchange = :exch ORDER BY symbol",
                params={"exch": exchange.upper()},
            )
        else:
            df = read_sql("SELECT symbol FROM universe WHERE is_active = TRUE ORDER BY symbol")
        if not df.empty:
            return df["symbol"].tolist()
    except Exception as e:
        log.warning("Could not read universe from DB: %s", e)

    return NIFTY_50_SYMBOLS


def sync_universe_from_kite(exchange: str = "NSE", limit: int | None = None, replace: bool = True) -> int:
    """Build the universe from Kite's instrument master (the PREFERRED source).

    NSE's website blocks server-side scraping (403), so `sync_universe_to_db`
    falls back to a hardcoded 50. Kite is the authoritative, paid instrument
    feed: this refreshes `kite_instruments` if empty, then upserts every EQ
    tradingsymbol on `exchange` into `universe`.

    Requires an active Kite session -- start the backend and open
    http://localhost:8000/api/kite/login_url first.

    Args:
        exchange: "NSE" or "BSE".
        limit:    cap the number of symbols (alphabetical) for a faster first run.
        replace:  deactivate the existing universe for this exchange first, so
                  downstream backfills only touch the fresh Kite-sourced names.
    """
    import json as _json
    from qsde.db import read_sql, execute_sql
    from qsde.ingestion.kite_client import get_kite_client

    client = get_kite_client()
    if not client.is_authenticated:
        raise RuntimeError(
            "Kite not authenticated. Start the backend and open "
            "http://localhost:8000/api/kite/login_url to log in, then re-run."
        )

    n_inst = int(read_sql(
        "SELECT COUNT(*) AS n FROM kite_instruments WHERE exchange = :e",
        params={"e": exchange},
    ).iloc[0]["n"])
    if not n_inst:
        log.info("kite_instruments empty for %s; refreshing from Kite...", exchange)
        client.refresh_instruments(exchange=exchange)

    # Kite's NSE "EQ" list also carries non-stocks (T-bills, G-Secs, SGBs, SDLs)
    # whose tradingsymbols can exceed the VARCHAR(20) `symbol` column. Those
    # aren't equities, so filter by length + common government-security suffixes
    # rather than widening the schema.
    df = read_sql(
        """SELECT tradingsymbol AS symbol, name AS company_name, lot_size
             FROM kite_instruments
            WHERE instrument_type = 'EQ' AND exchange = :e
              AND LENGTH(tradingsymbol) <= 20
              AND tradingsymbol ~ '^[A-Za-z]'        -- real equities start with a letter; drops digit-prefixed bonds/NCDs/T-bills/SDLs
              AND tradingsymbol NOT LIKE :p_tb
              AND tradingsymbol NOT LIKE :p_gs
              AND tradingsymbol NOT LIKE :p_sg
              AND tradingsymbol NOT LIKE :p_gb
              AND tradingsymbol NOT LIKE :p_sl
         ORDER BY tradingsymbol""",
        params={"e": exchange, "p_tb": "%-TB", "p_gs": "%-GS",
                "p_sg": "%-SG", "p_gb": "%-GB", "p_sl": "%-SL"},
    )
    if df.empty:
        log.warning("No EQ instruments for %s in kite_instruments", exchange)
        return 0
    if limit:
        df = df.head(int(limit))

    df["sector"] = ""
    df["industry"] = ""
    df["index_membership"] = _json.dumps([f"{exchange} EQ"])
    df["is_active"] = True
    df["exchange"] = exchange

    if replace:
        execute_sql("UPDATE universe SET is_active = FALSE WHERE exchange = %(e)s", {"e": exchange})

    n = upsert_dataframe(
        df[["symbol", "company_name", "sector", "industry", "index_membership", "is_active", "exchange"]],
        table="universe",
        conflict_columns=["symbol"],
        update_columns=["company_name", "index_membership", "is_active", "exchange"],
    )
    log.info("Universe from Kite: %d active %s EQ symbols", n, exchange)
    return n
