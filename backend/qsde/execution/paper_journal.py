"""
Paper-trade journal — the live-validation loop.

The backtest says ~0.9 intraday / ~0.43 long net Sharpe. This module lets you
find out if that holds on data the model has never seen, by:

  1. take_trade()          — record a signal you'd actually take (entry price,
                             target, stop, horizon), once per (symbol, horizon,
                             day, strategy).
  2. reconcile_open_trades — walk forward through real OHLCV (daily) /
                             ohlcv_intraday (minute) and mark each OPEN trade
                             against its triple barriers: WIN (target first),
                             LOSS (stop first), TIME (neither -> exit at horizon
                             close). Realized return is net of an assumed
                             round-trip cost so it's directly comparable to the
                             stress-tested net Sharpe.
  3. track_record()        — aggregate the closed trades into a live scorecard:
                             hit rate, avg net return, realized Sharpe — next to
                             the backtested edge band, so you can see whether
                             reality matches the backtest.

Strategy column (since migration 010):
  Every paper trade carries a `strategy` tag. The ML signals use "model";
  baseline strategies use "baseline_top_momentum", "baseline_nifty",
  "baseline_random". The drift/scorecard reports compare model vs baselines
  so we never deploy a model that's just adding overhead instead of edge.

Long-only by default (Indian retail can't easily short cash equity). The
barrier logic mirrors qsde/models/triple_barrier so labels and live outcomes
speak the same language.
"""
from __future__ import annotations

import logging
import random
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db.connection import execute_sql, read_sql
from qsde.risk.costs import cost_bps as _horizon_cost_bps

log = logging.getLogger(__name__)

# Horizon -> barrier window in NSE sessions. Mirrors triple_barrier t1.
_HORIZON_SESSIONS = {"intraday": 1, "swing": 5, "long": 20}


# ── take ────────────────────────────────────────────────────────────

def take_trade(symbol: str, horizon: str, cost_bps: Optional[float] = None,
               entry_price: Optional[float] = None,
               strategy: str = "model") -> dict:
    """Record a paper trade from the latest signal for (symbol, horizon).

    Pulls entry/target/stop/rank from the most recent signals row. Entry
    price defaults to the signal's entry_price (latest close); pass an
    explicit entry_price to use your actual decision-time price (more honest
    for intraday).

    Args:
        symbol:      NSE symbol.
        horizon:     intraday | swing | long.
        cost_bps:    Round-trip cost in bps. None -> horizon-aware default
                     from qsde.risk.costs (PAPER_DEFAULT_BPS).
        entry_price: Decision-time price; defaults to signal's entry_price.
        strategy:    Tag identifying which strategy generated this trade.
                     "model" for the ML signal (default). For baselines see
                     take_baseline_trades().
    """
    sym = symbol.upper().strip()
    hzn = horizon.lower().strip()
    if hzn not in _HORIZON_SESSIONS:
        return {"ok": False, "error": f"unknown horizon {hzn}"}
    if cost_bps is None:
        cost_bps = _horizon_cost_bps(hzn, paper_default=True)

    sig = read_sql(
        """SELECT symbol, horizon, direction, entry_price, target_price,
                  stop_price, ranking_score, is_liquid, date
             FROM signals
            WHERE symbol = :s AND horizon = :h
         ORDER BY date DESC LIMIT 1""",
        params={"s": sym, "h": hzn},
    )
    if sig.empty:
        return {"ok": False, "error": f"no signal for {sym}/{hzn}"}
    r = sig.iloc[0]
    if not bool(r["is_liquid"]):
        return {"ok": False, "error": f"{sym} is not liquid (ADV gate) — not tradeable"}
    direction = int(r["direction"]) or 1   # default long if HOLD slipped through
    entry = float(entry_price) if entry_price is not None else (
        float(r["entry_price"]) if pd.notna(r["entry_price"]) else None)
    if entry is None or entry <= 0:
        return {"ok": False, "error": "no usable entry price"}

    try:
        execute_sql(
            """INSERT INTO paper_trades
                 (symbol, horizon, entry_date, entry_price, direction,
                  target_price, stop_price, rank_pct, horizon_sessions,
                  cost_bps, strategy)
               VALUES
                 (%(s)s, %(h)s, %(d)s, %(e)s, %(dir)s, %(t)s, %(st)s, %(rk)s,
                  %(hs)s, %(c)s, %(strat)s)
               ON CONFLICT (strategy, symbol, horizon, entry_date) DO NOTHING""",
            {
                "s": sym, "h": hzn, "d": date.today(), "e": entry, "dir": direction,
                "t": float(r["target_price"]) if pd.notna(r["target_price"]) else None,
                "st": float(r["stop_price"]) if pd.notna(r["stop_price"]) else None,
                "rk": float(r["ranking_score"]) if pd.notna(r["ranking_score"]) else None,
                "hs": _HORIZON_SESSIONS[hzn], "c": cost_bps,
                "strat": strategy,
            },
        )
        return {"ok": True, "symbol": sym, "horizon": hzn, "entry": entry,
                "direction": direction, "strategy": strategy,
                "cost_bps": cost_bps}
    except Exception as e:  # noqa: BLE001
        log.exception("take_trade failed")
        return {"ok": False, "error": str(e)}


