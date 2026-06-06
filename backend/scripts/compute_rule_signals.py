#!/usr/bin/env python
"""Tier 1 rule-based signal pipeline — daily entry point.

Runs the Tier 1 pipeline end-to-end for each requested horizon:

  1. rule_engine.run_for_horizon()  -> compute factor scores, rank, composite
  2. rule_signal_writer.write_rule_signals()  -> upsert 5 streams to signals
  3. paper_journal.take_tier1_trades()  -> take paper trades on tier1_* signals

Called from `daily_eod.py` AFTER ML signals are generated, but BEFORE the
baseline strategy taker. This ordering matters because (a) Tier 1 and ML
operate on the same OHLCV but write to different strategy slots in `signals`,
and (b) Tier 1's per-strategy paper trades enter at the same daily open as
the model's, so net Sharpe is comparable apples-to-apples.

Intraday is intentionally skipped — Tier 1 has no daily-bar factors
suitable for intraday horizons.

Usage
-----
Run all eligible horizons (swing + long):
    python backend/scripts/compute_rule_signals.py

Single horizon:
    python backend/scripts/compute_rule_signals.py --horizon swing

Compute signals but skip paper trade entry (useful for backtest replay):
    python backend/scripts/compute_rule_signals.py --no-take

Exit codes
----------
  0  success, at least one signal generated
  1  unrecoverable error
  2  ran clean but produced zero signals (empty universe / no OHLCV)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

# Make `qsde` importable when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [tier1] %(message)s",
)
log = logging.getLogger("compute_rule_signals")


ELIGIBLE_HORIZONS = ("swing", "long")


def run(horizons: list[str], take_paper_trades: bool = True) -> int:
    """Run the Tier 1 pipeline. Returns total rows written across all horizons."""
    from qsde.execution.paper_journal import take_tier1_trades
    from qsde.research.rule_engine import run_for_horizon
    from qsde.research.rule_signal_writer import write_rule_signals

    total_signals = 0
    total_trades = 0

    for horizon in horizons:
        if horizon == "intraday":
            log.info("Skipping intraday — Tier 1 has no intraday-suitable factors")
            continue
        if horizon not in ELIGIBLE_HORIZONS:
            log.warning("Unknown horizon %r, skipping", horizon)
            continue

        log.info("─" * 60)
        log.info("Horizon: %s", horizon)
        t_h = time.time()

        # 1. Compute scores + ranks + composite
        signals_df = run_for_horizon(horizon)
        if signals_df.empty:
            log.warning("[%s] engine returned empty — no signals to write", horizon)
            continue

        # 2. Write 5 strategy streams to signals table
        n_written = write_rule_signals(signals_df)
        total_signals += n_written
        log.info("[%s] wrote %d signal rows (engine produced %d, took %.1fs)",
                 horizon, n_written, len(signals_df), time.time() - t_h)

        # 3. Take paper trades
        if take_paper_trades:
            result = take_tier1_trades(horizon=horizon)
            if result.get("ok"):
                per_strat = result.get("results", {})
                for strat, info in per_strat.items():
                    taken = info.get("taken", 0)
                    if taken > 0:
                        log.info("[%s] %s -> %d paper trades", horizon, strat, taken)
                    total_trades += taken
            else:
                log.error("[%s] take_tier1_trades failed: %s", horizon, result.get("error"))

    log.info("=" * 60)
    log.info("Tier 1 pipeline complete: %d signals, %d paper trades",
             total_signals, total_trades)
    return total_signals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument(
        "--horizon",
        choices=("all", "swing", "long"),
        default="all",
        help="Horizon to run (default: all eligible).",
    )
    ap.add_argument(
        "--no-take",
        action="store_true",
        help="Compute and write signals but do NOT enter paper trades.",
    )
    args = ap.parse_args()

    horizons = list(ELIGIBLE_HORIZONS) if args.horizon == "all" else [args.horizon]
    log.info("Tier 1 rule-based pipeline starting (%s, horizons=%s, take=%s)",
             date.today().isoformat(), horizons, not args.no_take)

    t0 = time.time()
    try:
        n = run(horizons=horizons, take_paper_trades=not args.no_take)
    except Exception as e:  # noqa: BLE001
        log.exception("Pipeline failed: %s", e)
        sys.exit(1)

    log.info("Total elapsed: %.1fs", time.time() - t0)
    sys.exit(0 if n > 0 else 2)


if __name__ == "__main__":
    main()
