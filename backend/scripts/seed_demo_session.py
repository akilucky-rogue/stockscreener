"""
Seed a synthetic intraday session into ohlcv_intraday — for verifying the
live pipeline WITHOUT a live Kite stream (e.g. pre-market or no token).

Generates ~150 deterministic 1-minute bars for one symbol: a mild uptrend with
a mid-session liquidity sweep (a dip that pierces the prior lows then closes
back above them), so the microstructure + signal modules have something
realistic to chew on. Upserts into ohlcv_intraday (symbol, ts) so reruns are
idempotent.

Usage (from qsde/backend, venv active):
    python -m scripts.seed_demo_session --symbol KEI --date 2026-05-26
    python -m scripts.seed_demo_session --symbol KEI            # defaults to today IST

This is SYNTHETIC demo data. When a real Kite stream populates today's bars,
the loader will naturally prefer the latest real session.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend on path

import numpy as np
import pandas as pd

from qsde.db.connection import upsert_dataframe


def build_session(symbol: str, date_str: str, n: int = 150, base: float = 3800.0, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp(f"{date_str} 09:15:00", tz="Asia/Kolkata")
    idx = pd.date_range(start, periods=n, freq="1min")

    rets = rng.normal(0.00035, 0.0009, n)        # gentle uptrend
    close = base * np.exp(np.cumsum(rets))

    # Inject a liquidity sweep around bars 60-64: pierce recent lows then reclaim.
    sweep = slice(60, 64)
    close[sweep] = close[sweep] * 0.985           # quick flush down
    close[64:] = close[64:] * 0.995               # partial continuation after reclaim

    open_ = np.r_[close[0], close[:-1]]
    body_noise = np.abs(rng.normal(0, 0.0007, n)) * close
    high = np.maximum(open_, close) + body_noise
    low = np.minimum(open_, close) - body_noise
    # Exaggerate the wick-down during the sweep so it pierces prior lows.
    low[sweep] = low[sweep] - close[sweep] * 0.004

    vol = rng.integers(3_000, 25_000, n).astype("int64")
    vol[sweep] = vol[sweep] * 3                   # volume spike on the flush
    typical = (high + low + close) / 3.0
    n_ticks = rng.integers(20, 90, n).astype("int64")

    return pd.DataFrame(
        {
            "symbol": symbol.upper(),
            "ts": idx,
            "open": np.round(open_, 2),
            "high": np.round(high, 2),
            "low": np.round(low, 2),
            "close": np.round(close, 2),
            "volume": vol,
            "vwap": np.round(typical, 2),
            "n_ticks": n_ticks,
        }
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="KEI")
    ap.add_argument("--date", default=pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d"))
    ap.add_argument("--bars", type=int, default=150)
    ap.add_argument("--base", type=float, default=3800.0)
    args = ap.parse_args()

    df = build_session(args.symbol, args.date, n=args.bars, base=args.base)
    n = upsert_dataframe(
        df,
        table="ohlcv_intraday",
        conflict_columns=["symbol", "ts"],
        update_columns=["open", "high", "low", "close", "volume", "vwap", "n_ticks"],
    )
    print(f"Seeded {n} synthetic bars for {args.symbol.upper()} on {args.date} "
          f"({df['ts'].min()} .. {df['ts'].max()}).")


if __name__ == "__main__":
    main()