# ── baselines ───────────────────────────────────────────────────────

# Baseline strategies are the null we benchmark the ML model against. If
# the model can't beat ALL of these on net Sharpe over 30+ sessions,
# either the model is overhead or the cost model is wrong — either way,
# don't deploy real money on it.

_BASELINE_STRATEGIES = (
    "baseline_top_momentum",   # buy yesterday's top-3 NIFTY 200 movers
    "baseline_nifty",          # buy/hold NIFTY 200 representative
    "baseline_random",         # random pick from liquid universe
)


def _atr_levels(symbol: str, entry: float, horizon: str,
                direction: int = 1) -> tuple[Optional[float], Optional[float]]:
    """ATR-based target/stop for baseline trades — same multipliers as the
    model so realized stats are comparable like-for-like."""
    df = read_sql(
        """SELECT factor_value FROM factor_pit
            WHERE symbol = :s AND factor_name = 'tech_atr_pct'
              AND valid_to = 'infinity'::timestamptz
         ORDER BY as_of_date DESC LIMIT 1""",
        params={"s": symbol},
    )
    atr_pct = float(df.iloc[0]["factor_value"]) if not df.empty else None
    if atr_pct is None:
        # Per-horizon fallback ATR (matches qsde/risk/trade_levels._ATR_FALLBACK).
        atr_pct = {"intraday": 1.0, "swing": 2.2, "long": 4.5}.get(horizon, 2.2)
    # Auto-detect percent vs fraction (factor stores percent; math wants fraction).
    atr_frac = atr_pct / 100.0 if atr_pct > 1.0 else atr_pct
    # Multipliers mirror qsde/risk/trade_levels._HORIZON_MULTIPLIERS.
    s_mult, t_mult = {
        "intraday": (0.75, 1.50),
        "swing":    (1.50, 2.50),
        "long":     (2.50, 4.50),
    }.get(horizon, (1.50, 2.50))
    atr_abs = atr_frac * entry
    if direction > 0:
        return (entry + t_mult * atr_abs, entry - s_mult * atr_abs)
    return (entry - t_mult * atr_abs, entry + s_mult * atr_abs)


def _latest_close(symbol: str) -> Optional[float]:
    df = read_sql(
        "SELECT close FROM ohlcv WHERE symbol = :s ORDER BY date DESC LIMIT 1",
        params={"s": symbol},
    )
    return float(df.iloc[0]["close"]) if not df.empty else None


