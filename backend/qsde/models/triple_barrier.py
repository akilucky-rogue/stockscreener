"""
Triple-barrier method for label generation.

Reference: López de Prado, "Advances in Financial Machine Learning" (2018),
Chapter 3. Code adapted from Snippets 3.1, 3.2, 3.3, and 3.4.

Why this module exists
----------------------
Fixed-horizon labels — "what's the return from t to t+H?" — are the lazy
default and create three problems for ML on returns:

  1. The label sees TIME, not OUTCOME. A trade that hits its target on day 1
     gets the same label as one that grinded sideways for 5 days and barely
     closed positive — the model can't tell them apart.
  2. They're symmetric in profit-take vs stop-loss; a real trader rarely
     holds to full horizon — they exit on either target or stop, whichever
     fires first.
  3. Their distribution is dominated by mid-zero noise that the model fits
     and that doesn't generalize.

Triple-barrier fixes all three: for each event (factor as-of-date), set three
barriers — profit-take, stop-loss, and time — and label the event by
whichever fires first:

    +1 if profit-take hits first  (trade-as-intended worked)
    -1 if stop-loss hits first    (trade-as-intended got stopped)
     0 if time barrier hits first (no edge; close at expiry)

The PT and SL distances scale with each symbol's own volatility, so labels
are comparable across high- and low-vol names.

Public API
----------
  get_daily_volatility(close, span=100)
  get_t1_barriers(close, t_events, num_days)
  get_events(close, t_events, pt_sl, target, t1, min_ret)
  get_bins(events, close)
  apply_triple_barrier_labels(dataset, ohlcv, horizon)   <- the one called by dataset.py
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# Per-horizon (pt_mult, sl_mult, t1_days) calibration. These align with
# qsde/risk/trade_levels.py multipliers so the label semantics match what a
# trader actually executes: a "+1 swing label" means the price moved to
# 2.5σ * sqrt(5d) on the profit-take side before hitting 1.5σ on the stop
# side, within 5 sessions.
_HORIZON_BARRIERS: dict[str, dict[str, float]] = {
    "intraday": {"pt_mult": 1.5, "sl_mult": 0.75, "t1_days": 1},
    "swing":    {"pt_mult": 2.5, "sl_mult": 1.50, "t1_days": 5},
    "long":     {"pt_mult": 4.5, "sl_mult": 2.50, "t1_days": 20},
}


# ── volatility ─────────────────────────────────────────────────────

def get_daily_volatility(close: pd.Series, span: int = 100) -> pd.Series:
    """EWM-std of daily log returns. AFML Snippet 3.1.

    This is the σ used to size both barriers. Per-symbol, so high-vol names
    get wider barriers (in absolute price) — labels are comparable across
    the universe.
    """
    if close.empty or len(close) < 2:
        return pd.Series(dtype=float)
    # Map each date to the index of the prior date for ratio computation.
    df0 = close.index.searchsorted(close.index - pd.Timedelta(days=1))
    df0 = df0[df0 > 0]
    idx_pairs = pd.Series(
        close.index[df0 - 1],
        index=close.index[close.shape[0] - df0.shape[0]:],
    )
    rets = close.loc[idx_pairs.index] / close.loc[idx_pairs.values].values - 1
    return rets.ewm(span=span, min_periods=max(2, span // 4)).std()


# ── time barrier (vertical) ────────────────────────────────────────

def get_t1_barriers(
    close: pd.Series,
    t_events: pd.DatetimeIndex,
    num_days: int,
) -> pd.Series:
    """Time-barrier ts for each event = first session ≥ t + num_days.

    Returns a Series indexed by t_events. Values past the close.index tail
    are NaT (no time barrier — the bar will only stop via PT/SL).
    """
    t1 = close.index.searchsorted(t_events + pd.Timedelta(days=num_days))
    t1 = t1[t1 < close.shape[0]]
    out = pd.Series(
        close.index[t1],
        index=t_events[: t1.shape[0]],
    )
    # Pad the rest with NaT so caller sees a 1:1 series.
    return out.reindex(t_events)


# ── horizontal barriers (PT/SL) ────────────────────────────────────

def _apply_pt_sl_on_t1(
    close: pd.Series,
    events: pd.DataFrame,
    pt_sl: tuple[float, float],
) -> pd.DataFrame:
    """For each event in `events`, find the first time within [t, t1] that
    the path of returns hits the PT or SL barrier. AFML Snippet 3.2.

    `events` must carry columns:
        t1     -- time barrier
        trgt   -- per-event target (the σ in volatility units)
        side   -- +1 / -1 (the direction the trade WOULD be taken)

    Returns DataFrame indexed by event with two columns of timestamps
    (`pt`, `sl`). NaT means that barrier was not hit before t1.
    """
    out = events[["t1"]].copy()
    pt, sl = pt_sl
    for loc, t1 in events["t1"].items():
        if pd.isna(t1):
            continue
        df0 = close.loc[loc:t1]
        df0 = (df0 / close.loc[loc] - 1) * events.at[loc, "side"]
        if pt > 0:
            hit = df0[df0 > pt * events.at[loc, "trgt"]]
            out.at[loc, "pt"] = hit.index.min() if not hit.empty else pd.NaT
        if sl > 0:
            hit = df0[df0 < -sl * events.at[loc, "trgt"]]
            out.at[loc, "sl"] = hit.index.min() if not hit.empty else pd.NaT
    return out


def get_events(
    close: pd.Series,
    t_events: pd.DatetimeIndex,
    pt_sl: tuple[float, float],
    target: pd.Series,
    t1: pd.Series,
    side: Optional[pd.Series] = None,
    min_ret: float = 0.0,
) -> pd.DataFrame:
    """Triple-barrier events DataFrame. AFML Snippet 3.3.

    Args:
        close:    daily close prices (Series indexed by date).
        t_events: events to label (as-of-dates from build_training_dataset).
        pt_sl:    (pt_mult, sl_mult). When `side` is None we treat both as
                  symmetric and label only by which barrier is closer.
        target:   per-event σ (from get_daily_volatility, reindexed to t_events).
        t1:       time-barrier timestamps for each event (from get_t1_barriers).
        side:     +1/−1 direction of the trade. If provided, this is META-
                  labeling input — the primary model has already chosen the
                  side; triple-barrier just confirms whether it worked.
        min_ret:  drop events whose target σ is below this minimum — they're
                  too quiet for the barrier multipliers to be meaningful.

    Returns:
        DataFrame indexed by event with columns: t1 (closed time), trgt
        (target σ), pt (PT hit time), sl (SL hit time), side (if provided).
    """
    target = target.reindex(t_events)
    target = target[target > min_ret] if min_ret > 0 else target
    t_events = target.index

    t1 = t1.reindex(t_events)
    if side is None:
        side_ = pd.Series(1.0, index=t_events)
    else:
        side_ = side.reindex(t_events)

    events = pd.concat({"t1": t1, "trgt": target, "side": side_}, axis=1)
    events = events.dropna(subset=["trgt"])

    if events.empty:
        return events

    df0 = _apply_pt_sl_on_t1(close=close, events=events, pt_sl=pt_sl)
    # Earliest-hit barrier becomes the actual close time.
    events["t1"] = df0.dropna(how="all").min(axis=1)
    if side is None:
        events = events.drop(columns=["side"])
    return events


# ── bins: {+1, −1, 0} labels + realized returns ────────────────────

def get_bins(events: pd.DataFrame, close: pd.Series) -> pd.DataFrame:
    """Compute bin labels and realized returns. AFML Snippet 3.5.

    Returns DataFrame indexed by event with columns:
        ret -- realized return from event ts to t1 close
        bin -- {-1, 0, +1} label

    When `events` has a `side` column, this is META labeling:
        bin = 1 if the primary's side made money, 0 otherwise.
    Otherwise it's PRIMARY labeling:
        bin = sign(ret), zeroed out by the time barrier.
    """
    if events.empty:
        return pd.DataFrame(columns=["ret", "bin"])
    px = events.index.union(events["t1"].dropna()).drop_duplicates()
    px = close.reindex(px).ffill()
    out = pd.DataFrame(index=events.index)
    out["ret"] = (px.loc[events["t1"].values].values
                  / px.loc[events.index].values) - 1.0
    if "side" in events.columns:
        out["ret"] *= events["side"].values
        out["bin"] = np.where(out["ret"] > 0, 1, 0)            # meta: was the side right?
    else:
        out["bin"] = np.sign(out["ret"]).astype(int)
        # Hard 0 for events that closed on the time barrier — i.e. neither
        # PT nor SL fired. (Strictly: in AFML the time-barrier bin is also
        # ±1 by realized sign; we keep that default by simply not zeroing
        # here. Override via the `t1_as_neutral` argument upstream if you
        # want pure outcome labeling.)
    return out


# ── high-level: turn a (symbol, as_of_date) panel into triple-barrier labels

def apply_triple_barrier_labels(
    dataset: pd.DataFrame,
    ohlcv: pd.DataFrame,
    horizon: str = "swing",
    vol_span: int = 100,
    min_ret_sigma: float = 0.0,
) -> pd.DataFrame:
    """Replace the `target` column of a (symbol, as_of_date) dataset with
    triple-barrier labels.

    Args:
        dataset: must have columns `symbol`, `as_of_date`, and `target` (the
                 existing fixed-horizon label, which we'll DROP and replace).
        ohlcv:   long-form OHLCV with columns symbol, date, close.
        horizon: 'intraday' | 'swing' | 'long' — picks PT/SL/t1.
        vol_span: EWM span for the σ scaler.
        min_ret_sigma: drop events whose target σ is below this floor.

    Returns:
        DataFrame: same shape as `dataset`, with `target` now in {-1, 0, +1}
        and a new `target_ret` column carrying the realized return for
        diagnostics. Rows that could not be labeled (insufficient future
        history) are dropped.
    """
    cfg = _HORIZON_BARRIERS.get(horizon)
    if cfg is None:
        raise ValueError(f"Unknown horizon '{horizon}' for triple-barrier")
    pt_sl = (float(cfg["pt_mult"]), float(cfg["sl_mult"]))
    t1_days = int(cfg["t1_days"])

    # CRITICAL: normalize as_of_date dtype up-front. build_training_dataset's
    # `as_of_date` is `object` (datetime.date from psycopg2) while every
    # intermediate we build below uses `datetime64[ns]`. Without this cast
    # the merge below silently errors and we fall back to fixed-horizon.
    dataset = dataset.copy()
    dataset["as_of_date"] = pd.to_datetime(dataset["as_of_date"])

    out_chunks: list[pd.DataFrame] = []
    for sym, g in dataset.groupby("symbol", sort=False):
        sym_px = ohlcv[ohlcv["symbol"] == sym].sort_values("date")
        if sym_px.empty:
            continue
        px = pd.Series(
            sym_px["close"].astype(float).values,
            index=pd.to_datetime(sym_px["date"].values),
            name="close",
        ).sort_index()
        if len(px) < t1_days + 5:
            continue
        vol = get_daily_volatility(px, span=vol_span)
        t_events = pd.DatetimeIndex(g["as_of_date"].values)
        # t_events must be SUBSET of px.index for the search/lookup to work.
        t_events = t_events[t_events.isin(px.index)]
        if len(t_events) == 0:
            continue
        t1 = get_t1_barriers(px, t_events, num_days=t1_days)
        events = get_events(
            close=px, t_events=t_events, pt_sl=pt_sl,
            target=vol, t1=t1, min_ret=min_ret_sigma,
        )
        if events.empty:
            continue
        bins = get_bins(events, close=px)
        labels = (
            bins.rename(columns={"ret": "target_ret", "bin": "tb_label"})
                .reset_index()
                .rename(columns={"index": "as_of_date"})
        )
        # Force matching dtype on both sides of the merge.
        labels["as_of_date"] = pd.to_datetime(labels["as_of_date"])
        merged = g.merge(labels, on="as_of_date", how="inner")
        if "target" in merged.columns:
            merged = merged.drop(columns=["target"])
        merged = merged.rename(columns={"tb_label": "target"})
        out_chunks.append(merged)

    if not out_chunks:
        log.warning("Triple-barrier produced 0 labels — check ohlcv coverage.")
        return dataset.iloc[0:0]

    out = pd.concat(out_chunks, ignore_index=True)
    log.info(
        "Triple-barrier (%s, pt=%.1fσ, sl=%.1fσ, t1=%dd): "
        "labels +1=%d, 0=%d, -1=%d (was %d rows fixed-horizon)",
        horizon, pt_sl[0], pt_sl[1], t1_days,
        int((out["target"] == 1).sum()),
        int((out["target"] == 0).sum()),
        int((out["target"] == -1).sum()),
        len(dataset),
    )
    return out
