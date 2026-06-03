"""
Kite WebSocket (KiteTicker) live-tick streamer.

Connects to wss://ws.kite.trade using the active access_token, subscribes
to a set of instrument tokens, and republishes each tick into the
process-wide TickFanout for downstream consumers (aggregator, SSE, etc.).

The kiteconnect SDK's KiteTicker uses Twisted under the hood. We run it
in `threaded=True` mode so the FastAPI event loop is unaffected -- ticks
arrive on a Twisted reactor thread and we just hand them to our own
thread-safe fanout.

Modes supported:
  * MODE_LTP   -- only last-traded-price. Cheapest.
  * MODE_QUOTE -- LTP + OHLC + last-traded-quantity. RECOMMENDED for us.
  * MODE_FULL  -- everything including market depth. Heavy; only for
                  specific symbols if at all.

We default to MODE_QUOTE because the minute-bar aggregator only needs
price + volume + LTQ; depth data is huge and only useful for execution.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from qsde.config import settings
from qsde.db.connection import read_sql
from qsde.ingestion.tick_fanout import get_fanout

log = logging.getLogger(__name__)


class KiteLiveStreamer:
    """Wraps KiteTicker with auto-reconnect + token-to-symbol mapping +
    fanout publishing.

    Public API:
        streamer = KiteLiveStreamer(instrument_tokens=[...])
        streamer.start()    # non-blocking; spawns Twisted reactor thread
        streamer.stop()
    """

    def __init__(
        self,
        instrument_tokens: list[int],
        access_token: Optional[str] = None,
        mode: str = "quote",
    ) -> None:
        try:
            from kiteconnect import KiteTicker
        except ImportError as e:
            raise RuntimeError("kiteconnect SDK not installed.") from e

        if not settings.kite_api_key:
            raise RuntimeError("KITE_API_KEY not set.")
        if access_token is None:
            access_token = self._load_active_token()
        if not access_token:
            raise RuntimeError(
                "No active Kite access_token in DB. Re-login via /api/kite/login_url."
            )

        self._KiteTicker = KiteTicker
        # Use a mutable set under a lock so add_symbols() can race-safely
        # add tokens to a live socket.
        self._tokens_lock = threading.Lock()
        self._instrument_tokens: list[int] = list(instrument_tokens)
        self._connected = False  # flipped True in _on_connect
        self._mode_name = mode.lower()
        self._kws = KiteTicker(settings.kite_api_key, access_token)
        self._fanout = get_fanout()
        self._stop_event = threading.Event()
        # instrument_token -> tradingsymbol map, populated lazily.
        self._token_to_symbol = self._build_token_symbol_map(instrument_tokens)
        # Diagnostic: cumulative tick counter + first-tick wall-clock.
        # Heartbeat cadence read at construction time so the kite_stream
        # --heartbeat-every CLI override (which sets QSDE_TICK_HEARTBEAT
        # in main() before instantiation) is honored.
        self._tick_count = 0
        self._first_tick_at: Optional[datetime] = None
        self._heartbeat_every = int(os.getenv("QSDE_TICK_HEARTBEAT", "50"))

        self._kws.on_ticks = self._on_ticks
        self._kws.on_connect = self._on_connect
        self._kws.on_close = self._on_close
        self._kws.on_error = self._on_error
        self._kws.on_reconnect = self._on_reconnect
        self._kws.on_noreconnect = self._on_noreconnect

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _load_active_token() -> Optional[str]:
        df = read_sql(
            """SELECT access_token FROM kite_tokens
                WHERE is_active = TRUE AND expires_at > NOW()
             ORDER BY login_time DESC LIMIT 1"""
        )
        return df.iloc[0]["access_token"] if not df.empty else None

    def _build_token_symbol_map(self, tokens: list[int]) -> dict[int, str]:
        if not tokens:
            return {}
        df = read_sql(
            "SELECT instrument_token, tradingsymbol FROM kite_instruments "
            "WHERE instrument_token = ANY(:toks)",
            params={"toks": tokens},
        )
        return {int(r.instrument_token): r.tradingsymbol for r in df.itertuples()}

    def _mode(self):
        m = self._mode_name
        if m == "ltp":   return self._kws.MODE_LTP
        if m == "full":  return self._kws.MODE_FULL
        return self._kws.MODE_QUOTE  # default

    # ── KiteTicker callbacks ─────────────────────────────────────

    def _on_connect(self, ws, response):
        with self._tokens_lock:
            toks = list(self._instrument_tokens)
            self._connected = True
        log.info("KiteTicker connected. Subscribing to %d instruments (mode=%s)...",
                 len(toks), self._mode_name)
        if toks:
            ws.subscribe(toks)
            ws.set_mode(self._mode(), toks)

    def _on_close(self, ws, code, reason):
        with self._tokens_lock:
            self._connected = False
        log.warning("KiteTicker closed: code=%s reason=%s", code, reason)

    def _on_error(self, ws, code, reason):
        log.error("KiteTicker error: code=%s reason=%s", code, reason)

    def _on_reconnect(self, ws, attempts_count):
        log.info("KiteTicker reconnecting (attempt %d)...", attempts_count)

    def _on_noreconnect(self, ws):
        log.error("KiteTicker permanently disconnected. Restart the process.")

    def _on_ticks(self, ws, ticks):
        """Per the SDK, ticks is a list[dict]. Each dict has at minimum
        `instrument_token` and `last_price`. We add `symbol` and `ts`
        (the tick timestamp from Kite, falling back to wall-clock).
        """
        now_utc = datetime.now(timezone.utc)
        fanout = self._fanout
        n = len(ticks)
        # First-tick announcement: prove the WS callback is firing at all.
        if self._first_tick_at is None and n:
            self._first_tick_at = now_utc
            log.info("First tick received (batch_size=%d). WS callback is alive.", n)
        for tick in ticks:
            tok = tick.get("instrument_token")
            tick["symbol"] = self._token_to_symbol.get(tok)
            # Kite returns 'exchange_timestamp' (datetime) on quote/full;
            # on LTP-only it may be missing.
            ts = (
                tick.get("exchange_timestamp")
                or tick.get("last_trade_time")
                or now_utc
            )
            tick["ts"] = ts
            fanout.publish(tick)
        # Periodic heartbeat at INFO; per-batch trace at DEBUG.
        self._tick_count += n
        hb = self._heartbeat_every
        if hb > 0 and (self._tick_count // hb) != ((self._tick_count - n) // hb):
            log.info("Ticks received: total=%d (last batch=%d, syms=%s)",
                     self._tick_count, n,
                     ",".join(sorted({t.get("symbol") or str(t.get("instrument_token"))
                                      for t in ticks[:5]})))
        if log.isEnabledFor(logging.DEBUG):
            sample = ticks[0] if ticks else {}
            log.debug("on_ticks batch=%d sample=%s",
                      n,
                      {k: sample.get(k) for k in ("symbol", "instrument_token",
                                                  "last_price", "volume_traded",
                                                  "exchange_timestamp")})

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Start the WS in a background Twisted reactor thread."""
        log.info("Starting KiteTicker (threaded)...")
        self._kws.connect(threaded=True)

    def stop(self) -> None:
        log.info("Stopping KiteTicker...")
        self._stop_event.set()
        try:
            self._kws.close()
        except Exception:
            pass

    # ── Dynamic subscription (called from API request threads) ───

    @staticmethod
    def _resolve_symbols_to_tokens(symbols: list[str]) -> dict[int, str]:
        """Look up instrument_token for each tradingsymbol on NSE EQ.

        Returns {token: tradingsymbol}. Missing symbols are silently dropped
        (callers log).
        """
        clean = [s.strip().upper() for s in symbols if s and s.strip()]
        if not clean:
            return {}
        df = read_sql(
            "SELECT instrument_token, tradingsymbol FROM kite_instruments "
            "WHERE tradingsymbol = ANY(:syms) AND instrument_type='EQ' "
            "AND exchange='NSE'",
            params={"syms": clean},
        )
        return {int(r.instrument_token): r.tradingsymbol for r in df.itertuples()}

    def add_symbols(self, symbols: list[str]) -> dict:
        """Race-safely add tradingsymbols to the live subscription.

        - Resolves new symbols -> tokens via kite_instruments.
        - Skips tokens already subscribed.
        - If the WS is connected, sends subscribe + set_mode on the new batch.
        - If not yet connected, the new tokens are appended to the pending
          set and will be subscribed inside _on_connect.

        Returns {added: [...], skipped_already: [...], unknown: [...]}.
        """
        clean = [s.strip().upper() for s in symbols if s and s.strip()]
        token_by_sym = {v: k for k, v in self._resolve_symbols_to_tokens(clean).items()}
        unknown = [s for s in clean if s not in token_by_sym]

        added_tokens: list[int] = []
        skipped: list[str] = []
        with self._tokens_lock:
            current_set = set(self._instrument_tokens)
            for sym in clean:
                tok = token_by_sym.get(sym)
                if tok is None:
                    continue
                if tok in current_set:
                    skipped.append(sym)
                    continue
                self._instrument_tokens.append(tok)
                self._token_to_symbol[tok] = sym
                current_set.add(tok)
                added_tokens.append(tok)
            connected = self._connected

        if added_tokens and connected:
            try:
                self._kws.subscribe(added_tokens)
                self._kws.set_mode(self._mode(), added_tokens)
                log.info("Live-added %d tokens to subscription: %s",
                         len(added_tokens), [self._token_to_symbol[t] for t in added_tokens])
            except Exception as e:  # noqa: BLE001
                log.warning("add_symbols subscribe failed: %s", e)
        elif added_tokens:
            log.info("Queued %d tokens for subscription on connect: %s",
                     len(added_tokens), [self._token_to_symbol[t] for t in added_tokens])

        return {
            "added":           [self._token_to_symbol[t] for t in added_tokens],
            "skipped_already": skipped,
            "unknown":         unknown,
        }

    def subscribed_symbols(self) -> list[str]:
        with self._tokens_lock:
            return sorted(self._token_to_symbol.get(t) or str(t)
                          for t in self._instrument_tokens)

    @property
    def is_connected(self) -> bool:
        with self._tokens_lock:
            return self._connected
