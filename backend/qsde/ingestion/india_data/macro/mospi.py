"""MOSPI macro indicators — CPI, IIP, WPI, GDP.

MOSPI (Ministry of Statistics and Programme Implementation) is the
canonical source for India's real-economy indicators. There is no
public REST API — data flows through:

  1. Monthly press releases with HTML tables (CPI All-India, IIP)
  2. eSankhyiki portal CSV downloads (cleaner, but JS-rendered)
  3. PIB (Press Information Bureau) press releases

What this module ships today
----------------------------
* CPI All-India Combined (monthly) — parsed from the latest MOSPI press
  release URL. Stable text format that hasn't changed in years.
* IIP General Index (monthly) — same pattern, IIP press release page.

What's queued for after Bright Data is set up
---------------------------------------------
* WPI from DPIIT (separate ministry)
* GDP quarterly from CSO release
* eSankhyiki time-series CSV exports for backfill (need JS-capable scraper)

The architecture is the same regardless of source — each fetcher returns
list[tuple[date, value]] and persists via `_persist_series`. Adding new
series later is one new function + a series_id constant.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

from qsde.db.connection import execute_sql
from qsde.ingestion.india_data._common import (
    client,
    pit_now,
    polite_get,
)

log = logging.getLogger(__name__)


SOURCE_TAG = "mospi"


# Series IDs persisted to the macro table.
SERIES_CPI_ALL_INDIA_COMBINED = "mospi_cpi_all_combined"   # general inflation (CPI-C)
SERIES_CPI_INFLATION_YOY = "mospi_cpi_inflation_yoy"       # % YoY change
SERIES_IIP_GENERAL = "mospi_iip_general"                   # IIP general index
SERIES_IIP_GROWTH_YOY = "mospi_iip_growth_yoy"             # % YoY change


# MOSPI public landing pages for latest releases. These URLs are stable
# across release cycles (the page content updates monthly with the new
# month's values).
CPI_RELEASE_URL = "https://www.mospi.gov.in/cpi"
IIP_RELEASE_URL = "https://www.mospi.gov.in/iip"


# ──────────────────────────────────────────────────────────────────────
# Persistence helper (mirrors RBI module)
# ──────────────────────────────────────────────────────────────────────

def _persist_series(series_id: str, rows: list[tuple[date, float]]) -> int:
    """UPSERT (series_id, date, value) rows into macro with source='mospi'."""
    if not rows:
        return 0
    fetched_at = pit_now()
    n = 0
    for d, v in rows:
        if v is None or not isinstance(v, (int, float)):
            continue
        execute_sql(
            """
            INSERT INTO macro (series_id, date, value, source, fetched_at)
            VALUES (%(s)s, %(d)s, %(v)s, %(src)s, %(t)s)
            ON CONFLICT (series_id, date) DO UPDATE SET
                value      = EXCLUDED.value,
                source     = EXCLUDED.source,
                fetched_at = EXCLUDED.fetched_at
            """,
            {"s": series_id, "d": d, "v": float(v),
             "src": SOURCE_TAG, "t": fetched_at},
        )
        n += 1
    return n


# ──────────────────────────────────────────────────────────────────────
# CPI — latest monthly release
# ──────────────────────────────────────────────────────────────────────

# MOSPI's CPI release page consistently uses phrasings like:
#   "All India CPI ... for the month of MONTH YYYY ... 192.7"
#   "Year-on-year inflation rate ... 4.83 per cent"
# We extract the headline index and inflation % using bounded matchers.

CPI_INDEX_PATTERN = re.compile(
    r"(?:All\s*India\s*CPI[\s\w()-]*?)"
    r"(?:for\s*the\s*month\s*of\s*)(\w+)\s*(\d{4}).{0,200}?(\d{2,3}\.\d)",
    re.IGNORECASE | re.DOTALL,
)

CPI_INFLATION_PATTERN = re.compile(
    r"(?:Year-on-year|annual|YoY)[^.]{0,80}?inflation.{0,80}?(\d{1,2}\.\d{1,2})\s*per\s*cent",
    re.IGNORECASE | re.DOTALL,
)


def fetch_latest_cpi(c) -> dict[str, Optional[tuple[date, float]]]:
    """Return {'cpi_index': (date, value), 'inflation_yoy': (date, pct)}.

    Both may be None when the page format drifts. Caller persists only
    non-None entries.
    """
    out: dict[str, Optional[tuple[date, float]]] = {
        "cpi_index": None, "inflation_yoy": None,
    }
    try:
        resp = polite_get(c, CPI_RELEASE_URL)
        if resp.status_code != 200:
            log.warning("MOSPI CPI page %d", resp.status_code)
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("MOSPI CPI fetch failed: %s", e)
        return out

    html = resp.text

    m_idx = CPI_INDEX_PATTERN.search(html)
    release_date: Optional[date] = None
    if m_idx:
        month_str, year_str, idx_str = m_idx.group(1), m_idx.group(2), m_idx.group(3)
        try:
            release_date = datetime.strptime(f"01 {month_str} {year_str}",
                                             "%d %B %Y").date()
            out["cpi_index"] = (release_date, float(idx_str))
        except ValueError:
            pass

    m_infl = CPI_INFLATION_PATTERN.search(html)
    if m_infl and release_date is not None:
        try:
            out["inflation_yoy"] = (release_date, float(m_infl.group(1)))
        except ValueError:
            pass

    log.info("MOSPI CPI: %s", out)
    return out


# ──────────────────────────────────────────────────────────────────────
# IIP — latest monthly release
# ──────────────────────────────────────────────────────────────────────

IIP_INDEX_PATTERN = re.compile(
    r"(?:IIP[\s\w()-]*?)(?:for\s*the\s*month\s*of\s*)(\w+)\s*(\d{4}).{0,300}?(\d{2,4}\.\d)",
    re.IGNORECASE | re.DOTALL,
)

IIP_GROWTH_PATTERN = re.compile(
    r"(?:growth|change|rate)[^.]{0,80}?(\d{1,2}\.\d{1,2})\s*per\s*cent",
    re.IGNORECASE | re.DOTALL,
)


def fetch_latest_iip(c) -> dict[str, Optional[tuple[date, float]]]:
    """Return {'iip_index': (date, value), 'growth_yoy': (date, pct)}."""
    out: dict[str, Optional[tuple[date, float]]] = {
        "iip_index": None, "growth_yoy": None,
    }
    try:
        resp = polite_get(c, IIP_RELEASE_URL)
        if resp.status_code != 200:
            log.warning("MOSPI IIP page %d", resp.status_code)
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("MOSPI IIP fetch failed: %s", e)
        return out

    html = resp.text
    m_idx = IIP_INDEX_PATTERN.search(html)
    release_date: Optional[date] = None
    if m_idx:
        month_str, year_str, idx_str = m_idx.group(1), m_idx.group(2), m_idx.group(3)
        try:
            release_date = datetime.strptime(f"01 {month_str} {year_str}",
                                             "%d %B %Y").date()
            out["iip_index"] = (release_date, float(idx_str))
        except ValueError:
            pass

    m_grw = IIP_GROWTH_PATTERN.search(html)
    if m_grw and release_date is not None:
        try:
            out["growth_yoy"] = (release_date, float(m_grw.group(1)))
        except ValueError:
            pass

    log.info("MOSPI IIP: %s", out)
    return out


# ──────────────────────────────────────────────────────────────────────
# Public entry — orchestrate
# ──────────────────────────────────────────────────────────────────────

def refresh_mospi_data() -> dict:
    """Pull all currently-supported MOSPI series and upsert to macro.

    Honest scope note (verified 2026-06-09): MOSPI's /cpi and /iip pages
    return HTML shells with all data loaded client-side via JavaScript.
    Plain-HTTP fetches return 200 OK with empty bodies (just GTM tags +
    meta). The regex parsers correctly extract zero data because there
    is no data in the served HTML.

    This is gated behind QSDE_MOSPI_ENABLED to avoid wasting requests
    on a known-broken path until Bright Data (JS rendering) is wired
    up. Phase IN8 in the task list re-enables it.

    Set QSDE_MOSPI_ENABLED=true once Bright Data is available and the
    fetcher implementations are switched to use it.
    """
    import os
    if os.getenv("QSDE_MOSPI_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        log.info("MOSPI: skipped (QSDE_MOSPI_ENABLED!=true). "
                 "Plain-HTTP MOSPI returns JS-rendered shells. Set the env var "
                 "to re-enable once Bright Data wraps the fetcher.")
        return {"cpi_rows": 0, "iip_rows": 0, "skipped": True}

    summary: dict[str, object] = {}
    with client() as c:
        cpi = fetch_latest_cpi(c)
        n = 0
        if cpi["cpi_index"] is not None:
            n += _persist_series(SERIES_CPI_ALL_INDIA_COMBINED, [cpi["cpi_index"]])
        if cpi["inflation_yoy"] is not None:
            n += _persist_series(SERIES_CPI_INFLATION_YOY, [cpi["inflation_yoy"]])
        summary["cpi_rows"] = n

        iip = fetch_latest_iip(c)
        n = 0
        if iip["iip_index"] is not None:
            n += _persist_series(SERIES_IIP_GENERAL, [iip["iip_index"]])
        if iip["growth_yoy"] is not None:
            n += _persist_series(SERIES_IIP_GROWTH_YOY, [iip["growth_yoy"]])
        summary["iip_rows"] = n

    log.info("MOSPI summary: %s", summary)
    return summary


__all__ = [
    "SOURCE_TAG",
    "SERIES_CPI_ALL_INDIA_COMBINED",
    "SERIES_CPI_INFLATION_YOY",
    "SERIES_IIP_GENERAL",
    "SERIES_IIP_GROWTH_YOY",
    "fetch_latest_cpi",
    "fetch_latest_iip",
    "refresh_mospi_data",
]
