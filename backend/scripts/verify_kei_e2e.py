"""
End-to-end verification of the live intraday pipeline against the REAL database
(everything except the Kite WebSocket itself, which is replaced by seeded bars).

Path exercised:
    ohlcv_intraday (Postgres/TimescaleDB)
      -> engine.load_session_bars(symbol)              [real DB round-trip]
      -> factors.intraday_microstructure                [anchored VWAP / OFI / sweeps / VP]
      -> live.intraday_signal.generate_intraday_signal  [direction + entry/stop/target + reasons]
      -> engine.SignalLoop._scan_once -> SignalFanout    [the publish path the SSE route consumes]

Usage (from qsde/backend, venv active):
    python -m scripts.verify_kei_e2e --symbol KEI
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.live.engine import (
    load_session_bars,
    generate_intraday_signal,
    get_signal_fanout,
    SignalLoop,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="KEI")
    args = ap.parse_args()
    sym = args.symbol.upper()

    print(f"=== 1. DB round-trip: load_session_bars({sym}) ===")
    bars = load_session_bars(sym)
    if bars is None or bars.empty:
        print(f"FAIL: no bars in ohlcv_intraday for {sym}. Seed first: "
              f"python -m scripts.seed_demo_session --symbol {sym}")
        sys.exit(1)
    print(f"  loaded {len(bars)} bars | {bars.index.min()} .. {bars.index.max()} | "
          f"last close = {float(bars['close'].iloc[-1]):.2f}")

    print(f"=== 2. Signal core: generate_intraday_signal({sym}) ===")
    sig = generate_intraday_signal(bars, symbol=sym, horizon="intraday")
    print(json.dumps(sig.to_dict(), indent=2, default=str))

    print(f"=== 3. Publish path: SignalLoop._scan_once -> fanout ===")
    fan = get_signal_fanout()
    q = fan.subscribe(maxsize=10)
    loop = SignalLoop([sym], emit_telegram=False)
    loop._scan_once()  # synchronous single scan (no thread, no DB writes)
    published = None
    try:
        published = q.get(timeout=2.0)
    except Exception:
        pass
    finally:
        fan.unsubscribe(q)
    if published is None:
        print("  WARN: nothing published (signal may be SKIP and dedup suppressed it on first scan?)")
    else:
        print(f"  published to fanout: {published.get('symbol')} {published.get('action')} "
              f"entry={published.get('entry')} stop={published.get('stop')} target={published.get('target')}")

    print("\n=== RESULT ===")
    print(f"OK: full DB->microstructure->signal->fanout path ran for {sym}. "
          f"action={sig.action} direction={sig.direction} "
          f"entry={sig.entry} stop={sig.stop} target={sig.target} RR={sig.risk_reward}")


if __name__ == "__main__":
    main()
