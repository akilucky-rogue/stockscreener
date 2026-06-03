"""
Zerodha Kite Connect client.

Wraps the official `kiteconnect` SDK with QSDE-specific helpers:

  * Daily access-token storage in `kite_tokens` (Kite issues a fresh
    token every day at 06:00 IST; we cache it in DB).
  * Symbol -> instrument_token resolution via `kite_instruments`.
  * Historical OHLCV pulls that return DataFrames matching the
    shape our existing yfinance code already speaks (lower-cased
    columns, DatetimeIndex named "date").

Daily OAuth flow:

    1. Frontend redirects user to `client.login_url()`.
    2. Zerodha logs the user in, redirects to `kite_redirect_url`
       with a `request_token` query parameter.
    3. `api/routes/kite.py` calls `client.exchange_token(request_token)`
       which stores the new access_token in DB and marks any prior
       row inactive.
    4. All subsequent client.* calls pick up the active token
       automatically via `_load_active_token()`.

The kiteconnect SDK is required:  `pip install kiteconnect`
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import text

from qsde.config import settings
from qsde.db.connection import execute_sql, get_sync_conn, read_sql

log = logging.getLogger(__name__)


# Kite's per-request day-count caps for `historical_data`. The endpoint
# rejects ranges wider than these with "interval exceeds max limit: N days".
# Source: https://kite.trade/docs/connect/v3/historical/
KITE_HISTORICAL_MAX_DAYS: dict[str, int] = {
    "minute":   60,
    "3minute":  100,
    "5minute":  100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day":      2000,
}


def _next_token_expiry() -> datetime:
    """Kite tokens expire at 06:00 IST the next day. Returns that timestamp."""
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    # If we logged in BEFORE 6am, the token expires at 6am today; otherwise
    # 6am tomorrow.
    candidate = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_ist >= candidate:
        candidate += timedelta(days=1)
    return candidate


class KiteClient:
    """Thin wrapper around the kiteconnect SDK.

    Construct with no args; reads `kite_api_key` / `kite_api_secret` from
    settings and loads the active access_token from DB. If no active token
    exists, `historical_data` / `quote` / `instruments` will raise.
    Call `exchange_token(request_token)` after the OAuth handshake to
    populate a fresh token.
    """

    def __init__(self) -> None:
        try:
            from kiteconnect import KiteConnect
        except ImportError as e:
            raise RuntimeError(
                "kiteconnect SDK not installed. Run: pip install kiteconnect"
            ) from e

        if not settings.kite_api_key or not settings.kite_api_secret:
            raise RuntimeError(
                "KITE_API_KEY / KITE_API_SECRET not set in .env. "
                "See https://developers.kite.trade"
            )

        self._sdk_cls = KiteConnect
        self._kite = KiteConnect(api_key=settings.kite_api_key)
        self._load_active_token_if_present()

    # ── Auth / token plumbing ────────────────────────────────────────

    def login_url(self) -> str:
        """The URL the frontend should redirect the user to for daily login."""
        return self._kite.login_url()

    def exchange_token(self, request_token: str) -> dict:
        """Exchange a request_token (from the redirect URL) for an access_token.

        Persists the access_token to `kite_tokens`, deactivates older rows,
        and arms the SDK to use it for the rest of the day.
        """
        log.info("Exchanging Kite request_token for access_token...")
        data = self._kite.generate_session(
            request_token, api_secret=settings.kite_api_secret,
        )
        access_token = data["access_token"]
        public_token = data.get("public_token")
        user_id      = data.get("user_id")
        user_name    = data.get("user_name")

        self._kite.set_access_token(access_token)
        self._access_token = access_token

        # Deactivate older rows and insert the new one.
        with get_sync_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE kite_tokens SET is_active = FALSE WHERE is_active = TRUE;")
                cur.execute(
                    """
                    INSERT INTO kite_tokens
                        (access_token, public_token, user_id, user_name,
                         expires_at, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE);
                    """,
                    (access_token, public_token, user_id, user_name,
                     _next_token_expiry()),
                )
                conn.commit()
        log.info("Kite access_token stored. User: %s (%s)", user_name, user_id)
        return {
            "user_id":   user_id,
            "user_name": user_name,
            "expires_at": _next_token_expiry().isoformat(),
        }

    def _load_active_token_if_present(self) -> None:
        df = read_sql(
            """SELECT access_token, expires_at FROM kite_tokens
                WHERE is_active = TRUE
                  AND expires_at > NOW()
             ORDER BY login_time DESC
                LIMIT 1"""
        )
        if df.empty:
            log.warning("No active Kite access_token in DB. Re-login required.")
            self._access_token = None
            return
        self._access_token = df.iloc[0]["access_token"]
        self._kite.set_access_token(self._access_token)
        log.info("Loaded active Kite access_token (expires %s).",
                 df.iloc[0]["expires_at"])

    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None

    def _require_auth(self) -> None:
        if not self.is_authenticated:
            raise RuntimeError(
                "Kite client has no active access_token. "
                "Hit /api/kite/login_url and complete the OAuth flow first."
            )

    # ── Instrument map ───────────────────────────────────────────────

    def refresh_instruments(self, exchange: str = "NSE") -> int:
        """Pull the full instrument list and upsert into kite_instruments.

        The Kite historical-data API takes an instrument_token, not a
        symbol -- this map is what bridges our `universe.symbol` strings
        to those tokens. Run once per session day; safe to rerun.
        """
        self._require_auth()
        log.info("Fetching Kite instrument list for %s...", exchange)
        rows = self._kite.instruments(exchange)
        log.info("  got %d instruments", len(rows))

        records = [
            (
                r["instrument_token"], r["exchange_token"], r["tradingsymbol"],
                r.get("name"), r.get("last_price"),
                r.get("expiry") or None, r.get("strike"),
                r.get("tick_size"), r.get("lot_size"),
                r.get("instrument_type"), r.get("segment"), r.get("exchange"),
            )
            for r in rows
        ]
        with get_sync_conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO kite_instruments (
                        instrument_token, exchange_token, tradingsymbol,
                        name, last_price, expiry, strike, tick_size, lot_size,
                        instrument_type, segment, exchange
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (instrument_token) DO UPDATE SET
                        last_price = EXCLUDED.last_price,
                        refreshed_at = NOW();
                    """,
                    records,
                )
                conn.commit()
        return len(records)

    def get_instrument_token(self, symbol: str, exchange: str = "NSE") -> Optional[int]:
        df = read_sql(
            """SELECT instrument_token FROM kite_instruments
                WHERE tradingsymbol = :sym
                  AND exchange      = :exch
                  AND instrument_type = 'EQ'
                LIMIT 1""",
            params={"sym": symbol.upper(), "exch": exchange},
        )
        if df.empty:
            return None
        return int(df.iloc[0]["instrument_token"])

    # ── Historical data ──────────────────────────────────────────────

    def historical_ohlcv(
        self,
        symbol: str,
        from_date: date,
        to_date: date,
        interval: str = "day",
        exchange: str = "NSE",
    ) -> pd.DataFrame:
        """Fetch OHLCV bars for a symbol.

        `interval` is one of:
            "minute" / "3minute" / "5minute" / "10minute" / "15minute" /
            "30minute" / "60minute" / "day"

        Kite caps each `historical_data` call at a per-interval day limit
        (see KITE_HISTORICAL_MAX_DAYS). For "day" that's 2000 days
        (~5.5 years). If the requested range exceeds the cap, we
        automatically chunk into multiple sequential calls and stitch
        the results, with a small sleep between requests to stay under
        the 3 req/sec rate limit.

        Returned DataFrame matches the shape our yfinance ingestion already
        produces -- DatetimeIndex named "date", lowercase columns. NOTE: the
        `adj_close` column doesn't exist in the Kite payload; we copy
        `close` into it so downstream code that expects it doesn't break.
        Kite already returns split-adjusted prices for daily bars.
        """
        import time

        self._require_auth()
        token = self.get_instrument_token(symbol, exchange=exchange)
        if token is None:
            raise ValueError(
                f"{symbol} not found in kite_instruments. "
                "Run client.refresh_instruments() first."
            )

        max_days = KITE_HISTORICAL_MAX_DAYS.get(interval, 2000)
        # Leave a small safety margin to avoid off-by-one rejects.
        chunk_days = max_days - 2

        all_rows: list[dict] = []
        chunk_start = from_date
        chunk_idx = 0
        while chunk_start <= to_date:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), to_date)
            try:
                rows = self._kite.historical_data(
                    instrument_token=token,
                    from_date=chunk_start.isoformat(),
                    to_date=chunk_end.isoformat(),
                    interval=interval,
                    continuous=False,
                    oi=False,
                )
            except Exception as e:
                # Re-raise with context that includes the failing window so
                # the caller can decide whether to skip this symbol entirely
                # or just this chunk.
                raise RuntimeError(
                    f"Kite historical_data failed for {symbol} "
                    f"{chunk_start}..{chunk_end} ({interval}): {e}"
                ) from e

            if rows:
                all_rows.extend(rows)
            chunk_idx += 1
            chunk_start = chunk_end + timedelta(days=1)
            # Stay below the 3 req/sec ceiling -- gentle 350ms gap.
            if chunk_start <= to_date:
                time.sleep(0.35)

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        # Kite occasionally returns overlapping bars on chunk boundaries;
        # de-dup on the index.
        df = df[~df.index.duplicated(keep="last")]
        df.rename(columns=str.lower, inplace=True)
        if "adj_close" not in df.columns:
            df["adj_close"] = df["close"]
        return df[["open", "high", "low", "close", "adj_close", "volume"]]

    # ── Live quotes ──────────────────────────────────────────────────

    def quote(self, symbols: list[str], exchange: str = "NSE") -> dict:
        """Get live quote snapshots (LTP, OHLC, depth) for one or more symbols.

        Returns a dict keyed by "EXCHANGE:SYMBOL" (Kite's format).
        Cheap call; useful for the dashboard's "current price" display.
        """
        self._require_auth()
        keys = [f"{exchange}:{s.upper()}" for s in symbols]
        return self._kite.quote(keys)

    def ltp(self, symbols: list[str], exchange: str = "NSE") -> dict[str, float]:
        """Last-traded price only (cheaper than full `quote`)."""
        self._require_auth()
        keys = [f"{exchange}:{s.upper()}" for s in symbols]
        out = self._kite.ltp(keys)
        return {k.split(":", 1)[1]: v["last_price"] for k, v in out.items()}

    # ── Order placement (semi-auto; gated by qsde.execution) ─────────

    def place_order(
        self,
        *,
        tradingsymbol: str,
        transaction_type: str,        # "BUY" / "SELL"
        quantity: int,
        product: str = "MIS",         # MIS / CNC / NRML
        order_type: str = "MARKET",   # MARKET / LIMIT
        price: Optional[float] = None,
        exchange: str = "NSE",
        variety: str = "regular",
        tag: str = "qsde",
    ) -> str:
        """Forward a single order to Kite; returns the broker order_id.

        This is a thin SDK forwarder. Human confirmation, the kill-switch, the
        QSDE_ENABLE_LIVE_ORDERS gate, and risk checks all live in
        qsde.execution.order_tickets and MUST pass before this is called.
        """
        self._require_auth()
        params = dict(
            variety=variety,
            exchange=exchange,
            tradingsymbol=tradingsymbol.upper(),
            transaction_type=transaction_type,
            quantity=int(quantity),
            product=product,
            order_type=order_type,
            tag=tag,
        )
        if order_type == "LIMIT" and price is not None:
            params["price"] = float(price)
        return self._kite.place_order(**params)


# Singleton accessor so repeat calls don't reload the token row.
_INSTANCE: Optional[KiteClient] = None


def get_kite_client() -> KiteClient:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = KiteClient()
    return _INSTANCE
