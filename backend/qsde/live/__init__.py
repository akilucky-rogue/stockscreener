"""Live intraday signal layer (Phase 2).

`intraday_signal.generate_intraday_signal` is the pure, white-box decision
core: minute bars -> direction + entry/stop/target/RR + reasons. The streaming
wrapper (tick_fanout consumer -> SSE / JSON log / Telegram) builds on top of it.
"""
from qsde.live.intraday_signal import generate_intraday_signal, IntradaySignal

__all__ = ["generate_intraday_signal", "IntradaySignal"]
