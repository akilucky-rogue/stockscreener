"""Economic Times RSS news ingestion.

Same RSS 2.0 format as MoneyControl, so this is a thin config wrapper
around the shared `run_rss_pipeline` in moneycontrol_rss.py.

Coexists with MoneyControl + Finnhub: news_sentiment PK is
(symbol, date, source) after migration 012, so adding ET gives strictly
more coverage. The sentiment factor reader aggregates across all sources
at read time.
"""
from __future__ import annotations

import logging

from qsde.ingestion.india_data.news.moneycontrol_rss import run_rss_pipeline

log = logging.getLogger(__name__)


# Economic Times public RSS endpoints — markets + stocks + company + economy.
# Numeric IDs in URLs are ET's section IDs; published at
# economictimes.indiatimes.com/rssfeedstopstories.cms (master list).
DEFAULT_FEEDS: tuple[str, ...] = (
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms",
    "https://economictimes.indiatimes.com/news/company/rssfeeds/1232708088.cms",
    "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
    "https://economictimes.indiatimes.com/markets/ipo/rssfeeds/4036558.cms",
)


SOURCE_TAG = "economic_times"


def refresh_economic_times_news(
    feeds: tuple[str, ...] = DEFAULT_FEEDS,
) -> dict:
    """Run the ET pipeline. Returns the same diagnostic dict as MoneyControl."""
    return run_rss_pipeline(feeds=feeds, source_tag=SOURCE_TAG)


__all__ = ["DEFAULT_FEEDS", "SOURCE_TAG", "refresh_economic_times_news"]
