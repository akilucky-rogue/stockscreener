#!/usr/bin/env python
"""Tier 1 IC validation entry point — populates rule_factor_ic.

Computes rolling 60d Information Coefficient, decile hit rates, and
decile-spread Sharpe per (factor, horizon) from historical signals +
OHLCV. Writes one row per (factor_name, horizon, as_of_date) into
rule_factor_ic. The composite_weight column there is read by
qsde.research.rule_engine.load_ic_weights on the NEXT session's run,
so the composite self-tunes to whichever factors are working.

Cold-start safe: factors with < MIN_OBSERVATIONS (20) get composite_weight=0,
which falls back to equal-weight in the engine. Until ~30+ paper sessions
of resolved signals exist, this is effectively a no-op that maintains the
table state correctly.

Called from daily_eod.py as step 8 (between Tier 1 pipeline and baselines)
so that today's freshly-written Tier 1 signals can inform tomorrow's
composite. Order matters:
   step 7: Tier 1 writes signals (uses YESTERDAY's IC weights)
   step 8: IC update reads historical signals -> writes TODAY's IC
   step 9: baselines

Usage
-----
    python backend/scripts/compute_rule_ic.py
    python backend/scripts/compute_rule_ic.py --as-of 2026-06-10

Exit codes
----------
  0  success (rows always written, even if all NaN — cold-start expected)
  1  unrecoverable error
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [tier1-ic] %(message)s",
)
log = logging.getLogger("compute_rule_ic")


def run(as_of_date: date | None = None) -> int:
    """Run the validation update. Returns rows written."""
    from qsde.research.rule_validation import update_composite_weights
    return update_composite_weights(as_of_date=as_of_date)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument(
        "--as-of",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=None,
        help="Date to compute as_of (YYYY-MM-DD). Default: today.",
    )
    args = ap.parse_args()

    log.info("Tier 1 IC validation starting (as_of=%s)",
             (args.as_of or date.today()).isoformat())
    t0 = time.time()
    try:
        n = run(as_of_date=args.as_of)
    except Exception as e:  # noqa: BLE001
        log.exception("IC update failed: %s", e)
        sys.exit(1)

    log.info("Wrote %d rule_factor_ic rows in %.1fs", n, time.time() - t0)
    sys.exit(0)


if __name__ == "__main__":
    main()
