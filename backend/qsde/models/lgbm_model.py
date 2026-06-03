"""
LightGBM model training with purged k-fold cross-validation.

Replaces the previous vanilla 80/20 time-series split, which leaks
forward-return labels across the train/test boundary and inflates DSR.
See qsde/models/purged_cv.py for the López de Prado purged k-fold
algorithm with embargo.

The final "production" model is retrained on the full dataset AFTER the
CV evaluation. The CV scores (Sharpe, DSR, IC) are computed on the
out-of-sample predictions accumulated across all folds -- this is what
gets reported to the model_runs table and the user.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Literal

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew, spearmanr

from qsde.db.connection import execute_sql
from qsde.models.deflated_sharpe import deflated_sharpe_ratio, should_promote
from qsde.models.meta_model import (
    build_meta_dataset,
    promote_meta_candidate,
    train_meta_model,
)
from qsde.models.purged_cv import horizon_to_days, purged_kfold_indices

log = logging.getLogger(__name__)


LGBM_PARAMS = {
    "objective":        "regression",
    "boosting_type":    "dart",
    "metric":           "rmse",
    "learning_rate":    0.05,
    "num_leaves":       31,
    "max_depth":        5,
    "feature_fraction": 0.8,
    "verbose":          -1,
    "random_state":     42,
}
N_BOOST_ROUNDS = 100
N_CV_SPLITS = 5

# Embargo per horizon. The embargo must cover the autocorrelation horizon
# of the LABEL, not a one-size-fits-all 5d. For a 1-day target, only the
# trade-day-itself need be embargoed; for 20-day targets, anything within
# 5d of the fold boundary leaks. This is the dominant fix for the
# previously-inflated intraday DSR (~0.999): with 5d embargo and 1d label,
# train/test factor windows still overlapped through factor autocorrelation.
EMBARGO_BY_HORIZON: dict[str, int] = {
    "intraday": 1,   # 1d label -> 1d embargo
    "swing":    5,
    "long":     5,
}
# Backwards-compat default for any code path that imports the constant.
EMBARGO_DAYS = 5

# n_trials in DSR is the number of independent strategy candidates evaluated
# during selection. We currently train ONE model with one fixed param set,
# so n_trials = 1 (DSR collapses to PSR). When Optuna hyperparam search is
# added, bump this to the actual trial count.
DSR_N_TRIALS = 1

EXCLUDE_COLS = {
    "symbol", "as_of_date", "target", "target_ret", "yf_symbol", "source",
    "label_end_date",
}


def _train_one_fold(train, test, features):
    """Train LightGBM on `train`, return predictions for `test`."""
    X_train, y_train = train[features], train["target"]
    X_test, y_test = test[features], test["target"]

    train_data = lgb.Dataset(X_train, label=y_train)
    valid_data = lgb.Dataset(X_test, label=y_test, reference=train_data)
    model = lgb.train(
        LGBM_PARAMS,
        train_data,
        num_boost_round=N_BOOST_ROUNDS,
        valid_sets=[valid_data],
    )
    return model.predict(X_test)


def _evaluate_oos(test_predictions, horizon_days):
    """
    Compute IC, top-vs-bottom strategy Sharpe, and DSR from accumulated
    out-of-sample predictions.

    Sharpe is computed on NON-OVERLAPPING horizon-day returns. The strategy
    holds positions for `horizon_days`; daily forward-return aggregation
    therefore produces overlapping windows where adjacent observations
    share horizon_days - 1 days of return. Empirical std of that series
    equals sigma of an N-day return -- but annualizing by sqrt(252) treats
    it as daily, over-annualizing by sqrt(N). Fix: subsample every Nth
    observation and annualize by sqrt(252 / horizon_days).

    Return-column convention:
      * When triple-barrier labels are in use, `target` is in {-1, 0, +1}
        (a category vote) and `target_ret` is the realized return at
        barrier hit (continuous). Sharpe MUST be computed on target_ret,
        otherwise we're computing the Sharpe of a vote ratio and the
        number is meaningless as a tradable metric.
      * When fixed-horizon labels are in use, `target` IS the realized
        return, and target_ret is absent. Fall back to `target` in that
        case.

    IC is always Spearman(prediction, target) — that's the rank-correlation
    of the model's score against the label, which is what we want regardless
    of which label convention is in play.

    `test_predictions` has columns: symbol, as_of_date, target, prediction,
    and optionally target_ret.
    """
    ic, _ = spearmanr(test_predictions["prediction"], test_predictions["target"])
    if np.isnan(ic):
        ic = 0.0

    # Pick the right return series for the L/S strategy Sharpe.
    return_col = "target_ret" if "target_ret" in test_predictions.columns else "target"

    # Long top-decile / short bottom-decile spread, per day.
    test_predictions = test_predictions.copy()
    test_predictions["rank"] = test_predictions.groupby("as_of_date")["prediction"].rank(pct=True)
    test_predictions["strategy_ret"] = 0.0
    test_predictions.loc[test_predictions["rank"] > 0.9, "strategy_ret"] = test_predictions[return_col]
    test_predictions.loc[test_predictions["rank"] < 0.1, "strategy_ret"] = -test_predictions[return_col]

    daily_overlap = test_predictions.groupby("as_of_date")["strategy_ret"].mean().sort_index()

    # Non-overlapping subsample: every horizon_days-th day.
    nonoverlap = daily_overlap.iloc[::horizon_days]

    mean_ret = float(nonoverlap.mean())
    std_ret = float(nonoverlap.std())
    n_periods = len(nonoverlap)

    if std_ret > 0 and n_periods >= 30:
        per_period_sr = mean_ret / std_ret
        # Strategy rebalances every horizon_days, so periods_per_year = 252/horizon_days.
        periods_per_year = 252.0 / horizon_days
        ann_sr = per_period_sr * float(np.sqrt(periods_per_year))
        sk = float(skew(nonoverlap))
        ku = float(kurtosis(nonoverlap, fisher=False))
        dsr = float(deflated_sharpe_ratio(
            observed_sharpe=ann_sr,
            n_trials=DSR_N_TRIALS,
            n_obs=n_periods,
            skew=sk,
            kurtosis=ku,
        ))
    else:
        ann_sr = 0.0
        dsr = 0.0
        sk = 0.0
        ku = 3.0

    return {
        "ic":               ic,
        "ann_sharpe":       ann_sr,
        "dsr":              dsr,
        "skew":             sk,
        "kurtosis":         ku,
        "n_obs":            n_periods,
        "n_obs_overlap":    len(daily_overlap),
    }


def _avg_turnover(picks_by_date: "pd.Series") -> float:
    """Mean per-period fraction of basket names that change.

    picks_by_date: Series indexed by as_of_date with set-of-symbols values.
    Returns the average of |new_today \\ yesterday| / |today| across the
    available period transitions, in [0, 1].
    """
    dates = sorted(picks_by_date.index.tolist())
    if len(dates) < 2:
        return 1.0
    tos: list[float] = []
    for i in range(1, len(dates)):
        prev = picks_by_date.loc[dates[i - 1]]
        curr = picks_by_date.loc[dates[i]]
        if not prev or not curr:
            continue
        new = len(curr - prev)
        tos.append(new / max(1, len(curr)))
    return float(np.mean(tos)) if tos else 1.0


def _evaluate_oos_with_costs(
    test_predictions: "pd.DataFrame",
    horizon_days: int,
    cost_bps_round_trip: float = 15.0,
) -> dict:
    """Same L/S spread as _evaluate_oos, but applies realistic costs.

    Cost model:
      * The portfolio is 100 % long top-decile + 100 % short bottom-decile
        → 200 % notional.
      * Each "rebalance" (every horizon_days sessions), some fraction of
        the basket turns over. Realized turnover is computed from the OOF
        predictions themselves — we don't assume 100 % (which would be
        the worst case and would crush every short-horizon strategy).
      * Round-trip cost per name = `cost_bps_round_trip` (default 15 bps,
        the midpoint of QSDE's 5–8 bps large-cap / 12–20 bps mid-cap band,
        documented in CLAUDE.md).
      * Cost per period = avg_turnover × 200 % notional × cost_bps / 10000.

    Returns gross AND net Sharpe / DSR plus the turnover assumptions used,
    so the caller can show a sensitivity table.
    """
    return_col = "target_ret" if "target_ret" in test_predictions.columns else "target"

    tp = test_predictions.copy()
    tp["rank"] = tp.groupby("as_of_date")["prediction"].rank(pct=True)
    tp["strategy_ret"] = 0.0
    tp.loc[tp["rank"] > 0.9, "strategy_ret"] = tp[return_col]
    tp.loc[tp["rank"] < 0.1, "strategy_ret"] = -tp[return_col]

    daily_overlap = tp.groupby("as_of_date")["strategy_ret"].mean().sort_index()
    nonoverlap = daily_overlap.iloc[::horizon_days]

    # Realized turnover on each leg, computed from the SAME OOF preds.
    long_basket = tp[tp["rank"] > 0.9].groupby("as_of_date")["symbol"].apply(set)
    short_basket = tp[tp["rank"] < 0.1].groupby("as_of_date")["symbol"].apply(set)
    to_long = _avg_turnover(long_basket)
    to_short = _avg_turnover(short_basket)
    avg_turnover = (to_long + to_short) / 2.0

    # Cost per period in the same units as strategy_ret (fractions).
    # 2.0 = leg count (long + short), each carrying 100 % notional.
    cost_per_period = avg_turnover * 2.0 * cost_bps_round_trip / 10000.0
    net_periodic = nonoverlap - cost_per_period

    def _annualize(series: "pd.Series") -> dict:
        mean = float(series.mean())
        std = float(series.std())
        n = len(series)
        if std > 0 and n >= 30:
            per_p_sr = mean / std
            ann = per_p_sr * float(np.sqrt(252.0 / horizon_days))
            sk = float(skew(series))
            ku = float(kurtosis(series, fisher=False))
            dsr = float(deflated_sharpe_ratio(
                observed_sharpe=ann, n_trials=DSR_N_TRIALS,
                n_obs=n, skew=sk, kurtosis=ku,
            ))
            return {"sharpe": ann, "dsr": dsr, "skew": sk, "kurt": ku, "n": n}
        return {"sharpe": 0.0, "dsr": 0.0, "skew": 0.0, "kurt": 3.0, "n": n}

    gross = _annualize(nonoverlap)
    net = _annualize(net_periodic)

    return {
        "cost_bps_round_trip":   cost_bps_round_trip,
        "turnover_long":         to_long,
        "turnover_short":        to_short,
        "avg_turnover":          avg_turnover,
        "cost_per_period_frac":  cost_per_period,
        "ann_sharpe_gross":      gross["sharpe"],
        "ann_sharpe_net":        net["sharpe"],
        "dsr_gross":             gross["dsr"],
        "dsr_net":               net["dsr"],
        "n_periods":             gross["n"],
        "skew_gross":            gross["skew"],
        "kurt_gross":            gross["kurt"],
        "skew_net":              net["skew"],
        "kurt_net":              net["kurt"],
    }


def train_lightgbm_model(dataset, horizon="swing"):
    """Train a LightGBM DART regressor with purged k-fold CV.

    Steps:
        1. For each of N_CV_SPLITS folds, train on the purged training set
           and predict on the test fold. Accumulate predictions.
        2. Compute IC, annualized Sharpe, and DSR on the full out-of-sample
           prediction set.
        3. Retrain a production model on ALL data; save to disk for live
           signal generation.

    Returns the production model.
    """
    if dataset.empty:
        log.warning("Empty dataset provided for horizon " + str(horizon))
        return None

    dataset = dataset.sort_values("as_of_date").reset_index(drop=True)
    dataset["as_of_date"] = pd.to_datetime(dataset["as_of_date"])

    features = [c for c in dataset.columns if c not in EXCLUDE_COLS]

    # Build label_end_date for purging: as_of_date + horizon days.
    # Approximate trading days with calendar days * 1.5 -- conservatively
    # over-estimates the purge window, which is safer than under-estimating.
    horizon_days = horizon_to_days(horizon)
    label_end_offset = pd.Timedelta(days=int(round(horizon_days * 1.5)))
    dataset["label_end_date"] = dataset["as_of_date"] + label_end_offset

    # Per-horizon embargo. Falls back to the legacy 5d constant for unknown
    # horizons so adding a new one without registering it doesn't crash.
    embargo = EMBARGO_BY_HORIZON.get(horizon, EMBARGO_DAYS)

    log.info(
        "Training LightGBM " + horizon + " model with purged "
        + str(N_CV_SPLITS) + "-fold CV (embargo=" + str(embargo)
        + "d) on " + format(len(dataset), ",") + " samples, "
        + str(len(features)) + " features..."
    )

    # 1. Purged k-fold CV
    oos_chunks = []
    splits = purged_kfold_indices(
        dates=dataset["as_of_date"],
        label_end_dates=dataset["label_end_date"],
        n_splits=N_CV_SPLITS,
        embargo_days=embargo,
    )
    for fold_i, (train_idx, test_idx) in enumerate(splits):
        train = dataset.iloc[train_idx]
        test = dataset.iloc[test_idx]
        if len(train) < 1000 or len(test) < 100:
            log.warning(
                "  Fold " + str(fold_i + 1) + ": skipped, too few samples "
                "after purge (train=" + str(len(train))
                + ", test=" + str(len(test)) + ")"
            )
            continue

        preds = _train_one_fold(train, test, features)
        # Carry target_ret through to the OOF set if the dataset was labeled
        # with triple-barrier (target_ret = realized return at barrier hit).
        # _evaluate_oos uses it instead of the categorical `target` to compute
        # tradable Sharpe; without this, Sharpe is the Sharpe of a vote.
        keep_cols = ["symbol", "as_of_date", "target"]
        if "target_ret" in test.columns:
            keep_cols.append("target_ret")
        chunk = test[keep_cols].copy()
        chunk["prediction"] = preds
        oos_chunks.append(chunk)
        log.info(
            "  Fold " + str(fold_i + 1) + "/" + str(N_CV_SPLITS)
            + ": trained on " + format(len(train), ",")
            + ", tested on " + format(len(test), ",")
        )

    if not oos_chunks:
        log.error("All folds skipped; cannot evaluate model.")
        return None

    oos = pd.concat(oos_chunks, ignore_index=True)

    # Persist OOF to disk so we can recompute metrics later without a full
    # retrain (which currently costs ~12 min per horizon). Parquet keeps
    # dtypes and is ~5x smaller than CSV.
    try:
        oos_dir = os.path.join(os.path.dirname(__file__), "weights")
        os.makedirs(oos_dir, exist_ok=True)
        oos_path = os.path.join(oos_dir, "oos_" + horizon + ".parquet")
        oos.to_parquet(oos_path, index=False)
        log.info("Cached OOF predictions -> %s (%d rows)", oos_path, len(oos))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not cache OOF parquet: %s — re-eval will need retrain", e)

    metrics = _evaluate_oos(oos, horizon_days=horizon_days)

    # 1b. META-MODEL — AFML Ch. 3.6.
    # The out-of-fold primary predictions are the only honest input for a
    # meta-classifier: in-sample primary preds leak the answer. We zip them
    # with the original features and triple-barrier labels (when present)
    # to produce a binary "was the primary call right?" target.
    #
    # CRITICAL: train_meta_model() saves to CANDIDATE only. The active
    # meta-model is promoted further down, gated on the primary's DSR.
    # Otherwise the API ends up serving (old primary -> new meta), which
    # produces miscalibrated probabilities.
    meta_status = "skipped"
    try:
        meta_ds = build_meta_dataset(
            features_df=dataset,
            primary_oof_preds=oos[["symbol", "as_of_date", "prediction"]],
        )
        meta_meta = train_meta_model(meta_ds, horizon=horizon)
        meta_status = meta_meta.get("status", "unknown")
        log.info("Meta-model %s: %s (candidate only)", horizon, meta_status)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "Meta-model training failed for %s (%s) — primary saved anyway. "
            "Signal generator will fall back to magnitude-score confidence.",
            horizon, e,
        )

    log.info(
        "Purged-CV out-of-sample: IC=" + format(metrics["ic"], ".4f")
        + ", Annualized Sharpe=" + format(metrics["ann_sharpe"], ".3f")
        + " (non-overlapping, " + str(horizon_days) + "d rebalance)"
        + ", DSR=" + format(metrics["dsr"], ".4f")
        + " (n_periods=" + str(metrics["n_obs"])
        + " / " + str(metrics["n_obs_overlap"]) + " daily, n_trials="
        + str(DSR_N_TRIALS) + ")"
    )

    # 2. Production model trained on full data
    log.info("Retraining production model on full dataset...")
    full_data = lgb.Dataset(dataset[features], label=dataset["target"])
    prod_model = lgb.train(LGBM_PARAMS, full_data, num_boost_round=N_BOOST_ROUNDS)

    # 3. Feature importance (from production model)
    importance = prod_model.feature_importance(importance_type="gain")
    feat_imp = sorted(zip(features, importance), key=lambda x: x[1], reverse=True)[:15]
    feat_imp_dict = [{"name": k, "importance": float(v)} for k, v in feat_imp]

    # 4. Promotion gate — DSR is the ONLY promotion metric (Blueprint #2).
    #    A failing model is saved as a candidate but does NOT overwrite the
    #    live/active model that signal_generator loads.
    gate = should_promote(metrics["dsr"])

    model_dir = os.path.join(os.path.dirname(__file__), "weights")
    os.makedirs(model_dir, exist_ok=True)
    candidate_path = os.path.join(model_dir, "lgbm_" + horizon + "_candidate.txt")
    active_path = os.path.join(model_dir, "lgbm_" + horizon + ".txt")
    prod_model.save_model(candidate_path)
    if gate["promote"]:
        prod_model.save_model(active_path)
        log.info("PROMOTED %s primary -> %s (%s)", horizon, active_path, gate["reason"])
        # Primary promoted -> the meta candidate is calibrated against THIS
        # primary, so it's safe to make active. Tracking primary+meta as
        # one coupled artifact prevents the meta-primary misalignment bug.
        if meta_status == "ok":
            promote_meta_candidate(horizon)
        else:
            log.info("Meta-model promotion skipped (status=%s).", meta_status)
    else:
        log.warning(
            "%s primary NOT promoted (%s). Primary candidate saved to %s; "
            "active model unchanged. Meta-model candidate stays at "
            "weights/meta_%s_candidate.txt and is NOT activated, so the "
            "API keeps serving (active_primary -> active_meta) consistently.",
            horizon, gate["reason"], candidate_path, horizon,
        )

    # 5. Log run (records the promotion decision for the SEBI audit trail).
    execute_sql(
        """
        INSERT INTO model_runs (
            horizon, model_type, train_start, train_end, test_start, test_end,
            n_features, n_samples, ic_mean, sharpe, deflated_sharpe,
            params_json, feature_importance, promoted, dsr_threshold, promotion_note
        ) VALUES (
            %(horizon)s, %(model_type)s, %(train_start)s, %(train_end)s,
            %(test_start)s, %(test_end)s,
            %(n_features)s, %(n_samples)s, %(ic_mean)s, %(sharpe)s,
            %(deflated_sharpe)s, %(params_json)s, %(feature_importance)s,
            %(promoted)s, %(dsr_threshold)s, %(promotion_note)s
        )
        """,
        {
            "horizon":          horizon,
            "model_type":       "lightgbm_dart_purgedcv",
            "train_start":      dataset["as_of_date"].min(),
            "train_end":        dataset["as_of_date"].max(),
            "test_start":       oos["as_of_date"].min(),
            "test_end":         oos["as_of_date"].max(),
            "n_features":       len(features),
            "n_samples":        len(dataset),
            "ic_mean":          float(metrics["ic"]),
            "sharpe":           float(metrics["ann_sharpe"]),
            "deflated_sharpe":  float(metrics["dsr"]),
            "params_json":      json.dumps({
                **LGBM_PARAMS,
                "n_cv_splits":   N_CV_SPLITS,
                "embargo_days":  embargo,
                "dsr_n_trials":  DSR_N_TRIALS,
            }),
            "feature_importance": json.dumps(feat_imp_dict),
            "promoted":         bool(gate["promote"]),
            "dsr_threshold":    float(gate["threshold"]),
            "promotion_note":   gate["reason"],
        },
    )

    return prod_model
