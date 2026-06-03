"""
Kite-first REAL-DATA ingest (no NSE scrape, no slow yfinance loop).

Source of truth:
  * universe + daily OHLCV  -> Zerodha Kite (paid, authoritative)
  * macro                   -> FRED (real, unlimited)
  * fundamentals            -> yfinance (OPTIONAL; Kite has no fundamentals)

PREREQUISITE: an active Kite session. Start the backend, then open
http://localhost:8000/api/kite/login_url in a browser and log in (Zerodha
issues a fresh token daily at 06:00 IST).

Usage (qsde/backend, venv active):
  python scripts/kite_ingest.py --years 20                 # all NSE EQ, 20y daily
  python scripts/kite_ingest.py --years 10 --limit 200     # faster first run
  python scripts/kite_ingest.py --years 20 --with-fundamentals   # + yfinance fundamentals (slow)

Then:
  python -c "from qsde.factors.engine import compute_factors_batch; from qsde.db import read_sql; compute_factors_batch(read_sql('SELECT symbol FROM universe WHERE is_active').symbol.tolist())"
  python run_pipeline.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

KITE_REQ_INTERVAL_SEC = 0.35  # stay under Kite's 3 req/sec ceiling


def main() -> None:
    ap = argparse.ArgumentParser(description="QSDE Kite-first real-data ingest")
    ap.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    ap.add_argument("--years", type=int, default=20, help="daily OHLCV history depth")
    ap.add_argument("--limit", type=int, default=None, help="cap universe size (default: all EQ)")
    ap.add_argument("--with-fundamentals", action="store_true", help="also pull yfinance fundamentals (slow)")
    ap.add_argument("--no-macro", dest="macro", action="store_false", help="skip FRED macro sync")
    ap.set_defaults(macro=True)
    args = ap.parse_args()

    from qsde.ingestion.kite_client import get_kite_client
    client = get_kite_client()
    if not client.is_authenticated:
        log.error(
            "No active Kite token. Start the backend (uvicorn api.main:app --port 8000), "
            "open http://localhost:8000/api/kite/login_url to log in, then re-run."
        )
        sys.exit(1)

    # 1. Universe from Kite instrument master
    from qsde.ingestion.universe import sync_universe_from_kite, get_universe_symbols
    sync_universe_from_kite(exchange=args.exchange, limit=args.limit)
    symbols = get_universe_symbols(exchange=args.exchange)
    if not symbols:
        log.error("Universe is empty after Kite sync; aborting.")
        sys.exit(1)

    # 2. Daily OHLCV from Kite
    from qsde.db import upsert_dataframe
    to_d = date.today()
    from_d = to_d - timedelta(days=365 * args.years)
    log.info("Backfilling %d %s symbols, %dy daily OHLCV via Kite (%s..%s)...",
             len(symbols), args.exchange, args.years, from_d, to_d)
    ok = skip = rows = 0
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        try:
            df = client.historical_ohlcv(
                symbol=sym, from_date=from_d, to_date=to_d,
                interval="day", exchange=args.exchange,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[%d/%d] %s: %s", i, len(symbols), sym, e)
            skip += 1
            time.sleep(KITE_REQ_INTERVAL_SEC)
            continue
        if df is not None and not df.empty:
            df = df.reset_index().rename(columns={"index": "date"})
            df["symbol"] = sym
            df["source"] = "kite_connect"
            df["date"] = pd.to_datetime(df["date"]).dt.date
            upsert_dataframe(
                df[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume", "source"]],
                table="ohlcv", conflict_columns=["symbol", "date"],
                update_columns=["open", "high", "low", "close", "adj_close", "volume", "source"],
            )
            ok += 1
            rows += len(df)
        if i % 25 == 0 or i == len(symbols):
            el = time.time() - t0
            log.info("  [%d/%d] ok=%d skip=%d rows=%d (%.0fs, %.1f/s)",
                     i, len(symbols), ok, skip, rows, el, i / max(el, 1e-6))
        time.sleep(KITE_REQ_INTERVAL_SEC)
    log.info("OHLCV backfill complete: %d ok, %d skipped, %d rows.", ok, skip, rows)

    # 3. Macro (FRED, real)
    if args.macro:
        from qsde.ingestion.fred_client import sync_all_macro_to_db
        log.info("FRED macro: %d rows synced.", sync_all_macro_to_db())

    # 4. Fundamentals (yfinance, optional + slow; Kite has none)
    if args.with_fundamentals:
        from qsde.ingestion.yfinance_client import sync_fundamentals_to_db
        log.info("Fundamentals (yfinance): %d rows.", sync_fundamentals_to_db(symbols))

    log.info("Kite ingest complete. Next: compute_factors_batch -> run_pipeline.py")


if __name__ == "__main__":
    main()
