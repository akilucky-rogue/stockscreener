"""
Fast daily OHLCV refresh from Kite Connect.

Pulls only the last N days of bars for every symbol in the active universe
and upserts. Designed to run nightly (cron / scheduled task) after NSE close
(~16:00 IST). Idempotent -- safe to run multiple times in a single day.

Usage:
    # Default: pull last 7 days
    python scripts/kite_daily_refresh.py

    # Custom lookback
    python scripts/kite_daily_refresh.py --days 14

Why 7 days as default?
    Covers holiday weekends and any single-symbol re-listing edge cases.
    A 7-day window is ~5-6 trading days; we re-upsert them all so corrections
    pushed by NSE (e.g. settlement-day volume reposts) propagate cleanly.

Recommended cron entry:
    # Every weekday at 18:00 IST, after NSE close + settlement.
    0 18 * * 1-5  cd /path/to/qsde/backend && \
        .venv/bin/python scripts/kite_daily_refresh.py

On Windows Task Scheduler:
    Action: Start a program
    Program: C:\\path\\to\\qsde\\.venv\\Scripts\\python.exe
    Args:    scripts\\kite_daily_refresh.py
    Start in: C:\\path\\to\\qsde\\backend
    Trigger: Daily at 18:00 IST, Mon-Fri
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

from qsde.db.connection import read_sql, upsert_dataframe
from qsde.ingestion.kite_client import get_kite_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Stay below Kite's 3 req/sec ceiling.
KITE_REQ_INTERVAL_SEC = 0.35


def refresh_daily_ohlcv(days: int = 7, exchange: str = "NSE") -> dict:
    """Refresh the last `days` of daily OHLCV for the active universe.

    Importable by the daily EOD orchestrator. Returns a stats dict.
    Raises RuntimeError if there's no active Kite token (caller decides
    whether that's fatal).
    """
    to_d   = date.today()
    from_d = to_d - timedelta(days=days)

    client = get_kite_client()
    if not client.is_authenticated:
        raise RuntimeError(
            "No active Kite token. Re-login at http://localhost:8000/api/kite/login_url"
        )

    # Refresh the instrument map if it's stale (>24h old or empty).
    stale = read_sql(
        """SELECT COUNT(*) AS n
             FROM kite_instruments
            WHERE refreshed_at > NOW() - INTERVAL '24 hours'"""
    ).iloc[0]["n"]
    if stale == 0:
        log.info("kite_instruments stale or empty; refreshing...")
        client.refresh_instruments(exchange=exchange)

    symbols = read_sql(
        "SELECT symbol FROM universe WHERE is_active = TRUE ORDER BY symbol"
    )["symbol"].tolist()
    log.info("Daily refresh: %d symbols, %s -> %s (%d-day window)",
             len(symbols), from_d, to_d, days)

    n_ok = n_skip = n_rows_total = 0
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        try:
            df = client.historical_ohlcv(
                symbol=sym, from_date=from_d, to_date=to_d,
                interval="day", exchange=exchange,
            )
        except (ValueError, RuntimeError) as e:
            log.warning("[%d/%d] %s: %s", i, len(symbols), sym, e)
            n_skip += 1
            time.sleep(KITE_REQ_INTERVAL_SEC)
            continue
        if df.empty:
            n_skip += 1
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
        time.sleep(KITE_REQ_INTERVAL_SEC)

    elapsed = time.time() - t0
    log.info("Done. %d ok, %d skipped, %d rows refreshed in %.1fs.",
             n_ok, n_skip, n_rows_total, elapsed)
    return {"ok": n_ok, "skipped": n_skip, "rows": n_rows_total, "elapsed": elapsed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in calendar days (default 7)")
    parser.add_argument("--exchange", type=str, default="NSE")
    args = parser.parse_args()
    try:
        refresh_daily_ohlcv(days=args.days, exchange=args.exchange)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
