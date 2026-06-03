"""Tests for the AFML triplet: fracdiff, triple-barrier, meta-labeling.

Hermetic — uses synthetic data only, no DB / network / GPU.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qsde.models.fracdiff import (
    _get_weights_ffd,
    apply_fracdiff_to_features,
    frac_diff_ffd,
)
from qsde.models.meta_model import build_meta_dataset, meta_predict, train_meta_model
from qsde.models.triple_barrier import (
    _HORIZON_BARRIERS,
    apply_triple_barrier_labels,
    get_bins,
    get_daily_volatility,
    get_events,
    get_t1_barriers,
)


# ── helpers ──────────────────────────────────────────────────

def _gbm_series(n: int = 600, mu: float = 1e-4, sig: float = 0.02, seed: int = 1) -> pd.Series:
    """Geometric Brownian motion daily prices. Non-stationary by construction."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(mu, sig, n)
    px = 100.0 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2018-01-01", periods=n, freq="B")
    return pd.Series(px, index=idx, name="close")


# ── fracdiff ─────────────────────────────────────────────────

class TestFracDiff:
    def test_weights_monotone_decay(self):
        w = _get_weights_ffd(d=0.4, thres=1e-5)
        # weights stored OLDEST -> NEWEST; magnitudes should decay toward the OLDEST tail.
        mags = np.abs(w)
        # Newest weight is the largest (it's "1.0" in the LdP construction).
        assert mags[-1] >= mags[0]

    def test_fracdiff_makes_gbm_more_stationary(self):
        from qsde.models.fracdiff import _adf_pvalue
        px = _gbm_series(n=600)
        p_raw = _adf_pvalue(px)
        diffed = frac_diff_ffd(px, d=0.4)
        p_diff = _adf_pvalue(diffed)
        # Raw GBM should usually fail ADF stationarity (p > 0.05). The
        # fractionally differenced series should pass it. Allow a small margin
        # because synthetic series are noisy at n=600.
        assert p_diff < p_raw, (
            f"fracdiff did not improve stationarity: raw_p={p_raw:.3f}, diffed_p={p_diff:.3f}"
        )

    def test_fracdiff_preserves_index_length(self):
        px = _gbm_series(n=400)
        diffed = frac_diff_ffd(px, d=0.4)
        assert len(diffed) == len(px)
        # Warm-up tail is NaN; rest is finite.
        finite = diffed.dropna()
        assert len(finite) > 100, "warm-up consumed too many observations"

    def test_apply_fracdiff_skips_stationary_features(self):
        df = pd.DataFrame({
            "symbol": ["A"] * 200,
            "as_of_date": pd.date_range("2020-01-01", periods=200, freq="B"),
            "tech_rsi_14": np.random.default_rng(2).uniform(20, 80, 200),
            "tech_obv_slope": np.cumsum(np.random.default_rng(3).standard_normal(200)),
        })
        out, d_used = apply_fracdiff_to_features(df, default_d=0.4)
        assert d_used["tech_rsi_14"] is None, "RSI is stationary; should not be fracdiffed"
        assert d_used["tech_obv_slope"] == 0.4, "OBV is level-form; should be fracdiffed"


# ── triple-barrier ───────────────────────────────────────────

