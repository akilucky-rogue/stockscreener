"""
Daily end-of-day orchestrator.

Runs the post-close refresh so the dashboard has fresh signals every morning
WITHOUT a manual pipeline run. Sequence:

  1. Refresh daily OHLCV from Kite (last 7 days)            [needs Kite token]
  2. Recompute factors for the active universe (last ~400d) [-> factor_pit]
  3. Generate signals for all 3 horizons (+ liquidity/ADV)  [-> signals]
  4. Universe hygiene: deactivate any leaked bond/NCD rows

It does NOT retrain models — that's a heavier, less-frequent job
(run_pipeline.py, weekly at most). EOD only refreshes data + signals off the
already-promoted models.

Graceful degradation: if there's no active Kite token (the daily OAuth login
wasn't done), step 1 is skipped with a clear warning and steps 2-4 still run
on whatever OHLCV already exists — so the dashboard at least gets fresh
factors/signals/liquidity rather than failing outright.

Exit code 0 if signals were generated; non-zero if the run produced nothing
usable (so Task Scheduler shows a failure you can notice).

Usage:
    python scripts/daily_eod.py
    python scripts/daily_eod.py --ohlcv-days 14 --factor-lookback 500
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [eod] %(message)s",
)
log = logging.getLogger("daily_eod")


def _banner(step: str) -> None:
    log.info("=" * 70)
    log.info(step)
    log.info("=" * 70)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ohlcv-days", type=int, default=7,
                    help="Daily OHLCV lookback window (calendar days).")
    ap.add_argument("--factor-lookback", type=int, default=400,
                    help="Days of history to recompute factors over. 400 gives "
                         "enough lookback for the 252d rolling factors while "
                         "staying fast.")
    ap.add_argument("--write-tail-days", type=int, default=10,
                    help="Only persist the last N days of factors to factor_pit "
                         "(daily refresh appends fresh rows instead of rewriting "
                         "the whole 14M-row history). 10 covers long weekends.")
    ap.add_argument("--skip-ohlcv", action="store_true",
                    help="Skip the Kite OHLCV refresh (use existing data).")
    args = ap.parse_args()

    t_start = time.time()
    log.info("Daily EOD run starting (%s)", date.today().isoformat())
    ohlcv_ok = False
    signals_total = 0

    # ── 1. Refresh daily OHLCV from Kite ────────────────────────────
    if args.skip_ohlcv:
        log.warning("Skipping OHLCV refresh (--skip-ohlcv).")
    else:
        _banner("1/4  Refresh daily OHLCV from Kite")
        try:
            from kite_daily_refresh import refresh_daily_ohlcv  # same scripts/ dir
            stats = refresh_daily_ohlcv(days=args.ohlcv_days)
            ohlcv_ok = stats.get("ok", 0) > 0
            log.info("OHLCV refresh: %s", stats)
        except RuntimeError as e:
            # No Kite token -> not fatal; continue on existing data.
            log.warning("OHLCV refresh skipped: %s", e)
            log.warning("Continuing on existing OHLCV (factors/signals will use "
                        "whatever data is already in the DB).")
        except Exception as e:  # noqa: BLE001
            log.exception("OHLCV refresh failed: %s", e)

    # ── 2. Recompute factors ────────────────────────────────────────
    _banner("2/4  Recompute factors -> factor_pit")
    try:
        from qsde.db.connection import read_sql
        from qsde.factors.engine import compute_factors_batch
        symbols = read_sql(
            "SELECT symbol FROM universe WHERE is_active = TRUE ORDER BY symbol"
        )["symbol"].tolist()
        start = (date.today() - timedelta(days=args.factor_lookback)).isoformat()
        log.info("Computing factors for %d symbols from %s (persist last %dd only)...",
                 len(symbols), start, args.write_tail_days)
        # Full lookback for correct rolling factors, but only persist the
        # freshly-changed recent dates (daily refresh, not a full rewrite).
        combined = compute_factors_batch(
            symbols, start=start, write_tail_days=args.write_tail_days,
        )
        log.info("Factors computed: %d rows.", len(combined))
    except Exception as e:  # noqa: BLE001
        log.exception("Factor computation failed: %s", e)

    # ── 3. Generate signals (all horizons) ──────────────────────────
    _banner("3/4  Generate signals (+ liquidity/ADV)")
    try:
        from qsde.models.signal_generator import generate_signals
        for h in ("intraday", "swing", "long"):
            n = generate_signals(h)
            signals_total += int(n or 0)
            log.info("  %s: %s signals", h, n)
    except Exception as e:  # noqa: BLE001
        log.exception("Signal generation failed: %s", e)

    # ── 3b. India-native data refresh ──────────────────────────────
    # MoneyControl + ET RSS news, RBI + MOSPI macro, NSE bhavcopy
    # ground-truth check. Each source runs independently; one failing
    # doesn't block the others. Replaces Finnhub/FMP/FRED placeholders.
    _banner("3b/9  Refresh India-native data (news + macro + ground-truth)")
    try:
        from refresh_india_data import run as run_india_data
        india_summary = run_india_data(["news", "macro", "ground_truth"])
        log.info("India-data refresh summary: %s", india_summary)
    except Exception as e:  # noqa: BLE001
        log.warning("India-data refresh failed (continuing): %s", e)

    # ── 4. Universe hygiene ─────────────────────────────────────────
    _banner("4/5  Universe hygiene (deactivate bond/NCD rows)")
    try:
        from qsde.db.connection import execute_sql
        execute_sql(
            "UPDATE universe SET is_active = FALSE "
            "WHERE is_active = TRUE AND "
            "(symbol ~ '-[A-Z0-9]{2}$' OR company_name ILIKE '%GOI%LOAN%')"
        )
        log.info("Universe hygiene applied.")
    except Exception as e:  # noqa: BLE001
        log.warning("Universe hygiene failed: %s", e)

    # ── 5. Reconcile paper trades ───────────────────────────────────
    _banner("5/7  Reconcile paper trades (live track record)")
    try:
        from qsde.execution.paper_journal import reconcile_open_trades
        res = reconcile_open_trades()
        log.info("Paper reconcile: %s", res)
    except Exception as e:  # noqa: BLE001
        log.warning("Paper reconcile failed: %s", e)

    # ── 6. Auto-take top model signals ──────────────────────────────
    # Without this, the model strategy track record depends on a human
    # clicking "take" in the UI every day — which means missed days,
    # selection bias toward what you remember liking, and a fake record.
    # Auto-taker records the top-K liquid long-only signals per horizon
    # so the model column in the drift report reflects what the system
    # would have actually done.
    _banner("6/7  Auto-take top model signals (model column for drift)")
    try:
        from qsde.execution.auto_taker import take_top_model_signals_all_horizons
        r = take_top_model_signals_all_horizons()
        log.info("Auto-take summary: %d total trades across 3 horizons", r.get("total_taken", 0))
        for h, hres in (r.get("per_horizon") or {}).items():
            symbols_taken = [x["symbol"] for x in (hres.get("results") or []) if x.get("ok")]
            log.info("  model/%s: taken=%d symbols=%s",
                     h, hres.get("taken", 0), symbols_taken or "[]")
    except Exception as e:  # noqa: BLE001
        log.warning("Auto-take failed: %s", e)

    # ── 7. Tier 1 rule-based pipeline (compute + write + take) ──────
    # Mirrors the ML model path: compute signals from raw OHLCV with the
    # four rule-based factors, write to signals (tier1_jt, tier1_mop,
    # tier1_bab, tier1_rsi2, tier1_composite), then enter paper trades.
    # Runs alongside ML during the validation window so per-strategy
    # realized stats are comparable. ML promotions are paused via
    # QSDE_ML_PROMOTION_ENABLED=false during this window — only the
    # currently-promoted ML model keeps producing signals.
    _banner("7/9  Tier 1 rule-based pipeline (compute + write + take)")
    try:
        from compute_rule_signals import run as run_tier1
        n_t1 = run_tier1(horizons=["swing", "long"], take_paper_trades=True)
        log.info("Tier 1 signals written: %d", n_t1)
        # Roll Tier 1 counts into signals_total so the end-of-EOD no-op check
        # ("if signals_total == 0: sys.exit(2)") correctly reflects ALL signal
        # sources, not just ML. Without this, an ML-only outage on a healthy
        # Tier 1 day would still register as a failed EOD.
        signals_total += int(n_t1 or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("Tier 1 pipeline failed: %s", e)

    # ── 8. Tier 1 IC validation — populate rule_factor_ic ───────────
    # Reads historical signals + OHLCV, computes per-factor rolling IC,
    # hit rates, and decile-spread Sharpe. Writes one row per
    # (factor_name, horizon, as_of_date) into rule_factor_ic. The
    # composite_weight column there is read by rule_engine.load_ic_weights
    # on TOMORROW's run, so the composite self-tunes over time.
    # Cold-start safe: all NaN/zero until ~30 sessions of resolved signals.
    _banner("8/9  Tier 1 IC validation (self-tuning composite weights)")
    try:
        from compute_rule_ic import run as run_tier1_ic
        n_ic = run_tier1_ic()
        log.info("rule_factor_ic rows written: %d", n_ic)
    except Exception as e:  # noqa: BLE001
        log.warning("IC validation failed: %s", e)

    # ── 9. Record baseline paper trades for drift comparison ────────
    # The model can't be evaluated in a vacuum — we need a daily snapshot
    # of "what would buying yesterday's top movers / NIFTY proxy / random
    # picks have done?" so the weekly drift report can answer the only
    # question that matters: "is the ML beating these by enough to deploy
    # real money?"
    _banner("9/9  Record baseline paper trades (model vs baselines vs Tier 1)")
    try:
        from qsde.execution.paper_journal import take_baseline_trades
        for h in ("intraday", "swing", "long"):
            r = take_baseline_trades(horizon=h)
            log.info("  baselines/%s: %s", h, r.get("results"))
    except Exception as e:  # noqa: BLE001
        log.warning("Baseline trade recording failed: %s", e)

    elapsed = time.time() - t_start
    _banner(f"Daily EOD complete in {elapsed:.0f}s  ·  "
            f"ohlcv_refreshed={ohlcv_ok}  signals={signals_total}")

    # Fail the task only if we produced no signals at all.
    if signals_total == 0:
        log.error("No signals generated — EOD run is a no-op. Check the log above.")
        sys.exit(2)


if __name__ == "__main__":
    main()
