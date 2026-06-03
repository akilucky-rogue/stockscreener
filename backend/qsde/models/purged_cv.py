"""
Purged k-fold cross-validation with embargo.

Lopez de Prado, "Advances in Financial Machine Learning" (2018), Chapter 7,
Algorithm 7.3. Standard k-fold CV on financial time series produces inflated
out-of-sample scores because forward-return labels overlap across the
train/test boundary -- a 20-day forward return computed on as_of_date=t
"knows" the price at t+20, so a test fold starting at t+5 effectively
shares information with training samples ending at t+20.

This module implements:
  * `purged_kfold_indices` -- generates (train_idx, test_idx) splits where
    training samples whose label window overlaps the test window are
    PURGED (dropped).
  * `embargo_days` -- an additional gap on either side of the test fold,
    used to defang serial autocorrelation in residuals that survives label
    separation. Blueprint Part 9.1 mandates a 5-day embargo.

This is the practical purged k-fold variant. The full Combinatorial Purged
CV (CPCV) from Section 12 of the same book yields more robust DSRs but is
~C(N,k) more expensive; we'll add CPCV when DSR is being used as a
production promotion gate.
"""

from __future__ import annotations

from typing import Iterator, Tuple

import numpy as np
import pandas as pd


# Single source of truth for horizon -> forward-return window mapping.
# Other modules (dataset.py, signal_generator.py, run_pipeline.py, analyze)
# all reference this dict. Adding a new horizon (e.g. "weekly": 10) is a
# one-line edit here and a single addition to each frontend toggle.
HORIZON_DAYS = {
    "intraday": 1,    # blueprint Part 1.4 short end -- next-day position
    "swing":    5,    # 5-day forward return
    "long":     20,   # 20-day forward return
}


def horizon_to_days(horizon: str) -> int:
    """Map model horizon name to forward-return window in trading days."""
    if horizon not in HORIZON_DAYS:
        raise ValueError(f"Unknown horizon: {horizon}")
    return HORIZON_DAYS[horizon]


def purged_kfold_indices(
    dates: pd.Series,
    label_end_dates: pd.Series,
    n_splits: int = 5,
    embargo_days: int = 5,
) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """
    Yield (train_idx, test_idx) for each of n_splits contiguous-time folds.

    Within each split, training samples whose label window (date,
    label_end_date) overlaps the test window (extended by `embargo_days` on
    each side) are dropped. This is the purging step.

    Args:
        dates: as_of_date per sample (the date the factor was computed).
        label_end_dates: the date the target return references -- e.g.
            for a 20-day forward return, as_of_date + 20 trading days.
        n_splits: number of CV folds. 5 is standard.
        embargo_days: extra gap added on both sides of the test window.

    Yields:
        (train_idx, test_idx) numpy arrays of row indices into the original
        DataFrame. Test folds are contiguous in time; training folds are
        whatever remains after purging.
    """
    assert len(dates) == len(label_end_dates), "dates / label_end_dates length mismatch"
    n = len(dates)
    if n_splits < 2:
        raise ValueError("n_splits must be >= 2")

    dates = pd.to_datetime(dates).reset_index(drop=True)
    label_end_dates = pd.to_datetime(label_end_dates).reset_index(drop=True)

    # Sort by date for contiguous fold blocks.
    order = np.argsort(dates.values)
    fold_size = n // n_splits
    embargo = pd.Timedelta(days=embargo_days)

    for fold_i in range(n_splits):
        start = fold_i * fold_size
        end = (fold_i + 1) * fold_size if fold_i < n_splits - 1 else n

        test_idx = order[start:end]
        test_dates = dates.iloc[test_idx]
        test_lo = test_dates.min() - embargo
        test_hi = test_dates.max() + embargo

        # Candidate training rows: everything not in this test fold.
        candidate = np.concatenate([order[:start], order[end:]])

        cand_dates = dates.iloc[candidate]
        cand_label_end = label_end_dates.iloc[candidate]

        # Purge rule: a candidate sample is KEPT only if its full window
        # (cand_date -> cand_label_end) does not overlap [test_lo, test_hi].
        #   Overlap occurs iff cand_date <= test_hi AND cand_label_end >= test_lo.
        # Equivalently, KEEP if cand_label_end < test_lo OR cand_date > test_hi.
        keep = (cand_label_end < test_lo) | (cand_dates > test_hi)
        train_idx = candidate[keep.values]

        yield train_idx, test_idx
