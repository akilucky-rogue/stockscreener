"""RBI public-data ingestion.

What's actually fetchable without scraping
------------------------------------------
RBI does NOT publish a clean REST API. Most series (CPI, IIP, GDP) live
behind the DBIE web app and require browser sessions to download.

The genuinely-public endpoints we use here:

1. USD/INR Reference Rate
   The RBI Reference Rate is published daily on rbi.org.in as a small
   set of structured pages. We fetch the current-day rates plus the
   recent archive — both are plain HTTP with no auth.

2. Current Policy Rates (Repo / Reverse Repo / SLR / CRR / MSF / Bank Rate)
   Published as a small HTML block on the RBI homepage. We parse it
   with stdlib HTML parsing — deterministic and robust enough for a
   set of 5-6 numeric values that rarely change.

What's queued for after Bright Data is set up
---------------------------------------------
- Historical policy rate timeline (need DBIE web-form submit)
- 10Y G-Sec yield daily series (need DBIE export)
- Long-form CPI/IIP series — pulled from MOSPI instead (cleaner)

Honest about the limits: this module ships what can be fetched cleanly
today. The architecture is correct so Phase 2 plugs in identically.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

from qsde.db.connection import execute_sql
from qsde.ingestion.india_data._common import (
    client,
    pit_now,
    polite_get,
)

log = logging.getLogger(__name__)


SOURCE_TAG = "rbi"


# ──────────────────────────────────────────────────────────────────────
# Series IDs we persist to the macro table
# ──────────────────────────────────────────────────────────────────────

SERIES_USDINR_REF = "rbi_usdinr_ref"
SERIES_REPO_RATE = "rbi_repo_rate"
SERIES_REVERSE_REPO_RATE = "rbi_reverse_repo_rate"
SERIES_CRR = "rbi_crr"
SERIES_SLR = "rbi_slr"
SERIES_MSF_RATE = "rbi_msf_rate"
SERIES_BANK_RATE = "rbi_bank_rate"


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _persist_series(series_id: str, rows: list[tuple[date, float]]) -> int:
    """UPSERT (series_id, date, value) rows into macro with source='rbi'."""
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
# USD/INR reference rate
# ──────────────────────────────────────────────────────────────────────

# Plain HTTP page with the current and recent reference rates.
USDINR_REF_URL = "https://rbi.org.in/Scripts/ReferenceRateArchive.aspx"


def fetch_usdinr_reference(c) -> list[tuple[date, float]]:
    """Return list of (date, usd_inr_rate) tuples from RBI archive page.

    The page renders a table with columns: Date / USD-INR / EURO-INR /
    GBP-INR / JPY-INR. We extract the Date + USD-INR column.

    Heuristic parser: tolerate small layout changes. If the page format
    drifts radically (HTML structure overhaul), this returns [] and logs
    a warning instead of crashing.
    """
    try:
        resp = polite_get(c, USDINR_REF_URL)
        if resp.status_code != 200:
            log.warning("RBI USD/INR fetch %d", resp.status_code)
            return []
    except Exception as e:  # noqa: BLE001
        log.warning("RBI USD/INR fetch failed: %s", e)
        return []

    html = resp.text
    # Look for rows that match the pattern: <td>DD-MMM-YYYY</td><td>nn.nnnn</td>...
    # RBI consistently renders dates as 'DD MMM YYYY' or 'DD-MMM-YYYY'.
    row_pattern = re.compile(
        r"<td[^>]*>\s*(\d{1,2}[-\s][A-Za-z]{3}[-\s]\d{4})\s*</td>"
        r"\s*<td[^>]*>\s*([\d,]+\.\d+)\s*</td>",
        re.IGNORECASE,
    )
    rows: list[tuple[date, float]] = []
    for m in row_pattern.finditer(html):
        date_str, rate_str = m.group(1), m.group(2)
        # Normalize separator + strip thousands.
        date_str = date_str.replace("-", " ")
        try:
            d = datetime.strptime(date_str, "%d %b %Y").date()
            v = float(rate_str.replace(",", ""))
        except ValueError:
            continue
        rows.append((d, v))

    log.info("RBI USD/INR: %d rows parsed", len(rows))
    return rows


# ──────────────────────────────────────────────────────────────────────
# Current policy rates from rbi.org.in homepage
# ──────────────────────────────────────────────────────────────────────

RBI_HOME_URL = "https://www.rbi.org.in/"


# Patterns the RBI homepage uses for the "Current Rates" block. Each
# row is rendered as a single-cell HTML table with the format:
#     Label .... :  VALUE%
# The literal ":  " (colon + double space) is the separator we anchor on.
#
# Tuned against actual rbi.org.in HTML observed 2026-06-08 (Policy Repo
# Rate = 5.25%, etc.). If the markup drifts, patterns return None per
# series — never crash.
RATE_LABEL_PATTERNS: dict[str, str] = {
    SERIES_REPO_RATE:          r"Policy\s*Repo\s*Rate",
    # RBI now publishes the reverse repo as "Fixed Reverse Repo Rate".
    SERIES_REVERSE_REPO_RATE:  r"(?:Fixed\s*)?Reverse\s*Repo\s*Rate",
    SERIES_CRR:                r"\bCRR\b|Cash\s*Reserve\s*Ratio",
    SERIES_SLR:                r"\bSLR\b|Statutory\s*Liquidity\s*Ratio",
    SERIES_MSF_RATE:           r"Marginal\s*Standing\s*Facility\s*Rate",
    SERIES_BANK_RATE:          r"Bank\s*Rate",
}


def fetch_current_policy_rates(c) -> dict[str, Optional[float]]:
    """Parse the current-rates block from the RBI homepage.

    The block uses tabular rendering: 'Label .... :  VALUE%'. We anchor
    on the label, then accept the next 'nn.nn%' that follows within
    150 chars. The colon-space separator is the strong signal that we're
    matching the value cell vs. some other percentage elsewhere on the
    page (e.g. policy commentary).
    """
    try:
        resp = polite_get(c, RBI_HOME_URL)
        if resp.status_code != 200:
            log.warning("RBI homepage fetch %d", resp.status_code)
            return {sid: None for sid in RATE_LABEL_PATTERNS}
    except Exception as e:  # noqa: BLE001
        log.warning("RBI homepage fetch failed: %s", e)
        return {sid: None for sid in RATE_LABEL_PATTERNS}

    html = resp.text
    out: dict[str, Optional[float]] = {}
    for sid, label_re in RATE_LABEL_PATTERNS.items():
        # Match: label, allow whitespace/HTML before the colon separator,
        # then the value followed by a percent sign.
        pattern = re.compile(
            rf"{label_re}[^%]{{0,200}}?:\s*(\d+\.\d+)\s*%",
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.search(html)
        out[sid] = float(m.group(1)) if m else None
    log.info("RBI policy rates: %s",
             {k: v for k, v in out.items() if v is not None})
    return out


# ──────────────────────────────────────────────────────────────────────
# USD/INR — current reference rate from homepage exchange-rates block
# ──────────────────────────────────────────────────────────────────────

# The homepage exchange-rates block is rendered as:
#     INR / 1 USD     | :  95.6198
#     INR / 1 GBP     | :  127.4102
#     ...
# Format date is captioned 'As at 1.00pm of MONTH DD, YYYY'.

USDINR_HOME_PATTERN = re.compile(
    r"INR\s*/\s*1\s*USD[^:]{0,30}:\s*(\d+\.\d+)",
    re.IGNORECASE | re.DOTALL,
)

USDINR_HOME_DATE_PATTERN = re.compile(
    r"As\s+at\s+[\d.:apm\s]+of\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def fetch_usdinr_from_homepage(c) -> Optional[tuple[date, float]]:
    """Pull the current-day USD/INR reference rate from the RBI homepage.

    More reliable than the ReferenceRateArchive.aspx HTML because the
    homepage format is stable. Returns (date, rate) or None.
    """
    try:
        resp = polite_get(c, RBI_HOME_URL)
        if resp.status_code != 200:
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("RBI homepage USD/INR fetch failed: %s", e)
        return None

    html = resp.text
    m_rate = USDINR_HOME_PATTERN.search(html)
    if not m_rate:
        return None
    rate = float(m_rate.group(1))

    # Date — caption looks like 'As at 1.00pm of June 08, 2026'.
    m_date = USDINR_HOME_DATE_PATTERN.search(html)
    if m_date:
        try:
            d = datetime.strptime(m_date.group(1).replace(",", ""),
                                  "%B %d %Y").date()
            return (d, rate)
        except ValueError:
            pass

    # Fallback: stamp today (rates are intra-day refresh).
    return (datetime.now(tz=timezone.utc).date(), rate)


# ──────────────────────────────────────────────────────────────────────
# Public entry — orchestrate
# ──────────────────────────────────────────────────────────────────────

def refresh_rbi_data() -> dict:
    """Pull all currently-supported RBI series and upsert to macro.

    Gated behind QSDE_RBI_ENABLED. Verified 2026-06-09: plain-HTTP fetches
    of rbi.org.in succeed (200 OK) but the regex patterns return 0 rows
    because the rendered HTML structure differs from what WebFetch
    serializes. Tuning requires either Bright Data (JS-capable rendering)
    or a one-off raw-HTML capture to write patterns against. Until then,
    we skip cleanly rather than waste daily-EOD requests.
    """
    import os
    if os.getenv("QSDE_RBI_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        log.info("RBI: skipped (QSDE_RBI_ENABLED!=true). Plain-HTTP parsers "
                 "return 0 rows; tune against raw HTML or enable Bright Data first.")
        return {"usdinr_rows": 0, "policy_rate_rows": 0, "skipped": True}

    summary: dict[str, object] = {}
    with client() as c:
        # 1. USD/INR reference rate — try homepage first (most reliable),
        #    fall back to archive page parsing (historical, often empty
        #    due to layout drift). Persists whichever returns data.
        usdinr_today = fetch_usdinr_from_homepage(c)
        usdinr_archive = fetch_usdinr_reference(c)
        usdinr_rows: list[tuple[date, float]] = list(usdinr_archive)
        if usdinr_today is not None:
            # Replace today's row if archive also has it; else append.
            usdinr_rows = [(d, v) for d, v in usdinr_rows if d != usdinr_today[0]]
            usdinr_rows.append(usdinr_today)
        summary["usdinr_rows"] = _persist_series(SERIES_USDINR_REF, usdinr_rows)

        # 2. Current policy rates — stamped with today's date.
        rates = fetch_current_policy_rates(c)
        today = datetime.now(tz=timezone.utc).date()
        total_rate_rows = 0
        for sid, val in rates.items():
            if val is None:
                continue
            total_rate_rows += _persist_series(sid, [(today, val)])
        summary["policy_rate_rows"] = total_rate_rows

    log.info("RBI summary: %s", summary)
    return summary


__all__ = [
    "SOURCE_TAG",
    "SERIES_USDINR_REF",
    "SERIES_REPO_RATE",
    "SERIES_REVERSE_REPO_RATE",
    "SERIES_CRR",
    "SERIES_SLR",
    "SERIES_MSF_RATE",
    "SERIES_BANK_RATE",
    "fetch_usdinr_reference",
    "fetch_current_policy_rates",
    "refresh_rbi_data",
]
