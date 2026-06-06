"""Tests for qsde/research/rule_validation.py.

Tests the pure-math kernels with synthetic input. The DB-dependent loaders
(`_load_signals`, `_load_close_panel`) are tested via monkeypatch so the
suite stays hermetic.

Run:
    pytest backend/tests/test_rule_validation.py -v
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from qsde.research.rule_validation import (
    HORIZON_SESSIONS,
    LONG_DECILE,
    SHORT_DECILE,
    _attach_realized_returns,
    _cross_sectional_rank_by_date,
    compute_decile_spread_sharpe,
    compute_factor_hit_rates,
    compute_factor_ic_history,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers — synthetic data builders
# ──────────────────────────────────────────────────────────────────────

def _bdays(n: int, start: str = "2025-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


def _signals_frame(dates: pd.DatetimeIndex, symbols: list[str],
                   scores: dict[str, np.ndarray]) -> pd.DataFrame:
    """Build a long-form signals DataFrame: one row per (date, symbol).

    scores: per-symbol score series (length = len(dates)).
    """
    rows = []
    for sym in symbols:
        for i, d in enumerate(dates):
            rows.append({"date": d, "symbol": sym, "ranking_score": float(scores[sym][i])})
    return pd.DataFrame(rows)


def _close_panel(dates: pd.DatetimeIndex, symbols: list[str],
                 levels: dict[str, np.ndarray]) -> pd.DataFrame:
    """Build a close panel (rows = date, cols = symbol)."""
    return pd.DataFrame({sym: levels[sym] for sym in symbols}, index=dates)


# ──────────────────────────────────────────────────────────────────────
# Pure kernel tests
# ──────────────────────────────────────────────────────────────────────

class TestCrossSectionalRank:
    def test_monotone_ranks_correctly_per_date(self):
        idx = _bdays(2)
        # Date 1: A=1, B=2, C=3, D=4 -> ranks 0.25, 0.5, 0.75, 1.0
        # Date 2: A=4, B=3, C=2, D=1 -> ranks 1.0, 0.75, 0.5, 0.25
        scores = {"A": [1.0, 4.0], "B": [2.0, 3.0], "C": [3.0, 2.0], "D": [4.0, 1.0]}
        sigs = _signals_frame(idx, ["A", "B", "C", "D"],
                              {k: np.array(v) for k, v in scores.items()})
        ranked = _cross_sectional_rank_by_date(sigs)
        # Pull date 1's A rank.
        d1 = ranked[ranked["date"] == idx[0]].set_index("symbol")["rank_pct"]
        assert d1.loc["A"] == pytest.approx(0.25)
        assert d1.loc["D"] == pytest.approx(1.0)
        d2 = ranked[ranked["date"] == idx[1]].set_index("symbol")["rank_pct"]
        assert d2.loc["A"] == pytest.approx(1.0)
        assert d2.loc["D"] == pytest.approx(0.25)


class TestAttachRealizedReturns:
    def test_attaches_horizon_forward_return_net_of_cost(self):
        # 10 sessions, 2 symbols. Constant +1%/day on A, -1%/day on B.
        n = 30
        idx = _bdays(n)
        a_levels = 100 * (1.01 ** np.arange(n))
        b_levels = 100 * (0.99 ** np.arange(n))
        close = _close_panel(idx, ["A", "B"],
                             {"A": a_levels, "B": b_levels})
        sigs = pd.DataFrame({
            "date": [idx[0], idx[0]],
            "symbol": ["A", "B"],
            "ranking_score": [1.0, -1.0],
            "rank_pct": [1.0, 0.0],
        })
        resolved = _attach_realized_returns(sigs, close, horizon="swing")
        # swing = 5 sessions. A return = 1.01^5 - 1 = 5.1% gross, minus cost.
        # cost for swing = 15 bps default. So ~5.1% - 0.15% = ~5.0%.
        a_ret = resolved[resolved["symbol"] == "A"]["realized_ret_net"].iloc[0]
        b_ret = resolved[resolved["symbol"] == "B"]["realized_ret_net"].iloc[0]
        assert a_ret == pytest.approx((1.01 ** 5) - 1.0 - 15e-4, abs=1e-6)
        assert b_ret == pytest.approx((0.99 ** 5) - 1.0 - 15e-4, abs=1e-6)


# ──────────────────────────────────────────────────────────────────────
# DB-mocked integration tests
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db(monkeypatch):
    """Patch _load_signals and _load_close_panel so high-level functions
    don't need a real DB. Tests inject the synthetic data via the
    returned `state` dict."""
    state = {"signals": pd.DataFrame(), "close": pd.DataFrame()}

    def fake_load_signals(strategy, horizon, since):
        return state["signals"].copy()

    def fake_load_close_panel(symbols, since):
        return state["close"].copy()

    monkeypatch.setattr("qsde.research.rule_validation._load_signals", fake_load_signals)
    monkeypatch.setattr("qsde.research.rule_validation._load_close_panel", fake_load_close_panel)
    return state


def _build_perfect_factor_dataset(n_dates: int = 25, n_symbols: int = 20):
    """Synthetic dataset where higher rank_pct -> higher realized return.

    Returns (signals_df, close_panel).
    """
    idx = _bdays(n_dates + 10)  # extra room for horizon walk-forward
    symbols = [f"SYM{i:02d}" for i in range(n_symbols)]

    # Each symbol has a fixed "alpha" rank (SYM00 worst, SYM19 best).
    # Close path: SYM_i grows at i * 10 bps/day; gives high score = high return.
    levels = {}
    rng = np.random.default_rng(42)
    for i, sym in enumerate(symbols):
        drift = (i - n_symbols / 2) * 1e-3  # range -1% to +1% per day
        noise = rng.normal(0, 0.005, len(idx))   # small noise
        log_rets = drift + noise
        levels[sym] = 100.0 * np.exp(log_rets.cumsum())
    close = _close_panel(idx, symbols, levels)

    # Signal per (date, symbol): ranking_score = same drift (perfect predictor).
    score_dates = idx[:n_dates]
    rows = []
    for d in score_dates:
        for i, sym in enumerate(symbols):
            drift = (i - n_symbols / 2) * 1e-3
            rows.append({"date": d, "symbol": sym, "ranking_score": float(drift)})
    sigs = pd.DataFrame(rows)
    return sigs, close


class TestComputeFactorICHistory:
    def test_perfect_factor_gives_positive_ic(self, mock_db):
        sigs, close = _build_perfect_factor_dataset(n_dates=25, n_symbols=20)
        mock_db["signals"] = sigs
        mock_db["close"] = close
        # Use the latest signal date as "as_of" so the lookback covers everything.
        as_of = (sigs["date"].max() + pd.Timedelta(days=10)).date()
        hist = compute_factor_ic_history(
            "jt", "swing", lookback_days=365, as_of_date=as_of,
        )
        assert not hist.empty
        # IC should be strongly positive on a perfect-predictor synthetic.
        assert hist["ic"].mean() > 0.5

    def test_anti_predictive_factor_gives_negative_ic(self, mock_db):
        sigs, close = _build_perfect_factor_dataset(n_dates=25, n_symbols=20)
        # Flip the sign of all scores -> anti-predictive.
        sigs["ranking_score"] = -sigs["ranking_score"]
        mock_db["signals"] = sigs
        mock_db["close"] = close
        as_of = (sigs["date"].max() + pd.Timedelta(days=10)).date()
        hist = compute_factor_ic_history(
            "jt", "swing", lookback_days=365, as_of_date=as_of,
        )
        assert not hist.empty
        assert hist["ic"].mean() < -0.5

    def test_empty_signals_returns_empty(self, mock_db):
        mock_db["signals"] = pd.DataFrame()
        mock_db["close"] = pd.DataFrame()
        hist = compute_factor_ic_history("jt", "swing")
        assert hist.empty
        assert list(hist.columns) == ["date", "ic", "n_symbols"]

    def test_invalid_factor_raises(self, mock_db):
        with pytest.raises(ValueError):
            compute_factor_ic_history("nonsense", "swing")

    def test_invalid_horizon_raises(self, mock_db):
        with pytest.raises(ValueError):
            compute_factor_ic_history("jt", "intraday")


class TestComputeFactorHitRates:
    def test_perfect_factor_high_hit_rates(self, mock_db):
        sigs, close = _build_perfect_factor_dataset(n_dates=25, n_symbols=20)
        mock_db["signals"] = sigs
        mock_db["close"] = close
        as_of = (sigs["date"].max() + pd.Timedelta(days=10)).date()
        hit = compute_factor_hit_rates(
            "jt", "swing", lookback_days=365, as_of_date=as_of,
        )
        # Top decile of a perfect-predictor factor should win most of the time.
        assert hit["hit_rate_top"] > 0.7
        # Bottom decile should also be predictive of losses.
        assert hit["hit_rate_bot"] > 0.7
        assert hit["n_top"] > 0
        assert hit["n_bot"] > 0

    def test_empty_returns_nan(self, mock_db):
        mock_db["signals"] = pd.DataFrame()
        hit = compute_factor_hit_rates("jt", "swing")
        assert hit["n_top"] == 0
        assert hit["n_bot"] == 0
        assert pd.isna(hit["hit_rate_top"])
        assert pd.isna(hit["hit_rate_bot"])


class TestComputeDecileSpreadSharpe:
    def test_perfect_factor_positive_sharpe(self, mock_db):
        sigs, close = _build_perfect_factor_dataset(n_dates=25, n_symbols=20)
        mock_db["signals"] = sigs
        mock_db["close"] = close
        as_of = (sigs["date"].max() + pd.Timedelta(days=10)).date()
        sp = compute_decile_spread_sharpe(
            "jt", "swing", lookback_days=365, as_of_date=as_of,
        )
        # Annualized Sharpe of a perfect cross-sectional predictor should be > 1.
        assert sp["sharpe_ann"] > 1.0
        assert sp["n_observations"] >= 10

    def test_empty_returns_nan(self, mock_db):
        mock_db["signals"] = pd.DataFrame()
        sp = compute_decile_spread_sharpe("jt", "swing")
        assert sp["n_observations"] == 0
        assert pd.isna(sp["sharpe_ann"])


# ──────────────────────────────────────────────────────────────────────
# Module-level invariants
# ──────────────────────────────────────────────────────────────────────

class TestInvariants:
    def test_horizon_sessions_canonical(self):
        assert HORIZON_SESSIONS["swing"] == 5
        assert HORIZON_SESSIONS["long"] == 20

    def test_decile_thresholds_match_engine(self):
        # If these diverge from rule_engine.py the IC/composite is inconsistent.
        from qsde.research.rule_engine import LONG_DECILE as ENGINE_LONG
        from qsde.research.rule_engine import SHORT_DECILE as ENGINE_SHORT
        assert LONG_DECILE == ENGINE_LONG
        assert SHORT_DECILE == ENGINE_SHORT
