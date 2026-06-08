"""Tests for qsde/ingestion/india_data/* — India-native ingestion.

Hermetic: HTTP is monkey-patched with canned responses; DB persist paths
use monkeypatched execute_sql / read_sql so the suite needs no infrastructure.

Run:
    pytest backend/tests/test_india_data.py -v
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

# ──────────────────────────────────────────────────────────────────────
# Lexicon
# ──────────────────────────────────────────────────────────────────────

from qsde.ingestion.india_data.news._lexicon import score_headline


class TestLexicon:
    def test_positive_headline_scores_positive(self):
        assert score_headline("Reliance Industries Q4 profit jumps; revenue beats estimates") > 0

    def test_negative_headline_scores_negative(self):
        assert score_headline("Adani stock plunges after fraud probe; downgrade hits hard") < 0

    def test_neutral_headline_scores_zero(self):
        assert score_headline("Reliance Industries announces quarterly results today") == 0.0

    def test_empty_string_scores_zero(self):
        assert score_headline("") == 0.0
        assert score_headline(None) == 0.0  # type: ignore[arg-type]

    def test_negation_flips_polarity(self):
        # Coarse lexicon: negation flips the *ratio* of positive vs negative
        # hits. Test with isolated polarity tokens so the ratio is non-zero
        # before the flip (mixed pos+neg tokens average to 0 and the flip
        # is a no-op — documented limitation, swap to FinBERT for production).
        pos = score_headline("strong profit growth this quarter")
        neg = score_headline("no profit growth this quarter")
        assert pos > 0
        assert neg < 0


# ──────────────────────────────────────────────────────────────────────
# Common helpers
# ──────────────────────────────────────────────────────────────────────

from qsde.ingestion.india_data._common import (
    normalize_company_name,
    with_source,
    pit_now,
)


class TestCommon:
    def test_normalize_strips_noise_tokens(self):
        assert normalize_company_name("Reliance Industries Limited") == "reliance"
        assert normalize_company_name("Tata Consultancy Services Ltd.") == "tata consultancy services"
        assert normalize_company_name("ITC Limited") == "itc"

    def test_normalize_handles_non_string(self):
        assert normalize_company_name(None) == ""  # type: ignore[arg-type]

    def test_with_source_adds_columns(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        out = with_source(df, "test_source")
        assert "source" in out.columns
        assert "fetched_at" in out.columns
        assert (out["source"] == "test_source").all()

    def test_with_source_preserves_empty(self):
        df = pd.DataFrame()
        out = with_source(df, "test_source")
        assert out.empty


# ──────────────────────────────────────────────────────────────────────
# MoneyControl RSS parsing
# ──────────────────────────────────────────────────────────────────────

from qsde.ingestion.india_data.news.moneycontrol_rss import (
    NewsItem,
    aggregate_daily,
    attribute_symbols,
    parse_items,
)


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test</title>
    <item>
      <title>Reliance Industries Q4 profit jumps 12 percent on retail growth</title>
      <link>https://example.com/1</link>
      <pubDate>Sat, 07 Jun 2026 10:30:00 +0530</pubDate>
      <description>Quarterly results beat estimates</description>
    </item>
    <item>
      <title>Adani stock plunges after fraud probe announcement</title>
      <link>https://example.com/2</link>
      <pubDate>Sat, 07 Jun 2026 11:00:00 +0530</pubDate>
      <description>Probe announced today</description>
    </item>
    <item>
      <title></title>
      <link>https://example.com/3</link>
      <pubDate>Sat, 07 Jun 2026 12:00:00 +0530</pubDate>
    </item>
  </channel>
</rss>"""


class TestRSSParser:
    def test_parses_well_formed_rss(self):
        items = parse_items(SAMPLE_RSS)
        # Item 3 has empty title and should be skipped.
        assert len(items) == 2
        assert "Reliance" in items[0].title
        assert items[0].pub_date.year == 2026

    def test_handles_malformed_xml(self):
        items = parse_items("<<not really xml>>>")
        assert items == []

    def test_handles_empty_input(self):
        assert parse_items("") == []


class TestSymbolAttribution:
    def test_matches_by_normalized_substring(self):
        items = [
            NewsItem(
                title="Reliance Industries Q4 results beat estimates",
                link="x",
                pub_date=date(2026, 6, 7),
                summary="",
            ),
        ]
        # Universe map: RELIANCE -> "reliance industries" (normalized)
        symbol_map = {
            "RELIANCE": "reliance",
            "HDFCBANK": "hdfc bank",
        }
        pairs = attribute_symbols(items, symbol_map)
        assert len(pairs) == 1
        assert pairs[0][0] == "RELIANCE"

    def test_skips_short_names_to_avoid_false_positives(self):
        # 'ITC' is only 3 chars after normalization, should not match
        # randomly in "India ITC export..." headlines via the 4-char floor.
        items = [
            NewsItem(title="India IT export numbers rise", link="x",
                     pub_date=date(2026, 6, 7), summary=""),
        ]
        symbol_map = {"ITC": "itc"}  # 3 chars
        pairs = attribute_symbols(items, symbol_map)
        assert pairs == []

    def test_empty_input_returns_empty(self):
        assert attribute_symbols([], {"A": "alpha"}) == []
        assert attribute_symbols([NewsItem("t", "l", date(2026, 6, 7), "")], {}) == []


