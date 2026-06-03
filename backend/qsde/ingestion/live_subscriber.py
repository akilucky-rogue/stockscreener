"""
Process-singleton live Kite subscription manager.

Problem this solves: previously the user had to run `scripts/kite_stream.py`
in a separate terminal before the live chart on the UI would receive ticks.
That's annoying. This module lets the FastAPI process itself own ONE
KiteLiveStreamer (+ MinuteBarAggregator + PeriodicFlusher), lazy-init it
when the first /api/analysis/... request comes in, and dynamically add
symbols to its subscription set as the user clicks new tickers.

Design:
  * One singleton (`get_manager()`); idempotent.
  * `ensure_subscribed(symbols)` -> lazy-creates the streamer + aggregator
    on first call (only if Kite is authenticated), then adds tokens to the
    live socket race-safely.
  * Silently degrades if Kite isn't authenticated yet (returns auth=False
    instead of raising). The UI keeps working with stale DB bars; once the
    user logs in via /api/kite/login_url, the next chart click brings up
    the WS.
  * `shutdown()` is wired into the FastAPI lifespan so uvicorn reloads
    don't leak threads.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from qsde.config import settings
from qsde.db.connection import read_sql
from qsde.ingestion.kite_ticker import KiteLiveStreamer
from qsde.ingestion.intraday_storage import MinuteBarAggregator, PeriodicFlusher

log = logging.getLogger(__name__)


class LiveStreamerManager:
    """Singleton wrapper around streamer + aggregator + flusher.

    Thread-safe. All public methods are idempotent and never raise on a
    missing Kite token -- they just return auth=False so callers can show
    a banner instead of crashing.
    """

    _instance: "Optional[LiveStreamerManager]" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streamer: Optional[KiteLiveStreamer] = None
        self._aggregator: Optional[MinuteBarAggregator] = None
        self._flusher: Optional[PeriodicFlusher] = None
        self._started = False

    # ── Public API ────────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "LiveStreamerManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _has_active_token(self) -> bool:
        df = read_sql(
            "SELECT 1 FROM kite_tokens WHERE is_active = TRUE AND expires_at > NOW() LIMIT 1"
        )
        return not df.empty

    def ensure_subscribed(self, symbols: list[str]) -> dict:
        """Make sure the singleton streamer is up and subscribed to `symbols`.

        Returns:
          {
            "auth":       bool,         # Kite session live?
            "started":    bool,         # streamer running?
            "added":      [str],        # symbols newly subscribed
            "skipped":    [str],        # already subscribed
            "unknown":    [str],        # not in kite_instruments (EQ NSE)
            "subscribed": [str],        # full live subscription set
          }
        """
        clean = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        if not clean:
            return {"auth": True, "started": self._started, "added": [], "skipped": [],
                    "unknown": [], "subscribed": self._subscribed_locked()}

        if settings.market_data_source.lower() != "kite":
            log.debug("MARKET_DATA_SOURCE != kite; skipping auto-subscribe.")
            return {"auth": False, "started": False, "added": [], "skipped": [],
                    "unknown": [], "subscribed": [],
                    "note": "MARKET_DATA_SOURCE != kite"}

        if not self._has_active_token():
            return {"auth": False, "started": False, "added": [], "skipped": [],
                    "unknown": [], "subscribed": [],
                    "note": "no active Kite token — log in at /api/kite/login_url"}

        with self._lock:
            if self._streamer is None:
                self._boot_locked(initial_symbols=clean)
                # initial_symbols were passed in; the rest of the flow still
                # routes through add_symbols so the bookkeeping stays in one
                # place.
                result = self._streamer.add_symbols(clean) if self._streamer else \
                         {"added": [], "skipped_already": [], "unknown": []}
            else:
                result = self._streamer.add_symbols(clean)

            return {
                "auth":       True,
                "started":    self._started,
                "added":      result.get("added", []),
                "skipped":    result.get("skipped_already", []),
                "unknown":    result.get("unknown", []),
                "subscribed": self._subscribed_locked(),
            }

    def status(self) -> dict:
        return {
            "auth":       self._has_active_token(),
            "started":    self._started,
            "connected":  bool(self._streamer and self._streamer.is_connected),
            "subscribed": self._subscribed_locked(),
        }

    def shutdown(self) -> None:
        with self._lock:
            if not self._started:
                return
            log.info("LiveStreamerManager.shutdown: stopping streamer + aggregator.")
            try:
                if self._streamer:
                    self._streamer.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("streamer.stop failed: %s", e)
            try:
                if self._flusher:
                    self._flusher.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("flusher.stop failed: %s", e)
            try:
                if self._aggregator:
                    self._aggregator.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("aggregator.stop failed: %s", e)
            self._started = False
            self._streamer = None
            self._aggregator = None
            self._flusher = None

    # ── Internals ─────────────────────────────────────────────────

    def _boot_locked(self, initial_symbols: list[str]) -> None:
        """Spin up aggregator + flusher + streamer. Caller holds self._lock."""
        log.info("LiveStreamerManager: bootstrapping (initial=%s)...", initial_symbols)
        # Resolve initial symbols to tokens up-front so the streamer connects
        # with an empty-or-populated initial subscription. (Even if this comes
        # back empty, add_symbols on the connected socket will fill it.)
        initial_token_by_sym = KiteLiveStreamer._resolve_symbols_to_tokens(initial_symbols)
        initial_tokens = list(initial_token_by_sym.keys())

        self._aggregator = MinuteBarAggregator(log_raw_ticks=True)
        self._flusher    = PeriodicFlusher(self._aggregator, interval_sec=30.0)
        self._streamer   = KiteLiveStreamer(instrument_tokens=initial_tokens, mode="quote")

        self._aggregator.start()
        self._flusher.start()
        self._streamer.start()
        self._started = True

    def _subscribed_locked(self) -> list[str]:
        if self._streamer is None:
            return []
        return self._streamer.subscribed_symbols()


# ── Module-level shortcuts ───────────────────────────────────────

def get_manager() -> LiveStreamerManager:
    return LiveStreamerManager.instance()


def ensure_subscribed(symbols: list[str]) -> dict:
    return get_manager().ensure_subscribed(symbols)


def shutdown_manager() -> None:
    LiveStreamerManager.instance().shutdown()
