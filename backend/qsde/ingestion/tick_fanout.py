"""
Thread-safe tick fanout.

The Kite WebSocket runs in a Twisted reactor thread (managed by the
kiteconnect SDK). Multiple in-process consumers need each tick:

  * The minute-bar aggregator -- writes to ohlcv_intraday
  * The raw-tick logger -- writes to ticks_raw (debug / replay)
  * The SSE endpoint -- streams to browser clients

Rather than have each one independently subscribe to KiteTicker callbacks
(which only allows one `on_ticks` handler), we publish to a single
fanout queue and let consumers register their own bounded buffers.

Design properties:
  * Subscribers are decoupled from publishers via a thread-safe lock.
  * Each subscriber gets a bounded queue -- slow consumers don't back up
    the WebSocket thread. If a queue overflows, we drop ticks on that
    subscriber only (with a counter for ops visibility).
  * The fanout is a module-level singleton -- get_fanout().
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional


log = logging.getLogger(__name__)


class TickFanout:
    """Many-readers, one-writer tick distribution.

    Publishers call `publish(tick_list)`. Subscribers call `subscribe()`
    to receive a `queue.Queue` they can `get()` from in their own thread.
    """

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._dropped: dict[int, int] = {}   # queue_id -> drop count

    def publish(self, tick: dict) -> None:
        """Push one tick to all subscribers. Drops on overflow per-subscriber."""
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(tick)
            except queue.Full:
                qid = id(q)
                self._dropped[qid] = self._dropped.get(qid, 0) + 1
                if self._dropped[qid] % 100 == 1:
                    log.warning(
                        "Subscriber queue full (id=%s); %d ticks dropped so far.",
                        qid, self._dropped[qid],
                    )

    def subscribe(self, maxsize: int = 5000) -> queue.Queue:
        """Register a new subscriber. Returns its dedicated queue."""
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.add(q)
        log.info("Subscriber registered (queue_id=%s, maxsize=%d)", id(q), maxsize)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a subscriber. Idempotent."""
        with self._lock:
            self._subscribers.discard(q)
        log.info("Subscriber removed (queue_id=%s)", id(q))

    def n_subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)


# Module-level singleton.
_INSTANCE: Optional[TickFanout] = None


def get_fanout() -> TickFanout:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = TickFanout()
    return _INSTANCE