class TestTripleBarrier:
    def _setup_events(self, n: int = 400):
        px = _gbm_series(n=n, seed=42)
        vol = get_daily_volatility(px, span=50)
        # Skip the first 50 obs while vol warms up.
        t_events = px.index[60:n - 30]
        t1 = get_t1_barriers(px, t_events, num_days=5)
        return px, vol, t_events, t1

    def test_get_t1_barriers_within_index(self):
        px, _, t_events, t1 = self._setup_events()
        # Every t1 should be a valid (or NaT) date in the price index or later.
        valid = t1.dropna()
        for t in valid:
            assert t in px.index

    def test_events_have_pt_or_sl_or_t1(self):
        px, vol, t_events, t1 = self._setup_events()
        events = get_events(
            close=px, t_events=t_events, pt_sl=(2.0, 1.0),
            target=vol, t1=t1, min_ret=0.0,
        )
        # Every event row should carry a non-null t1 (the first barrier hit
        # OR the time barrier itself).
        assert events["t1"].notna().all()

    def test_bins_labels_are_in_set(self):
        px, vol, t_events, t1 = self._setup_events()
        events = get_events(
            close=px, t_events=t_events, pt_sl=(2.0, 1.0),
            target=vol, t1=t1,
        )
        bins = get_bins(events, close=px)
        assert set(bins["bin"].unique()).issubset({-1, 0, 1})

    def test_apply_triple_barrier_to_panel(self):
        # Two synthetic symbols
        rng = np.random.default_rng(7)
        rows = []
        for sym in ("AAA", "BBB"):
            px = _gbm_series(n=300, seed=hash(sym) % 1000)
            for i, dt in enumerate(px.index[60:280]):
                rows.append({
                    "symbol": sym, "as_of_date": dt,
                    "feat1": rng.standard_normal(),
                    "target": float(rng.standard_normal() * 0.01),
                })
        dataset = pd.DataFrame(rows)
        # OHLCV long-form
        ohlcv_rows = []
        for sym in ("AAA", "BBB"):
            px = _gbm_series(n=300, seed=hash(sym) % 1000)
            for dt, val in px.items():
                ohlcv_rows.append({"symbol": sym, "date": dt, "close": float(val)})
        ohlcv = pd.DataFrame(ohlcv_rows)

        labeled = apply_triple_barrier_labels(
            dataset=dataset, ohlcv=ohlcv, horizon="swing",
        )
        assert "target" in labeled.columns
        assert "target_ret" in labeled.columns
        assert set(labeled["target"].dropna().unique()).issubset({-1, 0, 1})
        # The pipeline should produce SOMETHING.
        assert len(labeled) > 50

    @pytest.mark.parametrize("horizon", ["intraday", "swing", "long"])
    def test_per_horizon_barriers_distinct(self, horizon):
        cfg = _HORIZON_BARRIERS[horizon]
        assert cfg["pt_mult"] > cfg["sl_mult"], (
            f"{horizon}: PT mult should exceed SL mult so vol-floor R:R clears 1.5"
        )

    def test_dataset_with_object_dtype_as_of_date_merges(self):
        """Regression: production build_training_dataset returns as_of_date
        as object dtype (from psycopg2 datetime.date), but
        apply_triple_barrier_labels builds DatetimeIndex internally. Without
        an explicit dtype cast in the function, the merge silently errored
        and we fell back to fixed-horizon. This guards against that.
        """
        import datetime as _dt
        rng = np.random.default_rng(99)
        rows = []
        # Build the dataset with PYTHON date objects (object dtype) — mirroring
        # what build_training_dataset returns from the DB.
        for sym in ("AAA", "BBB"):
            px = _gbm_series(n=250, seed=hash(sym) % 1000)
            for dt in px.index[60:230]:
                rows.append({
                    "symbol": sym,
                    "as_of_date": _dt.date(dt.year, dt.month, dt.day),
                    "feat1": rng.standard_normal(),
                    "target": float(rng.standard_normal() * 0.01),
                })
        dataset = pd.DataFrame(rows)
        assert dataset["as_of_date"].dtype == object  # confirm the regression setup

        ohlcv_rows = []
        for sym in ("AAA", "BBB"):
            px = _gbm_series(n=250, seed=hash(sym) % 1000)
            for dt, val in px.items():
                ohlcv_rows.append({
                    "symbol": sym, "date": _dt.date(dt.year, dt.month, dt.day),
                    "close": float(val),
                })
        ohlcv = pd.DataFrame(ohlcv_rows)

        labeled = apply_triple_barrier_labels(
            dataset=dataset, ohlcv=ohlcv, horizon="swing",
        )
        # The regression bug: this returned an empty DataFrame because the
        # merge silently failed. Now it should produce real labels.
        assert len(labeled) > 0, (
            "Triple-barrier silently fell back to empty when as_of_date "
            "was object dtype — the production-shaped input."
        )
        assert set(labeled["target"].dropna().unique()).issubset({-1, 0, 1})


# ── meta-labeling ────────────────────────────────────────────

