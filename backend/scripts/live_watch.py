"""
LIVE "when to buy/sell" terminal feed — stateful, event-triggered edition.

The daily ML cards are your morning WATCHLIST ("which stocks look good today").
This is the LIVE half. But unlike a naive "is price above VWAP" indicator (which
is 'actionable' every single minute and would whipsaw you), this feed is a small
STATE MACHINE that mirrors how you'd actually trade:

  WAIT   — the default. No clean setup right now. Most names sit here.
  BUY /  — a *fresh trigger this bar*: a VWAP reclaim or a liquidity-sweep
  SELL     reclaim, confirmed by the white-box bias. Entry/stop/target are
           anchored to real structure (swing low / value-area), cost-gated.
  HOLD   — you're in the trade. Levels are FIXED at entry (they do NOT re-quote);
           we show live P&L and hold until target or stop.
  EXIT   — target hit (win) or stop hit (loss) — printed once, then back to WAIT.

So a quiet tape shows mostly WAIT; a real reclaim shows ONE BUY, then HOLD, then
EXIT. That is the precise entry/exit timing you asked for — and it's honest about
how rarely a clean setup actually appears.

It auto-pulls the watchlist from today's top-ranked liquid intraday signals.
Override with --symbols for specific names.

PREREQS (during market hours):
  1. Kite logged in (http://localhost:8000/api/kite/login_url)
  2. scripts/kite_stream.py running  (populates ohlcv_intraday with live bars)

USAGE:
  python scripts/live_watch.py                  # auto watchlist, top 10
  python scripts/live_watch.py --top 15
  python scripts/live_watch.py --symbols KEI,RELIANCE,TCS
  python scripts/live_watch.py --interval 60    # refresh cadence (s)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Enable ANSI colors on Windows terminals + force UTF-8 so output never
# crashes on a legacy cp1252 console.
os.system("")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from qsde.db.connection import read_sql
from qsde.factors.intraday_microstructure import compute_intraday_microstructure
from qsde.live.engine import load_session_bars
from qsde.live.intraday_signal import generate_intraday_signal

# ── ANSI ────────────────────────────────────────────────────────────
RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN = "\033[92m"; RED = "\033[91m"; CYAN = "\033[96m"; AMBER = "\033[93m"; GREY = "\033[90m"
CLEAR = "\033[2J\033[H"

_MIN_BARS = 15          # ATR(14) + microstructure warmup
_BUY_TH = 0.35          # |bias| confirmation, matches the white-box core
_MIN_STOP_FRAC = 0.0025 # >= ~2x round-trip cost so a stop isn't pure noise
_MAX_STOP_FRAC = 0.010  # cap intraday risk at 1%
_RR = 2.0               # target = entry +/- RR * risk
_SWING = 10             # bars for the structural swing high/low
_COST_FRAC = 0.0012     # ~12 bps NSE round-trip (brokerage+STT+slippage), liquid
_MIN_EDGE_MULT = 3.0    # target move must clear >= 3x cost to be worth firing


# ============================================================
# Per-symbol state: one open position + last-processed bar.
# ============================================================
@dataclass
class _Pos:
    side: int            # +1 long / -1 short
    entry: float
    stop: float
    target: float
    ts: str
    bars_held: int = 0


@dataclass
class _State:
    pos: Optional[_Pos] = None
    last_ts: Optional[str] = None


_STATES: dict[str, _State] = {}


def _watchlist(top: int) -> list[str]:
    """Today's top-ranked liquid intraday names = the watchlist."""
    df = read_sql(
        """SELECT symbol
             FROM signals
            WHERE horizon = 'intraday' AND is_liquid = TRUE
              AND date = (SELECT MAX(date) FROM signals WHERE horizon = 'intraday')
         ORDER BY ranking_score DESC
            LIMIT :n""",
        params={"n": top},
    )
    return df["symbol"].tolist()


def _is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= mins < (15 * 60 + 30)


def _fmt(v, dash="—"):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return dash


def _safe(row, key, default=np.nan) -> float:
    try:
        return float(row.get(key, default))
    except (TypeError, ValueError):
        return default


def _long_levels(c: float, bars, m) -> tuple[float, float, float]:
    """Entry/stop/target for a long, anchored to structure below price."""
    swing_low = float(bars["low"].iloc[-_SWING:].min())
    val = _safe(m, "intraday_vp_val")
    lower = _safe(m, "intraday_avwap_lower")
    supports = [x for x in (swing_low, val, lower) if np.isfinite(x) and 0 < x < c]
    support = max(supports) if supports else c * (1 - _MAX_STOP_FRAC)
    dist = min(max(c - support, _MIN_STOP_FRAC * c), _MAX_STOP_FRAC * c)
    return c, c - dist, c + _RR * dist


