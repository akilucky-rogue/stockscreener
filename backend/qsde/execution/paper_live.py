"""Live paper-trade tracker — assembles everything the /paper/{id}/live page needs.

Loads a single paper trade, walks forward through OHLCV (intraday for the
intraday horizon, daily for swing/long), builds an equal-weighted NIFTY 50
benchmark series for the same window, computes derived stats (MFE, MAE,
current PnL, delta-vs-benchmark, time elapsed/remaining), and joins back
to signals to pull the model's predicted_return + confidence so the UI can
show "what we expected" alongside "what's actually happening".

Pure backend — no I/O outside the DB. The frontend page renders the JSON
shape returned by `build_live_payload(trade_id)`.

Why this exists
---------------
136 open paper trades and counting. Without a live tracker, those are
abstract rows in a database. With it, the trader can stare at one trade
and ask "is reality matching what the model thought would happen?" — which
is the only honest test of the system.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from qsde.db.connection import read_sql

log = logging.getLogger(__name__)


# Horizon -> number of NSE trading sessions for the barrier window.
HORIZON_SESSIONS = {"intraday": 1, "swing": 5, "long": 20}


# ──────────────────────────────────────────────────────────────────────
# Trade lookup
# ──────────────────────────────────────────────────────────────────────

def _load_trade(trade_id: int) -> Optional[dict]:
    """Load a single paper_trade row by id."""
    df = read_sql(
        """
        SELECT id, symbol, horizon, taken_at, entry_date, entry_price, direction,
               target_price, stop_price, rank_pct, horizon_sessions, cost_bps,
               status, exit_date, exit_price, realized_ret, realized_ret_net,
               strategy, notes
          FROM paper_trades
         WHERE id = :id
        """,
        params={"id": trade_id},
    )
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    # Convert pandas/numpy types to JSON-friendly natives.
    for k in ("entry_date", "exit_date"):
        v = row.get(k)
        if v is not None and not pd.isna(v):
            row[k] = pd.to_datetime(v).date().isoformat()
        else:
            row[k] = None
    if row.get("taken_at") is not None and not pd.isna(row.get("taken_at")):
        row["taken_at"] = pd.to_datetime(row["taken_at"]).isoformat()
    else:
        row["taken_at"] = None
    for k in ("entry_price", "target_price", "stop_price", "exit_price",
              "rank_pct", "cost_bps", "realized_ret", "realized_ret_net"):
        v = row.get(k)
        row[k] = None if (v is None or pd.isna(v)) else float(v)
    for k in ("direction", "horizon_sessions", "id"):
        v = row.get(k)
        row[k] = None if (v is None or pd.isna(v)) else int(v)
    return row


# ──────────────────────────────────────────────────────────────────────
# OHLCV loaders — adapt granularity to the horizon
# ──────────────────────────────────────────────────────────────────────

def _load_stock_candles(
    symbol: str,
    horizon: str,
    entry_date: str,
    exit_date: Optional[str] = None,
) -> list[dict]:
    """Return candle rows in the shape lightweight-charts wants.

    For intraday: ohlcv_intraday (5-min bars).
    For swing / long: daily ohlcv.

    Times are unix seconds (UTC). lightweight-charts accepts either an
    epoch-second number (for intraday) or a YYYY-MM-DD string (for daily).
    We use one or the other to match the chart's expected timescale.
    """
    if horizon == "intraday":
        df = read_sql(
            """
            SELECT ts, open, high, low, close, volume
              FROM ohlcv_intraday
             WHERE symbol = :s
               AND ts >= :since
             ORDER BY ts
            """,
            params={"s": symbol, "since": entry_date},
        )
        if df.empty:
            return []
        return [
            {
                "time": int(pd.to_datetime(r["ts"]).timestamp()),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]) if r["volume"] is not None else 0,
            }
            for _, r in df.iterrows()
        ]

    # Daily for swing / long. We want a small lead-in (5 sessions BEFORE entry)
    # so the chart shows what the price was doing leading up to the trade.
    entry = pd.to_datetime(entry_date).date()
    lead_in = entry - timedelta(days=10)
    df = read_sql(
        """
        SELECT date, open, high, low, close, volume
          FROM ohlcv
         WHERE symbol = :s
           AND date  >= :since
         ORDER BY date
        """,
        params={"s": symbol, "since": lead_in},
    )
    if df.empty:
        return []
    return [
        {
            "time": pd.to_datetime(r["date"]).date().isoformat(),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["volume"]) if r["volume"] is not None else 0,
        }
        for _, r in df.iterrows()
    ]


def _load_benchmark_line(
    horizon: str,
    entry_date: str,
) -> tuple[list[dict], str]:
    """Equal-weighted NIFTY 50 close series over the same window.

    Returns (line points, benchmark name). Line shape matches
    lightweight-charts LineSeries: [{time, value}, ...].

    Synthesized on demand from NIFTY 50 constituents' OHLCV — we don't
    have a separate index quote in the DB. Same approach as
    qsde.research.rule_engine.build_benchmark_series, but only needs
    the close series.
    """
    benchmark_name = "NIFTY50_EQ"

    # Load NIFTY 50 constituents from the universe table.
    members = read_sql(
        """
        SELECT symbol
          FROM universe
         WHERE is_active = TRUE
           AND index_membership @> '["NIFTY 50"]'::jsonb
        """
    )
    if members.empty:
        return [], benchmark_name
    symbols = [str(s) for s in members["symbol"].tolist()]
    if not symbols:
        return [], benchmark_name

    entry = pd.to_datetime(entry_date).date()
    lead_in = entry - timedelta(days=10 if horizon != "intraday" else 1)
    sym_list = ",".join(f"'{s}'" for s in symbols)
    closes = read_sql(
        f"""
        SELECT symbol, date, close
          FROM ohlcv
         WHERE symbol IN ({sym_list})
           AND date >= :since
         ORDER BY date, symbol
        """,
        params={"since": lead_in},
    )
    if closes.empty:
        return [], benchmark_name

    closes["date"] = pd.to_datetime(closes["date"])
    closes["close"] = closes["close"].astype(float)
    wide = closes.pivot(index="date", columns="symbol", values="close").sort_index()
    rets = wide.pct_change(fill_method=None)
    avg_ret = rets.mean(axis=1)
    synth_level = (1.0 + avg_ret.fillna(0)).cumprod() * 100.0

    points = [
        {"time": d.date().isoformat() if horizon != "intraday"
                 else int(d.timestamp()),
         "value": float(v)}
        for d, v in synth_level.items()
        if pd.notna(v)
    ]
    return points, benchmark_name


# ──────────────────────────────────────────────────────────────────────
# Stats: MFE, MAE, current PnL, vs-benchmark
# ──────────────────────────────────────────────────────────────────────

def _compute_stats(
    trade: dict,
    candles: list[dict],
    benchmark_points: list[dict],
) -> dict:
    """Derived diagnostics for the side panel."""
    if not candles:
        return {
            "current_price": None,
            "current_pnl_pct": None,
            "current_pnl_bps": None,
            "mfe": None,
            "mae": None,
            "benchmark_ret": None,
            "delta_vs_benchmark": None,
            "sessions_elapsed": 0,
            "sessions_remaining": int(trade.get("horizon_sessions") or 0),
        }

    entry_price = float(trade["entry_price"])
    direction = int(trade["direction"]) or 1
    horizon_sessions = int(trade.get("horizon_sessions") or 0)

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    last_close = closes[-1]

    # Direction-aware P&L: long -> (last - entry); short -> (entry - last).
    current_pnl_pct = direction * (last_close - entry_price) / entry_price
    # Direction-aware MFE/MAE:
    #   LONG  MFE = (max high - entry)/entry,  MAE = (min low - entry)/entry
    #   SHORT MFE = (entry - min low)/entry,   MAE = (entry - max high)/entry
    if direction > 0:
        mfe = (max(highs) - entry_price) / entry_price
        mae = (min(lows) - entry_price) / entry_price
    else:
        mfe = (entry_price - min(lows)) / entry_price
        mae = (entry_price - max(highs)) / entry_price

    # Time elapsed = number of candles after entry (lead-in candles ignored).
    entry_date = trade.get("entry_date")
    if entry_date and trade["horizon"] != "intraday":
        # Daily case: count daily candles strictly after entry_date.
        post_entry = [c for c in candles
                      if isinstance(c.get("time"), str) and c["time"] > entry_date]
        sessions_elapsed = len(post_entry)
    else:
        # Intraday case: rough — one "session" = one trading day. We can't
        # do better without a session boundary table, so treat any data
        # after entry as in-session.
        sessions_elapsed = 1 if len(candles) > 0 else 0
    sessions_remaining = max(0, horizon_sessions - sessions_elapsed)

    # Benchmark delta: change in benchmark over the same window.
    benchmark_ret = None
    delta_vs_benchmark = None
    if benchmark_points:
        bm_first = benchmark_points[0]["value"]
        bm_last = benchmark_points[-1]["value"]
        if bm_first > 0:
            benchmark_ret = (bm_last - bm_first) / bm_first
            delta_vs_benchmark = current_pnl_pct - benchmark_ret

    return {
        "current_price":      float(last_close),
        "current_pnl_pct":    round(current_pnl_pct, 6),
        "current_pnl_bps":    round(current_pnl_pct * 1e4, 1),
        "mfe":                round(mfe, 6),
        "mae":                round(mae, 6),
        "benchmark_ret":      None if benchmark_ret is None else round(benchmark_ret, 6),
        "delta_vs_benchmark": None if delta_vs_benchmark is None else round(delta_vs_benchmark, 6),
        "sessions_elapsed":   int(sessions_elapsed),
        "sessions_remaining": int(sessions_remaining),
    }


# ──────────────────────────────────────────────────────────────────────
# Pull expected return + confidence from the originating signal
# ──────────────────────────────────────────────────────────────────────

def _load_expected(trade: dict) -> dict:
    """Best-effort lookup of the signal that triggered this paper trade.

    Joins by (strategy, symbol, horizon, date <= entry_date) — picks the
    most recent matching signal. Returns predicted_return, confidence,
    ranking_score, atr_pct so the UI can show "model thought X" alongside
    the realized outcome.
    """
    df = read_sql(
        """
        SELECT predicted_return, confidence, ranking_score, atr_pct, top_factors
          FROM signals
         WHERE strategy = :st
           AND symbol   = :sy
           AND horizon  = :hz
           AND date    <= :d
         ORDER BY date DESC
         LIMIT 1
        """,
        params={
            "st": trade.get("strategy") or "model",
            "sy": trade["symbol"],
            "hz": trade["horizon"],
            "d":  trade["entry_date"],
        },
    )
    if df.empty:
        return {"predicted_return": None, "confidence": None,
                "ranking_score": None, "atr_pct": None, "top_factors": None}
    r = df.iloc[0]
    def _f(v):
        return None if (v is None or pd.isna(v)) else float(v)
    return {
        "predicted_return": _f(r["predicted_return"]),
        "confidence":       _f(r["confidence"]),
        "ranking_score":    _f(r["ranking_score"]),
        "atr_pct":          _f(r["atr_pct"]),
        "top_factors":      r["top_factors"],
    }


# ──────────────────────────────────────────────────────────────────────
# Public entry point — what the route handler calls
# ──────────────────────────────────────────────────────────────────────

def build_live_payload(trade_id: int) -> Optional[dict]:
    """Assemble the full /paper/{id}/live response.

    Returns None if the trade doesn't exist; the route maps that to a 404.
    """
    trade = _load_trade(trade_id)
    if trade is None:
        return None

    candles = _load_stock_candles(
        symbol=trade["symbol"],
        horizon=trade["horizon"],
        entry_date=trade["entry_date"],
        exit_date=trade["exit_date"],
    )
    benchmark_points, benchmark_name = _load_benchmark_line(
        horizon=trade["horizon"],
        entry_date=trade["entry_date"],
    )
    stats = _compute_stats(trade, candles, benchmark_points)
    expected = _load_expected(trade)

    return {
        "trade":             trade,
        "stock_candles":     candles,
        "benchmark":         {"name": benchmark_name, "points": benchmark_points},
        "stats":             stats,
        "expected":          expected,
    }


__all__ = ["build_live_payload", "HORIZON_SESSIONS"]
