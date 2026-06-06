"""
Live intraday signal loop (Phase 2 streaming wrapper).

Wraps the pure decision core (qsde/live/intraday_signal.generate_intraday_signal)
in a background loop that, once per closed minute bar, for each tracked symbol:

    1. loads the session-to-date 1-minute bars from `ohlcv_intraday`,
    2. runs the white-box intraday signal (entry/stop/target + reasons),
    3. publishes to:
         • a process-wide SignalFanout  -> consumed by the SSE route
           (api/routes/live_signals.py) for the live dashboard,
         • a structured JSON log line   -> always-on audit trail with precise
           timestamps (backtest/headless/debug),
         • Telegram (@Stoxsybot)         -> only on *actionable* changes, so the
           channel isn't spammed every minute.

Decoupling: the loop reads completed bars from the DB rather than tapping the
tick stream directly, so it never blocks the Kite WebSocket thread and stays
correct for illiquid symbols (whose bars are flushed by PeriodicFlusher).

Run it either:
  • in-process with the API (POST /api/live/start) so SSE clients get ticks, or
  • standalone via scripts/run_signal_loop.py (logs + Telegram only).

The pure helpers (emit/alert decisions, formatting) are unit-tested; the loop
I/O needs a live DB + bars to exercise end-to-end.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import pandas as pd

from qsde.ingestion.tick_fanout import TickFanout
from qsde.factors.intraday_microstructure import _session_key
from qsde.live.intraday_signal import IntradaySignal, generate_intraday_signal

log = logging.getLogger(__name__)
signal_log = logging.getLogger("qsde.signals")  # route to its own file/handler if desired

_IST = "Asia/Kolkata"
_MIN_BARS = 15  # need enough bars for ATR(14) + microstructure warmup


# ============================================================
# Signal fanout (reuse the generic TickFanout pub/sub)
# ============================================================
_SIGNAL_FANOUT: Optional[TickFanout] = None


def get_signal_fanout() -> TickFanout:
    """Process-wide fanout carrying signal dicts (separate from the tick one)."""
    global _SIGNAL_FANOUT
    if _SIGNAL_FANOUT is None:
        _SIGNAL_FANOUT = TickFanout()
    return _SIGNAL_FANOUT


# ============================================================
# Pure helpers (unit-tested)
# ============================================================
def is_new_bar(prev: Optional[dict], new: IntradaySignal) -> bool:
    """Emit to fanout/log once per fresh bar (ts advanced) or on first sight."""
    if prev is None:
        return True
    return prev.get("ts") != new.ts


def should_alert(sig: IntradaySignal) -> bool:
    """Telegram-worthy on its own merits: an actionable, good-quality trade."""
    return sig.action in ("BUY", "SELL") and sig.quality == "good"


def alert_worthy(prev: Optional[dict], new: IntradaySignal) -> bool:
    """Alert only when the *call* meaningfully changes, to avoid per-minute spam."""
    if not should_alert(new):
        return False
    if prev is None:
        return True
    return prev.get("action") != new.action or prev.get("direction") != new.direction


def signal_log_line(sig: IntradaySignal) -> str:
    """One JSON line per emission — the always-on, timestamp-precise audit trail."""
    return json.dumps(sig.to_dict(), default=str)


def build_telegram_alert(sig: IntradaySignal) -> str:
    """HTML message for @Stoxsybot."""
    arrow = "🟢 BUY" if sig.direction > 0 else "🔴 SELL" if sig.direction < 0 else "⚪ WATCH"
    levels = ""
    if sig.entry is not None and sig.stop is not None and sig.target is not None:
        levels = (
            f"Entry <b>{sig.entry}</b> | SL <b>{sig.stop}</b> | "
            f"Target <b>{sig.target}</b> | R:R {sig.risk_reward}\n"
        )
    reasons = "\n".join(f"  • {r}" for r in sig.reasons[:5])
    return (
        f"<b>⚡ QSDE Intraday — {sig.symbol}</b>  {arrow}  ({sig.horizon})\n"
        f"Price {sig.price} | conf {sig.confidence:.0%} | bias {sig.bias:+.2f}\n"
        f"{levels}\n{reasons}\n<code>{sig.ts}</code>"
    )


# ============================================================
# Session bar loader
# ============================================================
def load_session_bars(symbol: str, lookback: int = 500, tz: str = _IST) -> pd.DataFrame:
    """Latest session-to-date 1-minute bars for `symbol` from ohlcv_intraday.

    Pulls the last `lookback` bars (a session is ~375 1-min bars) and keeps only
    the most recent trading session, so anchored-VWAP / volume-profile are
    correctly session-anchored.
    """
    from qsde.db.connection import read_sql  # lazy: avoid DB import at module load

    df = read_sql(
        """SELECT ts, open, high, low, close, volume
             FROM ohlcv_intraday
            WHERE symbol = :sym
         ORDER BY ts DESC
            LIMIT :lim""",
        params={"sym": symbol.upper(), "lim": lookback},
    )
    if df.empty:
        return df
    df = df.iloc[::-1]  # ascending
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts")
    key = _session_key(df, tz)
    return df[key == key.iloc[-1]]


# ============================================================
# The loop
# ============================================================
class SignalLoop(threading.Thread):
    """Periodic signal generator. Wakes a few seconds after each minute close."""

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        horizon: str = "intraday",
        interval_sec: float = 60.0,
        post_close_delay: float = 3.0,
        emit_telegram: bool = True,
    ) -> None:
        super().__init__(daemon=True, name="SignalLoop")
        self.symbols = [s.upper() for s in symbols]
        self.horizon = horizon
        self.interval = float(interval_sec)
        self.post_close_delay = float(post_close_delay)
        self.emit_telegram = emit_telegram
        self._stop = threading.Event()
        self._last: dict[str, dict] = {}
        self._fanout = get_signal_fanout()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("SignalLoop started for %d symbols (horizon=%s).", len(self.symbols), self.horizon)
        # First pass immediately so a freshly started loop emits without waiting a full minute.
        self._scan_once()
        while not self._stop.is_set():
            now = time.time()
            sleep_s = (self.interval - (now % self.interval)) + self.post_close_delay
            if self._stop.wait(sleep_s):
                break
            self._scan_once()
        log.info("SignalLoop stopped.")

    def _scan_once(self) -> None:
        for sym in self.symbols:
            try:
                self._scan_symbol(sym)
            except Exception as e:  # one bad symbol must not kill the loop
                log.warning("SignalLoop: %s failed: %s", sym, e)

    def _scan_symbol(self, sym: str) -> None:
        bars = load_session_bars(sym)
        if bars is None or len(bars) < _MIN_BARS:
            return
        sig = generate_intraday_signal(bars, symbol=sym, horizon=self.horizon)
        prev = self._last.get(sym)
        if is_new_bar(prev, sig):
            payload = sig.to_dict()
            payload["_type"] = "signal"
            payload["emitted_at"] = datetime.now(timezone.utc).isoformat()
            self._fanout.publish(payload)
            signal_log.info(signal_log_line(sig))
            if self.emit_telegram and alert_worthy(prev, sig):
                self._send_telegram(sig)
        self._last[sym] = sig.to_dict()

    @staticmethod
    def _send_telegram(sig: IntradaySignal) -> None:
        try:
            from qsde.notifications.telegram import send_message_sync
            send_message_sync(build_telegram_alert(sig))
        except Exception as e:
            log.warning("Telegram alert failed for %s: %s", sig.symbol, e)


# Module-level handle so the API can start/stop a single loop in-process.
_LOOP: Optional[SignalLoop] = None


def start_signal_loop(symbols: Iterable[str], **kwargs) -> SignalLoop:
    """Start (or restart) the in-process signal loop. Returns the running loop."""
    global _LOOP
    if _LOOP is not None and _LOOP.is_alive():
        _LOOP.stop()
    _LOOP = SignalLoop(symbols, **kwargs)
    _LOOP.start()
    return _LOOP


def stop_signal_loop() -> bool:
    global _LOOP
    if _LOOP is not None and _LOOP.is_alive():
        _LOOP.stop()
        return True
    return False


def loop_status() -> dict:
    alive = _LOOP is not None and _LOOP.is_alive()
    return {
        "running": alive,
        "symbols": _LOOP.symbols if alive else [],
        "horizon": _LOOP.horizon if alive else None,
        "subscribers": get_signal_fanout().n_subscribers(),
    }


def latest_signals() -> list[dict]:
    """Most recent signal dict per symbol from the running loop.

    Used by the budget screener as a candidate source when the caller doesn't
    pass an explicit list. Empty if no loop is running.
    """
    return list(_LOOP._last.values()) if _LOOP is not None else []


__all__ = [
    "get_signal_fanout",
    "SignalLoop",
    "start_signal_loop",
    "stop_signal_loop",
    "loop_status",
    "latest_signals",
    "load_session_bars",
    "is_new_bar",
    "should_alert",
    "alert_worthy",
    "signal_log_line",
    "build_telegram_alert",
]
