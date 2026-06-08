"""NSE daily bhavcopy ground-truth cross-check.

Compares our stored OHLCV (Kite-sourced) against the official NSE-published
bhavcopy CSV. Diff > tolerance flags either a Kite adjustment error, an
ingestion gap, or a corporate-action we missed.

What NSE publishes
------------------
Daily after EOD (around 18:00 IST), NSE publishes a CSV at
archives.nseindia.com containing every equity's open/high/low/close for
that session. URL format (as of 2025-2026):

    https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv

The legacy ZIP format at www1.nseindia.com/content/historical/EQUITIES/...
is also still up for older dates. We try the modern URL first, fall back.

NSE blocking
------------
NSE aggressively rate-limits and 403s automated requests. Browser headers
(handled by _common.client) help but are not sufficient when the IP is
flagged. When NSE blocks, we log a warning and return empty — the daily
orchestrator catches it; the data integrity check just doesn't run that
day. Real production use needs Bright Data to bypass the block reliably.
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime
from typing import Optional

import pandas as pd

from qsde.db.connection import read_sql
from qsde.ingestion.india_data._common import (
    client,
    polite_get,
)

log = logging.getLogger(__name__)


SOURCE_TAG = "nse_bhavcopy"

# Default tolerance (relative) for a Kite-vs-bhavcopy close-price diff to
# be flagged as a discrepancy. 0.1% catches anything material; below that
# is noise from rounding / tick-aggregation differences.
DEFAULT_TOLERANCE_PCT = 0.001


# ──────────────────────────────────────────────────────────────────────
# URL builders
# ──────────────────────────────────────────────────────────────────────

def modern_url(d: date) -> str:
    return (
        f"https://archives.nseindia.com/products/content/"
        f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
    )


def legacy_url(d: date) -> str:
    """Legacy ZIP at www1.nseindia.com — used as fallback for older dates."""
    return (
        f"https://www1.nseindia.com/content/historical/EQUITIES/"
        f"{d.year}/{d.strftime('%b').upper()}/cm{d.strftime('%d%b%Y').upper()}bhav.csv.zip"
    )


# ──────────────────────────────────────────────────────────────────────
# Fetcher — tries modern URL, falls back to legacy ZIP
# ──────────────────────────────────────────────────────────────────────

def fetch_bhavcopy(d: date) -> Optional[pd.DataFrame]:
    """Return the bhavcopy DataFrame for date `d` or None when unreachable.

    Columns differ between modern and legacy formats; we normalize to:
        symbol, series, open, high, low, close, volume
    """
    needed = {"symbol", "series", "open", "high", "low", "close", "volume"}
    with client() as c:
        # Modern CSV first.
        try:
            resp = polite_get(c, modern_url(d))
            if resp.status_code == 200 and resp.text.strip():
                df = pd.read_csv(io.StringIO(resp.text))
                df.columns = [str(col).strip() for col in df.columns]
                rename = {
                    "SYMBOL": "symbol", "SERIES": "series",
                    "OPEN_PRICE": "open", "HIGH_PRICE": "high",
                    "LOW_PRICE": "low", "CLOSE_PRICE": "close",
                    "TTL_TRD_QNTY": "volume",
                }
                df = df.rename(columns=rename)
                # Strip whitespace in symbol/series (NSE pads them with spaces).
                for col in ("symbol", "series"):
                    if col in df.columns:
                        df[col] = df[col].astype(str).str.strip()
                if needed.issubset(df.columns):
                    return df[list(needed)]
                log.warning("NSE modern format missing columns; got %s", list(df.columns)[:10])
            else:
                log.warning("NSE modern bhavcopy %s -> %d", d, resp.status_code)
        except Exception as e:  # noqa: BLE001
            log.warning("NSE modern fetch failed for %s: %s", d, e)

        # Legacy ZIP fallback.
        try:
            resp = polite_get(c, legacy_url(d))
            if resp.status_code == 200 and resp.content:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
                    if csv_name is None:
                        return None
                    with zf.open(csv_name) as fp:
                        df = pd.read_csv(fp)
                df.columns = [str(col).strip() for col in df.columns]
                rename = {
                    "SYMBOL": "symbol", "SERIES": "series",
                    "OPEN": "open", "HIGH": "high",
                    "LOW": "low", "CLOSE": "close",
                    "TOTTRDQTY": "volume",
                }
                df = df.rename(columns=rename)
                for col in ("symbol", "series"):
                    if col in df.columns:
                        df[col] = df[col].astype(str).str.strip()
                if needed.issubset(df.columns):
                    return df[list(needed)]
        except Exception as e:  # noqa: BLE001
            log.warning("NSE legacy fetch failed for %s: %s", d, e)

    return None


# ──────────────────────────────────────────────────────────────────────
# Cross-check vs stored OHLCV
# ──────────────────────────────────────────────────────────────────────

def compare_to_stored_ohlcv(
    d: date,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> pd.DataFrame:
    """Return rows where Kite close vs bhavcopy close differs by more than tolerance.

    Output columns:
        symbol, ours_close, nse_close, abs_diff, pct_diff

    Empty DataFrame is the happy path — Kite matches NSE for every symbol.
    """
    bc = fetch_bhavcopy(d)
    if bc is None or bc.empty:
        log.warning("Bhavcopy unavailable for %s — ground-truth check skipped", d)
        return pd.DataFrame(columns=["symbol", "ours_close", "nse_close",
                                     "abs_diff", "pct_diff"])

    # Only compare EQ series (regular cash equity); ignore derivatives,
    # ETFs in DR series, etc.
    bc = bc[bc["series"].str.upper() == "EQ"]
    bc["close"] = pd.to_numeric(bc["close"], errors="coerce")
    bc = bc.dropna(subset=["close"]).set_index("symbol")["close"].to_dict()

    ours = read_sql(
        "SELECT symbol, close FROM ohlcv WHERE date = :d",
        params={"d": d},
    )
    if ours.empty:
        log.warning("No stored OHLCV on %s to compare", d)
        return pd.DataFrame(columns=["symbol", "ours_close", "nse_close",
                                     "abs_diff", "pct_diff"])

    rows = []
    for _, r in ours.iterrows():
        sym = str(r["symbol"]).strip()
        if sym not in bc:
            continue
        ours_c = float(r["close"])
        nse_c = float(bc[sym])
        if ours_c <= 0 or nse_c <= 0:
            continue
        diff_pct = abs(ours_c - nse_c) / nse_c
        if diff_pct > tolerance_pct:
            rows.append({
                "symbol": sym,
                "ours_close": ours_c,
                "nse_close": nse_c,
                "abs_diff": ours_c - nse_c,
                "pct_diff": diff_pct,
            })

    if rows:
        log.warning("Bhavcopy diff on %s: %d symbols exceed %.3f%% tolerance",
                    d, len(rows), tolerance_pct * 100)
        for row in rows[:5]:
            log.warning("  %s: ours=%.2f vs NSE=%.2f (%.3f%%)",
                        row["symbol"], row["ours_close"], row["nse_close"],
                        row["pct_diff"] * 100)
    else:
        log.info("Bhavcopy ground-truth check %s: PASS (all symbols within tolerance)", d)

    return pd.DataFrame(rows).sort_values("pct_diff", ascending=False) if rows \
        else pd.DataFrame(columns=["symbol", "ours_close", "nse_close",
                                   "abs_diff", "pct_diff"])


# ──────────────────────────────────────────────────────────────────────
# Public entry — check the most recent trading day
# ──────────────────────────────────────────────────────────────────────

def run_ground_truth_check(d: Optional[date] = None) -> dict:
    """Check our stored close vs NSE bhavcopy for `d` (default: yesterday).

    Diffs are logged at WARN level. Returns summary diagnostics for the
    daily orchestrator to surface in its end-of-run banner.
    """
    if d is None:
        # Most recent trading day = today if weekday, else most recent past
        # weekday. NSE doesn't publish bhavcopy on weekends.
        today = datetime.now().date()
        d = today
        while d.weekday() >= 5:
            d = date.fromordinal(d.toordinal() - 1)

    diffs = compare_to_stored_ohlcv(d)
    return {
        "checked_date": d.isoformat(),
        "n_diffs":      len(diffs),
        "max_diff_pct": float(diffs["pct_diff"].max()) if not diffs.empty else 0.0,
        "available":    not diffs.empty or "Bhavcopy unavailable" not in str(diffs),
    }


__all__ = [
    "SOURCE_TAG",
    "DEFAULT_TOLERANCE_PCT",
    "modern_url",
    "legacy_url",
    "fetch_bhavcopy",
    "compare_to_stored_ohlcv",
    "run_ground_truth_check",
]