def _short_levels(c: float, bars, m) -> tuple[float, float, float]:
    """Entry/stop/target for a short, anchored to structure above price."""
    swing_high = float(bars["high"].iloc[-_SWING:].max())
    vah = _safe(m, "intraday_vp_vah")
    upper = _safe(m, "intraday_avwap_upper")
    resists = [x for x in (swing_high, vah, upper) if np.isfinite(x) and x > c > 0]
    resist = min(resists) if resists else c * (1 + _MAX_STOP_FRAC)
    dist = min(max(resist - c, _MIN_STOP_FRAC * c), _MAX_STOP_FRAC * c)
    return c, c + dist, c - _RR * dist


def _cost_ok(entry: float, target: float) -> bool:
    """Suppress setups whose target move can't clear a multiple of costs."""
    return abs(target - entry) / max(entry, 1e-9) >= _MIN_EDGE_MULT * _COST_FRAC


# ============================================================
# Pure decision core (trigger + position lifecycle) — DB-free, unit-tested.
# ============================================================
def decide(bars, micro, sig, st: _State) -> dict:
    """Advance one symbol's state machine for its latest closed bar.

    Pure given lowercased minute `bars`, their microstructure frame `micro`,
    the white-box `sig` (only `.bias` is read), and the persistent `_State`.
    Returns a display row (no symbol label). This is the heart of the feed and
    is exercised directly by tests/test_live_watch_state.py without a DB.
    """
    c = float(bars["close"].iloc[-1])
    c_prev = float(bars["close"].iloc[-2])
    m = micro.iloc[-1]
    a = _safe(m, "intraday_avwap")
    a_prev = _safe(micro.iloc[-2], "intraday_avwap")
    ts = str(bars.index[-1])

    above = np.isfinite(a) and c > a
    above_prev = np.isfinite(a_prev) and c_prev > a_prev
    crossed_up = (not above_prev) and above
    crossed_down = above_prev and (not above)
    sweep_lo = _safe(m, "intraday_sweep_low_reclaim", 0.0) >= 1
    sweep_hi = _safe(m, "intraday_sweep_high_reject", 0.0) >= 1
    bias = sig.bias

    long_trig = (crossed_up or sweep_lo) and bias >= _BUY_TH
    short_trig = (crossed_down or sweep_hi) and bias <= -_BUY_TH

    new_bar = ts != st.last_ts
    row = {"last": c, "bias": bias}

    # --- lifecycle advances only once per freshly-closed bar ---
    if new_bar:
        st.last_ts = ts
        if st.pos is None:
            if long_trig:
                e, s, t = _long_levels(c, bars, m)
                if _cost_ok(e, t):
                    st.pos = _Pos(+1, e, s, t, ts)
                    why = "swept lows + reclaimed VWAP" if sweep_lo else "reclaimed VWAP"
                    row.update(kind="enter", side=+1, pos=st.pos,
                               why=f"{why}, bias {bias:+.2f}")
                    return row
            elif short_trig:
                e, s, t = _short_levels(c, bars, m)
                if _cost_ok(e, t):
                    st.pos = _Pos(-1, e, s, t, ts)
                    why = "swept highs + lost VWAP" if sweep_hi else "lost VWAP"
                    row.update(kind="enter", side=-1, pos=st.pos,
                               why=f"{why}, bias {bias:+.2f}")
                    return row
        else:
            p = st.pos
            p.bars_held += 1
            hit_stop = c <= p.stop if p.side > 0 else c >= p.stop
            hit_tgt = c >= p.target if p.side > 0 else c <= p.target
            if hit_stop or hit_tgt:
                pnl = (c - p.entry) / p.entry * 100 * p.side
                kind = "exit_win" if hit_tgt else "exit_loss"
                st.pos = None
                row.update(kind=kind, side=p.side, pos=p, pnl=pnl,
                           why=f"{'target' if hit_tgt else 'stop'} hit, "
                               f"closed {pnl:+.2f}%")
                return row

    # --- no lifecycle change: render current state ---
    if st.pos is not None:
        p = st.pos
        pnl = (c - p.entry) / p.entry * 100 * p.side
        row.update(kind="hold", side=p.side, pos=p, pnl=pnl,
                   why=f"P&L {pnl:+.2f}%, held {p.bars_held}m")
    else:
        side_txt = "above VWAP" if above else "below VWAP"
        row.update(kind="wait", why=f"{side_txt}, no trigger (bias {bias:+.2f})")
    return row


