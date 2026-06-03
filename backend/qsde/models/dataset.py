"""
Training-dataset builder: combines point-in-time factors with forward-return labels.

Target conventions (configurable via `intraday_target` and `label_method`):

  Fixed-horizon close-to-close (legacy):
      target = close[t + H] / close[t] - 1
    Simple, but the label sees TIME not OUTCOME — see
    qsde/models/triple_barrier.py for why we now prefer triple-barrier.

  Fixed-horizon open-to-close (intraday):
      target = close[t + 1] / open[t + 1] - 1
    Excludes overnight gaps; honest for an intraday-execution model.

  Triple-barrier (AFML Ch. 3, default for all horizons since the AFML slice):
      target = +1 if PT hit first, -1 if SL hit first, 0 if time barrier.
    PT/SL multipliers per horizon are in triple_barrier._HORIZON_BARRIERS;
    they match the live trade_levels.py multipliers so labels and execution
    speak the same language.

Feature transformation:
  apply_fracdiff=True (default since the AFML slice) routes every non-
  stationary feature through fractional differencing (frac_diff_ffd, d=0.4)
  before training. Stationary features (returns, ratios, RSI, ...) are
  left alone. This eliminates the "tech_obv_slope SHAP +16M" pathology
  the audit caught.
"""

import gc
import logging
import time
from datetime import date as _date, timedelta as _timedelta
from typing import Optional, Literal

import numpy as np
import pandas as pd

from qsde.db.connection import read_sql
from qsde.models.purged_cv import HORIZON_DAYS

log = logging.getLogger(__name__)

IntradayTarget = Literal["close_to_close", "open_to_close"]
LabelMethod = Literal["fixed_horizon", "triple_barrier"]


