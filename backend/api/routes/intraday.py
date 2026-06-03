"""
Intraday API: minute-bar query + Server-Sent Events live tick stream.

  GET  /api/intraday/{symbol}?lookback=120          -> last N minute bars
  GET  /api/intraday/latest                         -> latest bar per symbol
  GET  /api/intraday/stream                         -> SSE live tick stream
                                                       (text/event-stream)

The SSE stream is consumed by the frontend via the EventSource browser
API; each event contains a JSON tick. Multiple browser tabs can subscribe
simultaneously -- TickFanout fans out to each one independently.

The SSE endpoint only emits ticks if the kite_stream.py daemon is
running in the same Python process. Since the daemon and the FastAPI app
are typically separate processes (so the WS doesn't block the API), the
practical setup is:
  * Run scripts/kite_stream.py in one PowerShell window  (background)
  * Run uvicorn ...api.main:app             in another window (foreground)
And the frontend connects to the SSE endpoint on the API process.

For the SSE to actually receive ticks, the same TickFanout must be
shared. That requires running the daemon in-process with uvicorn. Two
ways to do that:
  (a) Start the streamer inline in api/main.py on startup (simple)
  (b) Use Redis pubsub as the cross-process bus (production-grade)

We default to (a) for now -- see api/main.py for the wiring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from qsde.db.connection import read_sql
from qsde.ingestion.tick_fanout import get_fanout

log = logging.getLogger(__name__)
router = APIRouter()


def _serialize_tick(tick: dict) -> str:
    """Convert a raw tick dict into a JSON-safe SSE event payload."""
    out = {}
    for k in ("symbol", "instrument_token", "last_price",
              "volume_traded", "ohlc", "change", "average_traded_price"):
        if k in tick:
            v = tick[k]
            if isinstance(v, datetime):
                v = v.isoformat()
            out[k] = v
    ts = tick.get("ts")
    if isinstance(ts, datetime):
        out["ts"] = ts.isoformat()
    elif ts is not None:
        out["ts"] = str(ts)
    return json.dumps(out, default=str)


@router.get("/intraday/stream")
async def stream_ticks(symbols: Optional[str] = Query(default=None,
                       description="Comma-separated symbols to filter (default: all)")):
    """Server-Sent Events stream of live ticks.

    Browser EventSource usage:
        const es = new EventSource('/api/intraday/stream?symbols=RELIANCE,TCS');
        es.onmessage = (e) => { const tick = JSON.parse(e.data); ... };
    """
    filter_set = (
        {s.strip().upper() for s in symbols.split(",") if s.strip()}
        if symbols else None
    )

    fanout = get_fanout()
    sub_queue = fanout.subscribe(maxsize=2000)

    async def event_generator():
        # Initial keepalive so the client knows the stream is open.
        yield "event: ready\ndata: connected\n\n"
        try:
            loop = asyncio.get_event_loop()
            while True:
                # Pull a tick from the threading.Queue without blocking the loop.
                tick = await loop.run_in_executor(
                    None, lambda: _safe_get(sub_queue, timeout=15.0)
                )
                if tick is None:
                    # 15s with no tick -> send a keepalive comment so proxies
                    # don't kill the connection.
                    yield ": keepalive\n\n"
                    continue
                sym = tick.get("symbol")
                if filter_set and sym not in filter_set:
                    continue
                yield f"data: {_serialize_tick(tick)}\n\n"
        finally:
            fanout.unsubscribe(sub_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _safe_get(q: "queue.Queue", timeout: float):
    try:
        return q.get(timeout=timeout)
    except Exception:
        return None


@router.get("/intraday/{symbol}")
def get_intraday_bars(
    symbol: str,
    lookback: int = Query(default=240, ge=1, le=2000,
                          description="Number of recent 1-min bars"),
):
    """Last N 1-minute bars for a symbol."""
    df = read_sql(
        """SELECT ts, open, high, low, close, volume, vwap, n_ticks
             FROM ohlcv_intraday
            WHERE symbol = :sym
         ORDER BY ts DESC
            LIMIT :lim""",
        params={"sym": symbol.upper(), "lim": lookback},
    )
    if df.empty:
        return {"symbol": symbol, "bars": [], "count": 0}
    # Reverse to ASC for charting.
    df = df.iloc[::-1]
    return {
        "symbol": symbol.upper(),
        "bars":   df.to_dict("records"),
        "count":  len(df),
    }


@router.get("/intraday/latest/all")
def latest_intraday_per_symbol(limit: int = Query(default=50, ge=1, le=500)):
    """Latest 1-minute bar per symbol -- useful as the live grid for the dashboard."""
    df = read_sql(
        """SELECT DISTINCT ON (symbol)
                  symbol, ts, open, high, low, close, volume, vwap, n_ticks
             FROM ohlcv_intraday
         ORDER BY symbol, ts DESC
            LIMIT :lim""",
        params={"lim": limit},
    )
    return {"bars": df.to_dict("records"), "count": len(df)}


@router.get("/intraday/_status")
def stream_status():
    """Diagnostic: how many SSE subscribers are connected right now."""
    f = get_fanout()
    return {
        "fanout_subscribers": f.n_subscribers(),
    }
