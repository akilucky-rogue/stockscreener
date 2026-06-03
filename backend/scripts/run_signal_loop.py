"""
Run the QSDE intraday signal loop standalone (structured logs + Telegram).

This is the headless / pure-performance surface: no API, no browser. It tails
ohlcv_intraday each minute, computes the white-box intraday signal per symbol,
logs one JSON line per new bar, and pings Telegram on actionable changes.

Usage (from qsde/backend, with the venv active):
    python -m scripts.run_signal_loop --symbols RELIANCE,TCS,KEI
    python -m scripts.run_signal_loop --symbols KEI --no-telegram --interval 60

Prereqs: ohlcv_intraday is being populated (run scripts/kite_stream.py during
market hours, or seed bars). DB config comes from qsde/.env.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Make `qsde` importable when run as a script.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.live.engine import SignalLoop  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="QSDE intraday signal loop")
    ap.add_argument("--symbols", required=True, help="Comma-separated NSE symbols (no .NS)")
    ap.add_argument("--horizon", default="intraday", choices=["intraday", "swing", "long"])
    ap.add_argument("--interval", type=float, default=60.0, help="Scan interval seconds")
    ap.add_argument("--no-telegram", action="store_true", help="Disable Telegram alerts")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        ap.error("no symbols parsed from --symbols")

    loop = SignalLoop(
        symbols,
        horizon=args.horizon,
        interval_sec=args.interval,
        emit_telegram=not args.no_telegram,
    )
    loop.start()
    logging.getLogger(__name__).info(
        "SignalLoop running for %s (horizon=%s). Ctrl-C to stop.", symbols, args.horizon
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        loop.stop()
        loop.join(timeout=5)
        logging.getLogger(__name__).info("SignalLoop stopped.")


if __name__ == "__main__":
    main()
