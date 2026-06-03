"""
yfinance data client — batch OHLCV download for Nifty 200.

Handles:
  - Historical backfill (2006–2026, 20 years)
  - Daily incremental updates
  - Batch download with retry logic
  - Upsert into PostgreSQL ohlcv table
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from qsde.db import upsert_dataframe, read_sql

log = logging.getLogger(__name__)

# yfinance uses .NS suffix for NSE stocks
def _to_yf_symbol(symbol: str) -> str:
    """Convert internal symbol to yfinance format."""
    if symbol.startswith("^"):
        return symbol  # indices like ^NSEI
    if not symbol.endswith(".NS"):
        return f"{symbol}.NS"
    return symbol


def _from_yf_symbol(yf_symbol: str) -> str:
    """Convert yfinance symbol back to internal format."""
    return yf_symbol.replace(".NS", "")


def download_ohlcv(
    symbols: list[str],
    start: str = "2006-01-01",
    end: Optional[str] = None,
    batch_size: int = 25,
) -> pd.DataFrame:
    """
    Download OHLCV data for multiple symbols in batches.

    Args:
        symbols: List of internal symbols (e.g., ['RELIANCE', 'TCS']).
        start: Start date string (YYYY-MM-DD).
        end: End date string. Defaults to today.
        batch_size: Number of symbols per yfinance batch call.

    Returns:
        DataFrame with columns: symbol, date, open, high, low, close,
        adj_close, volume, source.
    """
    if end is None:
        end = date.today().isoformat()

    yf_symbols = [_to_yf_symbol(s) for s in symbols]
    all_frames = []

    # Process in batches to avoid yfinance rate limits
    for i in range(0, len(yf_symbols), batch_size):
        batch = yf_symbols[i:i + batch_size]
        batch_str = " ".join(batch)
        log.info(
            "Downloading OHLCV batch %d/%d (%d symbols): %s...",
            i // batch_size + 1,
            (len(yf_symbols) + batch_size - 1) // batch_size,
            len(batch),
            batch[:3],
        )

        try:
            data = yf.download(
                batch_str,
                start=start,
                end=end,
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )

            if data.empty:
                log.warning("Empty data for batch starting at index %d", i)
                continue

            # Handle single vs multi-ticker response
            if len(batch) == 1:
                sym = batch[0]
                df = data.copy()
                df["yf_symbol"] = sym
                all_frames.append(df)
            else:
                for sym in batch:
                    try:
                        if sym in data.columns.get_level_values(0):
                            df = data[sym].copy()
                            df["yf_symbol"] = sym
                            all_frames.append(df)
                    except (KeyError, TypeError):
                        log.debug("No data for %s in batch", sym)

        except Exception as e:
            log.error("yfinance download failed for batch %d: %s", i // batch_size, e)
            continue

    if not all_frames:
        log.warning("No OHLCV data downloaded")
        return pd.DataFrame()

    # Combine and normalize
    combined = pd.concat(all_frames, ignore_index=False)
    combined = combined.reset_index()

    # Normalize column names (yfinance returns various capitalizations)
    col_map = {}
    for c in combined.columns:
        cl = str(c).lower().strip()
        if cl in ("date", "datetime"):
            col_map[c] = "date"
        elif cl == "open":
            col_map[c] = "open"
        elif cl == "high":
            col_map[c] = "high"
        elif cl == "low":
            col_map[c] = "low"
        elif cl == "close":
            col_map[c] = "close"
        elif cl in ("adj close", "adj_close"):
            col_map[c] = "adj_close"
        elif cl == "volume":
            col_map[c] = "volume"

    combined = combined.rename(columns=col_map)
    combined["symbol"] = combined["yf_symbol"].apply(_from_yf_symbol)
    combined["source"] = "yfinance"

    # Ensure date is proper date type
    combined["date"] = pd.to_datetime(combined["date"]).dt.date

    # Select and clean columns
    keep_cols = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume", "source"]
    for c in keep_cols:
        if c not in combined.columns:
            combined[c] = None

    result = combined[keep_cols].dropna(subset=["close"])
    result = result.drop_duplicates(subset=["symbol", "date"])

    log.info("Downloaded %d OHLCV rows for %d symbols", len(result), result["symbol"].nunique())
    return result


def backfill_ohlcv(
    symbols: list[str],
    start: str = "2006-01-01",
    end: Optional[str] = None,
) -> int:
    """
    Download and upsert historical OHLCV data into PostgreSQL.

    Args:
        symbols: List of internal symbols.
        start: Start date (YYYY-MM-DD). Default is 2006 for 20-year backfill.
        end: End date. Default is today.

    Returns:
        Number of rows upserted.
    """
    df = download_ohlcv(symbols, start=start, end=end)
    if df.empty:
        return 0

    return upsert_dataframe(
        df,
        table="ohlcv",
        conflict_columns=["symbol", "date"],
        update_columns=["open", "high", "low", "close", "adj_close", "volume", "source"],
    )


def sync_ohlcv_to_db(
    symbols: list[str],
    start: str = "2006-01-01",
    end: Optional[str] = None,
    batch_size: int = 25,
) -> int:
    """Fetch and upsert OHLCV for given symbols."""
    df = download_ohlcv(symbols, start, end, batch_size)
    if df.empty:
        return 0

    return upsert_dataframe(
        df, table="ohlcv",
        conflict_columns=["symbol", "date"],
    )

def fetch_fundamentals_batch(symbols: list[str]) -> pd.DataFrame:
    """Fetch live fundamentals from yfinance."""
    all_rows = []
    
    for sym in symbols:
        yf_sym = _to_yf_symbol(sym)
        try:
            tk = yf.Ticker(yf_sym)
            info = tk.info
            
            # Fetch financials (Income Statement, Balance Sheet, Cash Flow)
            fin = tk.financials
            bs = tk.balance_sheet
            cf = tk.cashflow
            
            # Use info for trailing metrics
            trailing = {
                "symbol": sym,
                "pe_ratio": info.get("trailingPE"),
                "pb_ratio": info.get("priceToBook"),
                "ps_ratio": info.get("priceToSalesTrailing12Months"),
                "ev_ebitda": info.get("enterpriseToEbitda"),
                "ev_to_revenue": info.get("enterpriseToRevenue"),
                "roe": info.get("returnOnEquity") * 100 if info.get("returnOnEquity") else None,
                "roa": info.get("returnOnAssets") * 100 if info.get("returnOnAssets") else None,
                "gross_margin": info.get("grossMargins") * 100 if info.get("grossMargins") else None,
                "operating_margin": info.get("operatingMargins") * 100 if info.get("operatingMargins") else None,
                "net_margin": info.get("profitMargins") * 100 if info.get("profitMargins") else None,
                "debt_equity": info.get("debtToEquity", 0) / 100 if info.get("debtToEquity") else None,
                "revenue_growth_yoy": info.get("revenueGrowth") * 100 if info.get("revenueGrowth") else None,
                "dividend_yield": info.get("dividendYield", 0) * 100 if info.get("dividendYield") else None,
                "market_cap": info.get("marketCap"),
                "enterprise_value": info.get("enterpriseValue"),
                "eps": info.get("trailingEps"),
                "source": "yfinance",
            }
            
            # Get historical periods
            if not fin.empty:
                for col in fin.columns:
                    fiscal_date = pd.to_datetime(col).date()
                    # filing date approx
                    filing_date = fiscal_date + timedelta(days=45)
                    
                    row = trailing.copy()
                    row["fiscal_date"] = fiscal_date
                    row["filing_date"] = filing_date
                    
                    # Extract history
                    try:
                        row["revenue"] = float(fin.loc["Total Revenue", col]) if "Total Revenue" in fin.index else None
                    except Exception:
                        row["revenue"] = None
                        
                    try:
                        row["net_income"] = float(fin.loc["Net Income", col]) if "Net Income" in fin.index else None
                    except Exception:
                        row["net_income"] = None
                        
                    if not cf.empty and col in cf.columns:
                        try:
                            fcf = float(cf.loc["Free Cash Flow", col]) if "Free Cash Flow" in cf.index else None
                            row["free_cash_flow"] = fcf
                            
                            shares = info.get("sharesOutstanding")
                            if fcf and shares:
                                row["fcf_per_share"] = fcf / shares
                        except Exception:
                            pass
                            
                    all_rows.append(row)
        except Exception as e:
            log.warning(f"Failed to fetch fundamentals for {sym}: {e}")
            
    df = pd.DataFrame(all_rows)
    return df

def sync_fundamentals_to_db(symbols: list[str]) -> int:
    """Fetch and upsert fundamentals for given symbols."""
    df = fetch_fundamentals_batch(symbols)
    if df.empty:
        return 0
    return upsert_dataframe(
        df, table="fundamentals",
        conflict_columns=["symbol", "fiscal_date", "filing_date"],
    )


def get_latest_date(symbol: str) -> Optional[date]:
    """Get the most recent OHLCV date for a symbol from the database."""
    try:
        df = read_sql(
            "SELECT MAX(date) as max_date FROM ohlcv WHERE symbol = :symbol",
            params={"symbol": symbol},
        )
        if not df.empty and df.iloc[0]["max_date"] is not None:
            return pd.to_datetime(df.iloc[0]["max_date"]).date()
    except Exception:
        pass
    return None


def incremental_update(symbols: list[str]) -> int:
    """
    Update OHLCV data from the latest available date to today.

    For each symbol, finds the latest date in the DB and downloads
    only the missing data.

    Returns:
        Total number of new rows upserted.
    """
    total = 0
    today = date.today()

    for sym in symbols:
        latest = get_latest_date(sym)
        if latest and latest >= today - timedelta(days=1):
            continue  # Already up to date

        start = (latest + timedelta(days=1)).isoformat() if latest else "2006-01-01"
        count = backfill_ohlcv([sym], start=start, end=today.isoformat())
        total += count

    log.info("Incremental update: %d new rows across %d symbols", total, len(symbols))
    return total
