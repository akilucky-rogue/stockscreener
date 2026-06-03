"""
Tests for qsde/risk/budget_screener.py (Phase 3).

Pure sizing + ranking + budget-fit logic. No DB / no I/O.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.risk.budget_screener import size_position, screen_budget


def test_size_position_budget_bound():
    s = size_position(100.0, 100.0, 98.0, budget=100_000, risk_per_trade=5_000)
    # max_qty = 5000/2 = 2500; budgeted = 1000 -> budget binds
    assert s["budgeted_qty"] == 1000
    assert s["max_qty"] == 2500
    assert s["final_qty"] == 1000
    assert s["capital_required"] == 100_000.0
    assert s["risk_at_stop"] == 2000.0
    assert s["affordable"] is True


def test_size_position_risk_bound():
    # Wide stop (10 away): risk cap binds before budget.
    s = size_position(100.0, 100.0, 90.0, budget=100_000, risk_per_trade=2_000)
    assert s["max_qty"] == 200          # 2000/10
    assert s["final_qty"] == 200
    assert s["capital_required"] == 20_000.0
    assert s["risk_at_stop"] == 2000.0


def test_size_position_unaffordable():
    s = size_position(5000.0, 5000.0, 4900.0, budget=1_000, risk_per_trade=5_000)
    assert s["budgeted_qty"] == 0
    assert s["final_qty"] == 0
    assert s["affordable"] is False


def test_size_position_lot_rounding():
    # lot=150: 1000 affordable shares floor to 900 (6 lots).
    s = size_position(100.0, 100.0, 99.0, budget=100_000, risk_per_trade=10**9, lot_size=150)
    assert s["final_qty"] == 900
    assert s["capital_required"] == 90_000.0
    # lot bigger than affordable qty -> 0 -> dropped
    s2 = size_position(100.0, 100.0, 99.0, budget=1_000, risk_per_trade=10**9, lot_size=50)
    assert s2["final_qty"] == 0 and s2["affordable"] is False


def _candidates():
    return [
        {"symbol": "A", "direction": 1, "price": 100, "entry": 100, "stop": 98, "confidence": 0.8, "alpha_pct": 0.9},
        {"symbol": "B", "direction": 1, "price": 200, "entry": 200, "stop": 196, "confidence": 0.6, "alpha_pct": 0.7},
        {"symbol": "C", "direction": 0, "price": 50, "entry": 50, "stop": 49, "confidence": 0.9},      # no trade
        {"symbol": "D", "direction": -1, "price": 5000, "entry": 5000, "stop": 4900, "confidence": 0.9},  # short, no alpha
        {"symbol": "E", "direction": 1, "price": 1_000_000, "entry": 1_000_000, "stop": 990_000, "confidence": 0.9},  # unaffordable
    ]


def test_screen_ranks_filters_and_fits():
    res = screen_budget(
        _candidates(), budget=100_000, risk_per_trade=5_000,
        max_positions=10, max_position_weight=0.25,
    )
    sel = res["selected"]
    syms = [r["symbol"] for r in sel]
    # C (direction 0) and E (unaffordable) excluded
    assert "C" not in syms and "E" not in syms
    # D ranks highest (strength=conf=0.9), then A (0.9*0.8/0.4=1.8), then B
    assert syms == ["D", "A", "B"]
    # per-position cap respected (<= 25% of budget)
    assert all(r["capital_required"] <= 25_000 + 1e-6 for r in sel)
    # budget respected
    assert res["summary"]["capital_deployed"] <= 100_000 + 1e-6


def test_screen_summary_fields():
    res = screen_budget(_candidates(), budget=100_000, risk_per_trade=5_000, max_position_weight=0.25)
    s = res["summary"]
    assert s["n_candidates"] == 5
    assert s["n_tradeable"] == 3            # A, B, D
    assert s["n_selected"] == 3
    assert s["cash_remaining"] == round(100_000 - s["capital_deployed"], 2)
    assert s["total_risk_at_stop"] > 0


def test_screen_single_weight_consumes_budget():
    # Default weight 1.0: each name can use the whole budget -> only one fits.
    res = screen_budget(_candidates(), budget=100_000, risk_per_trade=5_000)
    assert res["summary"]["n_selected"] == 1
    assert res["selected"][0]["symbol"] == "D"  # top rank
