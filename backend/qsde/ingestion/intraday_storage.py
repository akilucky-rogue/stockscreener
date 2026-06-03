"""
Minute-bar aggregator + raw-tick logger.

Subscribes to the TickFanout, buckets each tick into its 1-minute window
keyed by (symbol, minute_start), and on the bar's completion (next-minute
tick OR explicit flush) upserts the bar into `ohlcv_intraday`.

Two background threads:

  1. MinuteBarAggregator -- consumes ticks, maintains in-memory bar state,
     flushes completed bars to ohlcv_intraday. Also writes raw ticks to
     `ticks_raw` (rate-limited batch) for replay/debug.

  2. PeriodicFlusher -- once per minute, force-flush any "stale" bars whose
     minute window has elapsed but no tick has arrived for the next bar.
     Important for illiquid symbols.

Both are clean-shutdown-aware via a threading.Event.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from qsde.db.connection import get_sync_conn
from qsde.ingestion.tick_fanout import get_fanout

log = logging.getLogger(__name__)


def _floor_to_minute(ts: datetime) -> datetime:
    """Return the start-of-minute timestamp for `ts` (UTC, tz-aware)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.replace(second=0, microsecond=0)


@dataclass
class _BarState:
    """In-memory state for a single (symbol, minute) bar being built."""
    symbol:     str
    bucket:     datetime         # start-of-minute
    open_px:    float = 0.0
    high_px:    float = 0.0
    low_px:     float = 0.0
    close_px:   float = 0.0
    volume:     int   = 0
    pv_sum:     float = 0.0      # for VWAP: sum(price * delta_volume)
    vol_sum:    int   = 0        # denominator for VWAP
    last_cumulative_volume: int = 0
    n_ticks:    int   = 0


