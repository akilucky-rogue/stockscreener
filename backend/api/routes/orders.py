"""
Semi-auto order ticket API (Phase 5).

  POST /api/orders/ticket    build a SUGGESTED ticket (from a signal + budget/risk)
  POST /api/orders/confirm   human confirm -> dry-run (default) or live placement
  GET  /api/orders           recent tickets
  GET  /api/orders/kill-switch   current kill-switch + live-enabled state
  POST /api/orders/kill-switch   {"enabled": true|false}  toggle the panic stop

Safety: nothing reaches the broker unless the request says live:true AND
QSDE_ENABLE_LIVE_ORDERS is set AND the kill-switch is off AND the echoed
confirm_token matches AND risk gates pass. Default is always dry-run.
"""

from __future__ import annotations

import json
import logging
from fastapi import APIRouter, Body, HTTPException

from qsde.db import execute_sql, read_sql
from qsde.execution import (
    build_ticket,
    verify_confirm,
    check_risk_gates,
    place_dry_run,
    place_live,
    kill_switch_on,
    set_kill_switch,
    live_orders_enabled,
)
from qsde.execution.order_tickets import OrderTicket, SUGGESTED, PLACED, DRYRUN, REJECTED
from qsde.live.engine import latest_signals

log = logging.getLogger(__name__)
router = APIRouter()


def _persist_new(t: OrderTicket) -> None:
    execute_sql(
        """INSERT INTO orders (
               ticket_id, symbol, side, qty, order_type, product,
               entry_price, limit_price, stop_price, target_price, risk_reward,
               horizon, bias, confidence, capital_required, risk_at_stop,
               status, confirm_token, dry_run, reasons)
           VALUES (
               %(ticket_id)s, %(symbol)s, %(side)s, %(qty)s, %(order_type)s, %(product)s,
               %(entry_price)s, %(limit_price)s, %(stop_price)s, %(target_price)s, %(risk_reward)s,
               %(horizon)s, %(bias)s, %(confidence)s, %(capital_required)s, %(risk_at_stop)s,
               %(status)s, %(confirm_token)s, TRUE, %(reasons)s::jsonb)""",
        {
            "ticket_id": t.ticket_id, "symbol": t.symbol, "side": t.side, "qty": t.qty,
            "order_type": t.order_type, "product": t.product, "entry_price": t.entry_price,
            "limit_price": t.limit_price, "stop_price": t.stop_price, "target_price": t.target_price,
            "risk_reward": t.risk_reward, "horizon": t.horizon, "bias": t.bias,
            "confidence": t.confidence, "capital_required": t.capital_required,
            "risk_at_stop": t.risk_at_stop, "status": t.status, "confirm_token": t.confirm_token,
            "reasons": json.dumps(t.reasons),
        },
    )


def _get_row(ticket_id: str) -> dict | None:
    df = read_sql("SELECT * FROM orders WHERE ticket_id = :tid", params={"tid": ticket_id})
    return None if df.empty else df.iloc[0].to_dict()


def _update(ticket_id: str, **fields) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in fields) + ", updated_at = NOW()"
    params = {**fields, "tid": ticket_id}
    execute_sql(f"UPDATE orders SET {sets} WHERE ticket_id = %(tid)s", params)


