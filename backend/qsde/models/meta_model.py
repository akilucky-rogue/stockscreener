"""
Meta-labeling: a secondary classifier on top of the primary signal model.

Reference: López de Prado, "Advances in Financial Machine Learning" (2018),
Chapter 3.6.

Why this module exists
----------------------
The primary regressor / direction classifier answers WHICH WAY. It's a
forecast over a noisy signal, so even when it's right on average, many
individual calls are wrong. A naïve "always trade when primary fires" loses
to costs.

The meta-model answers SHOULD WE TRADE THIS CALL? It's a binary classifier
trained on:
    features:  primary_pred + a subset of the original feature set
    label:     +1 if the primary's direction was correct in the next horizon
               0 otherwise

The output is a calibrated probability P(primary correct). That probability
becomes the input to the bet-sizing step (e.g., size = max(0, 2p − 1)).

This is what unbreaks the "Score" field:
  * BEFORE: score = min(1, |pred|/0.04) — saturates at 99% for almost
    everything, no probabilistic meaning.
  * AFTER:  score = P(primary correct) — calibrated against historical
    hit-rate by purged-CV folds; usually peaks in the 60-75% range, which
    is the honest band for equity ML.

Public API
----------
  build_meta_dataset(features_df, primary_oof_preds, triple_barrier_labels)
  train_meta_model(meta_dataset, horizon)
  load_meta_model(horizon)
  meta_predict(meta_model, X)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


META_LGBM_PARAMS = {
    "objective":        "binary",
    "boosting_type":    "gbdt",
    "metric":           "binary_logloss",
    "learning_rate":    0.05,
    "num_leaves":       16,           # smaller than primary; binary task
    "max_depth":        4,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq":     5,
    "verbose":          -1,
    "random_state":     42,
}
META_N_BOOST_ROUNDS = 100

# Where to read/write meta-models. Kept alongside the primary weights/.
def _weights_dir() -> str:
    return os.path.join(
        os.path.dirname(__file__), "weights",
    )


def _meta_path(horizon: str) -> str:
    """Path of the ACTIVE meta-model (the one /api/analyze loads)."""
    return os.path.join(_weights_dir(), f"meta_{horizon}.txt")


def _meta_candidate_path(horizon: str) -> str:
    """Path of the freshly-trained CANDIDATE meta-model.

    The promotion gate (see lgbm_model.train_lightgbm_model) only copies
    this to the active path when the PRIMARY model also passes its DSR
    gate. Otherwise the active meta stays aligned with the active primary.
    """
    return os.path.join(_weights_dir(), f"meta_{horizon}_candidate.txt")


# ── dataset assembly ───────────────────────────────────────────────

def build_meta_dataset(
    features_df: pd.DataFrame,
    primary_oof_preds: pd.DataFrame,
    triple_barrier_labels: Optional[pd.Series] = None,
    join_on: tuple[str, str] = ("symbol", "as_of_date"),
) -> pd.DataFrame:
    """Assemble the (features + primary_pred, meta_label) training set.

    Args:
        features_df:       wide DataFrame with at least the join columns. The
                           primary model's features become the meta model's
                           features too — empirically this is the sweet spot
                           between "just primary_pred" (underfits) and "all
                           original + primary_pred" (mostly redundant).
        primary_oof_preds: DataFrame with the join columns + 'prediction'.
                           MUST come from out-of-fold prediction — using
                           in-sample primary predictions creates information
                           leakage in the meta-model.
        triple_barrier_labels: Series indexed by (symbol, as_of_date) with
                           triple-barrier outcome {-1, 0, +1}. If None, falls
                           back to features_df['target'] (assumes already
                           triple-barrier-labeled).

    Returns:
        DataFrame with feature cols + 'primary_pred' + 'meta_label' (0/1).
    """
    j = list(join_on)
    df = features_df.merge(
        primary_oof_preds[[*j, "prediction"]].rename(columns={"prediction": "primary_pred"}),
        on=j, how="inner",
    )
    if triple_barrier_labels is not None:
        tb = (
            triple_barrier_labels.reset_index()
            .rename(columns={"index": j[1], 0: "tb"})
        )
        df = df.merge(tb, on=j, how="inner")
        tb_col = "tb"
    else:
        if "target" not in df.columns:
            raise ValueError(
                "build_meta_dataset: need either `triple_barrier_labels` "
                "or features_df.target to derive the meta label."
            )
        tb_col = "target"

    # meta label: 1 if sign(primary) == sign(target), 0 otherwise.
    #
    # Auto-detect the target form. Triple-barrier produces strictly {-1, 0, +1}
    # — in that case we drop the 0 rows (HOLD, non-event for "was the call
    # right?"). Continuous-return targets (the fallback when triple-barrier
    # didn't run) are handled by signing them: any |target| > 0 becomes
    # ±1 and is kept. Targets within 1bp of zero are treated as HOLD noise
    # and dropped.
    tgt = df[tb_col].astype(float)
    unique_vals = set(np.unique(tgt.dropna().round(6)))
    is_categorical = unique_vals.issubset({-1.0, 0.0, 1.0})
    if is_categorical:
        df = df[df[tb_col].isin([-1, 1])].copy()
        target_sign = np.sign(df[tb_col])
    else:
        # Continuous fallback: drop noise-band rows, sign the rest.
        df = df[tgt.abs() > 1e-4].copy()
        target_sign = np.sign(df[tb_col])
    df["meta_label"] = (np.sign(df["primary_pred"]) == target_sign).astype(int)
    df = df.drop(columns=[tb_col])
    return df


# ── train / save / load ────────────────────────────────────────────

def train_meta_model(
    meta_dataset: pd.DataFrame,
    horizon: str,
    exclude_cols: tuple[str, ...] = (
        "symbol", "as_of_date", "meta_label", "target", "target_ret",
        "label_end_date", "yf_symbol", "source",
    ),
) -> dict:
    """Train the meta-model for one horizon and persist as a CANDIDATE.

    Does NOT overwrite the active meta-model on disk. That promotion is
    the caller's decision — it must happen ONLY when the primary model
    also passes its own promotion gate, otherwise we end up serving a
    meta trained on a different primary than the one actually live
    (the meta-primary misalignment bug).

    To promote the candidate after a passing primary, call
    `promote_meta_candidate(horizon)`.
    """
    if meta_dataset.empty:
        log.warning("Empty meta-dataset; skipping meta-model training.")
        return {"status": "empty"}

    features = [c for c in meta_dataset.columns if c not in exclude_cols]
    X = meta_dataset[features]
    y = meta_dataset["meta_label"].astype(int)
    base_rate = float(y.mean())

    train_data = lgb.Dataset(X, label=y)
    model = lgb.train(
        META_LGBM_PARAMS, train_data, num_boost_round=META_N_BOOST_ROUNDS,
    )

    try:
        from sklearn.metrics import roc_auc_score
        train_auc = float(roc_auc_score(y, model.predict(X)))
    except Exception:  # noqa: BLE001
        train_auc = float("nan")

    os.makedirs(_weights_dir(), exist_ok=True)
    candidate = _meta_candidate_path(horizon)
    model.save_model(candidate)
    log.info(
        "Meta-model %s trained (CANDIDATE): n=%d, base_hit_rate=%.2f%%, "
        "training_AUC=%.3f, saved=%s",
        horizon, len(meta_dataset), 100 * base_rate, train_auc, candidate,
    )
    return {
        "status":          "ok",
        "horizon":         horizon,
        "n_obs":           int(len(meta_dataset)),
        "n_features":      len(features),
        "base_hit_rate":   base_rate,
        "training_auc":    train_auc,
        "candidate_path":  candidate,
        "features_used":   features,
    }


def promote_meta_candidate(horizon: str) -> bool:
    """Copy weights/meta_{horizon}_candidate.txt -> weights/meta_{horizon}.txt.

    Called by lgbm_model.train_lightgbm_model ONLY after the primary model
    has passed its DSR gate in the same run. Safe to call when the
    candidate doesn't exist (returns False).
    """
    import shutil
    src = _meta_candidate_path(horizon)
    dst = _meta_path(horizon)
    if not os.path.exists(src):
        log.warning("promote_meta_candidate %s: no candidate at %s", horizon, src)
        return False
    shutil.copyfile(src, dst)
    log.info("PROMOTED meta-model %s -> %s", horizon, dst)
    return True


def load_meta_model(horizon: str) -> Optional[lgb.Booster]:
    """Load the meta-model for `horizon`, or None if not trained yet.
    Pure-read; no side effects. Cached by callers if needed."""
    path = _meta_path(horizon)
    if not os.path.exists(path):
        return None
    try:
        return lgb.Booster(model_file=path)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to load meta-model %s: %s", horizon, e)
        return None


def meta_predict(meta_model: lgb.Booster, features_wide: pd.DataFrame) -> np.ndarray:
    """Return P(primary correct) for each row of `features_wide`.

    The caller is responsible for aligning columns to what the model was
    trained on (LightGBM aligns by name when given a DataFrame). Missing
    feature columns become NaN and LightGBM handles them natively.
    """
    expected = meta_model.feature_name()
    cols = {}
    for col in expected:
        if col in features_wide.columns:
            cols[col] = pd.to_numeric(features_wide[col], errors="coerce").values
        else:
            cols[col] = np.full(len(features_wide), np.nan)
    aligned = pd.DataFrame(cols, index=features_wide.index, dtype=np.float64)
    proba = meta_model.predict(aligned)
    # Clamp because LightGBM's GBDT raw probabilities can spill marginally
    # outside [0,1] due to leaf-wise smoothing. We display this as percent.
    return np.clip(proba, 0.0, 1.0)
