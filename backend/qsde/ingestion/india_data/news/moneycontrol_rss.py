"""MoneyControl RSS news ingestion.

Drop-in replacement for finnhub_client's news pipeline. Better coverage of
NSE/BSE names because MoneyControl is India-native.

Pipeline:
  1. fetch_feeds()      -> raw RSS XML from each configured feed
  2. parse_items()      -> list of {title, link, pub_date, summary}
  3. attribute_symbols  -> map each item to one or more NSE symbols via
                           substring match on normalized company name
  4. score              -> per-headline polarity from finance lexicon
  5. aggregate_daily    -> group by (symbol, date) -> news_count + avg_polarity
  6. persist            -> UPSERT into news_sentiment with source='moneycontrol'

The schema is unchanged (migration 012 just allows multiple sources per
(symbol, date)). The sentiment factor reads aggregate across sources, so
adding MoneyControl alongside Finnhub gives strictly more coverage, never
less.

Failure mode
------------
If MoneyControl is down or blocks us, this returns empty DataFrames and
logs warnings. The daily orchestrator catches per-source so one dead feed
doesn't kill the others.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

from qsde.db.connection import execute_sql, read_sql
from qsde.ingestion.india_data._common import (
    client,
    normalize_company_name,
    pit_now,
    polite_get,
)
from qsde.ingestion.india_data.news._lexicon import score_headline

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Feed list — MoneyControl's main equity-relevant RSS endpoints
# ──────────────────────────────────────────────────────────────────────

DEFAULT_FEEDS: tuple[str, ...] = (
    "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/business.xml",
    "https://www.moneycontrol.com/rss/results.xml",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.moneycontrol.com/rss/economy.xml",
)


SOURCE_TAG = "moneycontrol"


# ──────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    pub_date: date
    summary: str


# ──────────────────────────────────────────────────────────────────────
# Fetch + parse
# ──────────────────────────────────────────────────────────────────────

def fetch_feed(c, url: str) -> Optional[str]:
    """Return raw RSS XML body or None on failure (logged)."""
    try:
        resp = polite_get(c, url)
        if resp.status_code != 200:
            log.warning("moneycontrol %s -> HTTP %d", url, resp.status_code)
            return None
        return resp.text
    except Exception as e:  # noqa: BLE001
        log.warning("moneycontrol fetch failed: %s -> %s", url, e)
        return None


def _parse_rss_pubdate(s: str) -> Optional[date]:
    """RSS pubDate is RFC 2822: 'Sat, 07 Jun 2026 12:34:56 +0530'.

    We only care about the calendar date for daily aggregation. Tolerant
    of minor format variations — returns None if unparseable.
    """
    if not s:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(s.strip(), fmt).astimezone(timezone.utc).date()
        except ValueError:
            continue
    log.debug("unparseable pubDate: %r", s)
    return None


def parse_items(xml_text: str) -> list[NewsItem]:
    """Extract NewsItem list from one feed's XML."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("moneycontrol XML parse failed: %s", e)
        return []

    items: list[NewsItem] = []
    # RSS 2.0: <channel><item><title/>, <link/>, <pubDate/>, <description/></item>...
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = item.findtext("pubDate") or ""
        pub = _parse_rss_pubdate(pub_raw)
        if not title or pub is None:
            continue
        summary = (item.findtext("description") or "").strip()
        items.append(NewsItem(title=title, link=link, pub_date=pub, summary=summary))
    return items


# ──────────────────────────────────────────────────────────────────────
# Symbol attribution
# ──────────────────────────────────────────────────────────────────────

def _load_symbol_to_name_map() -> dict[str, str]:
    """Return {symbol: normalized_company_name} for the active universe.

    Used to scan RSS titles for substring matches.
    """
    df = read_sql(
        "SELECT symbol, company_name FROM universe WHERE is_active = TRUE"
    )
    if df.empty:
        return {}
    out: dict[str, str] = {}
    for _, r in df.iterrows():
        sym = str(r["symbol"]).strip()
        name = str(r.get("company_name") or "").strip()
        if not sym or not name:
            continue
        out[sym] = normalize_company_name(name)
    return out


def attribute_symbols(
    items: list[NewsItem],
    symbol_name_map: dict[str, str],
) -> list[tuple[str, NewsItem]]:
    """For each item, return (symbol, item) pairs for every match.

    One headline can match multiple symbols (e.g. JV announcements).
    Empty if no match — that's expected; most market-wide headlines
    don't name a specific company.

    Matching is case-insensitive substring on the normalized name. Skips
    matches shorter than 4 chars to avoid trivial false positives
    (e.g. matching "RBI" against every headline that mentions the bank).
    """
    if not items or not symbol_name_map:
        return []
    pairs: list[tuple[str, NewsItem]] = []
    for item in items:
        haystack = normalize_company_name(item.title + " " + item.summary)
        if not haystack:
            continue
        for sym, name in symbol_name_map.items():
            if len(name) >= 4 and name in haystack:
                pairs.append((sym, item))
    return pairs


# ──────────────────────────────────────────────────────────────────────
# Aggregate to (symbol, date) → news_count, avg_polarity
# ──────────────────────────────────────────────────────────────────────