def _baseline_top_momentum_picks(n: int = 3) -> list[str]:
    """Yesterday's top-N NIFTY 200 daily-return names (long-only).

    Uses universe.index_membership (JSONB array; e.g. ["NIFTY 200","NIFTY 500"])
    rather than a separate membership table. Falls back to the active liquid
    universe if no rows match (e.g. on day 1 before index_membership is
    populated)."""
    df = read_sql(
        """WITH ranked AS (
              SELECT o.symbol, o.date,
                     (o.close / LAG(o.close) OVER (PARTITION BY o.symbol ORDER BY o.date) - 1) AS ret
                FROM ohlcv o
                JOIN universe u ON u.symbol = o.symbol
               WHERE u.is_active = TRUE
                 AND u.index_membership @> '["NIFTY 200"]'::jsonb
                 AND o.date >= CURRENT_DATE - INTERVAL '7 days'
           )
           SELECT symbol, ret FROM ranked
            WHERE date = (SELECT MAX(date) FROM ranked) AND ret IS NOT NULL
         ORDER BY ret DESC LIMIT :n""",
        params={"n": n},
    )
    if not df.empty:
        return [str(s) for s in df["symbol"].tolist()]
    # Fallback: if no NIFTY 200 membership rows yet, pull from liquid signals.
    df = read_sql(
        """WITH ranked AS (
              SELECT o.symbol, o.date,
                     (o.close / LAG(o.close) OVER (PARTITION BY o.symbol ORDER BY o.date) - 1) AS ret
                FROM ohlcv o
                JOIN universe u ON u.symbol = o.symbol
               WHERE u.is_active = TRUE
                 AND o.date >= CURRENT_DATE - INTERVAL '7 days'
           )
           SELECT symbol, ret FROM ranked
            WHERE date = (SELECT MAX(date) FROM ranked) AND ret IS NOT NULL
         ORDER BY ret DESC LIMIT :n""",
        params={"n": n},
    )
    return [str(s) for s in df["symbol"].tolist()] if not df.empty else []


def _baseline_nifty_proxy_picks() -> list[str]:
    """Use a fixed basket of 5 NIFTY 50 mega-caps as the index proxy. Buying
    a single ETF is cleaner but proxies a simple 'long Nifty' default for
    the paper journal."""
    return ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"]


def _baseline_random_picks(n: int = 3) -> list[str]:
    """Random picks from the currently-liquid universe (matches model's
    ADV gate so cost assumptions are comparable)."""
    df = read_sql(
        """SELECT DISTINCT symbol FROM signals
            WHERE is_liquid = TRUE
              AND date = (SELECT MAX(date) FROM signals)"""
    )
    pool = df["symbol"].tolist() if not df.empty else []
    if len(pool) <= n:
        return pool
    return random.sample(pool, n)


def take_baseline_trades(horizon: str = "swing") -> dict:
    """Record one paper trade per baseline strategy for today.

    Called by daily_eod.py after model signals are generated. Each baseline
    picks its names independently of the model so the comparison is honest.
    """
    hzn = horizon.lower().strip()
    if hzn not in _HORIZON_SESSIONS:
        return {"ok": False, "error": f"unknown horizon {hzn}"}

    bps = _horizon_cost_bps(hzn, paper_default=True)
    today = date.today()
    out: dict[str, dict] = {}

    plans = {
        "baseline_top_momentum": _baseline_top_momentum_picks(3),
        "baseline_nifty":        _baseline_nifty_proxy_picks(),
        "baseline_random":       _baseline_random_picks(3),
    }

    for strat, picks in plans.items():
        if not picks:
            out[strat] = {"taken": 0, "skipped": "no_picks"}
            continue
        taken = 0
        errors: list[str] = []
        for sym in picks:
            entry = _latest_close(sym)
            if entry is None or entry <= 0:
                errors.append(f"{sym}:no_price")
                continue
            tgt, stp = _atr_levels(sym, entry, hzn, direction=1)
            try:
                execute_sql(
                    """INSERT INTO paper_trades
                         (symbol, horizon, entry_date, entry_price, direction,
                          target_price, stop_price, rank_pct, horizon_sessions,
                          cost_bps, strategy)
                       VALUES
                         (%(s)s, %(h)s, %(d)s, %(e)s, 1, %(t)s, %(st)s, NULL,
                          %(hs)s, %(c)s, %(strat)s)
                       ON CONFLICT (strategy, symbol, horizon, entry_date) DO NOTHING""",
                    {"s": sym, "h": hzn, "d": today, "e": entry,
                     "t": tgt, "st": stp, "hs": _HORIZON_SESSIONS[hzn],
                     "c": bps, "strat": strat},
                )
                taken += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sym}:{e}")
        out[strat] = {"taken": taken, "picks": picks,
                      "errors": errors if errors else None}

    return {"ok": True, "horizon": hzn, "as_of": today.isoformat(), "results": out}


