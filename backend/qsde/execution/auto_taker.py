"""
Auto-taker for paper-trade model signals.

The paper-validation loop only works if model signals ACTUALLY get recorded
every trading day. Doing that by clicking "take" in the UI scales poorly
and creates the worst failure mode: skipped days, biased recall (you take
the ones you remember liking), and a track record that doesn't reflect
what the system would have actually done.

This module records, per horizon, the top-K long-only model signals each
EOD — no human in the loop. The pick rule mirrors the baselines so the
drift comparison is like-for-like:

  - long-only (direction == 1)
  - liquid (is_liquid == TRUE; ADV >= Rs 10 cr)
  - top-K by ranking_score (cross-sectional percentile)
  - require ranking_score >= min_rank (default 0.90 = top 10%)

Idempotent via the existing `paper_trades(strategy,symbol,horizon,entry_date)`
unique constraint — re-runs in the same session add nothing.

Default K = 3 per horizon, matching the baseline pickers. Override via
env var (QSDE_AUTOTAKE_K) or function argument if you want fewer/more.

Called by:
  scripts/daily_eod.py  -> step 5.5 (between reconcile and baselines)
  POST /api/paper/auto-take?horizon=...  -> manual trigger / backfill
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Literal

from qsde.db.connection import read_sql
from qsde.execution.paper_journal import take_trade

log = logging.getLogger(__name__)

Horizon = Literal["intraday", "swing", "long"]

# How many top model signals to record per horizon, per day. Match the
# baseline pickers (3 picks each) so trade counts are comparable.
DEFAULT_TOP_K = int(os.getenv("QSDE_AUTOTAKE_K", "3"))

# Minimum cross-sectional percentile rank required. 0.90 = "in the top 10%".
# Below this the prediction is too close to median to be actionable.
DEFAULT_MIN_RANK = float(os.getenv("QSDE_AUTOTAKE_MIN_RANK", "0.90"))

# Minimum predicted return required PER PICK (as a fraction; 0.0 = no floor).
# When set > 0, the auto-taker rejects symbols whose predicted return is
# below the floor — useful once the model is cost-aware so we skip names
# whose gross edge doesn't even cover friction.
DEFAULT_MIN_PREDICTED_RETURN = float(os.getenv("QSDE_AUTOTAKE_MIN_PRED_RET", "0.0"))

# Skip recording ANY model trades if the median predicted return across the
# top-K candidates is negative. Default off (False) so the model column has
# data on bad days for honest drift comparison; turn on (env="true") once
# you've decided you don't want to log known-bad days.
SKIP_BEARISH_DAYS = os.getenv("QSDE_AUTOTAKE_SKIP_BEARISH", "false").lower() in (
    "1", "true", "yes", "y", "on",
)


def take_top_model_signals(
    horizon: str = "swing",
    top_k: int = DEFAULT_TOP_K,
    min_rank: float = DEFAULT_MIN_RANK,
    direction: int = 1,
    min_predicted_return: float = DEFAULT_MIN_PREDICTED_RETURN,
    skip_bearish_days: bool = SKIP_BEARISH_DAYS,
) -> dict:
    """Record paper trades for the top-K model signals on the latest signal date.

    Args:
        horizon:              intraday | swing | long.
        top_k:                how many top-ranked symbols to take (long side).
        min_rank:             minimum ranking_score (0..1).
        direction:            +1 long-only (default), -1 short-only, 0 = both.
        min_predicted_return: per-pick floor on predicted_return (fraction).
                              0.0 = no floor.
        skip_bearish_days:    if True, take ZERO trades when the median
                              predicted_return across the top-K candidates is
                              negative. Default False (honest drift data).

    Returns:
        dict with attempted/taken counts + per-symbol results. Errors per
        symbol (e.g. "not liquid", "no usable entry price") are preserved
        so the EOD log explains exactly what happened.
    """
    hzn = horizon.lower().strip()
    if hzn not in ("intraday", "swing", "long"):
        return {"ok": False, "error": f"unknown horizon {hzn}"}

    where_dir = "direction = :d" if direction != 0 else "direction <> 0"
    params: dict[str, object] = {
        "h": hzn, "min_rank": min_rank, "k": int(top_k),
        "min_pred": float(min_predicted_return),
    }
    if direction != 0:
        params["d"] = int(direction)

    df = read_sql(
        f"""SELECT symbol, ranking_score, predicted_return, entry_price
              FROM signals
             WHERE horizon = :h
               AND date = (SELECT MAX(date) FROM signals WHERE horizon = :h)
               AND is_liquid = TRUE
               AND ranking_score >= :min_rank
               AND {where_dir}
               AND (predicted_return IS NULL OR predicted_return >= :min_pred)
          ORDER BY ranking_score DESC
             LIMIT :k""",
        params=params,
    )

    if df.empty:
        return {
            "ok": True, "horizon": hzn, "as_of": date.today().isoformat(),
            "attempted": 0, "taken": 0,
            "note": (
                f"no liquid model signals clearing rank>={min_rank:.2f}, "
                f"direction={direction}, predicted_return>={min_predicted_return:.4f} "
                f"on the latest signal date"
            ),
            "results": [],
        }

    # Bearish-day guard: if the median predicted return across the candidates
    # is negative, optionally skip recording any trades. Off by default —
    # we'd rather have the model column show a bad day than have selection
    # bias toward "system only trades on good days".
    if skip_bearish_days:
        rets = df["predicted_return"].dropna().astype(float)
        if len(rets) > 0:
            med = float(rets.median())
            if med < 0:
                return {
                    "ok": True, "horizon": hzn,
                    "as_of": date.today().isoformat(),
                    "attempted": len(df), "taken": 0,
                    "note": (
                        f"bearish-day guard: median predicted_return "
                        f"across top-{len(df)} candidates is {med:+.4f}; "
                        f"skipping all trades for honest comparison "
                        f"(set QSDE_AUTOTAKE_SKIP_BEARISH=false to disable)"
                    ),
                    "results": [],
                    "median_predicted_return": med,
                }

    results: list[dict] = []
    taken = 0
    for _, row in df.iterrows():
        sym = str(row["symbol"])
        r = take_trade(sym, hzn, strategy="model")
        results.append({
            "symbol": sym,
            "ranking_score": float(row["ranking_score"]) if row["ranking_score"] is not None else None,
            "predicted_return": float(row["predicted_return"]) if row["predicted_return"] is not None else None,
            "ok": bool(r.get("ok")),
            "error": r.get("error"),
        })
        if r.get("ok"):
            taken += 1

    return {
        "ok":        True,
        "horizon":   hzn,
        "as_of":     date.today().isoformat(),
        "attempted": len(df),
        "taken":     taken,
        "skipped":   len(df) - taken,
        "results":   results,
        "params":    {
            "top_k": top_k, "min_rank": min_rank, "direction": direction,
            "min_predicted_return": min_predicted_return,
            "skip_bearish_days":    skip_bearish_days,
        },
    }


def take_top_model_signals_all_horizons(
    top_k: int = DEFAULT_TOP_K,
    min_rank: float = DEFAULT_MIN_RANK,
    direction: int = 1,
    min_predicted_return: float = DEFAULT_MIN_PREDICTED_RETURN,
    skip_bearish_days: bool = SKIP_BEARISH_DAYS,
) -> dict:
    """Run take_top_model_signals across all three horizons. Returns a
    summary keyed by horizon — used by the EOD orchestrator."""
    out: dict[str, dict] = {}
    for h in ("intraday", "swing", "long"):
        out[h] = take_top_model_signals(
            horizon=h, top_k=top_k, min_rank=min_rank, direction=direction,
            min_predicted_return=min_predicted_return,
            skip_bearish_days=skip_bearish_days,
        )
    summary = {
        "as_of":       date.today().isoformat(),
        "total_taken": sum((v.get("taken") or 0) for v in out.values()),
        "per_horizon": out,
    }
    return summary
