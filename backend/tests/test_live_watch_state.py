"""
Deterministic test for the live feed's state machine (scripts/live_watch.decide).

Proves the behaviour a closed-market snapshot can't show:
  * a setup fires only on a *fresh trigger bar* (not just "above VWAP"),
  * once in a trade the entry/stop/target are FIXED (do not re-quote every bar),
  * the position HOLDs until it actually reaches target or stop,
  * a winning path exits exactly once, then returns to WAIT (no re-entry churn),
  * the quiet bars before the trigger are WAIT, not "actionable".

The microstructure math itself (anchored VWAP, sweeps, volume profile) is tested
separately in test_intraday_microstructure.py; here we feed `decide` controlled
micro/signal inputs so the lifecycle logic is isolated and reproducible.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# scripts/ is not a package; import live_watch directly.
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))

import live_watch as lw  # noqa: E402


class _FakeSig:
    """Stand-in for IntradaySignal — decide() only reads .bias."""
    def __init__(self, bias: float):
        self.bias = bias


def _frames(closes, avwap):
    """Build (bars, micro) frames for a synthetic session of `closes`.

    avwap is held flat at `avwap` so we control exactly when price crosses it.
    Lows/highs hug the close; value-area/band columns sit outside so the
    structural stop has a sane anchor.
    """
    idx = pd.date_range("2026-06-02 09:15", periods=len(closes), freq="min")
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.10 for c in closes],
            "low": [c - 0.10 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )
    micro = pd.DataFrame(
        {
            "intraday_avwap": [avwap] * len(closes),
            "intraday_avwap_lower": [avwap - 1.5] * len(closes),
            "intraday_avwap_upper": [avwap + 1.5] * len(closes),
            "intraday_vp_val": [avwap - 0.5] * len(closes),
            "intraday_vp_vah": [avwap + 0.5] * len(closes),
            "intraday_sweep_low_reclaim": [0] * len(closes),
            "intraday_sweep_high_reject": [0] * len(closes),
        },
        index=idx,
    )
    return bars, micro


def _replay(closes, biases, avwap):
    """Drive decide() bar-by-bar with one shared state; return per-bar rows."""
    bars, micro = _frames(closes, avwap)
    st = lw._State()
    rows = []
    for i in range(1, len(closes)):  # need >=2 bars for prev-bar comparison
        rows.append(lw.decide(bars.iloc[: i + 1], micro.iloc[: i + 1], _FakeSig(biases[i]), st))
    return rows, st


def test_long_lifecycle_enter_hold_exit_win():
    # 0-14 drift DOWN below avwap(98.5); bar 15 jumps ABOVE -> fresh long trigger;
    # 16-18 rise into the target; 19 stays up (must NOT re-enter).
    closes = [100, 99.8, 99.6, 99.4, 99.2, 99.0, 98.8, 98.6, 98.4, 98.2,
              98.0, 97.9, 97.8, 97.7, 97.6,        # 0-14 below VWAP
              99.5,                                 # 15 cross up -> ENTER
              100.2, 100.8, 101.6, 102.0]           # 16-19 run up -> EXIT win
    biases = [0.0] * 15 + [0.6, 0.0, 0.0, 0.0, 0.0]
    rows, st = _replay(closes, biases, avwap=98.5)
    kinds = [r["kind"] for r in rows]  # rows start at bar index 1

    # Quiet pre-trigger tape is WAIT, never "actionable".
    assert all(k == "wait" for k in kinds[:14]), kinds  # bars 1..14

    # Exactly one entry, and it's a long.
    enters = [r for r in rows if r["kind"] == "enter"]
    assert len(enters) == 1
    entry_row = enters[0]
    assert entry_row["side"] == 1
    assert entry_row["pos"].entry == pytest.approx(99.5)

    # Levels are sane: stop below entry, target above, ~2:1.
    e, s, t = entry_row["pos"].entry, entry_row["pos"].stop, entry_row["pos"].target
    assert s < e < t
    assert (t - e) / (e - s) == pytest.approx(lw._RR, rel=1e-6)

    # While holding, levels are FIXED (the headline fix — no per-bar re-quote).
    holds = [r for r in rows if r["kind"] == "hold"]
    assert holds, "expected at least one HOLD bar between entry and exit"
    for h in holds:
        assert h["pos"].entry == e
        assert h["pos"].stop == s
        assert h["pos"].target == t

    # Exactly one exit, a win, and flat afterwards.
    exits = [r for r in rows if r["kind"] in ("exit_win", "exit_loss")]
    assert len(exits) == 1
    assert exits[0]["kind"] == "exit_win"
    assert exits[0]["pnl"] > 0
    assert st.pos is None
    assert kinds[-1] == "wait"  # bar 19: above VWAP but no fresh cross -> WAIT


def test_being_above_vwap_is_not_a_trigger():
    # Price opens and stays ABOVE avwap the whole time with strong bias.
    # The OLD logic called this BUY every bar; the new logic must WAIT (no cross).
    closes = [101, 101.1, 101.0, 101.2, 101.1, 101.3, 101.2, 101.4, 101.3, 101.5,
              101.4, 101.6, 101.5, 101.7, 101.6, 101.8]
    biases = [0.6] * len(closes)
    rows, st = _replay(closes, biases, avwap=100.0)
    assert all(r["kind"] == "wait" for r in rows), [r["kind"] for r in rows]
    assert st.pos is None


def test_long_stop_out_is_a_loss_and_resets():
    # Cross up -> ENTER, then collapse through the stop -> EXIT loss -> flat.
    closes = [100, 99.8, 99.6, 99.4, 99.2, 99.0, 98.8, 98.6, 98.4, 98.2,
              98.0, 97.9, 97.8, 97.7, 97.6,   # below VWAP
              99.5,                            # cross up -> ENTER
              99.0, 98.3, 97.5, 97.0]          # dump through stop -> EXIT loss
    biases = [0.0] * 15 + [0.6, 0.0, 0.0, 0.0, 0.0]
    rows, st = _replay(closes, biases, avwap=98.5)
    exits = [r for r in rows if r["kind"] in ("exit_win", "exit_loss")]
    assert len(exits) == 1
    assert exits[0]["kind"] == "exit_loss"
    assert exits[0]["pnl"] < 0
    assert st.pos is None