@router.post("/orders/ticket")
def create_ticket(payload: dict = Body(default={})):
    signal = payload.get("signal")
    if not signal:
        sym = (payload.get("symbol") or "").upper()
        signal = next((s for s in latest_signals() if s.get("symbol") == sym), None)
        if not signal:
            raise HTTPException(404, f"no live signal for '{sym}'. Pass 'signal' or start the live loop.")
    budget = float(payload.get("budget", 0) or 0)
    risk = float(payload.get("risk_per_trade", 0) or 0)
    if budget <= 0 or risk <= 0:
        raise HTTPException(400, "budget and risk_per_trade must both be > 0")
    try:
        t = build_ticket(
            signal,
            budget=budget,
            risk_per_trade=risk,
            lot_size=int(payload.get("lot_size", 1)),
            product=payload.get("product", "MIS"),
            order_type=payload.get("order_type", "MARKET"),
            max_position_weight=float(payload.get("max_position_weight", 1.0)),
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    _persist_new(t)
    out = t.to_dict()
    out["note"] = "SUGGESTED — POST /api/orders/confirm with {ticket_id, confirm_token} to place (dry-run by default)."
    return out


@router.post("/orders/confirm")
def confirm_ticket(payload: dict = Body(default={})):
    ticket_id = payload.get("ticket_id")
    token = payload.get("confirm_token")
    live = bool(payload.get("live", False))
    if not ticket_id or not token:
        raise HTTPException(400, "ticket_id and confirm_token are required")

    row = _get_row(ticket_id)
    if row is None:
        raise HTTPException(404, "ticket not found")
    if row["status"] != SUGGESTED:
        raise HTTPException(409, f"ticket already {row['status']} (only SUGGESTED can be confirmed)")
    if not verify_confirm(row["confirm_token"], token):
        raise HTTPException(403, "confirm_token mismatch")

    # Risk context from the DB (orders placed today).
    n_open = int(read_sql(
        "SELECT COUNT(*) AS n FROM orders WHERE status = 'PLACED' "
        "AND created_at >= date_trunc('day', NOW())"
    ).iloc[0]["n"])
    deployed = float(read_sql(
        "SELECT COALESCE(SUM(capital_required),0) AS c FROM orders WHERE status = 'PLACED' "
        "AND created_at >= date_trunc('day', NOW())"
    ).iloc[0]["c"])

    ok, reasons = check_risk_gates(
        live=live,
        n_open=n_open,
        max_positions=int(payload.get("max_positions", 10)),
        deployed_capital=deployed,
        capital_cap=float(payload.get("capital_cap", float("inf"))),
        incoming_capital=float(row.get("capital_required") or 0.0),
    )
    if not ok:
        _update(ticket_id, status=REJECTED, reasons=json.dumps(reasons))
        return {"ticket_id": ticket_id, "status": REJECTED, "reasons": reasons,
                "live_enabled": live_orders_enabled(), "kill_switch": kill_switch_on()}

    ticket = OrderTicket(
        ticket_id=str(row["ticket_id"]), symbol=row["symbol"], side=row["side"],
        qty=int(row["qty"]), order_type=row["order_type"], product=row["product"],
        entry_price=row.get("entry_price"), limit_price=row.get("limit_price"),
        stop_price=row.get("stop_price"), target_price=row.get("target_price"),
        risk_reward=row.get("risk_reward"), horizon=row.get("horizon", "intraday"),
        bias=row.get("bias"), confidence=row.get("confidence"),
        capital_required=float(row.get("capital_required") or 0.0),
        risk_at_stop=row.get("risk_at_stop"), confirm_token=row["confirm_token"],
    )
    result = place_live(ticket) if live else place_dry_run(ticket)
    _update(
        ticket_id,
        status=result["status"],
        dry_run=(not live),
        broker_order_id=result.get("broker_order_id"),
        error=result.get("error"),
    )
    return {"ticket_id": ticket_id, **result,
            "live_enabled": live_orders_enabled(), "kill_switch": kill_switch_on()}


@router.get("/orders")
def list_orders(limit: int = 50):
    df = read_sql(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT :lim", params={"lim": limit}
    )
    return {"orders": df.to_dict("records"), "count": len(df)}


@router.get("/orders/kill-switch")
def get_kill_switch():
    return {"kill_switch": kill_switch_on(), "live_enabled": live_orders_enabled()}


@router.post("/orders/kill-switch")
def post_kill_switch(payload: dict = Body(default={})):
    enabled = bool(payload.get("enabled", True))
    return {"kill_switch": set_kill_switch(enabled), "live_enabled": live_orders_enabled()}
