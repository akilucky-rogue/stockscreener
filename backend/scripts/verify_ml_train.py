"""
Verify the walk-forward ML train pipeline end-to-end against the real DB,
WITHOUT a 20-year ingest. Builds a deterministic synthetic (factors, target)
dataset with a genuine predictive signal, runs train_lightgbm_model (purged
k-fold CV -> IC / non-overlapping Sharpe / DSR -> promotion gate -> model_runs
+ weights), and confirms the gate + audit row + saved weights.

Runs with QSDE_FORCE_PROMOTE=true so a model is produced even though synthetic
DSR won't clear 0.95 — that exercises the active-model promotion path. The
*decision* logic (promote iff DSR>=threshold) is unit-tested separately.

Usage (qsde/backend, venv): python scripts/verify_ml_train.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QSDE_FORCE_PROMOTE", "true")

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import numpy as np
import pandas as pd

from qsde.models.lgbm_model import train_lightgbm_model
from qsde.db.connection import read_sql


def synth(n_sym: int = 12, n_days: int = 180, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rows = []
    for d in dates:
        for s in range(n_sym):
            feats = {f"feat{j}": float(rng.normal()) for j in range(8)}
            # target genuinely depends on two features -> non-trivial IC
            target = 0.015 * feats["feat0"] + 0.008 * feats["feat1"] + float(rng.normal(0, 0.02))
            rows.append({"symbol": f"S{s:02d}", "as_of_date": d, "target": target, **feats})
    return pd.DataFrame(rows)


def main() -> None:
    ds = synth()
    print(f"synthetic dataset: {len(ds)} rows, {ds['symbol'].nunique()} symbols, {ds['as_of_date'].nunique()} dates")

    model = train_lightgbm_model(ds, horizon="swing")
    assert model is not None, "training returned None (all folds skipped?)"

    row = read_sql(
        "SELECT horizon, ic_mean, sharpe, deflated_sharpe, promoted, dsr_threshold, "
        "promotion_note, n_samples FROM model_runs ORDER BY created_at DESC LIMIT 1"
    )
    print("model_runs latest row:")
    print(row.to_string(index=False))

    wdir = BACKEND / "qsde" / "models" / "weights"
    active = wdir / "lgbm_swing.txt"
    cand = wdir / "lgbm_swing_candidate.txt"
    print(f"candidate weights exists: {cand.exists()} | active weights exists: {active.exists()}")

    assert cand.exists(), "candidate model not saved"
    promoted = bool(row.iloc[0]["promoted"])
    assert promoted, "force-promote did not record promoted=True"
    assert active.exists(), "promoted but active weights missing"
    print("\nRESULT: purged-CV train + DSR gate + model_runs audit + weights promotion all OK.")


if __name__ == "__main__":
    main()
