"""
NSE institutional flows client — FII/DII daily data + India VIX.

FII/DII flow regime is a market-level signal: sustained FII selling
for 10+ sessions is a reliable short-term bearish signal on Nifty 50
constituents (Blueprint §5.2).
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

import httpx
import pandas as pd

from qsde.config import settings
from qsde.db import upsert_dataframe

log = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
}


def _nse_get(path: str) -> dict | list | None:
    """GET from NSE API with session cookies."""
    client = httpx.Client(timeout=15.0, headers=NSE_HEADERS, follow_redirects=True)
    try:
        client.get(settings.nse_base_url)
        time.sleep(1)
        url = f"{settings.nse_base_url}/api/{path.lstrip('/')}"
        resp = client.get(url)
        if resp.status_code == 200:
            return resp.json()
        log.warning("NSE %d for %s", resp.status_code, path)
        return None
    except Exception as e:
        log.error("NSE error for %s: %s", path, e)
        return None
    finally:
        client.close()


def fetch_fii_dii_activity() -> pd.DataFrame:
    """
    Fetch FII/DII daily activity from NSE.

    Returns:
        DataFrame with columns: date, category, buy_value, sell_value,
        net_value, source.
    """
    data = _nse_get("fiidiiActivity/activity")
    if not data:
        data = _nse_get("fiidiiActivity")
    if not data:
        return pd.DataFrame()

    entries = data if isinstance(data, list) else data.get("data", [])
    rows = []
    for entry in entries:
        cat = entry.get("category", "")
        if "FII" in cat.upper() or "FPI" in cat.upper():
            category = "FII"
        elif "DII" in cat.upper():
            category = "DII"
        else:
            continue
        rows.append({
            "date": pd.to_datetime(entry.get("date", ""), errors="coerce"),
            "category": category,
            "buy_value": entry.get("buyValue"),
            "sell_value": entry.get("sellValue"),
            "net_value": entry.get("netValue"),
            "source": "nse",
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = df["date"].dt.date
    return df


def fetch_india_vix() -> Optional[dict]:
    """Fetch current India VIX value."""
    data = _nse_get("allIndices")
    if not data or "data" not in data:
        return None
    for idx in data["data"]:
        if "VIX" in idx.get("index", "").upper():
            return {
                "date": idx.get("timeVal", ""),
                "value": idx.get("last"),
                "change_pct": idx.get("percentChange"),
            }
    return None


def sync_fii_dii_to_db() -> int:
    """Fetch and upsert FII/DII activity into DB."""
    df = fetch_fii_dii_activity()
    if df.empty:
        return 0
    return upsert_dataframe(
        df, table="institutional_flows",
        conflict_columns=["date", "category"],
        update_columns=["buy_value", "sell_value", "net_value"],
    )
