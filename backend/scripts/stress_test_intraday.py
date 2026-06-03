"""
Slippage + liquidity stress test for the intraday top-K long-only strategy.

The simulate_strategies.py top-5 intraday result (Sharpe +4.47 gross,
+3.6 @ 15bps) is too good to trust without stressing the two assumptions
that inflate intraday backtests most:

  1. LIQUIDITY — the naive top-K picks the highest-prediction names with
     no regard for whether you could actually fill 5 lots there. We re-rank
     within a tradeable universe filtered by trailing-20d average daily
     value traded (ADV in rupees) and watch the edge survive (or not) as
     we demand more liquidity.

  2. SLIPPAGE — target_ret assumes you exit the instant price TOUCHES the
     +1.5sigma barrier. Real fills happen past the touch. We add slippage
     bps on top of the brokerage/STT/tax cost and re-sweep.

  3. CONCENTRATION — kurt=6.2 says a few days carry the P&L. We measure
     what fraction of total return comes from the top 5% of days. If most
     of it does, those are the days most likely to be unexecutable.

No retrain — reads weights/oos_intraday.parquet + the ohlcv table.

Usage:
  python scripts/stress_test_intraday.py
  python scripts/stress_test_intraday.py --top-k 5 --cost-bps 15
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew

from qsde.db.connection import read_sql
from qsde.models.deflated_sharpe import deflated_sharpe_ratio

WEIGHTS = Path(__file__).resolve().parents[1] / "qsde" / "models" / "weights"
CR = 1e7  # 1 crore in rupees

# ADV thresholds (rupees/day) to sweep. 0 = no filter (baseline).
ADV_LEVELS = (0.0, 1 * CR, 5 * CR, 10 * CR, 25 * CR, 50 * CR)


def _load_oos_with_adv() -> pd.DataFrame | None:
    p = WEIGHTS / "oos_intraday.parquet"
    if not p.exists():
        print(f"no cached OOF at {p}")
        return None
    oos = pd.read_parquet(p)
    if "target_ret" not in oos.columns:
        print("OOF lacks target_ret — retrain intraday first")
        return None
    oos["as_of_date"] = pd.to_datetime(oos["as_of_date"])

    # Trailing-20d average daily value traded (rupees), per symbol.
    print("Loading OHLCV + computing trailing-20d ADV (rupees)...")
    ohlcv = read_sql("SELECT symbol, date, close, volume FROM ohlcv")
    ohlcv["date"] = pd.to_datetime(ohlcv["date"])
    ohlcv = ohlcv.sort_values(["symbol", "date"])
    ohlcv["dv"] = ohlcv["close"].astype(float) * ohlcv["volume"].astype(float)
    ohlcv["adv20"] = (
        ohlcv.groupby("symbol")["dv"]
             .transform(lambda s: s.rolling(20, min_periods=5).mean())
    )
    m = oos.merge(
        ohlcv[["symbol", "date", "adv20"]],
        left_on=["symbol", "as_of_date"], right_on=["symbol", "date"],
        how="left",
    )
    matched = m["adv20"].notna().mean()
    print(f"  ADV matched for {matched:.1%} of OOF rows")
    return m


def _topk_returns(df: pd.DataFrame, top_k: int) -> tuple[pd.Series, np.ndarray]:
    """Per-day equal-weight mean target_ret of the top-K by prediction.
    Returns (daily_series, all_trade_rets)."""
    daily: dict = {}
    trades: list[float] = []
    for d, day in df.groupby("as_of_date"):
        day = day.sort_values("prediction", ascending=False).head(top_k)
        if day.empty:
            continue
        daily[d] = float(day["target_ret"].mean())
        trades.extend(day["target_ret"].tolist())
    s = pd.Series(daily).sort_index()
    return s, np.array(trades, dtype=float)


def _turnover_topk(df: pd.DataFrame, top_k: int) -> float:
    prev: set | None = None
    tos: list[float] = []
    for d, day in df.groupby("as_of_date"):
        names = set(day.sort_values("prediction", ascending=False).head(top_k)["symbol"])
        if prev is not None and names:
            tos.append(len(names - prev) / max(1, len(names)))
        prev = names
    return float(np.mean(tos)) if tos else 1.0


def _ann(series: pd.Series, ppy: float = 252.0) -> dict:
    mean, std, n = float(series.mean()), float(series.std()), len(series)
    if std > 0 and n >= 20:
        ann = (mean / std) * float(np.sqrt(ppy))
        sk = float(skew(series)); ku = float(kurtosis(series, fisher=False))
        dsr = float(deflated_sharpe_ratio(ann, 1, n, sk, ku))
        return {"sharpe": ann, "dsr": dsr, "n": n}
    return {"sharpe": 0.0, "dsr": 0.0, "n": n}


def _concentration(daily: pd.Series, top_frac: float = 0.05) -> float:
    """Fraction of total summed return contributed by the top `top_frac`
    of days (by return). >0.5 = the edge rides on a handful of days."""
    total = daily.sum()
    if total <= 0:
        return float("nan")
    k = max(1, int(len(daily) * top_frac))
    top = daily.sort_values(ascending=False).head(k).sum()
    return float(top / total)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--cost-bps", type=float, default=15.0,
                    help="Brokerage/STT/tax round-trip (bps). Slippage is added on top.")
    ap.add_argument("--slippage-bps", type=str, default="0,5,10,15,25",
                    help="Slippage levels to add on top of cost.")
    args = ap.parse_args()
    slips = tuple(float(s) for s in args.slippage_bps.split(","))

    m = _load_oos_with_adv()
    if m is None:
        return

    print()
    print("=" * 96)
    print(f" INTRADAY TOP-{args.top_k} STRESS TEST  (cost={args.cost_bps:.0f}bps round-trip + slippage)")
    print("=" * 96)

    # ---- 1. Liquidity filter sweep (fixed cost, no slippage yet) ----
    print("\n[1] Liquidity filter — re-rank within tradeable universe")
    print(f"    {'ADV>=':>10}  {'universe':>9}  {'days':>5}  "
          f"{'gross Sh':>9}  {'net Sh':>7}  {'DSR net':>8}  {'concentr.':>9}")
    base_for_concentration = None
    for adv in ADV_LEVELS:
        if adv == 0:
            f = m.copy()
            label = "(all)"
        else:
            f = m[m["adv20"] >= adv]
            label = f"{adv/CR:.0f}cr"
        if f.empty:
            continue
        avg_uni = f.groupby("as_of_date")["symbol"].nunique().mean()
        daily, trades = _topk_returns(f, args.top_k)
        if len(daily) < 20:
            continue
        turnover = _turnover_topk(f, args.top_k)
        gross = _ann(daily)
        friction = turnover * 1.0 * args.cost_bps / 10000.0
        net = _ann(daily - friction)
        conc = _concentration(daily)
        if adv == 10 * CR:
            base_for_concentration = (daily, turnover)
        print(f"    {label:>10}  {avg_uni:>9.0f}  {len(daily):>5}  "
              f"{gross['sharpe']:>+9.2f}  {net['sharpe']:>+7.2f}  "
              f"{net['dsr']:>8.4f}  {conc:>9.1%}")

    # ---- 2. Slippage sweep at a sensible liquid universe (ADV>=10cr) ----
    print(f"\n[2] Slippage on top of {args.cost_bps:.0f}bps cost  (universe: ADV>=10cr)")
    liq = m[m["adv20"] >= 10 * CR]
    daily, trades = _topk_returns(liq, args.top_k)
    turnover = _turnover_topk(liq, args.top_k)
    print(f"    universe avg={liq.groupby('as_of_date')['symbol'].nunique().mean():.0f} names, "
          f"turnover/day={turnover:.1%}, days={len(daily)}")
    print(f"    {'total friction':>15}  {'net Sharpe':>11}  {'DSR net':>8}  "
          f"{'net avg/trade':>13}  {'win net':>8}")
    for slip in slips:
        total_bps = args.cost_bps + slip
        friction = turnover * 1.0 * total_bps / 10000.0
        net = _ann(daily - friction)
        net_trades = trades - total_bps / 10000.0
        print(f"    {args.cost_bps:.0f}+{slip:<4.0f}={total_bps:>4.0f}bps  "
              f"{net['sharpe']:>+11.2f}  {net['dsr']:>8.4f}  "
              f"{net_trades.mean()*1e4:>+10.0f}bps  {(net_trades>0).mean():>7.1%}")

    # ---- 3. Concentration detail on the liquid universe ----
    if base_for_concentration is not None:
        daily10, _ = base_for_concentration
        print(f"\n[3] Concentration (ADV>=10cr universe, top-{args.top_k})")
        for frac in (0.01, 0.05, 0.10):
            c = _concentration(daily10, frac)
            print(f"    top {frac:>4.0%} of days contribute {c:>6.1%} of total gross return")
        # Sharpe after removing the best 1% of days (robustness).
        k = max(1, int(len(daily10) * 0.01))
        trimmed = daily10.sort_values(ascending=False).iloc[k:]
        st = _ann(trimmed)
        print(f"    Sharpe after dropping best 1% of days: {st['sharpe']:+.2f} "
              f"(was {_ann(daily10)['sharpe']:+.2f})")

    print("\n" + "=" * 96)


if __name__ == "__main__":
    main()
