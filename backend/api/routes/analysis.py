"""
Intraday analysis API — the data powering the live chart (Phase 4 / #8).

  GET /api/analysis/intraday/{symbol}?lookback=375

Returns, for the latest session of `symbol`:
  * bars      : [{ts, open, high, low, close, volume}]
  * micro     : [{ts, avwap, avwap_upper, avwap_lower, ofi, vp_poc, vp_vah,
                  vp_val, sweep_high, sweep_low}]   (session-anchored, causal)
  * signal    : the current white-box intraday signal (direction + entry/stop/
                target + reasons) from generate_intraday_signal

The frontend plots candles + the anchored-VWAP band + POC/VAH/VAL + sweep
markers + entry/SL/target overlays from this single payload, and layers live
ticks on top via the SSE stream (/api/intraday/stream).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Query

from qsde.db.connection import read_sql
from qsde.live.engine import load_session_bars
from qsde.factors.intraday_microstructure import compute_intraday_microstructure
from qsde.live.intraday_signal import generate_intraday_signal
from qsde.ingestion.live_subscriber import ensure_subscribed, get_manager

log = logging.getLogger(__name__)
router = APIRouter()


# ── Intraday history via yfinance ─────────────────────────────────
#
# `ohlcv_intraday` only contains minute-bars from when we were actively
# streaming via Kite. To give every symbol a populated 1W/1M chart on
# first open (regardless of streaming history), short ranges fetch
# hourly bars from yfinance. Cached 5 min keyed on (symbol, range)
# because yfinance updates intraday data only every ~15 min and we
# really don't want to hammer it on every 60s poll.
_YF_INTRADAY_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_YF_INTRADAY_LOCK = threading.Lock()
_YF_INTRADAY_TTL_SEC = 300.0


# Kite Connect intraday config per range. Kite's historical_data caps:
# minute=60d, 5minute=100d, 15minute=200d, 60minute=400d, day=2000d. We pick
# the finest interval that yields a manageable bar count.
#   1w / 5 days  / minute   -> ~1875 bars (true 1-min granularity)
#   1m / 31 days / 5minute  -> ~1650 bars (yfinance hard-caps 1m at 7 days,
#                              but Kite's 5minute window covers 100 days)
_KITE_INTRADAY_MAP = {
    "1w": (5,  "minute"),
    "1m": (31, "5minute"),
}

# Yfinance fallback when Kite is offline or the symbol isn't in
# kite_instruments. period+interval form so we can reuse the existing fetcher.
_YF_INTRADAY_MAP = {
    "1w": ("5d",  "1m"),
    "1m": ("1mo", "5m"),
}


def _yf_intraday_get(key: tuple[str, str]) -> Optional[list[dict]]:
    with _YF_INTRADAY_LOCK:
        entry = _YF_INTRADAY_CACHE.get(key)
        if entry is None:
            return None
        ts, payload = entry
        if time.monotonic() - ts > _YF_INTRADAY_TTL_SEC:
            _YF_INTRADAY_CACHE.pop(key, None)
            return None
        return payload


def _yf_intraday_put(key: tuple[str, str], payload: list[dict]) -> None:
    with _YF_INTRADAY_LOCK:
        if len(_YF_INTRADAY_CACHE) > 500:
            _YF_INTRADAY_CACHE.clear()
        _YF_INTRADAY_CACHE[key] = (time.monotonic(), payload)


def _fetch_kite_intraday(symbol: str, days_back: int, interval: str) -> list[dict]:
    """Pull intraday OHLCV straight from Kite Connect's historical_data API.

    Returns [] if Kite isn't authenticated, the symbol isn't in
    `kite_instruments` (no instrument_token), or the call errors out.
    Caller falls back to yfinance on empty.
    """
    from datetime import timedelta
    try:
        from qsde.ingestion.kite_client import get_kite_client
        client = get_kite_client()
        if not client.is_authenticated:
            return []
        if client.get_instrument_token(symbol) is None:
            return []
        df = client.historical_ohlcv(
            symbol=symbol,
            from_date=date.today() - timedelta(days=days_back),
            to_date=date.today(),
            interval=interval,
        )
        if df is None or df.empty:
            return []
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "ts":     ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "open":   _f(row.get("open")),
                "high":   _f(row.get("high")),
                "low":    _f(row.get("low")),
                "close":  _f(row.get("close")),
                "volume": _f(row.get("volume")),
            })
        return bars
    except Exception as e:  # noqa: BLE001
        log.warning("Kite intraday fetch failed for %s @ %s: %s", symbol, interval, e)
        return []


def _fetch_yf_intraday(symbol: str, period: str, interval: str) -> list[dict]:
    """Pull intraday OHLCV from yfinance. Tries .NS first then .BO.

    Returns a list of bar dicts shaped like /analysis/historical's output:
    {ts, open, high, low, close, volume}. Empty list on failure.
    """
    try:
        import yfinance as yf
    except ImportError:
        return []

    sym = symbol.upper().strip()
    candidates = ([sym] if sym.endswith((".NS", ".BO"))
                  else [f"{sym}.NS", f"{sym}.BO"])
    for yf_sym in candidates:
        try:
            df = yf.Ticker(yf_sym).history(
                period=period, interval=interval,
                auto_adjust=False, prepost=False,
            )
            if df is None or df.empty:
                continue
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            bars = []
            for ts, row in df.iterrows():
                bars.append({
                    "ts":     ts.isoformat(),
                    "open":   _f(row.get("open")),
                    "high":   _f(row.get("high")),
                    "low":    _f(row.get("low")),
                    "close":  _f(row.get("close")),
                    "volume": _f(row.get("volume")),
                })
            return bars
        except Exception as e:  # noqa: BLE001
            log.debug("yfinance intraday %s %s/%s failed: %s",
                      yf_sym, period, interval, e)
    return []


# Historical range -> rough trading-day count (252 sessions/yr).
# Used by /analysis/historical to pull the LAST N rows of OHLCV regardless
# of how stale the daily-refresh pipeline is. Strict date cutoffs break
# when ingest hasn't run for a few days (e.g., 1W = 7 calendar days, but
# the latest ohlcv row is 9 days old -> returns 0 rows). Using a row LIMIT
# instead always returns whatever data is most recent.
_RANGE_ROWS = {
    "1w":   5,
    "1m":  22,
    "3m":  65,
    "6m": 130,
    "1y": 252,
    "2y": 504,
    "5y": 1260,
    "max": 252 * 25,
}


def _f(v):
    """float() that lets NaN through (CleanJSONResponse scrubs NaN -> null)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@router.get("/analysis/intraday/{symbol}")
