"""
Strategy simulation on cached OOF predictions — no retrain.

Tests two REALISTIC retail strategies the L/S decile-spread benchmark does
NOT represent (that benchmark is a 200%-notional market-neutral fund that
rebalances the whole basket every period — nobody trades it at retail).

  F — Monthly long-only quintile portfolio
      Every ~21 trading days: go long the top 20% of names by model
      prediction, equal-weight, hold one month. Long-only (no short),
      low turnover. The "smart smallcase" product.

  A — Top-K selective long-only held to barrier
      Every rebalance: go long only the top K (default 5) names,
      equal-weight, hold to the horizon's triple-barrier. The "live
      copilot picks a few trades" product.

Return convention: uses target_ret = realized return from entry to barrier
hit. For a LONG position the realized return IS target_ret (no sign flip).

Cost model (long-only): 100% notional (NOT 200% — there is no short leg).
  cost_per_period = avg_turnover * 1.0 * cost_bps_round_trip / 10000

Usage:
  python scripts/simulate_strategies.py
  python scripts/simulate_strategies.py --top-k 3 --costs-bps 5,10,15,20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew

from qsde.models.deflated_sharpe import deflated_sharpe_ratio
from qsde.models.purged_cv import horizon_to_days

WEIGHTS = Path(__file__).resolve().parents[1] / "qsde" / "models" / "weights"
DEFAULT_COSTS = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0)


def _load(horizon: str) -> pd.DataFrame | None:
    p = WEIGHTS / f"oos_{horizon}.parquet"
    if not p.exists():
        print(f"[{horizon}] no cached OOF at {p}")
        return None
    df = pd.read_parquet(p)
    if "target_ret" not in df.columns:
        print(f"[{horizon}] OOF has no target_ret — retrain needed for this test")
        return None
    df["as_of_date"] = pd.to_datetime(df["as_of_date"])
    return df


def _select_baskets(oos: pd.DataFrame, rebalance_days: int,
                    top_frac: float | None = None, top_k: int | None = None):
    """Return (baskets, period_returns, all_trade_rets).

    baskets:        dict[date] -> set(symbols) chosen on each rebalance date
    period_returns: dict[date] -> equal-weight mean target_ret of the basket
    all_trade_rets: flat list of every individual pick's target_ret
    """
    dates = sorted(oos["as_of_date"].unique())
    rebal_dates = dates[::rebalance_days]
    baskets: dict = {}
    period_returns: dict = {}
    all_trade_rets: list[float] = []
    for d in rebal_dates:
        day = oos[oos["as_of_date"] == d]
        if day.empty:
            continue
        day = day.sort_values("prediction", ascending=False)
        if top_k is not None:
            picks = day.head(top_k)
        else:
            n = max(1, int(round(len(day) * (top_frac or 0.2))))
            picks = day.head(n)
        if picks.empty:
            continue
        baskets[d] = set(picks["symbol"])
        period_returns[d] = float(picks["target_ret"].mean())
        all_trade_rets.extend(picks["target_ret"].tolist())
    return baskets, period_returns, all_trade_rets


def _avg_turnover(baskets: dict) -> float:
    dates = sorted(baskets.keys())
    if len(dates) < 2:
        return 1.0
    tos = []
    for i in range(1, len(dates)):
        prev, curr = baskets[dates[i - 1]], baskets[dates[i]]
        if not curr:
            continue
        tos.append(len(curr - prev) / max(1, len(curr)))
    return float(np.mean(tos)) if tos else 1.0


def _ann_stats(series: pd.Series, periods_per_year: float) -> dict:
    mean, std, n = float(series.mean()), float(series.std()), len(series)
    if std > 0 and n >= 20:
        ann = (mean / std) * float(np.sqrt(periods_per_year))
        sk = float(skew(series))
        ku = float(kurtosis(series, fisher=False))
        dsr = float(deflated_sharpe_ratio(ann, 1, n, sk, ku))
        return {"sharpe": ann, "dsr": dsr, "n": n, "skew": sk, "kurt": ku}
    return {"sharpe": 0.0, "dsr": 0.0, "n": n, "skew": 0.0, "kurt": 3.0}


def simulate(oos: pd.DataFrame, horizon_days: int, rebalance_days: int,
             top_frac=None, top_k=None, costs=DEFAULT_COSTS, label="") -> None:
    baskets, period_returns, all_trades = _select_baskets(
        oos, rebalance_days, top_frac=top_frac, top_k=top_k)
    if len(period_returns) < 20:
        print(f"  {label}: only {len(period_returns)} periods — too few to evaluate")
        return

    dates = sorted(period_returns.keys())
    gross = pd.Series([period_returns[d] for d in dates], index=dates)
    turnover = _avg_turnover(baskets)
    periods_per_year = 252.0 / rebalance_days
    avg_names = float(np.mean([len(baskets[d]) for d in dates]))

    # Per-trade stats (gross).
    trades = np.array(all_trades, dtype=float)
    gross_win = float((trades > 0).mean())
    gross_avg_bps = float(trades.mean() * 1e4)

    print(f"  {label}")
    print(f"      periods={len(dates)}  avg_names/period={avg_names:.0f}  "
          f"trades={len(trades):,}  turnover/period={turnover:.1%}")
    print(f"      gross: per-trade win={gross_win:.1%}  avg={gross_avg_bps:+.0f}bps  "
          f"skew={skew(gross):+.2f}  kurt={kurtosis(gross, fisher=False):.1f}")
    print(f"      {'cost bps':>8}  {'Sharpe net':>11}  {'DSR net':>8}  "
          f"{'avg/trade net':>13}  {'per-trade win net':>17}")
    for bps in costs:
        cost_per_period = turnover * 1.0 * bps / 10000.0   # long-only -> 1.0 notional
        net = gross - cost_per_period
        st = _ann_stats(net, periods_per_year)
        # Per-trade net: every pick pays a 1-name round trip once on entry+exit.
        net_trades = trades - (bps / 10000.0)
        net_win = float((net_trades > 0).mean())
        net_avg_bps = float(net_trades.mean() * 1e4)
        print(f"      {bps:>8.1f}  {st['sharpe']:>+11.3f}  {st['dsr']:>8.4f}  "
              f"{net_avg_bps:>+10.0f}bps  {net_win:>16.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--top-frac", type=float, default=0.20)
    ap.add_argument("--monthly-days", type=int, default=21)
    ap.add_argument("--costs-bps", type=str,
                    default=",".join(str(c) for c in DEFAULT_COSTS))
    args = ap.parse_args()
    costs = tuple(float(c.strip()) for c in args.costs_bps.split(","))

    print()
    print("=" * 92)
    print(" STRATEGY SIMULATION  (long-only, realistic retail cost model)")
    print("=" * 92)

    # ---- F: monthly long-only quintile ----
    print("\n[F] Monthly long-only quintile portfolio (top "
          f"{args.top_frac:.0%}, {args.monthly_days}-day rebalance)")
    for horizon in ("swing", "long"):
        oos = _load(horizon)
        if oos is None:
            continue
        simulate(oos, horizon_to_days(horizon), rebalance_days=args.monthly_days,
                 top_frac=args.top_frac, costs=costs,
                 label=f"source={horizon} OOF")

    # ---- A: top-K selective long-only held to barrier ----
    print(f"\n[A] Top-{args.top_k} selective long-only (held to barrier)")
    for horizon in ("intraday", "swing", "long"):
        oos = _load(horizon)
        if oos is None:
            continue
        hd = horizon_to_days(horizon)
        simulate(oos, hd, rebalance_days=hd, top_k=args.top_k, costs=costs,
                 label=f"horizon={horizon} (rebalance every {hd}d)")

    print("\n" + "=" * 92)


if __name__ == "__main__":
    main()
