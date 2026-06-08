"""India-native data ingestion modules.

Replaces the US-centric Finnhub / FMP / FRED stack with sources that
actually have good coverage of NSE/BSE equities and Indian macro:

  news/        — MoneyControl RSS, Economic Times RSS (sentiment input)
  macro/       — RBI DBIE, MOSPI (rates, inflation, IIP, FX)
  ground_truth/ — NSE daily bhavcopy ZIPs (price cross-check vs Kite)
  fundamentals/ — Screener.in (queued — needs Bright Data setup)

Each source persists to the same DB tables the old US-centric clients used
(news_sentiment, macro, fundamentals), with a source attribution column so
multiple sources can coexist. Factor code is unchanged — same shape in,
better data behind it.
"""
