"""
Compute all factors (technical / fundamental / flow) for the active universe
and persist them point-in-time into `factor_pit`.

Run this whenever the OHLCV table is materially newer than factor_pit:
  * After expanding the universe (e.g. Nifty 200 -> Nifty 500)
  * After a multi-day OHLCV gap that the daily refresh just filled
  * After any factor formula change

Idempotent: write_factors_to_pit() uses the bitemporal UPDATE...FROM
pattern, so re-runs just refresh in place.

Usage:
    # Full universe
    python scripts/compute_factors.py

    # Restrict to recently-added symbols (everything not in factor_pit yet)
    python scripts/compute_factors.py --only-new

    # Specific symbols
    python scripts/compute_factors.py --symbols RELIANCE,TCS,INFY
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import read_sql
from qsde.factors.engine import compute_factors_batch

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (default: full universe)")
    parser.add_argument("--only-new", action="store_true",
                        help="Only symbols not yet in factor_pit")
    parser.add_argument("--start", type=str, default="2018-01-01",
                        help="Earliest date to compute factors from")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.only_new:
        df = read_sql(
            """SELECT u.symbol
                 FROM universe u
            LEFT JOIN (SELECT DISTINCT symbol FROM factor_pit) f
                   ON f.symbol = u.symbol
                WHERE u.is_active = TRUE
                  AND f.symbol IS NULL
             ORDER BY u.symbol"""
        )
        symbols = df["symbol"].tolist()
        log.info("Found %d symbols in universe with no factor_pit rows.", len(symbols))
    else:
        df = read_sql(
            "SELECT symbol FROM universe WHERE is_active = TRUE ORDER BY symbol"
        )
        symbols = df["symbol"].tolist()

    if not symbols:
        log.info("No symbols to process. Done.")
        return

    log.info("Computing factors for %d symbols (start=%s)...", len(symbols), args.start)
    t0 = time.time()
    combined = compute_factors_batch(symbols, start=args.start)
    elapsed = time.time() - t0
    log.info(
        "Done. %d symbols, %d rows, %.1fs (%.1f sym/sec).",
        len(symbols), len(combined), elapsed, len(symbols) / max(elapsed, 1e-6),
    )


if __name__ == "__main__":
    main()
