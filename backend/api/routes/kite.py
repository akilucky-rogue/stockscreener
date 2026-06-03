"""
Kite Connect OAuth + status endpoints.

Kite's auth model requires a *daily* human-in-the-loop login (tokens expire
at 06:00 IST). Flow:

  GET  /api/kite/login_url        -> {"login_url": "https://kite.zerodha.com/..."}
  Frontend redirects user to that URL. Zerodha logs them in and bounces back
  to KITE_REDIRECT_URL (configured in dev console) with ?request_token=XYZ.

  GET  /api/kite/callback?request_token=XYZ
        -> exchanges request_token for access_token, stores in DB, returns
           a small JSON ack. The frontend can then redirect to /dashboard.

  GET  /api/kite/status
        -> {"authenticated": true/false, "user_name": "...", "expires_at": "..."}
        -> Frontend uses this to decide whether to show a "Login to Kite" banner.

  POST /api/kite/refresh_instruments
        -> pulls the daily Kite instrument list into kite_instruments table.
           Run once after each login so historical_ohlcv() can find tokens.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from qsde.config import settings
from qsde.db.connection import read_sql

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/kite/login_url")
def kite_login_url():
    """Return the Zerodha-hosted login URL the frontend should redirect to."""
    if not settings.kite_api_key:
        raise HTTPException(
            status_code=400,
            detail="KITE_API_KEY not configured. Set it in .env.",
        )
    try:
        from qsde.ingestion.kite_client import get_kite_client
        client = get_kite_client()
        return {"login_url": client.login_url()}
    except Exception as e:
        log.exception("kite_login_url failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/kite/callback", response_class=HTMLResponse)
def kite_callback(
    request_token: str = Query(..., description="From Zerodha redirect"),
    status: str = Query(default="success"),
):
    """OAuth redirect target. Zerodha sends `request_token` here; we
    exchange it for an `access_token` and persist to DB.

    Returns a simple HTML page (not JSON) so the user lands somewhere readable
    in the browser. Frontend should poll /kite/status after redirecting back.
    """
    if status != "success":
        return HTMLResponse(
            f"<h3>Kite login failed.</h3><p>Status: {status}</p>",
            status_code=400,
        )
    try:
        from qsde.ingestion.kite_client import get_kite_client
        client = get_kite_client()
        info = client.exchange_token(request_token)
        return HTMLResponse(
            f"""
            <html><head><title>Kite Connected</title></head><body
                style="font-family:monospace;background:#0a0e14;color:#0f8;padding:48px;">
              <h2>✓ Kite connected</h2>
              <p>User: <strong>{info.get('user_name') or '-'}</strong></p>
              <p>Token valid until: <strong>{info.get('expires_at')}</strong></p>
              <p>You can close this tab and return to the dashboard.</p>
              <script>setTimeout(()=>{{ window.location.href = "http://localhost:3000/"; }}, 2500);</script>
            </body></html>
            """
        )
    except Exception as e:
        log.exception("kite_callback failed")
        return HTMLResponse(
            f"<h3>Token exchange failed.</h3><pre>{e}</pre>",
            status_code=500,
        )


@router.post("/kite/exchange")
def kite_exchange(request_token: str = Query(..., description="request_token from the Zerodha redirect URL")):
    """Manual token exchange. Use when the browser redirect didn't hit /callback
    (e.g. the Kite app's Redirect URL isn't http://127.0.0.1:8000/api/kite/callback).
    Paste the request_token you landed on. Single-use; expires in ~2 minutes.
    """
    try:
        from qsde.ingestion.kite_client import get_kite_client
        client = get_kite_client()
        info = client.exchange_token(request_token)
        return {"authenticated": True, **info}
    except Exception as e:
        log.exception("kite_exchange failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/kite/status")
def kite_status():
    """Is the Kite session live? Used by the dashboard to show a banner."""
    df = read_sql(
        """SELECT user_id, user_name, expires_at
             FROM kite_tokens
            WHERE is_active = TRUE
              AND expires_at > NOW()
         ORDER BY login_time DESC
            LIMIT 1"""
    )
    if df.empty:
        return {"authenticated": False, "configured": bool(settings.kite_api_key)}
    row = df.iloc[0]
    return {
        "authenticated": True,
        "configured":    True,
        "user_id":       row["user_id"],
        "user_name":     row["user_name"],
        "expires_at":    row["expires_at"].isoformat() if hasattr(row["expires_at"], "isoformat") else str(row["expires_at"]),
    }


@router.post("/kite/refresh_instruments")
def kite_refresh_instruments(exchange: str = Query(default="NSE")):
    """Pull the daily Kite instrument list into kite_instruments.

    Idempotent. Run once per login day so historical_ohlcv() can resolve
    tradingsymbol -> instrument_token.
    """
    try:
        from qsde.ingestion.kite_client import get_kite_client
        client = get_kite_client()
        n = client.refresh_instruments(exchange=exchange)
        return {"refreshed": n, "exchange": exchange}
    except Exception as e:
        log.exception("kite_refresh_instruments failed")
        raise HTTPException(status_code=500, detail=str(e))