def intraday_analysis(symbol: str, lookback: int = Query(default=375, ge=10, le=1000)):
    sym = symbol.upper()
    # Auto-subscribe to Kite ticks for this symbol so the user doesn't have
    # to run scripts/kite_stream.py manually. No-op if Kite isn't
    # authenticated yet; the call falls through to whatever bars are
    # already in ohlcv_intraday.
    sub_info = ensure_subscribed([sym])
    bars = load_session_bars(sym, lookback=lookback)
    if bars is None or bars.empty:
        return {
            "symbol": sym, "count": 0, "bars": [], "micro": [], "signal": None,
            "subscription": sub_info,
            "note": ("waiting for first tick — Kite WS subscribed, ticks will arrive "
                     "once the symbol prints during market hours")
                    if sub_info.get("started") else
                    ("no intraday bars yet and Kite session not active — log in via "
                     "/api/kite/login_url to start streaming"),
        }

    micro = compute_intraday_microstructure(bars)

    ts = [t.isoformat() for t in bars.index]
    out_bars = [
        {"ts": ts[i], "open": _f(bars["open"].iloc[i]), "high": _f(bars["high"].iloc[i]),
         "low": _f(bars["low"].iloc[i]), "close": _f(bars["close"].iloc[i]),
         "volume": _f(bars["volume"].iloc[i])}
        for i in range(len(bars))
    ]
    out_micro = [
        {"ts": ts[i],
         "avwap": _f(micro["intraday_avwap"].iloc[i]),
         "avwap_upper": _f(micro["intraday_avwap_upper"].iloc[i]),
         "avwap_lower": _f(micro["intraday_avwap_lower"].iloc[i]),
         "ofi": _f(micro["intraday_ofi"].iloc[i]),
         "vp_poc": _f(micro["intraday_vp_poc"].iloc[i]),
         "vp_vah": _f(micro["intraday_vp_vah"].iloc[i]),
         "vp_val": _f(micro["intraday_vp_val"].iloc[i]),
         "sweep_high": int(micro["intraday_sweep_high"].iloc[i]),
         "sweep_low": int(micro["intraday_sweep_low"].iloc[i])}
        for i in range(len(micro))
    ]

    signal = None
    try:
        signal = generate_intraday_signal(bars, symbol=sym, horizon="intraday").to_dict()
    except Exception as e:  # noqa: BLE001
        log.warning("signal failed for %s: %s", sym, e)

    return {"symbol": sym, "count": len(out_bars), "bars": out_bars,
            "micro": out_micro, "signal": signal, "subscription": sub_info}


