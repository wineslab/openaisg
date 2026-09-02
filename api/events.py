"""Event fan-out to WebSocket clients.

The serial worker runs in its own thread and must never be slowed down by a
subscriber -- a laptop on bad wifi cannot be allowed to stall the AISG bus.
So each subscriber gets a bounded queue and the oldest event is dropped when
it fills, with a monotonic sequence number and a replay ring so a client can
resynchronise instead of silently missing an alarm.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import logging
import time

logger = logging.getLogger("openaisg.events")

QUEUE_DEPTH = 256
REPLAY = 500


class Subscriber:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self.dropped = 0


class EventBus:
    """Publishes worker events to subscribers. Event-loop thread only."""

    def __init__(self, replay: int = REPLAY):
        self._subs: set[Subscriber] = set()
        self._ring: collections.deque = collections.deque(maxlen=replay)
        self._seq = itertools.count(1)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish_nowait(self, event: dict):
        event = {"seq": next(self._seq), "ts": round(time.time(), 3), **event}
        self._ring.append(event)
        for sub in self._subs:
            try:
                sub.q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest, never block the publisher.
                try:
                    sub.q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                sub.dropped += 1
                try:
                    sub.q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def replay_since(self, seq: int) -> list[dict]:
        return [e for e in self._ring if e["seq"] > seq]

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber):
        self._subs.discard(sub)


class WorkerBridge:
    """Thread-safe hop from the worker into the event loop.

    The worker calls emit() from its own thread; call_soon_threadsafe is the
    only supported way across that boundary.
    """

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def emit(self, event: dict):
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self.bus.publish_nowait, event)
        except RuntimeError:
            pass  # loop closed during shutdown