def aggregate_daily(pairs: list[tuple[str, NewsItem]]) -> pd.DataFrame:
    """Group attributed items by (symbol, pub_date) -> {news_count, avg_polarity}."""
    if not pairs:
        return pd.DataFrame(columns=["symbol", "date", "news_count", "avg_polarity"])
    rows = []
    for sym, item in pairs:
        rows.append({
            "symbol": sym,
            "date": item.pub_date,
            "polarity": score_headline(item.title),
        })
    df = pd.DataFrame(rows)
    agg = df.groupby(["symbol", "date"]).agg(
        news_count=("polarity", "count"),
        avg_polarity=("polarity", "mean"),
    ).reset_index()
    return agg


# ──────────────────────────────────────────────────────────────────────
# Persist
# ──────────────────────────────────────────────────────────────────────

def persist(agg: pd.DataFrame, source_tag: str = SOURCE_TAG) -> int:
    """UPSERT one row per (symbol, date) into news_sentiment with the given source tag.

    Migration 012 extended the PK to (symbol, date, source) so multiple
    sources can coexist for the same (symbol, date).
    """
    if agg.empty:
        return 0
    fetched_at = pit_now()
    n = 0
    for _, r in agg.iterrows():
        execute_sql(
            """
            INSERT INTO news_sentiment
                (symbol, date, news_count, avg_polarity, source, fetched_at)
            VALUES
                (%(sym)s, %(d)s, %(n)s, %(p)s, %(src)s, %(t)s)
            ON CONFLICT (symbol, date, source) DO UPDATE SET
                news_count   = EXCLUDED.news_count,
                avg_polarity = EXCLUDED.avg_polarity,
                fetched_at   = EXCLUDED.fetched_at
            """,
            {
                "sym": str(r["symbol"]),
                "d":   r["date"],
                "n":   int(r["news_count"]),
                "p":   float(r["avg_polarity"]),
                "src": source_tag,
                "t":   fetched_at,
            },
        )
        n += 1
    return n


# ──────────────────────────────────────────────────────────────────────
# Generic RSS pipeline (reusable by any RSS 2.0 source)
# ──────────────────────────────────────────────────────────────────────

def run_rss_pipeline(
    feeds: tuple[str, ...],
    source_tag: str,
) -> dict:
    """Generic RSS 2.0 pipeline: fetch feeds, attribute, aggregate, persist.

    Used by:
      - refresh_moneycontrol_news (this module)
      - refresh_economic_times_news (economic_times_rss.py)

    Any future RSS 2.0 source can plug in by calling this with its own
    feed list + source tag. The XML parser, symbol attribution, polarity
    scoring, and persistence are all source-agnostic.
    """
    symbol_map = _load_symbol_to_name_map()
    if not symbol_map:
        log.warning("Universe is empty; %s ingest has nothing to attribute to", source_tag)
        return {"feeds_fetched": 0, "items_seen": 0, "items_attributed": 0,
                "symbols_touched": 0, "rows_upserted": 0}

    all_items: list[NewsItem] = []
    feeds_ok = 0
    with client() as c:
        for url in feeds:
            xml = fetch_feed(c, url)
            if xml is None:
                continue
            feeds_ok += 1
            items = parse_items(xml)
            log.info("%s %s -> %d items", source_tag, url.rsplit("/", 1)[-1], len(items))
            all_items.extend(items)

    pairs = attribute_symbols(all_items, symbol_map)
    agg = aggregate_daily(pairs)
    rows = persist(agg, source_tag=source_tag)

    summary = {
        "feeds_fetched":    feeds_ok,
        "items_seen":       len(all_items),
        "items_attributed": len(pairs),
        "symbols_touched":  int(agg["symbol"].nunique()) if not agg.empty else 0,
        "rows_upserted":    rows,
    }
    log.info("%s summary: %s", source_tag, summary)
    return summary


def refresh_moneycontrol_news(
    feeds: tuple[str, ...] = DEFAULT_FEEDS,
) -> dict:
    """Run the MoneyControl pipeline. Thin wrapper around run_rss_pipeline.

    Gated behind QSDE_MONEYCONTROL_ENABLED to avoid spamming 6 daily 403s.
    Verified 2026-06-09 that MoneyControl 403s plain-HTTP RSS requests
    across all 6 feeds (anti-bot tightened). Re-enable once Bright Data
    wraps the fetcher (Phase IN8) — same env-flag pattern as MOSPI.
    """
    import os
    if os.getenv("QSDE_MONEYCONTROL_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        log.info("MoneyControl: skipped (QSDE_MONEYCONTROL_ENABLED!=true). "
                 "Anti-bot 403s plain-HTTP RSS. Re-enable after Bright Data setup.")
        return {"feeds_fetched": 0, "items_seen": 0, "items_attributed": 0,
                "symbols_touched": 0, "rows_upserted": 0, "skipped": True}
    return run_rss_pipeline(feeds=feeds, source_tag=SOURCE_TAG)


__all__ = [
    "DEFAULT_FEEDS",
    "SOURCE_TAG",
    "NewsItem",
    "fetch_feed",
    "parse_items",
    "attribute_symbols",
    "aggregate_daily",
    "persist",
    "run_rss_pipeline",
    "refresh_moneycontrol_news",
]