@router.get("/analysis/historical/{symbol}")
def historical_chart(
    symbol: str,
    range: str = Query(default="1y", description="1w | 1m | 3m | 6m | 1y | 2y | 5y | max"),
):
    """OHLCV slice for the historical chart in the Custom panel.

    Granularity per range:
      * 1w  -> hourly bars (5d/1h)        ~ 32 bars across the trading week
      * 1m  -> hourly bars (1mo/1h)       ~ 140 bars
      * 3m+ -> daily bars from local DB   (53-1260 bars depending on range)

    Hourly bars come from yfinance (5-min TTL cache); daily bars come from
    the local `ohlcv` table using LIMIT-by-row-count rather than calendar
    cutoff, so the chart still works when the daily ingest is stale.
    """
    sym = symbol.upper()
    rng = range.lower()

    # 1W / 1M -> intraday bars. Try Kite first (authoritative paid source),
    # fall back to yfinance (free, may lag ~15min), then DB daily as last resort.
    if rng in _KITE_INTRADAY_MAP:
        cache_key = (sym, rng)
        cached = _yf_intraday_get(cache_key)   # cache is source-agnostic
        if cached is not None:
            return {
                "symbol": sym, "range": range, "count": len(cached), "bars": cached,
                "micro": [], "signal": None,
                "interval": _KITE_INTRADAY_MAP[rng][1],
                "latest_date": cached[-1]["ts"][:10] if cached else None,
                "days_stale": 0,
                "_source": "cached",
                "_cache": "hit",
            }

        # Live fetch — Kite first.
        days_back, kite_interval = _KITE_INTRADAY_MAP[rng]
        bars = _fetch_kite_intraday(sym, days_back, kite_interval)
        source = "kite_intraday"
        used_interval = kite_interval

        # Fallback: yfinance.
        if not bars and rng in _YF_INTRADAY_MAP:
            yf_period, yf_interval = _YF_INTRADAY_MAP[rng]
            bars = _fetch_yf_intraday(sym, yf_period, yf_interval)
            source = "yfinance_intraday"
            used_interval = yf_interval

        if bars:
            _yf_intraday_put(cache_key, bars)
            return {
                "symbol": sym, "range": range, "count": len(bars), "bars": bars,
                "micro": [], "signal": None,
                "interval": used_interval,
                "latest_date": bars[-1]["ts"][:10] if bars else None,
                "days_stale": 0,
                "_source": source,
                "_cache": "miss",
            }
        # Fall through to DB daily if both intraday sources had nothing.

    n_rows = _RANGE_ROWS.get(rng, 252)

    df = read_sql(
        """SELECT date, open, high, low, close, volume FROM (
              SELECT date, open, high, low, close, volume
                FROM ohlcv
               WHERE symbol = :sym
            ORDER BY date DESC
               LIMIT :n
           ) recent
         ORDER BY date ASC""",
        params={"sym": sym, "n": n_rows},
    )
    if df.empty:
        return {"symbol": sym, "range": range, "count": 0, "bars": [], "micro": [], "signal": None,
                "note": "no historical OHLCV — try /api/analyze/{symbol}/pin first"}

    bars = [
        {"ts": str(r["date"]),
         "open":  _f(r["open"]),  "high": _f(r["high"]),
         "low":   _f(r["low"]),   "close": _f(r["close"]),
         "volume": _f(r["volume"])}
        for r in df.to_dict("records")
    ]
    # If the LAST row is meaningfully older than today, tell the UI so it
    # can show a non-alarming "data through YYYY-MM-DD" caption instead of
    # the user thinking the chart is broken.
    latest = bars[-1]["ts"]
    days_stale = (date.today() - date.fromisoformat(latest)).days
    note = None
    if days_stale > 3:
        note = (f"latest OHLCV in DB is {latest} ({days_stale}d old) — run "
                "scripts/kite_daily_refresh.py or POST /api/analyze/{symbol}/pin to refresh")

    return {"symbol": sym, "range": range, "count": len(bars), "bars": bars,
            "micro": [], "signal": None, "note": note,
            "latest_date": latest, "days_stale": days_stale}


@router.post("/analysis/subscribe")
def subscribe(symbols: str = Query(..., description="Comma-separated NSE symbols")):
    """Manually add symbols to the live Kite subscription. The /intraday GET
    already does this implicitly; this is exposed only for diagnostics +
    bulk pre-warming (e.g. watchlist boot)."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return ensure_subscribed(syms)


@router.get("/analysis/subscribe/status")
def subscribe_status():
    """Live subscription health: auth/started/connected + current symbol set."""
    return get_manager().status()
