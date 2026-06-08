"""Shared HTTP + persistence helpers for india_data ingestion.

Why this module exists
----------------------
NSE, MoneyControl, RBI, MOSPI all reject default Python User-Agent strings
("Python-urllib/...", "httpx/..."). They want browser-shaped headers. They
also rate-limit hard if you fire requests in a tight loop.

The contract here is:
  * `client()` returns a context-managed httpx.Client with browser-like
    headers, sensible timeouts, and HTTP/2.
  * `polite_get(client, url, ...)` does exponential-backoff retries on
    transient failures (5xx, timeouts, connection resets) and inter-call
    sleep to be a good citizen. Returns the response or raises after
    max retries.
  * `pit_now()` returns the UTC timestamp to stamp on every persisted row
    so factor reads can honor point-in-time correctness.
  * `with_source(df, source)` adds source attribution + fetched_at.

Failures are LOGGED and propagated, never silenced. The daily orchestrator
catches them per-source so one dead feed doesn't kill the others.
"""
from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx
import pandas as pd

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# HTTP defaults
# ──────────────────────────────────────────────────────────────────────

# Chrome-on-Windows User-Agent. NSE specifically rejects anything else with 403.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=30.0,
    write=10.0,
    pool=10.0,
)


@contextmanager
def client(
    *,
    headers: Optional[dict] = None,
    timeout: Optional[httpx.Timeout] = None,
    follow_redirects: bool = True,
) -> Iterator[httpx.Client]:
    """Context-managed httpx Client with browser-like defaults."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    with httpx.Client(
        headers=merged,
        timeout=timeout or DEFAULT_TIMEOUT,
        follow_redirects=follow_redirects,
        http2=False,   # NSE has flakey HTTP/2; HTTP/1.1 is more reliable.
    ) as c:
        yield c


def polite_get(
    c: httpx.Client,
    url: str,
    *,
    params: Optional[dict] = None,
    max_retries: int = 3,
    base_backoff_s: float = 1.5,
    jitter_s: float = 0.5,
) -> httpx.Response:
    """GET with exponential backoff + jitter on transient failures.

    Retries on: 502/503/504, ConnectError, ReadTimeout, RemoteProtocolError.
    Does NOT retry on 4xx other than 429 — those are programmer errors,
    not transient. 429 retries with the longest possible backoff (rate limit).

    Returns the successful response. Raises httpx.HTTPError after all retries
    are exhausted.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = c.get(url, params=params)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError,
                httpx.PoolTimeout) as e:
            last_exc = e
            if attempt == max_retries:
                log.error("polite_get(%s) exhausted retries on %s", url, type(e).__name__)
                raise
            sleep_s = base_backoff_s * (2 ** attempt) + random.uniform(0, jitter_s)
            log.warning("polite_get(%s) %s — retry %d in %.1fs",
                        url, type(e).__name__, attempt + 1, sleep_s)
            time.sleep(sleep_s)
            continue

        # 429 = rate-limited — back off long and retry.
        if resp.status_code == 429:
            if attempt == max_retries:
                resp.raise_for_status()
            retry_after = float(resp.headers.get("Retry-After", "0") or 0)
            sleep_s = max(retry_after, base_backoff_s * (2 ** (attempt + 1)))
            log.warning("polite_get(%s) 429 — backing off %.1fs", url, sleep_s)
            time.sleep(sleep_s)
            continue

        # 5xx — retry.
        if 500 <= resp.status_code < 600:
            if attempt == max_retries:
                resp.raise_for_status()
            sleep_s = base_backoff_s * (2 ** attempt) + random.uniform(0, jitter_s)
            log.warning("polite_get(%s) %d — retry %d in %.1fs",
                        url, resp.status_code, attempt + 1, sleep_s)
            time.sleep(sleep_s)
            continue

        # Success or 4xx (programmer error).
        return resp

    # Should not reach here, but defensively:
    if last_exc:
        raise last_exc
    raise httpx.HTTPError(f"polite_get({url}) failed after {max_retries + 1} attempts")


# ──────────────────────────────────────────────────────────────────────
# PIT timestamping
# ──────────────────────────────────────────────────────────────────────

def pit_now() -> datetime:
    """UTC timestamp for fetched_at columns. Always tz-aware UTC."""
    return datetime.now(tz=timezone.utc)


def with_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Add source + fetched_at columns. Use before persisting any ingested rows.

    Both columns are required by the schema for honest provenance:
        source     -- which feed produced this row
        fetched_at -- when we observed it (PIT key)
    """
    if df.empty:
        return df
    out = df.copy()
    out["source"] = source
    out["fetched_at"] = pit_now()
    return out


# ──────────────────────────────────────────────────────────────────────
# Symbol normalization (RSS title -> NSE symbol)
# ──────────────────────────────────────────────────────────────────────

def normalize_company_name(name: str) -> str:
    """Strip noise so substring matching works on RSS headlines.

    'Reliance Industries Limited' -> 'reliance industries'
    'Tata Consultancy Services Ltd.' -> 'tata consultancy services'

    The point isn't perfect normalization — it's making "RELIANCE" symbol
    find "Reliance Industries..." in a headline most of the time.
    """
    if not isinstance(name, str):
        return ""
    s = name.lower().strip()
    for noise in (" limited", " ltd.", " ltd", " industries",
                  " pvt", " private", " corporation", " corp", " inc",
                  " india", " plc", " co.", " holdings"):
        s = s.replace(noise, " ")
    # Collapse whitespace.
    return " ".join(s.split())


__all__ = [
    "client",
    "polite_get",
    "pit_now",
    "with_source",
    "normalize_company_name",
    "DEFAULT_HEADERS",
    "DEFAULT_TIMEOUT",
]
