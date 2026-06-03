"""
Order ticket core — build, validate, gate, confirm, place (Phase 5).

Pure / DB-free (the route handles persistence). Safety model, defence in depth:

  1. dry_run is the DEFAULT. A ticket placed dry-run never touches the broker;
     it records a simulated order id for audit.
  2. LIVE placement requires ALL of:
       • env QSDE_ENABLE_LIVE_ORDERS truthy        (operator opt-in, off by default)
       • kill-switch OFF                            (global panic stop)
       • the caller echoes the ticket's confirm_token (no accidental fire)
       • risk gates pass (max positions, capital cap, day-loss cap)
       • the Kite client is authenticated
  3. Every ticket is auditable: status flow SUGGESTED -> CONFIRMED ->
     (DRYRUN | PLACED) | REJECTED | FAILED, with timestamps (persisted by the route).

This module deliberately does NOT manage stop/target as live bracket legs yet —
v1 places the ENTRY only and records SL/target as the managed plan. Wiring SL/
target as GTT/OCO legs is a follow-up once entry placement is proven live.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from qsde.risk.budget_screener import size_position

# Status constants
SUGGESTED = "SUGGESTED"
CONFIRMED = "CONFIRMED"
DRYRUN = "DRYRUN"
PLACED = "PLACED"
REJECTED = "REJECTED"
FAILED = "FAILED"


# ── Global kill-switch (in-process; flip via API) ────────────────────────────
_KILL = {"on": False}


def kill_switch_on() -> bool:
    return bool(_KILL["on"])


def set_kill_switch(on: bool) -> bool:
    _KILL["on"] = bool(on)
    return _KILL["on"]


def live_orders_enabled() -> bool:
    """Live broker placement is OFF unless the operator opts in via env."""
    return os.getenv("QSDE_ENABLE_LIVE_ORDERS", "false").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class OrderTicket:
    ticket_id: str
    symbol: str
    side: str                 # BUY / SELL
    qty: int
    order_type: str           # MARKET / LIMIT
    product: str              # MIS / CNC / NRML
    entry_price: Optional[float]
    limit_price: Optional[float]
    stop_price: Optional[float]
    target_price: Optional[float]
    risk_reward: Optional[float]
    horizon: str
    bias: Optional[float]
    confidence: Optional[float]
    capital_required: float
    risk_at_stop: Optional[float]
    confirm_token: str
    status: str = SUGGESTED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _confirm_token(symbol: str, side: str, qty: int, entry, stop, target, nonce: str) -> str:
    raw = f"{symbol}|{side}|{qty}|{entry}|{stop}|{target}|{nonce}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_ticket(
    signal: dict,
    *,
    budget: float,
    risk_per_trade: float,
    lot_size: int = 1,
    product: str = "MIS",
    order_type: str = "MARKET",
    max_position_weight: float = 1.0,
) -> OrderTicket:
    """Build a SUGGESTED ticket from a signal dict + budget/risk sizing.

    Raises ValueError if the signal isn't tradeable (HOLD, or unaffordable).
    """
    direction = int(signal.get("direction", 0) or 0)
    if direction == 0:
        raise ValueError("signal is HOLD/flat (direction == 0); nothing to place")

    price = signal.get("price") or signal.get("entry")
    if not price or price <= 0:
        raise ValueError("signal has no usable price")
    entry = float(signal.get("entry", price))
    stop = signal.get("stop")
    target = signal.get("target")

    per_position_cap = max(min(max_position_weight, 1.0), 0.0) * float(budget)
    sizing = size_position(
        float(price), entry, stop,
        budget=per_position_cap, risk_per_trade=float(risk_per_trade), lot_size=lot_size,
    )
    if not sizing["affordable"] or sizing["final_qty"] <= 0:
        raise ValueError(
            f"not tradeable in budget: final_qty={sizing['final_qty']} "
            f"(budgeted={sizing['budgeted_qty']}, risk-capped={sizing['max_qty']})"
        )

    side = "BUY" if direction > 0 else "SELL"
    qty = int(sizing["final_qty"])
    if order_type not in ("MARKET", "LIMIT"):
        raise ValueError(f"unsupported order_type {order_type}")
    if product not in ("MIS", "CNC", "NRML"):
        raise ValueError(f"unsupported product {product}")

    nonce = uuid.uuid4().hex
    token = _confirm_token(signal["symbol"], side, qty, entry, stop, target, nonce)

    return OrderTicket(
        ticket_id=str(uuid.uuid4()),
        symbol=str(signal["symbol"]).upper(),
        side=side,
        qty=qty,
        order_type=order_type,
        product=product,
        entry_price=round(entry, 2),
        limit_price=round(entry, 2) if order_type == "LIMIT" else None,
        stop_price=round(float(stop), 2) if stop is not None else None,
        target_price=round(float(target), 2) if target is not None else None,
        risk_reward=signal.get("risk_reward"),
        horizon=str(signal.get("horizon", "intraday")),
        bias=signal.get("bias"),
        confidence=signal.get("confidence"),
        capital_required=float(sizing["capital_required"]),
        risk_at_stop=sizing["risk_at_stop"],
        confirm_token=token,
        reasons=list(signal.get("reasons", [])),
    )


def verify_confirm(ticket_confirm_token: str, provided_token: str) -> bool:
    """Constant-ish-time match of the echoed confirm token."""
    if not ticket_confirm_token or not provided_token:
        return False
    return hmac.compare_digest(str(ticket_confirm_token), str(provided_token))


def check_risk_gates(
    *,
    live: bool,
    n_open: int = 0,
    max_positions: int = 10,
    deployed_capital: float = 0.0,
    capital_cap: float = float("inf"),
    incoming_capital: float = 0.0,
    day_loss: float = 0.0,
    max_day_loss: float = float("inf"),
) -> tuple[bool, list[str]]:
    """Pure pre-placement risk checks. Returns (ok, reasons-if-blocked)."""
    reasons: list[str] = []
    if kill_switch_on():
        reasons.append("kill-switch is ON")
    if live and not live_orders_enabled():
        reasons.append("live orders disabled (set QSDE_ENABLE_LIVE_ORDERS=true to enable)")
    if n_open >= max_positions:
        reasons.append(f"max open positions reached ({n_open}/{max_positions})")
    if deployed_capital + incoming_capital > capital_cap:
        reasons.append(
            f"capital cap exceeded ({deployed_capital + incoming_capital:.0f} > {capital_cap:.0f})"
        )
    if day_loss <= -abs(max_day_loss) and max_day_loss != float("inf"):
        reasons.append(f"daily loss cap hit ({day_loss:.0f})")
    return (len(reasons) == 0, reasons)


def place_dry_run(ticket: OrderTicket) -> dict:
    """Simulate placement — NEVER touches the broker. Default path."""
    return {
        "status": DRYRUN,
        "broker_order_id": f"DRYRUN-{ticket.ticket_id[:8]}",
        "dry_run": True,
        "message": "dry-run: no live order sent",
    }


def place_live(ticket: OrderTicket, *, kite=None, exchange: str = "NSE", tag: str = "qsde") -> dict:
    """Place a REAL order via Kite. Only call after gates pass + confirm verified.

    Returns {status, broker_order_id} on success or {status: FAILED, error}.
    """
    if not live_orders_enabled():
        return {"status": REJECTED, "error": "live orders disabled (QSDE_ENABLE_LIVE_ORDERS)"}
    if kill_switch_on():
        return {"status": REJECTED, "error": "kill-switch ON"}
    try:
        if kite is None:
            from qsde.ingestion.kite_client import get_kite_client
            kite = get_kite_client()
        order_id = kite.place_order(
            tradingsymbol=ticket.symbol,
            transaction_type=ticket.side,
            quantity=ticket.qty,
            product=ticket.product,
            order_type=ticket.order_type,
            price=ticket.limit_price if ticket.order_type == "LIMIT" else None,
            exchange=exchange,
            tag=tag,
        )
        return {"status": PLACED, "broker_order_id": str(order_id), "dry_run": False}
    except Exception as e:  # noqa: BLE001
        return {"status": FAILED, "error": str(e), "dry_run": False}