def build_training_dataset(
    horizon: Literal["intraday", "swing", "long"] = "swing",
    start_date: str = "2018-01-01",
    end_date: Optional[str] = None,
    intraday_target: IntradayTarget = "open_to_close",
    label_method: LabelMethod = "triple_barrier",
    apply_fracdiff: bool = True,
    fracdiff_d: float = 0.4,
) -> pd.DataFrame:
    """
    Build a labeled (factors, target) dataset for training.

    Args:
        horizon:         "intraday" / "swing" / "long".
        start_date:      Earliest as_of_date to pull factors for.
        end_date:        Latest as_of_date (default = today).
        intraday_target: For horizon=="intraday" + fixed_horizon target,
                         whether to use close-to-close (legacy) or
                         open-to-close (executable). Ignored otherwise.
        label_method:    "triple_barrier" (default, AFML Ch. 3) or
                         "fixed_horizon" (legacy). Triple-barrier produces
                         {-1, 0, +1} labels keyed to PT/SL/time barriers
                         scaled by EWM volatility per symbol.
        apply_fracdiff:  if True, transform every level-form feature with
                         frac_diff_ffd(d=fracdiff_d) (AFML Ch. 5). Stationary
                         features (RSI, returns, ratios) are skipped by
                         pattern. Strongly recommended — the previous models
                         trained on unscaled levels saturated SHAP.
        fracdiff_d:      differencing order. 0.4 is the LdP-recommended sweet
                         spot for equity series; 0.3 keeps more memory but
                         needs longer warm-up.
    """
    from datetime import date
    if end_date is None:
        end_date = date.today().isoformat()

    if horizon not in HORIZON_DAYS:
        raise ValueError(f"Unknown horizon: {horizon}")

    # 1. Fetch OHLCV. For intraday open-to-close we also need open prices.
    log.info("Fetching OHLCV for targets...")
    prices = read_sql(
        """
        SELECT symbol, date, open, close
          FROM ohlcv
         WHERE date >= CAST(:start_date AS DATE) - INTERVAL '30 days'
      ORDER BY symbol, date
        """,
        params={"start_date": start_date},
    )

    if prices.empty:
        log.warning("No price data found.")
        return pd.DataFrame()

    close_pivot = prices.pivot(index="date", columns="symbol", values="close")

    # 2. Compute forward returns according to the target spec.
    if horizon == "intraday" and intraday_target == "open_to_close":
        # Executable intraday: enter at next-day open, exit at next-day close.
        # Both reference t+1, so the as_of_date factor at t can predict it
        # cleanly without overnight-gap contamination.
        open_pivot = prices.pivot(index="date", columns="symbol", values="open")
        next_open  = open_pivot.shift(-1)
        next_close = close_pivot.shift(-1)
        fwd_returns_pivot = (next_close / next_open) - 1
        log.info("Target: intraday OPEN-to-CLOSE (executable, no overnight gap)")
    else:
        shift_days = -HORIZON_DAYS[horizon]
        fwd_returns_pivot = (close_pivot.shift(shift_days) / close_pivot) - 1
        log.info(
            f"Target: {horizon} close-to-close ({HORIZON_DAYS[horizon]}-day forward return)"
        )

    # 2a. Cost-aware target: subtract the realistic horizon round-trip cost
    # so the model learns to predict NET-of-cost returns directly. This
    # changes the decision threshold from "+predicted return" to "+predicted
    # return AFTER costs" — same downstream interpretation but the model
    # learns to reject trades whose gross edge is eaten by friction.
    #
    # Triple-barrier labeling (label_method=="triple_barrier", default) is
    # SIGN-of-net-return based and so benefits the same way: a trade that
    # would have hit gross-PT now needs to hit gross-PT + cost to register
    # as a WIN. Marginal trades flip from {+1, -1} to 0 (TIME exit),
    # exactly the calibration we want.
    try:
        from qsde.risk.costs import cost_frac
        cf = float(cost_frac(horizon))
        if cf > 0:
            fwd_returns_pivot = fwd_returns_pivot - cf
            log.info(
                "Applied horizon round-trip cost of %.4f (%.1f bps) to target",
                cf, cf * 1e4,
            )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "cost-aware target subtraction failed (%s) — training on gross returns",
            e,
        )

    targets = fwd_returns_pivot.melt(ignore_index=False, value_name="target").reset_index()
    targets = targets.dropna(subset=["target"])
    targets = targets.rename(columns={"date": "as_of_date"})

    # 3. Fetch factors in year-sized chunks so we get progress logs and
    # never hold more than one chunk of the long-form table in memory at
    # once. The chunked path is much friendlier to interrupt and tells
    # you immediately if Postgres is alive vs hung.
    log.info("Fetching point-in-time factors (chunked)...")
    chunk_days = int(
        # Adjustable per-environment. 365d = one calendar year per request.
        # Smaller = more SQL roundtrips but smoother memory. Larger = faster
        # but bigger spikes.
        365
    )
    start_d = pd.Timestamp(start_date).date()
    end_d = pd.Timestamp(end_date).date()
    chunks: list[pd.DataFrame] = []
    cur = start_d
    chunk_i = 0
    total_rows = 0
    t0 = time.time()
    while cur <= end_d:
        nxt = min(cur + _timedelta(days=chunk_days), end_d)
        chunk_i += 1
        t_chunk = time.time()
        df_chunk = read_sql(
            """
            SELECT symbol, as_of_date, factor_name, factor_value
              FROM factor_pit
             WHERE as_of_date >= CAST(:s AS DATE)
               AND as_of_date <= CAST(:e AS DATE)
               AND valid_to = 'infinity'::timestamptz
            """,
            params={"s": cur, "e": nxt},
        )
        n = len(df_chunk)
        total_rows += n
        log.info("  chunk %d  %s -> %s  rows=%d  (%.1fs)",
                 chunk_i, cur, nxt, n, time.time() - t_chunk)
        if not df_chunk.empty:
            df_chunk["factor_value"] = df_chunk["factor_value"].astype("float32")
            chunks.append(df_chunk)
        cur = nxt + _timedelta(days=1)
    log.info("factor_pit fetch complete: %d total rows in %.1fs across %d chunks",
             total_rows, time.time() - t0, chunk_i)

    if not chunks:
        log.warning("No factors found in the specified date range.")
        return pd.DataFrame()
    factors = pd.concat(chunks, ignore_index=True)
    del chunks
    gc.collect()

    log.info("Pivoting factors (float32 wide frame, %d total long rows)...",
             len(factors))
    # factor_value was already cast to float32 per chunk above.
    factors_wide = factors.pivot_table(
        index=["symbol", "as_of_date"],
        columns="factor_name",
        values="factor_value",
    ).reset_index()

    # 4. Merge factors with fixed-horizon target. We may immediately overwrite
    # `target` below if label_method == "triple_barrier".
    log.info("Merging factors and targets...")
    dataset = pd.merge(factors_wide, targets, on=["symbol", "as_of_date"], how="inner")
    # Downcast float64 -> float32 to halve memory before the pivot + fracdiff
    # passes. LightGBM accepts float32 natively; loss of precision below 7
    # decimal digits is irrelevant for these factor magnitudes.
    for col in dataset.select_dtypes(include=["float64"]).columns:
        dataset[col] = dataset[col].astype("float32")
    # Drop the big intermediates — `factors`, `factors_wide`, `targets` and
    # `close_pivot` together can be > 4× the merged dataset.
    del factors, factors_wide, targets, close_pivot
    if "open_pivot" in locals():
        del open_pivot
    if "next_open" in locals():
        del next_open
    if "next_close" in locals():
        del next_close
    if "fwd_returns_pivot" in locals():
        del fwd_returns_pivot
    gc.collect()

    # 5. (Optional) Replace fixed-horizon target with triple-barrier label.
    if label_method == "triple_barrier":
        try:
            from qsde.models.triple_barrier import apply_triple_barrier_labels
            dataset = apply_triple_barrier_labels(
                dataset=dataset,
                ohlcv=prices[["symbol", "date", "close"]],
                horizon=horizon,
            )
            log.info("Switched target -> triple-barrier {-1, 0, +1} labels.")
        except Exception as e:  # noqa: BLE001
            log.warning("Triple-barrier labeling failed (%s) — "
                        "falling back to fixed-horizon target.", e)
        # `prices` is no longer needed past this point.
        del prices
        gc.collect()
    else:
        del prices
        gc.collect()

    # 6. (Optional) Fractional differencing on level-form features.
    if apply_fracdiff:
        try:
            from qsde.models.fracdiff import apply_fracdiff_to_features
            non_feature = {
                "symbol", "as_of_date", "target", "target_ret", "label_end_date",
            }
            feature_cols = [c for c in dataset.columns if c not in non_feature]
            dataset, _d_used = apply_fracdiff_to_features(
                dataset, feature_cols=feature_cols, default_d=fracdiff_d,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("fracdiff transformation failed (%s) — "
                        "leaving features unchanged.", e)

    # 7. Final cleanup. After fracdiff the first (window-1) rows per symbol
    # are NaN by construction; drop them. For the rest, zero-fill missing
    # factor values (LightGBM handles NaN natively but it makes purged-CV
    # row counts noisier).
    dataset = dataset.dropna(subset=["target"])
    dataset = dataset.fillna(0)

    log.info(
        "Built dataset with %d rows, %d features, label_method=%s, fracdiff=%s.",
        len(dataset), len(dataset.columns) - 3, label_method, apply_fracdiff,
    )
    return dataset
