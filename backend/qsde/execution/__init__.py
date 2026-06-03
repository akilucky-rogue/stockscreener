"""Semi-auto execution layer (Phase 5).

Human-confirmed order tickets. QSDE never auto-fires: build a ticket, a human
confirms (echoing the confirm_token), and live placement is additionally gated
behind an env flag + kill-switch + risk gates. dry-run is the default.
"""
from qsde.execution.order_tickets import (
    OrderTicket,
    build_ticket,
    check_risk_gates,
    verify_confirm,
    place_dry_run,
    place_live,
    kill_switch_on,
    set_kill_switch,
    live_orders_enabled,
)

__all__ = [
    "OrderTicket",
    "build_ticket",
    "check_risk_gates",
    "verify_confirm",
    "place_dry_run",
    "place_live",
    "kill_switch_on",
    "set_kill_switch",
    "live_orders_enabled",
]
