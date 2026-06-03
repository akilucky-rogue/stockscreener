"""
Backfill daily OHLCV for the entire universe via Kite Connect.

Usage:
    # Backfill the last 7 years of daily bars
    python scripts/kite_seed_ohlcv.py

    # Custom date range
    python scripts/kite_seed_ohlcv.py --from 2020-01-01 --to 2026-05-19

Prerequisites:
    1. KITE_API_KEY / KITE_API_SECRET set in .env
    2. You've completed the OAuth flow at least once today
       (GET /api/kite/login_url -> browser login -> /api/kite/callback)
    3. kite_instruments table populated:
       curl -X POST http://localhost:8000/api/kite/refresh_instruments

Notes:
    * Kite's free historical-data API serves daily bars; intraday requires
      the ₹2k/month historical add-on.
    * Each symbol is one HTTP call. With Nifty 200 that's 191 calls -- about
      90 seconds at Kite's 3 req/sec rate limit. Nifty 500 = ~3.5 minutes.
    * Uses `ON CONFLICT` upsert so reruns just refresh recent rows; old
      history isn't re-fetched unless you delete and rerun.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Make sure backend/ is on PYTHONPATH when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from qsde.db.connection import read_sql, upsert_dataframe
from qsde.ingestion.kite_client import get_kite_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# Kite's official rate-limit is 3 req/sec. Stay under it with a small margin.
KITE_REQ_INTERVAL_SEC = 0.40


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", type=str, default=None,
                        help="Start date YYYY-MM-DD (default: 7 years ago)")
    parser.add_argument("--to", dest="to_date", type=str, default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--exchange", type=str, default="NSE")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only ingest the first N symbols (for testing)")
    args = parser.parse_args()

    to_d   = date.fromisoformat(args.to_date)   if args.to_date   else date.today()
    from_d = date.fromisoformat(args.from_date) if args.from_date else (to_d - timedelta(days=365 * 7))

    client = get_kite_client()
    if not client.is_authenticated:
        log.error(
            "No active Kite token. Log in first: "
            "open http://localhost:8000/api/kite/login_url in a browser."
        )
        sys.exit(1)

    # Make sure the instrument map is fresh -- without it, symbol lookups fail.
    n_inst = read_sql("SELECT COUNT(*) AS n FROM kite_instruments").iloc[0]["n"]
    if n_inst == 0:
        log.info("kite_instruments empty; refreshing...")
        client.refresh_instruments(exchange=args.exchange)

    symbols_df = read_sql(
        """SELECT symbol FROM universe
            WHERE is_active = TRUE
         ORDER BY symbol"""
    )
    symbols = symbols_df["symbol"].tolist()
    if args.limit:
        symbols = symbols[: args.limit]

    log.info(
        "Backfilling %d symbols, %s -> %s (interval=day) from Kite...",
        len(symbols), from_d, to_d,
    )

    n_ok = 0
    n_skip = 0
    n_rows_total = 0
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        try:
            df = client.historical_ohlcv(
                symbol=sym, from_date=from_d, to_date=to_d,
                interval="day", exchange=args.exchange,
            )
        except ValueError as e:
            log.warning("[%d/%d] %s: %s", i, len(symbols), sym, e)
            n_skip += 1
            time.sleep(KITE_REQ_INTERVAL_SEC)
            continue
        except Exception as e:
            log.warning("[%d/%d] %s: %s", i, len(symbols), sym, e)
            n_skip += 1
            time.sleep(KITE_REQ_INTERVAL_SEC)
            continue

        if df.empty:
            log.info("[%d/%d] %s: 0 rows", i, len(symbols), sym)
            time.sleep(KITE_REQ_INTERVAL_SEC)
            continue

        df = df.reset_index().rename(columns={"index": "date"})
        df["symbol"] = sym
        df["source"] = "kite_connect"
        df["date"]   = pd.to_datetime(df["date"]).dt.date

        upsert_dataframe(
            df[["symbol", "date", "open", "high", "low",
                "close", "adj_close", "volume", "source"]],
            table="ohlcv",
            conflict_columns=["symbol", "date"],
            update_columns=["open", "high", "low", "close",
                            "adj_close", "volume", "source"],
        )
        n_ok += 1
        n_rows_total += len(df)
        if i % 25 == 0 or i == len(symbols):
            elapsed = time.time() - t0
            log.info(
                "  [%d/%d] %s: +%d rows  (ok=%d, skip=%d, "
                "elapsed=%.0fs, rate=%.1f/s)",
                i, len(symbols), sym, len(df), n_ok, n_skip,
                elapsed, i / max(elapsed, 1e-6),
            )
        time.sleep(KITE_REQ_INTERVAL_SEC)

    log.info(
        "Done. %d symbols ok, %d skipped, %d total rows written.",
        n_ok, n_skip, n_rows_total,
    )


if __name__ == "__main__":
    main()
