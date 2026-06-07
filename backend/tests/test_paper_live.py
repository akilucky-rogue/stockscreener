"""Tests for qsde/execution/paper_live.py — live trade tracker.

Pure-math tests against `_compute_stats` use synthetic candles directly.
DB-touching `build_live_payload` is tested via monkeypatched loaders so
the suite stays hermetic.

Run:
    pytest backend/tests/test_paper_live.py -v
"""
from __future__ import annotations

import math

import pytest

from qsde.execution import paper_live
from qsde.execution.paper_live import (
    HORIZON_SESSIONS,
    _compute_stats,
    build_live_payload,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _trade(direction: int = 1, entry: float = 100.0,
           horizon: str = "swing", entry_date: str = "2026-06-01") -> dict:
    return {
        "id": 1, "symbol": "TEST", "horizon": horizon,
        "entry_date": entry_date, "entry_price": entry, "direction": direction,
        "target_price": entry * 1.05, "stop_price": entry * 0.97,
        "horizon_sessions": HORIZON_SESSIONS[horizon], "cost_bps": 15.0,
        "status": "OPEN", "exit_date": None, "exit_price": None,
        "realized_ret": None, "realized_ret_net": None,
        "strategy": "tier1_composite", "notes": None,
        "taken_at": None, "rank_pct": 0.9,
    }


def _candle(t: str, o: float, h: float, l: float, c: float, v: int = 0) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ──────────────────────────────────────────────────────────────────────
# _compute_stats — pure
# ──────────────────────────────────────────────────────────────────────

class TestComputeStatsLong:
    def test_rising_price_gives_positive_pnl_and_mfe(self):
        trade = _trade(direction=1, entry=100.0)
        candles = [
            _candle("2026-06-02", 100, 102, 99, 101),
            _candle("2026-06-03", 101, 105, 100, 104),
            _candle("2026-06-04", 104, 107, 103, 106),   # last close = 106
        ]
        stats = _compute_stats(trade, candles, benchmark_points=[])
        assert stats["current_price"] == 106.0
        # +6% from entry of 100.
        assert stats["current_pnl_pct"] == pytest.approx(0.06, abs=1e-9)
        # MFE = max high (107) - entry (100), MAE = min low (99) - entry.
        assert stats["mfe"] == pytest.approx(0.07, abs=1e-9)
        assert stats["mae"] == pytest.approx(-0.01, abs=1e-9)

    def test_falling_price_gives_negative_pnl(self):
        trade = _trade(direction=1, entry=100.0)
        candles = [
            _candle("2026-06-02", 100, 100, 95, 96),
        ]
        stats = _compute_stats(trade, candles, benchmark_points=[])
        assert stats["current_pnl_pct"] == pytest.approx(-0.04, abs=1e-9)
        assert stats["mae"] == pytest.approx(-0.05, abs=1e-9)


class TestComputeStatsShort:
    def test_short_with_falling_price_gives_positive_pnl(self):
        trade = _trade(direction=-1, entry=100.0)
        candles = [
            _candle("2026-06-02", 100, 100, 95, 96),
        ]
        stats = _compute_stats(trade, candles, benchmark_points=[])
        # Short profits as price drops: PnL = -1 * (96 - 100) / 100 = +4%.
        assert stats["current_pnl_pct"] == pytest.approx(0.04, abs=1e-9)
        # SHORT MFE = (entry - min low)/entry = (100 - 95)/100 = +5%.
        assert stats["mfe"] == pytest.approx(0.05, abs=1e-9)
        # SHORT MAE = (entry - max high)/entry = (100 - 100)/100 = 0%.
        assert stats["mae"] == pytest.approx(0.0, abs=1e-9)


class TestComputeStatsBenchmark:
    def test_benchmark_delta_computed_correctly(self):
        trade = _trade(direction=1, entry=100.0)
        candles = [
            _candle("2026-06-02", 100, 100, 99, 101),
            _candle("2026-06-03", 101, 103, 100, 102),
        ]
        # Benchmark moved +1% (100 -> 101)
        bm = [
            {"time": "2026-06-01", "value": 100.0},
            {"time": "2026-06-02", "value": 100.5},
            {"time": "2026-06-03", "value": 101.0},
        ]
        stats = _compute_stats(trade, candles, benchmark_points=bm)
        assert stats["benchmark_ret"] == pytest.approx(0.01, abs=1e-9)
        # Stock +2%, benchmark +1% -> delta = +1%.
        assert stats["delta_vs_benchmark"] == pytest.approx(0.01, abs=1e-9)


class TestComputeStatsEdgeCases:
    def test_empty_candles_returns_none_fields(self):
        trade = _trade()
        stats = _compute_stats(trade, candles=[], benchmark_points=[])
        assert stats["current_price"] is None
        assert stats["current_pnl_pct"] is None
        assert stats["mfe"] is None
        assert stats["sessions_elapsed"] == 0
        assert stats["sessions_remaining"] == trade["horizon_sessions"]

    def test_time_elapsed_counts_candles_after_entry(self):
        # Lead-in candles before entry should not count as "elapsed".
        trade = _trade(direction=1, entry=100.0, entry_date="2026-06-03")
        candles = [
            _candle("2026-06-01", 99, 100, 99, 100),   # lead-in
            _candle("2026-06-02", 100, 101, 99, 100),  # lead-in
            _candle("2026-06-04", 100, 102, 99, 101),  # day 1
            _candle("2026-06-05", 101, 103, 100, 102), # day 2
        ]
        stats = _compute_stats(trade, candles, benchmark_points=[])
        assert stats["sessions_elapsed"] == 2
        assert stats["sessions_remaining"] == trade["horizon_sessions"] - 2


# ──────────────────────────────────────────────────────────────────────
# build_live_payload — DB-mocked
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_loaders(monkeypatch):
    """Patch the four DB loaders so build_live_payload can be tested
    without a real database."""
    state = {
        "trade":     None,
        "candles":   [],
        "benchmark": ([], "NIFTY50_EQ"),
        "expected":  {"predicted_return": None, "confidence": None,
                      "ranking_score": None, "atr_pct": None, "top_factors": None},
    }
    monkeypatch.setattr(paper_live, "_load_trade",
                        lambda tid: state["trade"])
    monkeypatch.setattr(paper_live, "_load_stock_candles",
                        lambda **kw: state["candles"])
    monkeypatch.setattr(paper_live, "_load_benchmark_line",
                        lambda **kw: state["benchmark"])
    monkeypatch.setattr(paper_live, "_load_expected",
                        lambda trade: state["expected"])
    return state


class TestBuildLivePayload:
    def test_returns_none_when_trade_missing(self, mock_loaders):
        mock_loaders["trade"] = None
        assert build_live_payload(99999) is None

    def test_returns_assembled_payload_when_trade_present(self, mock_loaders):
        mock_loaders["trade"] = _trade(direction=1, entry=100.0)
        mock_loaders["candles"] = [
            _candle("2026-06-02", 100, 102, 99, 101),
            _candle("2026-06-03", 101, 105, 100, 104),
        ]
        mock_loaders["benchmark"] = (
            [{"time": "2026-06-01", "value": 100.0},
             {"time": "2026-06-03", "value": 101.5}],
            "NIFTY50_EQ",
        )
        mock_loaders["expected"] = {
            "predicted_return": 0.03, "confidence": 0.65,
            "ranking_score": 0.41, "atr_pct": 0.018,
            "top_factors": None,
        }
        payload = build_live_payload(1)
        assert payload is not None
        # Stable shape.
        assert set(payload.keys()) == {"trade", "stock_candles", "benchmark", "stats", "expected"}
        assert payload["trade"]["symbol"] == "TEST"
        assert payload["stats"]["current_price"] == 104.0
        # +4% stock, +1.5% benchmark -> delta = +2.5%.
        assert payload["stats"]["current_pnl_pct"] == pytest.approx(0.04, abs=1e-9)
        assert payload["stats"]["benchmark_ret"] == pytest.approx(0.015, abs=1e-9)
        assert payload["stats"]["delta_vs_benchmark"] == pytest.approx(0.025, abs=1e-9)
        assert payload["expected"]["predicted_return"] == 0.03


# ──────────────────────────────────────────────────────────────────────
# Module invariants
# ──────────────────────────────────────────────────────────────────────

class TestInvariants:
    def test_horizon_sessions_match_paper_journal(self):
        # If paper_journal._HORIZON_SESSIONS drifts from paper_live's,
        # the live tracker shows wrong "remaining sessions" countdowns.
        from qsde.execution.paper_journal import _HORIZON_SESSIONS
        assert HORIZON_SESSIONS == _HORIZON_SESSIONS
