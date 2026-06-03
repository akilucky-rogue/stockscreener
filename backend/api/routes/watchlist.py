"""
Watchlist CRUD endpoints.

Single-user app for now -- watchlist rows aren't tied to a user_id. When
auth is added (per Open Question 2 in the blueprint), filter by user_id.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from qsde.db import execute_sql, read_sql

log = logging.getLogger(__name__)
router = APIRouter()


class WatchlistAdd(BaseModel):
    symbol: str
    notes: Optional[str] = None


@router.get("/watchlist")
def list_watchlist():
    """Return all watchlist rows joined with the latest swing signal."""
    try:
        df = read_sql(
            """
            SELECT w.id, w.symbol, w.added_at, w.source, w.notes,
                   u.company_name, u.sector,
                   s.direction, s.confidence, s.predicted_return, s.ranking_score
              FROM watchlist w
              LEFT JOIN universe u  ON u.symbol = w.symbol
              LEFT JOIN LATERAL (
                  SELECT direction, confidence, predicted_return, ranking_score
                    FROM signals
                   WHERE symbol = w.symbol AND horizon = 'swing'
                ORDER BY date DESC LIMIT 1
              ) s ON TRUE
             ORDER BY w.added_at DESC
            """
        )
        return {"watchlist": df.to_dict("records"), "count": len(df)}
    except Exception as e:
        return {"watchlist": [], "count": 0, "error": str(e)}


@router.post("/watchlist")
def add_to_watchlist(body: WatchlistAdd):
    """Insert (or upsert) a watchlist row."""
    sym = body.symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")
    try:
        execute_sql(
            """INSERT INTO watchlist (symbol, source, notes)
               VALUES (%(symbol)s, 'manual', %(notes)s)
               ON CONFLICT (symbol) DO UPDATE SET notes = EXCLUDED.notes""",
            {"symbol": sym, "notes": body.notes},
        )
        return {"ok": True, "symbol": sym}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str):
    """Remove a symbol from the watchlist."""
    sym = symbol.strip().upper()
    try:
        execute_sql("DELETE FROM watchlist WHERE symbol = %(s)s", {"s": sym})
        return {"ok": True, "symbol": sym}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