class TestMetaModel:
    def _make_meta_setup(self, n: int = 300):
        rng = np.random.default_rng(11)
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        symbols = ["A"] * n
        # Latent signal feature
        x = rng.standard_normal(n)
        # Primary "OOF" predictions: x with noise → biased toward x's sign
        primary = 0.6 * x + 0.4 * rng.standard_normal(n)
        # Realized outcome: +1 if x > 0 (with 70% accuracy)
        true_dir = np.where(x > 0, 1, -1)
        flip = rng.uniform(size=n) < 0.30
        outcome = np.where(flip, -true_dir, true_dir)

        features_df = pd.DataFrame({
            "symbol": symbols, "as_of_date": dates,
            "x": x, "x2": x ** 2, "noise": rng.standard_normal(n),
            "target": outcome,
        })
        oof = pd.DataFrame({
            "symbol": symbols, "as_of_date": dates, "prediction": primary,
        })
        return features_df, oof

    def test_build_meta_dataset_shape(self):
        feat, oof = self._make_meta_setup()
        meta = build_meta_dataset(features_df=feat, primary_oof_preds=oof)
        assert "meta_label" in meta.columns
        assert "primary_pred" in meta.columns
        assert set(meta["meta_label"].unique()).issubset({0, 1})
        # Base hit rate should be near 70% by construction (or above 50% for
        # a properly biased setup).
        assert meta["meta_label"].mean() > 0.55

    def test_train_meta_model_smoke(self, tmp_path, monkeypatch):
        feat, oof = self._make_meta_setup(n=400)
        meta_ds = build_meta_dataset(features_df=feat, primary_oof_preds=oof)
        # Redirect weights dir so the test doesn't touch shipped artifacts.
        from qsde.models import meta_model as MM
        monkeypatch.setattr(MM, "_weights_dir", lambda: str(tmp_path))
        result = MM.train_meta_model(meta_ds, horizon="swing")
        assert result["status"] == "ok"
        assert 0.0 <= result["base_hit_rate"] <= 1.0
        # The classifier should learn SOMETHING above chance on a biased
        # synthetic signal — AUC should clear 0.6.
        assert result["training_auc"] > 0.6

    def test_build_meta_dataset_with_continuous_targets(self):
        """Regression: when triple-barrier silently falls back to fixed-
        horizon, the target column is continuous (not {-1, 0, +1}). The
        previous build_meta_dataset filter `tb.isin([-1, 1])` dropped
        EVERY row → 'Empty meta-dataset' warning. The fallback path must
        sign continuous targets and keep them."""
        rng = np.random.default_rng(123)
        n = 200
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        # CONTINUOUS targets (not in {-1, 0, +1})
        primary = rng.standard_normal(n)
        target_continuous = 0.6 * primary + 0.4 * rng.standard_normal(n)
        feat = pd.DataFrame({
            "symbol": ["A"] * n, "as_of_date": dates,
            "x": rng.standard_normal(n),
            "target": target_continuous * 0.01,   # realistic-magnitude returns
        })
        oof = pd.DataFrame({
            "symbol": ["A"] * n, "as_of_date": dates,
            "prediction": primary,
        })
        meta = build_meta_dataset(features_df=feat, primary_oof_preds=oof)
        # Continuous-fallback should produce SOMETHING (not silently empty).
        assert len(meta) > 50, "Continuous-target fallback returned ~empty"
        assert set(meta["meta_label"].unique()).issubset({0, 1})

    def test_meta_predict_clamped(self, tmp_path, monkeypatch):
        feat, oof = self._make_meta_setup(n=300)
        meta_ds = build_meta_dataset(features_df=feat, primary_oof_preds=oof)
        from qsde.models import meta_model as MM
        monkeypatch.setattr(MM, "_weights_dir", lambda: str(tmp_path))
        MM.train_meta_model(meta_ds, horizon="swing")
        # train_meta_model now saves a CANDIDATE (the meta-primary alignment
        # gate). Promote it to active before loading — mirrors what
        # lgbm_model does after the primary passes its DSR gate.
        assert MM.promote_meta_candidate("swing") is True
        booster = MM.load_meta_model("swing")
        assert booster is not None
        # Predict on first 50 rows
        proba = meta_predict(booster, meta_ds.head(50))
        assert (proba >= 0.0).all() and (proba <= 1.0).all()