# ── reconcile ───────────────────────────────────────────────────────

def _resolve_barrier_daily(prices: pd.DataFrame, entry_price: float,
                           target: Optional[float], stop: Optional[float],
                           direction: int) -> tuple[str, Optional[float], Optional[pd.Timestamp]]:
    """Walk daily bars chronologically; return (status, exit_price, exit_date).

    LONG: target hit if HIGH>=target; stop hit if LOW<=stop. SHORT mirrors.
    If a single bar hits BOTH, assume STOP first (pessimistic, honest).
    If neither across the window, TIME exit at the last bar's close.
    """
    for _, b in prices.iterrows():
        hi, lo, cl = float(b["high"]), float(b["low"]), float(b["close"])
        if direction > 0:
            hit_stop = stop is not None and lo <= stop
            hit_tgt  = target is not None and hi >= target
            if hit_stop:   # pessimistic: stop wins a same-bar tie
                return "LOSS", stop, b["date"]
            if hit_tgt:
                return "WIN", target, b["date"]
        else:
            hit_stop = stop is not None and hi >= stop
            hit_tgt  = target is not None and lo <= target
            if hit_stop:
                return "LOSS", stop, b["date"]
            if hit_tgt:
                return "WIN", target, b["date"]
    last = prices.iloc[-1]
    return "TIME", float(last["close"]), last["date"]


def reconcile_open_trades(asof: Optional[date] = None) -> dict:
    """Resolve every OPEN paper trade whose barrier window has elapsed.

    Returns counts {checked, closed, win, loss, time, still_open}.
    """
    asof = asof or date.today()
    open_df = read_sql(
        "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY entry_date"
    )
    if open_df.empty:
        return {"checked": 0, "closed": 0, "win": 0, "loss": 0, "time": 0, "still_open": 0}

    n_win = n_loss = n_time = n_open = 0
    for _, t in open_df.iterrows():
        sym = t["symbol"]
        entry_date = pd.to_datetime(t["entry_date"]).date()
        # Pull daily bars AFTER entry up to the horizon window (+slack for holidays).
        window = int(t["horizon_sessions"]) + 6
        bars = read_sql(
            """SELECT date, high, low, close FROM ohlcv
                WHERE symbol = :s AND date > :d
             ORDER BY date LIMIT :n""",
            params={"s": sym, "d": entry_date, "n": window},
        )
        # Not enough forward data yet -> leave OPEN.
        if bars.empty or len(bars) < int(t["horizon_sessions"]):
            n_open += 1
            continue
        bars["date"] = pd.to_datetime(bars["date"])
        bars = bars.head(int(t["horizon_sessions"]))   # only the barrier window

        status, exit_px, exit_dt = _resolve_barrier_daily(
            bars, float(t["entry_price"]),
            float(t["target_price"]) if pd.notna(t["target_price"]) else None,
            float(t["stop_price"]) if pd.notna(t["stop_price"]) else None,
            int(t["direction"]),
        )
        gross = (exit_px / float(t["entry_price"]) - 1.0) * int(t["direction"])
        net = gross - float(t["cost_bps"]) / 10000.0

        execute_sql(
            """UPDATE paper_trades
                  SET status = %(st)s, exit_date = %(xd)s, exit_price = %(xp)s,
                      realized_ret = %(g)s, realized_ret_net = %(n)s
                WHERE id = %(id)s""",
            {"st": status, "xd": exit_dt.date() if hasattr(exit_dt, "date") else exit_dt,
             "xp": float(exit_px), "g": float(gross), "n": float(net), "id": int(t["id"])},
        )
        if status == "WIN":   n_win += 1
        elif status == "LOSS": n_loss += 1
        else:                  n_time += 1

    closed = n_win + n_loss + n_time
    log.info("Paper reconcile: closed %d (win=%d loss=%d time=%d), %d still open",
             closed, n_win, n_loss, n_time, n_open)
    return {"checked": len(open_df), "closed": closed, "win": n_win,
            "loss": n_loss, "time": n_time, "still_open": n_open}


