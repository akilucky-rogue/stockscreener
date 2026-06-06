"""Tests for qsde/factors/rules.py — Tier 1 rule-based factor primitives.

Hermetic. No DB, no network, no fixtures from outside this file. Every
synthetic series has a hand-derivable expected answer so a regression
in any factor is caught locally.

Run:
    pytest backend/tests/test_rules.py -v
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from qsde.factors.rules import (
    BAB_BETA_WINDOW,
    CONNORS_RSI_PERIOD,
    CONNORS_TREND_FILTER,
    FACTOR_NAMES,
    HORIZON_FACTORS,
    JT_FORMATION_DAYS,
    JT_SKIP_DAYS,
    MOP_LOOKBACK_DAYS,
    bab_score,
    composite_rank_ic_weighted,
    connors_rsi2_score,
    cross_sectional_rank,
    jegadeesh_titman_score,
    mop_tsmom_score,
    rolling_beta,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _bdays(n: int, start: str = "2020-01-01") -> pd.DatetimeIndex:
    """Generate n business days starting from `start`."""
    return pd.bdate_range(start=start, periods=n)


def _geom_series(n: int, daily_return: float, start: float = 100.0) -> pd.Series:
    """Series with constant daily geometric growth — clean signal for tests."""
    idx = _bdays(n)
    levels = start * (1.0 + daily_return) ** np.arange(n)
    return pd.Series(levels, index=idx, name="close")


# ──────────────────────────────────────────────────────────────────────
# 1. Jegadeesh-Titman tests
# ──────────────────────────────────────────────────────────────────────

class TestJegadeeshTitman:
    def test_uptrend_positive_score(self):
        # +0.1%/day for 300 days. After warmup, JT score should be positive
        # and roughly equal to (1.001)^(formation-skip) - 1.
        close = _geom_series(300, 0.001)
        score = jegadeesh_titman_score(close)
        latest = score.iloc[-1]
        # Expected return over (formation - skip) = (252 - 21) = 231 days.
        expected = (1.001 ** 231) - 1.0
        assert latest > 0
        assert math.isclose(latest, expected, rel_tol=1e-6)

    def test_downtrend_negative_score(self):
        close = _geom_series(300, -0.001)
        score = jegadeesh_titman_score(close)
        assert score.iloc[-1] < 0

    def test_warmup_returns_nan(self):
        # First `formation` rows should be NaN.
        close = _geom_series(300, 0.001)
        score = jegadeesh_titman_score(close)
        # The 252nd row (index 251) is still within warmup; first valid is 252.
        assert score.iloc[JT_FORMATION_DAYS - 1] != score.iloc[JT_FORMATION_DAYS - 1]  # NaN check
        # First defined index where both shift(skip) and shift(formation) are valid.
        first_valid = JT_FORMATION_DAYS
        assert not pd.isna(score.iloc[first_valid])

    def test_short_history_all_nan(self):
        close = _geom_series(100, 0.001)  # < formation + 1
        score = jegadeesh_titman_score(close)
        assert score.isna().all()

    def test_skip_must_be_less_than_formation(self):
        close = _geom_series(300, 0.001)
        with pytest.raises(ValueError):
            jegadeesh_titman_score(close, formation=20, skip=20)

    def test_typeerror_on_non_series(self):
        with pytest.raises(TypeError):
            jegadeesh_titman_score([100, 101, 102])  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# 2. MOP time-series momentum tests
# ──────────────────────────────────────────────────────────────────────

class TestMopTsmom:
    def test_uptrend_positive_score(self):
        close = _geom_series(400, 0.001)
        score = mop_tsmom_score(close)
        assert score.iloc[-1] > 0

    def test_downtrend_negative_score(self):
        close = _geom_series(400, -0.001)
        score = mop_tsmom_score(close)
        assert score.iloc[-1] < 0

    def test_flat_series_score_is_nan(self):
        # A perfectly flat series has zero return AND zero vol; div by zero
        # is replaced with NaN, so the score should be NaN at the tail.
        close = pd.Series(100.0, index=_bdays(400))
        score = mop_tsmom_score(close)
        assert pd.isna(score.iloc[-1])

    def test_vol_scaling_reduces_magnitude_of_noisy_uptrend(self):
        # Noisy uptrend = same drift but higher vol = lower abs score.
        rng = np.random.default_rng(42)
        idx = _bdays(400)
        # Same compound drift but with daily noise.
        steady = pd.Series(100.0 * (1.001 ** np.arange(400)), index=idx)
        noise = rng.normal(0, 0.02, 400)  # 2% daily noise
        noisy_levels = steady.values * np.exp(noise.cumsum() - noise.cumsum())  # zero-mean noise
        # Actually inject noise into log returns:
        log_rets = np.log(1.001) + rng.normal(0, 0.02, 400)
        noisy_levels = 100.0 * np.exp(log_rets.cumsum())
        noisy = pd.Series(noisy_levels, index=idx)
        s_steady = mop_tsmom_score(steady).iloc[-1]
        s_noisy = mop_tsmom_score(noisy).iloc[-1]
        # Steady score should be larger in absolute terms than noisy score.
        assert abs(s_steady) > abs(s_noisy)

    def test_short_history_all_nan(self):
        close = _geom_series(100, 0.001)
        score = mop_tsmom_score(close)
        assert score.isna().all()


# ──────────────────────────────────────────────────────────────────────
# 3. BAB tests
# ──────────────────────────────────────────────────────────────────────

class TestRollingBeta:
    def test_beta_one_when_stock_mirrors_market(self):
        rng = np.random.default_rng(0)
        idx = _bdays(BAB_BETA_WINDOW + 50)
        mkt_ret = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
        stock_ret = mkt_ret.copy()  # perfect mirror
        beta = rolling_beta(stock_ret, mkt_ret)
        assert math.isclose(beta.iloc[-1], 1.0, rel_tol=1e-9)

    def test_beta_two_when_stock_doubles_market(self):
        rng = np.random.default_rng(1)
        idx = _bdays(BAB_BETA_WINDOW + 50)
        mkt_ret = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
        stock_ret = 2.0 * mkt_ret
        beta = rolling_beta(stock_ret, mkt_ret)
        assert math.isclose(beta.iloc[-1], 2.0, rel_tol=1e-9)

    def test_beta_zero_when_stock_uncorrelated(self):
        rng = np.random.default_rng(2)
        idx = _bdays(BAB_BETA_WINDOW + 50)
        mkt_ret = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
        # Use a different RNG draw so they're independent.
        stock_ret = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
        beta = rolling_beta(stock_ret, mkt_ret)
        # With 252 samples, uncorrelated betas concentrate near 0; allow ±0.15.
        assert abs(beta.iloc[-1]) < 0.15


class TestBab:
    def test_low_beta_higher_score_than_high_beta(self):
        # Two synthetic stocks; one with beta=0.5, one with beta=1.5,
        # against the same market series.
        rng = np.random.default_rng(3)
        idx = _bdays(BAB_BETA_WINDOW + 100)
        mkt_ret = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
        mkt_close = (1.0 + mkt_ret).cumprod() * 100.0
        low_beta_ret = 0.5 * mkt_ret
        hi_beta_ret = 1.5 * mkt_ret
        low_close = (1.0 + low_beta_ret).cumprod() * 100.0
        hi_close = (1.0 + hi_beta_ret).cumprod() * 100.0
        low_s = bab_score(low_close, mkt_close).iloc[-1]
        hi_s = bab_score(hi_close, mkt_close).iloc[-1]
        # BAB favors low beta -> low_s > hi_s.
        assert low_s > hi_s
        # Sanity: low beta ~0.5 -> score ~ -0.5; high beta ~1.5 -> score ~ -1.5.
        assert math.isclose(low_s, -0.5, abs_tol=0.05)
        assert math.isclose(hi_s, -1.5, abs_tol=0.05)

    def test_short_history_all_nan(self):
        close = _geom_series(100, 0.001)
        mkt = _geom_series(100, 0.0005)
        score = bab_score(close, mkt)
        assert score.isna().all()


# ──────────────────────────────────────────────────────────────────────
# 4. Connors-Alvarez RSI(2) tests
# ──────────────────────────────────────────────────────────────────────

class TestConnorsRsi2:
    def test_oversold_in_uptrend_gives_positive_score(self):
        # Long uptrend, then a 2-day sharp drop. Above SMA(200) AND
        # RSI(2) drops -> positive (buy-the-dip) score.
        n = CONNORS_TREND_FILTER + 50
        idx = _bdays(n)
        # Strong uptrend, then 2 down days at the end.
        levels = np.linspace(100.0, 200.0, n)
        levels[-2:] = [195.0, 188.0]   # sharp drop, still above SMA(200)
        close = pd.Series(levels, index=idx)
        score = connors_rsi2_score(close)
        assert score.iloc[-1] > 0

    def test_overbought_in_downtrend_gives_negative_score(self):
        n = CONNORS_TREND_FILTER + 50
        idx = _bdays(n)
        # Strong downtrend, then 2 up days at the end.
        levels = np.linspace(200.0, 100.0, n)
        levels[-2:] = [105.0, 112.0]   # sharp rally, still below SMA(200)
        close = pd.Series(levels, index=idx)
        score = connors_rsi2_score(close)
        assert score.iloc[-1] < 0

    def test_in_uptrend_but_not_oversold_score_near_zero(self):
        # Steady uptrend with no recent dip -> RSI(2) is high, not oversold.
        # Score (long_signal = (10 - rsi)/10) goes negative.
        n = CONNORS_TREND_FILTER + 50
        idx = _bdays(n)
        levels = np.linspace(100.0, 200.0, n)  # monotone up
        close = pd.Series(levels, index=idx)
        score = connors_rsi2_score(close)
        # RSI(2) on monotone uptrend = 100; long_signal = (10-100)/10 = -9.
        # This is by design — the buy-the-dip signal is OFF when no dip.
        assert score.iloc[-1] < 0

    def test_short_history_all_nan(self):
        close = _geom_series(100, 0.001)  # < trend_period + rsi_period
        score = connors_rsi2_score(close)
        assert score.isna().all()


# ──────────────────────────────────────────────────────────────────────
# Composite & cross-sectional rank tests
# ──────────────────────────────────────────────────────────────────────

class TestCrossSectionalRank:
    def test_monotone_row_ranks_correctly(self):
        df = pd.DataFrame({
            "A": [1.0, 5.0],
            "B": [2.0, 3.0],
            "C": [3.0, 1.0],
            "D": [4.0, 4.0],
        }, index=pd.date_range("2026-01-01", periods=2))
        ranked = cross_sectional_rank(df)
        # Row 1: ranks 1,2,3,4 -> pct 0.25,0.5,0.75,1.0 -> -0.25, 0, 0.25, 0.5.
        assert math.isclose(ranked.iloc[0]["A"], -0.25, abs_tol=1e-9)
        assert math.isclose(ranked.iloc[0]["D"], 0.50, abs_tol=1e-9)
        # Row 2: C=1, B=3, D=4, A=5 -> ranks 1,2,3,4 -> 0.25,0.5,0.75,1.0 -> shifted.
        assert ranked.iloc[1]["C"] == ranked.iloc[0]["A"]   # both lowest in row
        assert ranked.iloc[1]["A"] == ranked.iloc[0]["D"]   # both highest in row


class TestComposite:
    def _make_two_factor_frames(self):
        idx = pd.date_range("2026-01-01", periods=3)
        cols = ["A", "B", "C"]
        f1 = pd.DataFrame(
            [[-0.5, 0.0, 0.5], [-0.5, 0.0, 0.5], [-0.5, 0.0, 0.5]],
            index=idx, columns=cols,
        )
        # f2 is the opposite ranking.
        f2 = pd.DataFrame(
            [[0.5, 0.0, -0.5], [0.5, 0.0, -0.5], [0.5, 0.0, -0.5]],
            index=idx, columns=cols,
        )
        return f1, f2

    def test_equal_weight_cancels_opposite_ranks(self):
        f1, f2 = self._make_two_factor_frames()
        composite = composite_rank_ic_weighted(
            {"f1": f1, "f2": f2},
            ic_weights={"f1": 0.0, "f2": 0.0},   # both zero -> equal weight
        )
        # Equal-weight of opposite ranks -> all zeros.
        assert (composite.abs() < 1e-12).all().all()

    def test_higher_ic_dominates(self):
        f1, f2 = self._make_two_factor_frames()
        composite = composite_rank_ic_weighted(
            {"f1": f1, "f2": f2},
            ic_weights={"f1": 0.6, "f2": 0.2},
        )
        # f1 weight = 0.75, f2 weight = 0.25.
        # A: 0.75*-0.5 + 0.25*0.5 = -0.25
        # C: 0.75*0.5 + 0.25*-0.5 = 0.25
        assert math.isclose(composite.iloc[0]["A"], -0.25, abs_tol=1e-9)
        assert math.isclose(composite.iloc[0]["C"], 0.25, abs_tol=1e-9)

    def test_negative_ic_zeroed_not_shorted(self):
        f1, f2 = self._make_two_factor_frames()
        composite = composite_rank_ic_weighted(
            {"f1": f1, "f2": f2},
            ic_weights={"f1": 0.5, "f2": -0.5},   # f2 negative -> dropped
        )
        # f2 weight = 0, so composite == f1.
        pd.testing.assert_frame_equal(composite, f1)

    def test_empty_factors_raises(self):
        with pytest.raises(ValueError):
            composite_rank_ic_weighted({}, ic_weights={})


# ──────────────────────────────────────────────────────────────────────
# Module-level invariants
# ──────────────────────────────────────────────────────────────────────

class TestModuleInvariants:
    def test_factor_names_canonical(self):
        assert FACTOR_NAMES == ("jt", "mop", "bab", "rsi2")

    def test_horizon_factors_intraday_empty(self):
        # Tier 1 deliberately does NOT trade intraday — would need
        # microstructure factors, not daily bars.
        assert HORIZON_FACTORS["intraday"] == ()
        assert set(HORIZON_FACTORS["swing"]) <= set(FACTOR_NAMES)
        assert set(HORIZON_FACTORS["long"]) <= set(FACTOR_NAMES)
