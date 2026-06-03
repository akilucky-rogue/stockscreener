"""
Tests for the pure helpers in qsde/live/engine.py (Phase 2).

The loop's DB/Telegram I/O needs a live stack to exercise; here we test the
decision/formatting helpers that govern WHAT gets emitted and alerted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.live.intraday_signal import IntradaySignal
from qsde.live.engine import (
    is_new_bar,
    should_alert,
    alert_worthy,
    signal_log_line,
    build_telegram_alert,
    get_signal_fanout,
)


def _sig(action="BUY", direction=1, quality="good", ts="2024-03-01T10:00:00", symbol="KEI"):
    return IntradaySignal(
        symbol=symbol, ts=ts, horizon="intraday", price=100.0, direction=direction,
        action=action, bias=0.6, confidence=0.6, entry=100.0, stop=99.0, target=102.0,
        risk_reward=2.0, quality=quality, reasons=["above anchored VWAP", "buy-side order-flow"],
    )


def test_is_new_bar():
    s = _sig(ts="2024-03-01T10:01:00")
    assert is_new_bar(None, s) is True
    assert is_new_bar({"ts": "2024-03-01T10:00:00"}, s) is True   # ts advanced
    assert is_new_bar({"ts": "2024-03-01T10:01:00"}, s) is False  # same bar


def test_should_alert():
    assert should_alert(_sig(action="BUY", quality="good")) is True
    assert should_alert(_sig(action="WATCH", quality="good")) is False
    assert should_alert(_sig(action="BUY", quality="low")) is False


def test_alert_worthy_dedupes():
    new = _sig(action="BUY", direction=1)
    # first actionable -> alert
    assert alert_worthy(None, new) is True
    # same action+direction -> no repeat alert
    assert alert_worthy({"action": "BUY", "direction": 1}, new) is False
    # direction flip -> alert
    assert alert_worthy({"action": "SELL", "direction": -1}, new) is True
    # non-actionable never alerts
    assert alert_worthy(None, _sig(action="WATCH", quality="good")) is False


def test_signal_log_line_is_json():
    s = _sig()
    d = json.loads(signal_log_line(s))
    assert d["symbol"] == "KEI" and d["action"] == "BUY"
    assert d["entry"] == 100.0 and d["target"] == 102.0


def test_telegram_alert_contains_levels():
    msg = build_telegram_alert(_sig())
    assert "KEI" in msg and "BUY" in msg
    assert "Entry" in msg and "SL" in msg and "Target" in msg


def test_signal_fanout_pubsub():
    fan = get_signal_fanout()
    q = fan.subscribe(maxsize=10)
    fan.publish({"symbol": "KEI", "_type": "signal"})
    got = q.get(timeout=1.0)
    assert got["symbol"] == "KEI"
    fan.unsubscribe(q)
