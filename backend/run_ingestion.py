import logging
import sys
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

from qsde.ingestion.universe import sync_universe_to_db, get_universe_symbols
from qsde.ingestion.yfinance_client import backfill_ohlcv, sync_fundamentals_to_db

def main():
    print("--- 1. Syncing Universe ---")
    sync_universe_to_db()

    # Full Nifty 200 universe -- no slicing. The earlier 100-symbol cap
    # was for fast scaffolding; the model needs the full universe to learn.
    symbols = get_universe_symbols()
    print(f"--- 2. Fetched universe, proceeding with {len(symbols)} symbols ---")

    print("--- 3. Syncing Fundamentals (yfinance) ---")
    sync_fundamentals_to_db(symbols)

    print("--- 4. Syncing OHLCV (yfinance) ---")
    # Backfill window is configurable via QSDE_BACKFILL_YEARS (default 7).
    # 7y gives the 252-day momentum factors enough lookback for ~5y of usable
    # training data; set QSDE_BACKFILL_YEARS=20 for the full blueprint target
    # (slower; mind yfinance rate limits).
    import os
    years = int(os.getenv("QSDE_BACKFILL_YEARS", "7"))
    start_date = (date.today() - timedelta(days=365 * years)).isoformat()
    print(f"    backfill window: {years}y (from {start_date})")
    backfill_ohlcv(symbols, start=start_date)

    print("--- 5. Syncing Macro (FRED — real, unlimited) ---")
    from qsde.ingestion.fred_client import sync_all_macro_to_db
    sync_all_macro_to_db()

    print("--- 6. Syncing News Sentiment (Finnhub — real) ---")
    from qsde.ingestion.finnhub_client import sync_sentiment_to_db
    sync_sentiment_to_db(symbols, days=365)

    print("--- Initial Ingestion Complete! ---")

if __name__ == "__main__":
    main()