class MinuteBarAggregator(threading.Thread):
    """Background thread that reads ticks from a fanout queue and emits
    1-minute OHLCV bars to ohlcv_intraday.

    Volume handling note: Kite ticks carry `volume_traded` which is the
    CUMULATIVE intraday volume, not the per-tick delta. So per tick we
    compute `delta_vol = current_cumulative - previous_cumulative` and
    accumulate that into the bar. First tick of the day for a symbol
    sets the baseline; we skip its delta to avoid double-counting.
    """

    def __init__(self, log_raw_ticks: bool = True) -> None:
        super().__init__(daemon=True, name="MinuteBarAggregator")
        self._queue = get_fanout().subscribe(maxsize=20000)
        self._stop = threading.Event()
        self._bars: dict[tuple[str, datetime], _BarState] = {}
        self._raw_buf: list[tuple] = []
        self._log_raw_ticks = log_raw_ticks
        self._last_raw_flush = time.time()
        self._lock = threading.Lock()

    # ── Public ───────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    def force_flush_stale(self, now_utc: Optional[datetime] = None) -> int:
        """Flush any bar whose bucket is older than `now-1min`.
        Returns number of bars flushed. Safe to call from another thread.
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        cutoff = _floor_to_minute(now_utc)
        stale: list[_BarState] = []
        with self._lock:
            for key in list(self._bars.keys()):
                if self._bars[key].bucket < cutoff:
                    stale.append(self._bars.pop(key))
        if stale:
            self._persist_bars(stale)
        return len(stale)

    # ── Internals ────────────────────────────────────────────────

    def run(self) -> None:
        log.info("MinuteBarAggregator started.")
        while not self._stop.is_set():
            try:
                tick = self._queue.get(timeout=1.0)
            except queue.Empty:
                # Periodically flush raw-tick buffer even when idle.
                self._maybe_flush_raw()
                continue
            try:
                self._handle_tick(tick)
            except Exception as e:
                log.exception("MinuteBarAggregator: failed to handle tick: %s", e)
        # Drain on shutdown.
        with self._lock:
            remaining = list(self._bars.values())
            self._bars.clear()
        if remaining:
            self._persist_bars(remaining)
        self._maybe_flush_raw(force=True)
        log.info("MinuteBarAggregator stopped.")

    def _handle_tick(self, tick: dict) -> None:
        symbol = tick.get("symbol")
        if not symbol:
            return
        price = tick.get("last_price")
        if price is None:
            return
        ts = tick.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts is None:
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        bucket = _floor_to_minute(ts)
        key = (symbol, bucket)

        cum_vol = int(tick.get("volume_traded") or 0)

        with self._lock:
            # Detect bucket-roll: if any other bucket for this symbol exists
            # AND it's older, flush it before starting the new one.
            for k in list(self._bars.keys()):
                s, b = k
                if s == symbol and b < bucket:
                    stale = self._bars.pop(k)
                    # release the lock to do IO -- but we'll just append for now
                    # and persist below.
                    self._persist_bars([stale])

            bar = self._bars.get(key)
            if bar is None:
                # First tick of a fresh minute for this symbol.
                bar = _BarState(
                    symbol=symbol, bucket=bucket,
                    open_px=price, high_px=price, low_px=price, close_px=price,
                    last_cumulative_volume=cum_vol,
                )
                self._bars[key] = bar
            else:
                bar.high_px = max(bar.high_px, price)
                bar.low_px = min(bar.low_px, price)
                bar.close_px = price
                if cum_vol > bar.last_cumulative_volume:
                    delta_vol = cum_vol - bar.last_cumulative_volume
                    bar.volume += delta_vol
                    bar.pv_sum += price * delta_vol
                    bar.vol_sum += delta_vol
                bar.last_cumulative_volume = cum_vol
            bar.n_ticks += 1

        # Log raw tick.
        if self._log_raw_ticks:
            self._raw_buf.append((
                int(tick.get("instrument_token") or 0),
                symbol,
                ts,
                float(price),
                cum_vol or None,
                int(tick.get("total_buy_quantity") or 0) or None,
                int(tick.get("total_sell_quantity") or 0) or None,
            ))
            self._maybe_flush_raw()

    def _maybe_flush_raw(self, force: bool = False) -> None:
        if not self._log_raw_ticks:
            return
        now = time.time()
        if not force and len(self._raw_buf) < 500 and (now - self._last_raw_flush) < 5.0:
            return
        if not self._raw_buf:
            self._last_raw_flush = now
            return
        rows = self._raw_buf
        self._raw_buf = []
        self._last_raw_flush = now
        try:
            with get_sync_conn() as conn:
                cur = conn.cursor()
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    """INSERT INTO ticks_raw
                       (instrument_token, symbol, ts, last_price,
                        volume_traded, buy_quantity, sell_quantity)
                       VALUES %s""",
                    rows,
                    page_size=1000,
                )
                conn.commit()
        except Exception as e:
            log.warning("ticks_raw flush failed (%d rows lost): %s", len(rows), e)

    def _persist_bars(self, bars: list[_BarState]) -> None:
        """Upsert a list of completed bars into ohlcv_intraday."""
        if not bars:
            return
        rows = [
            (
                b.symbol, b.bucket,
                b.open_px, b.high_px, b.low_px, b.close_px,
                b.volume,
                (b.pv_sum / b.vol_sum) if b.vol_sum > 0 else None,
                b.n_ticks,
            )
            for b in bars
        ]
        try:
            with get_sync_conn() as conn:
                cur = conn.cursor()
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    """INSERT INTO ohlcv_intraday
                       (symbol, ts, open, high, low, close, volume, vwap, n_ticks)
                       VALUES %s
                       ON CONFLICT (symbol, ts) DO UPDATE SET
                           open = EXCLUDED.open,
                           high = GREATEST(ohlcv_intraday.high, EXCLUDED.high),
                           low  = LEAST(ohlcv_intraday.low,  EXCLUDED.low),
                           close = EXCLUDED.close,
                           volume = EXCLUDED.volume,
                           vwap   = EXCLUDED.vwap,
                           n_ticks = EXCLUDED.n_ticks""",
                    rows,
                    page_size=500,
                )
                conn.commit()
            log.debug("Persisted %d minute bars.", len(bars))
        except Exception as e:
            log.error("Failed to persist %d minute bars: %s", len(bars), e)


class PeriodicFlusher(threading.Thread):
    """Once a minute, asks the aggregator to flush bars whose minute has
    passed. Critical for illiquid symbols where ticks arrive sparsely."""

    def __init__(self, aggregator: MinuteBarAggregator, interval_sec: float = 30.0) -> None:
        super().__init__(daemon=True, name="PeriodicFlusher")
        self._agg = aggregator
        self._interval = interval_sec
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("PeriodicFlusher started (interval=%ss).", self._interval)
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            try:
                n = self._agg.force_flush_stale()
                if n:
                    log.debug("PeriodicFlusher flushed %d stale bars.", n)
            except Exception as e:
                log.warning("PeriodicFlusher error: %s", e)
        log.info("PeriodicFlusher stopped.")