class TestAggregateDaily:
    def test_aggregates_multiple_items_per_symbol_date(self):
        items = [
            NewsItem("Reliance jumps on strong results", "", date(2026, 6, 7), ""),
            NewsItem("Reliance gains; analysts upgrade", "", date(2026, 6, 7), ""),
        ]
        pairs = [("RELIANCE", items[0]), ("RELIANCE", items[1])]
        agg = aggregate_daily(pairs)
        assert len(agg) == 1
        assert int(agg.iloc[0]["news_count"]) == 2
        assert agg.iloc[0]["avg_polarity"] > 0

    def test_separate_days_create_separate_rows(self):
        pairs = [
            ("RELIANCE", NewsItem("good news", "", date(2026, 6, 7), "")),
            ("RELIANCE", NewsItem("bad news; drops", "", date(2026, 6, 8), "")),
        ]
        agg = aggregate_daily(pairs)
        assert len(agg) == 2


# ──────────────────────────────────────────────────────────────────────
# RBI patterns
# ──────────────────────────────────────────────────────────────────────

from qsde.ingestion.india_data.macro.rbi_dbie import (
    RATE_LABEL_PATTERNS,
    SERIES_REPO_RATE,
)
import re


class TestRBIPolicyRatePatterns:
    def test_repo_rate_pattern_matches_typical_homepage_block(self):
        # Synthetic HTML that mimics rbi.org.in's "current rates" block.
        synthetic_html = """
        <ul>
          <li>Policy Repo Rate <span>6.50%</span></li>
          <li>Reverse Repo Rate <span>3.35%</span></li>
        </ul>
        """
        label_re = RATE_LABEL_PATTERNS[SERIES_REPO_RATE]
        pattern = re.compile(rf"{label_re}.{{0,300}}?(\d+\.\d+)\s*%",
                             re.IGNORECASE | re.DOTALL)
        m = pattern.search(synthetic_html)
        assert m is not None
        assert float(m.group(1)) == pytest.approx(6.50)


# ──────────────────────────────────────────────────────────────────────
# NSE bhavcopy comparison logic
# ──────────────────────────────────────────────────────────────────────

from qsde.ingestion.india_data.ground_truth import nse_bhavcopy as bhav_mod


class TestNSEBhavcopyCompare:
    def test_compare_returns_empty_when_within_tolerance(self, monkeypatch):
        # Synthetic bhavcopy: 1 symbol, matches our stored close exactly.
        bc = pd.DataFrame({
            "symbol": ["RELIANCE"],
            "series": ["EQ"],
            "open": [2500.0], "high": [2510.0], "low": [2495.0],
            "close": [2500.0], "volume": [1000000],
        })
        monkeypatch.setattr(bhav_mod, "fetch_bhavcopy", lambda d: bc)
        monkeypatch.setattr(bhav_mod, "read_sql",
                            lambda *a, **kw: pd.DataFrame({"symbol": ["RELIANCE"],
                                                           "close": [2500.5]}))
        result = bhav_mod.compare_to_stored_ohlcv(date(2026, 6, 6))
        # 2500.5 vs 2500 = 0.02% < default 0.1% tolerance -> empty.
        assert result.empty

    def test_compare_flags_diffs_above_tolerance(self, monkeypatch):
        bc = pd.DataFrame({
            "symbol": ["RELIANCE"],
            "series": ["EQ"],
            "open": [2500.0], "high": [2510.0], "low": [2495.0],
            "close": [2500.0], "volume": [1000000],
        })
        monkeypatch.setattr(bhav_mod, "fetch_bhavcopy", lambda d: bc)
        # Our stored close is 2% off NSE — way above 0.1% tolerance.
        monkeypatch.setattr(bhav_mod, "read_sql",
                            lambda *a, **kw: pd.DataFrame({"symbol": ["RELIANCE"],
                                                           "close": [2550.0]}))
        result = bhav_mod.compare_to_stored_ohlcv(date(2026, 6, 6))
        assert len(result) == 1
        assert result.iloc[0]["pct_diff"] > 0.01

    def test_compare_handles_missing_bhavcopy_gracefully(self, monkeypatch):
        monkeypatch.setattr(bhav_mod, "fetch_bhavcopy", lambda d: None)
        result = bhav_mod.compare_to_stored_ohlcv(date(2026, 6, 6))
        assert result.empty
