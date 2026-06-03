"""
Tests for qsde/execution/order_tickets.py (Phase 5 semi-auto core).

Pure / DB-free. Verifies ticket build + sizing, the confirm-token gate, the
risk gates, dry-run placement (never touches a broker), and that live placement
is OFF by default.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.execution.order_tickets import (
    build_ticket, verify_confirm, check_risk_gates, place_dry_run,
    kill_switch_on, set_kill_switch, live_orders_enabled,
    SUGGESTED, DRYRUN,
)


def _sig(direction=1, price=3957.68, stop=3948.78, target=3970.69):
    return {
        "symbol": "KEI", "ts": "2026-05-26T06:14:00+00:00", "horizon": "intraday",
        "price": price, "direction": direction, "action": "WATCH",
        "bias": 0.548, "confidence": 0.548, "entry": price, "stop": stop,
        "target": target, "risk_reward": 1.46, "quality": "low",
        "reasons": ["holding above anchored VWAP"],
    }


def test_build_ticket_long():
    t = build_ticket(_sig(direction=1), budget=200_000, risk_per_trade=5_000)
    assert t.side == "BUY"
    assert t.qty > 0
    assert t.status == SUGGESTED
    assert len(t.confirm_token) == 16
    assert t.entry_price == 3957.68 and t.stop_price == 3948.78 and t.target_price == 3970.69
    assert 0 < t.capital_required <= 200_000


def test_build_ticket_short():
    t = build_ticket(_sig(direction=-1), budget=200_000, risk_per_trade=5_000)
    assert t.side == "SELL"


def test_build_ticket_hold_raises():
    try:
        build_ticket(_sig(direction=0), budget=200_000, risk_per_trade=5_000)
        assert False, "expected ValueError for HOLD"
    except ValueError:
        pass


def test_build_ticket_unaffordable_raises():
    try:
        build_ticket(_sig(direction=1), budget=100, risk_per_trade=5_000)
        assert False, "expected ValueError for unaffordable"
    except ValueError:
        pass


def test_verify_confirm():
    t = build_ticket(_sig(), budget=200_000, risk_per_trade=5_000)
    assert verify_confirm(t.confirm_token, t.confirm_token) is True
    assert verify_confirm(t.confirm_token, "deadbeefdeadbeef") is False
    assert verify_confirm(t.confirm_token, "") is False


def test_live_disabled_by_default():
    # No env var set in the test environment.
    assert live_orders_enabled() is False


def test_risk_gates():
    # Clean dry-run passes.
    ok, reasons = check_risk_gates(live=False, n_open=0, max_positions=10)
    assert ok and not reasons

    # live=True blocked because live orders are disabled by default.
    ok, reasons = check_risk_gates(live=True, n_open=0, max_positions=10)
    assert not ok and any("live orders disabled" in r for r in reasons)

    # max positions reached.
    ok, reasons = check_risk_gates(live=False, n_open=10, max_positions=10)
    assert not ok and any("max open positions" in r for r in reasons)

    # capital cap exceeded.
    ok, reasons = check_risk_gates(
        live=False, deployed_capital=90_000, incoming_capital=20_000, capital_cap=100_000
    )
    assert not ok and any("capital cap" in r for r in reasons)

    # kill-switch blocks everything.
    try:
        set_kill_switch(True)
        ok, reasons = check_risk_gates(live=False, n_open=0, max_positions=10)
        assert not ok and any("kill-switch" in r for r in reasons)
        assert kill_switch_on() is True
    finally:
        set_kill_switch(False)
    assert kill_switch_on() is False


def test_place_dry_run_never_hits_broker():
    t = build_ticket(_sig(), budget=200_000, risk_per_trade=5_000)
    res = place_dry_run(t)
    assert res["status"] == DRYRUN
    assert res["broker_order_id"].startswith("DRYRUN-")
    assert res["dry_run"] is True
