"""
Macro factor computation — market-wide regressors from the `macro` table
(real FRED data, ingested by qsde.ingestion.fred_client.sync_all_macro_to_db).

Macro factors are identical across symbols on a given date; they're joined onto
each symbol's daily index so the cross-sectional model can learn regime tilts
(e.g. "momentum pays less when the dollar is ripping / VIX is spiking").

Lookahead safety: FRED prints with a publication lag and we additionally
`.shift(1)` every aligned series, so a factor on date t uses only macro values
known strictly before t.

All columns prefixed `macro_` (engine.py's IC filter already recognizes it).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from qsde.db import read_sql

log = logging.getLogger(__name__)

# FRED series_id -> short handle (mirrors settings.fred_series).
SERIES = {
    "DGS10":          "us10y",
    "VIXCLS":         "vix",
    "FEDFUNDS":       "fedfunds",
    "DTWEXBGS":       "dxy",
    "DCOILBRENTEU":   "brent",
    "INDIRLTLT01STM": "india10y",
}


def compute_macro_features(wide: pd.DataFrame, ohlcv_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Pure: a date-indexed wide macro frame (columns = FRED series_id) +
    a target index -> `macro_*` factor frame aligned (ffill) to that index.
    """
    if len(ohlcv_index) == 0:
        return pd.DataFrame()
    if wide is None or wide.empty:
        return pd.DataFrame(index=ohlcv_index)

    wide = wide.copy()
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()

    idx = pd.to_datetime(ohlcv_index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)

    # ffill macro level onto trading days, then shift(1) for lookahead safety.
    aligned = wide.reindex(wide.index.union(idx)).ffill().reindex(idx)
    aligned.index = ohlcv_index
    aligned = aligned.shift(1)

    h = {sid: SERIES[sid] for sid in SERIES if sid in aligned.columns}
    out = pd.DataFrame(index=ohlcv_index)

    def col(sid):
        return aligned[sid] if sid in aligned.columns else None

    if "DGS10" in h:
        out["macro_us10y"] = col("DGS10")
        out["macro_us10y_chg20"] = col("DGS10").diff(20)
    if "VIXCLS" in h:
        out["macro_vix"] = col("VIXCLS")
        out["macro_vix_chg5"] = col("VIXCLS").diff(5)
    if "FEDFUNDS" in h:
        out["macro_fedfunds"] = col("FEDFUNDS")
    if "DGS10" in h and "FEDFUNDS" in h:
        out["macro_yield_curve"] = col("DGS10") - col("FEDFUNDS")
    if "DTWEXBGS" in h:
        out["macro_dxy_chg20"] = col("DTWEXBGS").pct_change(20) * 100
    if "DCOILBRENTEU" in h:
        out["macro_brent_chg20"] = col("DCOILBRENTEU").pct_change(20) * 100
    if "INDIRLTLT01STM" in h:
        out["macro_india10y"] = col("INDIRLTLT01STM")

    return out.replace([np.inf, -np.inf], np.nan)


def _load_macro_wide() -> pd.DataFrame:
    """Load the `macro` table and pivot to date-indexed wide (cols = series_id)."""
    df = read_sql("SELECT series_id, date, value FROM macro ORDER BY date")
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="series_id", values="value")


def compute_all_macro(ohlcv_index: pd.DatetimeIndex) -> pd.DataFrame:
    """DB-backed entry point: load FRED macro from DB, align onto `ohlcv_index`."""
    if len(ohlcv_index) == 0:
        return pd.DataFrame()
    return compute_macro_features(_load_macro_wide(), ohlcv_index)


__all__ = ["SERIES", "compute_macro_features", "compute_all_macro"]
