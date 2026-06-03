"""
FRED client — US macro data that drives Indian markets through FII channel.

11 macro series mapped (Blueprint §5.1). Free, unlimited API.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
import pandas as pd

from qsde.config import settings
from qsde.db import upsert_dataframe

log = logging.getLogger(__name__)


def fetch_fred_series(series_id: str, limit: int = 5000) -> pd.DataFrame:
    """
    Fetch a FRED time series.

    Returns:
        DataFrame with columns: series_id, date, value, source.
    """
    if not settings.fred_api_key:
        log.warning("FRED API key not configured")
        return pd.DataFrame()

    url = f"{settings.fred_base_url}/series/observations"
    params = {
        "series_id": series_id,
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }

    try:
        resp = httpx.get(url, params=params, timeout=15.0)
        if resp.status_code != 200:
            log.warning("FRED %d for %s", resp.status_code, series_id)
            return pd.DataFrame()

        data = resp.json()
        obs = data.get("observations", [])
        if not obs:
            return pd.DataFrame()

        rows = []
        for o in obs:
            val = o.get("value", ".")
            if val == ".":
                continue
            rows.append({
                "series_id": series_id,
                "date": o["date"],
                "value": float(val),
                "source": "fred",
            })

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    except Exception as e:
        log.error("FRED error for %s: %s", series_id, e)
        return pd.DataFrame()


def sync_all_macro_to_db() -> int:
    """Fetch all configured FRED series and upsert to macro table."""
    total = 0
    for name, series_id in settings.fred_series.items():
        df = fetch_fred_series(series_id)
        if df.empty:
            continue
        count = upsert_dataframe(
            df, table="macro",
            conflict_columns=["series_id", "date"],
            update_columns=["value"],
        )
        total += count
        log.info("Synced %d rows for %s (%s)", count, name, series_id)
    return total
