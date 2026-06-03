"""Recompute OOF metrics from cached predictions — no retrain.

run_pipeline.py writes weights/oos_{horizon}.parquet after every training
run. This script reads those files and computes IC / Sharpe / DSR with the
CURRENT version of _evaluate_oos(), plus a transaction-cost sensitivity
table from _evaluate_oos_with_costs().

Usage:
  python scripts/reevaluate_oos.py                          # all horizons
  python scripts/reevaluate_oos.py --horizon intraday       # one horizon
  python scripts/reevaluate_oos.py --costs-bps 5,10,15,20,30  # custom curve
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from qsde.models.lgbm_model import _evaluate_oos, _evaluate_oos_with_costs
from qsde.models.purged_cv import horizon_to_days


DEFAULT_COST_CURVE = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0)


def _fmt_signed(x: float, w: int = 7, p: int = 3) -> str:
    return f"{x:+{w}.{p}f}"


def reevaluate(horizon: str, cost_bps: tuple[float, ...]) -> None:
    weights_dir = Path(__file__).resolve().parents[1] / "qsde" / "models" / "weights"
    path = weights_dir / f"oos_{horizon}.parquet"
    if not path.exists():
        print(f"[{horizon:8s}] no cached OOF at {path}")
        return
    oos = pd.read_parquet(path)
    return_col = "target_ret" if "target_ret" in oos.columns else "target"
    horizon_days = horizon_to_days(horizon)

    base = _evaluate_oos(oos, horizon_days=horizon_days)
    print(f"\n[{horizon:8s}] n_obs={len(oos):>7,}  return_col={return_col}  "
          f"IC={_fmt_signed(base['ic'], 6, 4)}  "
          f"skew={_fmt_signed(base['skew'], 4, 2)}  "
          f"kurt={base['kurtosis']:.2f}")

    # Sensitivity table: walk a cost curve, show how Sharpe & DSR degrade.
    # avg_turnover is identical across cost levels (it's a property of the
    # picks, not the cost), so we only need to print it once.
    print(f"           {'cost bps':>8}  {'turnover':>9}  "
          f"{'Sharpe gross':>13}  {'Sharpe net':>11}  "
          f"{'DSR gross':>10}  {'DSR net':>8}")
    for bps in cost_bps:
        r = _evaluate_oos_with_costs(
            oos, horizon_days=horizon_days, cost_bps_round_trip=bps,
        )
        print(f"           {bps:>8.1f}  {r['avg_turnover']:>9.1%}  "
              f"{_fmt_signed(r['ann_sharpe_gross'], 13, 3)}  "
              f"{_fmt_signed(r['ann_sharpe_net'], 11, 3)}  "
              f"{r['dsr_gross']:>10.4f}  {r['dsr_net']:>8.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", choices=["intraday", "swing", "long", "all"],
                    default="all")
    ap.add_argument(
        "--costs-bps", type=str,
        default=",".join(str(c) for c in DEFAULT_COST_CURVE),
        help="Comma-separated round-trip costs to sweep, e.g. '5,10,15,20,30'",
    )
    args = ap.parse_args()
    cost_curve = tuple(float(c.strip()) for c in args.costs_bps.split(","))
    horizons = ("intraday", "swing", "long") if args.horizon == "all" else (args.horizon,)
    print()
    print("=" * 100)
    print(" OOF re-evaluation + transaction-cost ablation")
    print(" Cost model: 100% long + 100% short = 200% notional. Per-period cost =")
    print(" avg_turnover × 2 × round_trip_bps / 10000.  Turnover = avg fraction of basket")
    print(" names that change between rebalance dates, computed from the SAME OOF preds.")
    print("=" * 100)
    for h in horizons:
        reevaluate(h, cost_curve)
    print("=" * 100)


if __name__ == "__main__":
    main()