# ============================================================
# Evaluate one symbol -> a display row (loads bars, then `decide`).
# ============================================================
def _evaluate(sym: str) -> dict:
    try:
        bars = load_session_bars(sym)
    except Exception:
        bars = None
    if bars is None or len(bars) < _MIN_BARS:
        return {"sym": sym, "kind": "warming"}

    bars = bars.rename(columns=str.lower)
    micro = compute_intraday_microstructure(bars)
    sig = generate_intraday_signal(bars, micro=micro, symbol=sym, horizon="intraday")
    st = _STATES.setdefault(sym, _State())
    row = decide(bars, micro, sig, st)
    row["sym"] = sym
    return row


# ============================================================
# Render
# ============================================================
def _sig_cell(row: dict, width: int = 12) -> str:
    k = row["kind"]
    label, color = {
        "enter":    ("> BUY" if row.get("side", 0) > 0 else "> SELL",
                     GREEN if row.get("side", 0) > 0 else RED),
        "hold":     ("= HOLD L" if row.get("side", 0) > 0 else "= HOLD S", CYAN),
        "exit_win": (f"x +{abs(row.get('pnl', 0)):.2f}%", GREEN),
        "exit_loss": (f"x -{abs(row.get('pnl', 0)):.2f}%", RED),
        "wait":     (". WAIT", GREY),
        "warming":  ("...bars", GREY),
    }.get(k, (". WAIT", GREY))
    return f"{color}{BOLD if k in ('enter',) else ''}{label:<{width}}{RESET}"


def _render(symbols: list[str]) -> None:
    rows = [_evaluate(s) for s in symbols]

    open_txt = f"{GREEN}* MARKET OPEN{RESET}" if _is_market_open() else f"{AMBER}o MARKET CLOSED{RESET}"
    print(CLEAR, end="")
    print(f"{BOLD}{CYAN}QSDE - LIVE WHEN-TO-TRADE{RESET}   "
          f"{datetime.now():%Y-%m-%d %H:%M:%S} IST   {open_txt}   "
          f"{DIM}refreshes every bar - Ctrl-C to stop{RESET}")
    print(f"{DIM}WAIT = default - BUY/SELL = fresh trigger THIS bar - "
          f"HOLD = in trade (levels fixed) until target/stop - EXIT = closed{RESET}")
    print("-" * 104)
    print(f"{BOLD}{'SYMBOL':<12}{'LAST':>9}  {'SIGNAL':<12}{'ENTRY':>9}{'TARGET':>9}"
          f"{'STOP':>9}{'R:R':>6}  WHY{RESET}")
    print("-" * 104)

    n_trade = n_new = n_wait = 0
    for row in rows:
        sym = row["sym"]
        if row["kind"] == "warming":
            print(f"{sym:<12}{'-':>9}  {GREY}...waiting for bars{RESET}")
            continue
        last = _fmt(row.get("last"))
        pos = row.get("pos")
        if pos is not None and row["kind"] in ("enter", "hold", "exit_win", "exit_loss"):
            entry, target, stop = _fmt(pos.entry), _fmt(pos.target), _fmt(pos.stop)
            rr = f"{_RR:.2f}"
        else:
            entry = target = stop = "—"
            rr = "-"
        if row["kind"] in ("enter",):
            n_new += 1; n_trade += 1
        elif row["kind"] == "hold":
            n_trade += 1
        elif row["kind"] == "wait":
            n_wait += 1

        line = (f"{BOLD}{sym:<12}{RESET}{last:>9}  {_sig_cell(row)}"
                f"{entry:>9}{target:>9}{stop:>9}{rr:>6}  {DIM}{row['why'][:46]}{RESET}")
        print(line)

    print("-" * 104)
    print(f"{DIM}{len(rows)} watched - {n_trade} in trade ({n_new} new this bar) - "
          f"{n_wait} waiting. White-box, cost-gated. Paper-trade before real capital.{RESET}")
    if not _is_market_open():
        print(f"{AMBER}Market is closed - levels are last-session snapshots. "
              f"Live triggers resume 09:15 IST with kite_stream.py running.{RESET}")


def main() -> None:
    ap = argparse.ArgumentParser(description="QSDE live when-to-trade feed")
    ap.add_argument("--symbols", default=None, help="Comma-separated NSE symbols (default: auto watchlist)")
    ap.add_argument("--top", type=int, default=10, help="Watchlist size when auto (default 10)")
    ap.add_argument("--interval", type=float, default=60.0, help="Refresh seconds (default 60)")
    ap.add_argument("--once", action="store_true", help="Render one frame and exit (for testing).")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = _watchlist(args.top)
        if not symbols:
            print("No watchlist found — generate signals first "
                  "(scripts/daily_eod.py or run_pipeline.py).")
            sys.exit(1)

    if args.once:
        _render(symbols)
        return
    try:
        while True:
            _render(symbols)
            # Align to the next minute boundary + 3s so we read freshly-closed bars.
            now = time.time()
            time.sleep((args.interval - (now % args.interval)) + 3.0)
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")


if __name__ == "__main__":
    main()
