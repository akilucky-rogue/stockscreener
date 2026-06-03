"""
Upgrade the active universe from Nifty 200 -> Nifty 500 (or any target index).

Three-step flow this script orchestrates:

  1. Fetch the new index constituents from NSE and upsert into `universe`.
     Old rows aren't deactivated; they're refreshed in place.
  2. Run `kite_seed_ohlcv.py` against the active universe to backfill OHLCV
     for any newly-added symbols (existing ones are upserted, so reruns are
     cheap). Roughly +2 minutes per +100 new symbols at Kite's rate limit.
  3. Print a summary diff so you can confirm the upgrade.

Usage:
    # Default: upgrade to Nifty 500
    python scripts/upgrade_universe.py

    # Or target any other index NSE exposes
    python scripts/upgrade_universe.py --target "NIFTY 100"
    python scripts/upgrade_universe.py --target "NIFTY MIDCAP 150"

Before running:
    * Make sure Kite is authenticated (/api/kite/status -> authenticated:True)
    * kite_instruments must be populated (already done if you ran the seed once)
    * Backend doesn't need to be running -- this script talks directly to the DB
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import read_sql
from qsde.ingestion.universe import sync_universe_to_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=str, default="NIFTY 500",
                        help="Target NSE index name (default: NIFTY 500)")
    parser.add_argument("--skip-backfill", action="store_true",
                        help="Only sync the symbol list; don't pull OHLCV")
    args = parser.parse_args()

    # Snapshot before.
    before = read_sql(
        "SELECT COUNT(*) AS n FROM universe WHERE is_active = TRUE"
    ).iloc[0]["n"]
    log.info("Universe before: %d active symbols", before)

    # 1. Sync the symbol list.
    log.info("Syncing target index '%s' from NSE...", args.target)
    n_synced = sync_universe_to_db(target_index=args.target)
    log.info("Synced %d rows into universe.", n_synced)

    after = read_sql(
        "SELECT COUNT(*) AS n FROM universe WHERE is_active = TRUE"
    ).iloc[0]["n"]
    added = max(0, after - before)
    log.info("Universe after: %d active symbols (+%d)", after, added)

    if args.skip_backfill:
        log.info("--skip-backfill set; not running kite_seed_ohlcv.py")
        return

    # 2. Backfill OHLCV via Kite. Re-uses the existing seed script so
    # idempotency / chunking / rate-limiting are all in one place.
    log.info("Running kite_seed_ohlcv.py to backfill new symbols...")
    seed_script = Path(__file__).parent / "kite_seed_ohlcv.py"
    rc = subprocess.call([sys.executable, str(seed_script)])
    if rc != 0:
        log.error("kite_seed_ohlcv.py exited with code %d", rc)
        sys.exit(rc)

    # 3. Final diff.
    coverage = read_sql(
        """SELECT u.symbol IS NOT NULL AS in_universe,
                  o.symbol IS NOT NULL AS has_ohlcv,
                  COUNT(*) AS n
             FROM universe u
        LEFT JOIN (SELECT DISTINCT symbol FROM ohlcv WHERE source='kite_connect') o
               ON o.symbol = u.symbol
            WHERE u.is_active = TRUE
         GROUP BY 1, 2"""
    )
    log.info("Coverage check:\n%s", coverage.to_string(index=False))

    log.info(
        "Done. Next: rerun `python run_pipeline.py` to retrain on the expanded universe."
    )


if __name__ == "__main__":
    main()
