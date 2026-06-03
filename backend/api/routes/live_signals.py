"""
Live signals API: SSE stream of intraday signals + loop control.

  GET  /api/live/signals/stream?symbols=RELIANCE,KEI   -> SSE signal stream
  POST /api/live/start   {"symbols": [...], "horizon": "intraday"}  -> start loop
  POST /api/live/stop                                               -> stop loop
  GET  /api/live/status                                             -> loop status

The SSE stream consumes the process-wide SignalFanout. For ticks to flow, the
SignalLoop must run in THIS process -- start it via POST /api/live/start (the
loop then queries ohlcv_intraday each minute and publishes signals here). The
browser consumes via EventSource, exactly like /api/intraday/stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from typing import Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import StreamingResponse

from qsde.live.engine import (
    get_signal_fanout,
    start_signal_loop,
    stop_signal_loop,
    loop_status,
)

log = logging.getLogger(__name__)
router = APIRouter()


def _safe_get(q: "queue.Queue", timeout: float):
    try:
        return q.get(timeout=timeout)
    except Exception:
        return None


@router.get("/live/signals/stream")
async def stream_signals(
    symbols: Optional[str] = Query(default=None, description="Comma-separated filter"),
):
    """Server-Sent Events stream of intraday signals."""
    filter_set = (
        {s.strip().upper() for s in symbols.split(",") if s.strip()} if symbols else None
    )
    fan = get_signal_fanout()
    sub = fan.subscribe(maxsize=1000)

    async def event_generator():
        yield "event: ready\ndata: connected\n\n"
        try:
            loop = asyncio.get_event_loop()
            while True:
                sig = await loop.run_in_executor(None, lambda: _safe_get(sub, 15.0))
                if sig is None:
                    yield ": keepalive\n\n"
                    continue
                if filter_set and sig.get("symbol") not in filter_set:
                    continue
                yield f"data: {json.dumps(sig, default=str)}\n\n"
        finally:
            fan.unsubscribe(sub)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/live/start")
def start_live(payload: dict = Body(default={})):
    """Start (or restart) the in-process intraday signal loop."""
    symbols = payload.get("symbols") or []
    if not symbols:
        return {"started": False, "error": "no symbols provided"}
    horizon = payload.get("horizon", "intraday")
    emit_telegram = bool(payload.get("emit_telegram", True))
    loop = start_signal_loop(symbols, horizon=horizon, emit_telegram=emit_telegram)
    return {"started": True, "symbols": loop.symbols, "horizon": loop.horizon}


@router.post("/live/stop")
def stop_live():
    """Stop the in-process signal loop, if running."""
    return {"stopped": stop_signal_loop()}


@router.get("/live/status")
def status_live():
    """Report whether the loop is running, its symbols, and SSE subscriber count."""
    return loop_status()