# ── track record ────────────────────────────────────────────────────

def track_record(horizon: Optional[str] = None,
                 strategy: Optional[str] = None) -> dict:
    """Live scorecard from closed paper trades, optionally filtered.

    Args:
        horizon:  intraday | swing | long. None = all horizons broken out.
        strategy: model | baseline_top_momentum | baseline_nifty | baseline_random.
                  None = breakout per strategy so model vs baselines is visible.
    """
    where = "WHERE status IN ('WIN','LOSS','TIME')"
    params: dict[str, object] = {}
    if horizon:
        where += " AND horizon = :h"
        params["h"] = horizon.lower()
    if strategy:
        where += " AND strategy = :strat"
        params["strat"] = strategy
    df = read_sql(f"SELECT * FROM paper_trades {where}", params=params)

    from qsde.models.edge_stats import horizon_edge

    def _block(sub: pd.DataFrame, hzn: Optional[str]) -> dict:
        n = len(sub)
        if n == 0:
            return {"n": 0, "note": "no closed paper trades yet"}
        rets = sub["realized_ret_net"].astype(float)
        wins = int((sub["status"] == "WIN").sum())
        hit = float((rets > 0).mean())
        mean, std = float(rets.mean()), float(rets.std())
        sessions = float(sub["horizon_sessions"].mean()) or 1.0
        sharpe = (mean / std * float(np.sqrt(252.0 / sessions))) if std > 0 and n >= 10 else None
        edge = horizon_edge(hzn) if hzn else None
        return {
            "n": n, "wins": wins, "losses": int((sub["status"] == "LOSS").sum()),
            "time_exits": int((sub["status"] == "TIME").sum()),
            "win_rate": round(hit, 3),
            "avg_net_ret_bps": round(mean * 1e4, 1),
            "realized_net_sharpe": round(sharpe, 2) if sharpe is not None else None,
            "backtested_edge_band": (edge or {}).get("edge_band") if edge else None,
            "note": None if n >= 10 else f"only {n} trades — need ~10+ for a meaningful Sharpe",
        }

    def _strat_split(sub: pd.DataFrame, hzn: Optional[str]) -> dict:
        """Break out the block by strategy so model vs baselines is visible."""
        if "strategy" not in sub.columns:
            return {"model": _block(sub, hzn)}
        out: dict[str, dict] = {}
        for strat in ("model", *_BASELINE_STRATEGIES):
            out[strat] = _block(sub[sub["strategy"] == strat], hzn)
        return out

    if strategy and horizon:
        return {"horizon": horizon, "strategy": strategy, **_block(df, horizon.lower())}
    if strategy:
        out: dict[str, object] = {"strategy": strategy,
                                  "overall": _block(df, None)}
        for h in ("intraday", "swing", "long"):
            out[h] = _block(df[df["horizon"] == h], h)
        return out

    # No strategy filter -> always break out model vs baselines so the
    # comparison is visible in one call.
    out = {"overall": _strat_split(df, None)}
    for h in ("intraday", "swing", "long"):
        out[h] = _strat_split(df[df["horizon"] == h], h)
    return out
