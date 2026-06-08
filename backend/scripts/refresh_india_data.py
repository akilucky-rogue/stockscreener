#!/usr/bin/env python
"""Refresh all India-native data sources — MoneyControl + ET + RBI + MOSPI + NSE bhavcopy.

Replaces the Finnhub / FMP / FRED placeholders. Runs each source
independently — if one fails (NSE blocks, RBI HTML drifts, RSS down)
the others continue. Per-source error counts come back in the summary.

Wired into daily_eod.py as a new step. Can also run standalone for
ad-hoc backfills:

    python backend/scripts/refresh_india_data.py
    python backend/scripts/refresh_india_data.py --only news
    python backend/scripts/refresh_india_data.py --only macro,ground_truth
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [india-data] %(message)s",
)
log = logging.getLogger("refresh_india_data")


CATEGORIES = ("news", "macro", "ground_truth")


def _run_news() -> dict:
    """MoneyControl + Economic Times RSS."""
    from qsde.ingestion.india_data.news.economic_times_rss import refresh_economic_times_news
    from qsde.ingestion.india_data.news.moneycontrol_rss import refresh_moneycontrol_news

    out: dict[str, object] = {}
    try:
        out["moneycontrol"] = refresh_moneycontrol_news()
    except Exception as e:  # noqa: BLE001
        log.exception("MoneyControl failed: %s", e)
        out["moneycontrol"] = {"error": str(e)}
    try:
        out["economic_times"] = refresh_economic_times_news()
    except Exception as e:  # noqa: BLE001
        log.exception("Economic Times failed: %s", e)
        out["economic_times"] = {"error": str(e)}
    return out


def _run_macro() -> dict:
    """RBI + MOSPI."""
    from qsde.ingestion.india_data.macro.mospi import refresh_mospi_data
    from qsde.ingestion.india_data.macro.rbi_dbie import refresh_rbi_data

    out: dict[str, object] = {}
    try:
        out["rbi"] = refresh_rbi_data()
    except Exception as e:  # noqa: BLE001
        log.exception("RBI failed: %s", e)
        out["rbi"] = {"error": str(e)}
    try:
        out["mospi"] = refresh_mospi_data()
    except Exception as e:  # noqa: BLE001
        log.exception("MOSPI failed: %s", e)
        out["mospi"] = {"error": str(e)}
    return out


def _run_ground_truth() -> dict:
    """NSE bhavcopy cross-check."""
    from qsde.ingestion.india_data.ground_truth.nse_bhavcopy import run_ground_truth_check

    try:
        return {"nse_bhavcopy": run_ground_truth_check()}
    except Exception as e:  # noqa: BLE001
        log.exception("NSE bhavcopy failed: %s", e)
        return {"nse_bhavcopy": {"error": str(e)}}


CATEGORY_RUNNERS = {
    "news":         _run_news,
    "macro":        _run_macro,
    "ground_truth": _run_ground_truth,
}


def run(categories: list[str]) -> dict:
    """Run all requested categories, return aggregated summary."""
    summary: dict[str, object] = {}
    for cat in categories:
        if cat not in CATEGORY_RUNNERS:
            log.warning("Unknown category %r, skipping", cat)
            continue
        log.info("─" * 60)
        log.info("Category: %s", cat)
        t0 = time.time()
        summary[cat] = CATEGORY_RUNNERS[cat]()
        log.info("Category %s done in %.1fs", cat, time.time() - t0)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument(
        "--only",
        default="all",
        help="Comma-separated subset of {news,macro,ground_truth}. Default: all.",
    )
    args = ap.parse_args()

    if args.only == "all":
        cats = list(CATEGORIES)
    else:
        cats = [c.strip() for c in args.only.split(",") if c.strip()]

    log.info("India-data refresh starting (categories=%s)", cats)
    t0 = time.time()
    summary = run(cats)
    log.info("=" * 60)
    log.info("India-data refresh complete in %.1fs", time.time() - t0)
    log.info("Summary: %s", summary)
    sys.exit(0)


if __name__ == "__main__":
    main()
