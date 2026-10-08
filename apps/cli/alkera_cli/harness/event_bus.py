"""Typed in-memory pub/sub for IR events.

One bus per active chat (owned by the `HarnessRuntime`'s `ChatSession`
handle). Adapters publish; multiple consumers subscribe:

- The `ChatStore` writer (persists semantically-meaningful events to
  `chat.jsonl`; filters chunks/heartbeats via `NON_PERSISTED_EVENT_TYPES`).
- CLI REPL renderers / webview live-streamers (one per connection).

Backpressure policy:

- Subscriber queues are bounded. On overflow, the bus DROPS events for
  that subscriber and logs at warning. Dropping is the right tradeoff
  for chunk-level events (the finalized `PartCreated` carries the
  authoritative state; missing a chunk is recoverable).
- We never block the publisher. The harness can't tolerate stalls.
- The `chat.jsonl` writer is special — it's wired in via a synchronous
  callback path rather than this bus, so it can't be lossy. See
  `HarnessRuntime` for that wiring.

Design parallel: ACP's `session/update` notifications.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import weakref
from typing import TYPE_CHECKING

from alkera_cli.host.limits import env_count

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from alkera_core.schemas.chat import Event

logger = logging.getLogger(__name__)


# Default queue size per subscriber — how far a consumer may fall behind before
# its events are DROPPED. Tuned for ~4MB of buffered events in pathological
# cases (events average ~1KB JSON-encoded). Override per-subscribe call when the
# consumer is known to be slow (e.g. a webview over postMessage); the cloud
# mirror, which writes the web transcript, takes this default, so an operator on
# a slow link raises it here. 0 makes every default subscription unbounded, the
# shape persistence already uses — nothing is dropped, at the cost of memory.
ENV_QUEUE_MAXSIZE = "ALKERA_EVENT_QUEUE_MAXSIZE"
DEFAULT_QUEUE_MAXSIZE = env_count(os.environ.get(ENV_QUEUE_MAXSIZE), default=4096) or 0


class EventBus:
    """In-memory pub/sub. One per active chat.

    Lifecycle::

        bus = EventBus()
        # publishers:
        await bus.publish(event)
        # subscribers:
        sub = bus.subscribe()
        async for event in sub:
            ...
        # teardown:
        await bus.close()
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event | None]] = []
        self._closed = False
        self._published_count = 0
        self._event_sequences: dict[int, int] = {}
        # Counter of dropped events per subscriber — exposed for
        # debugging / metrics. Keyed by id(queue).
        self._dropped: dict[int, int] = {}
        # What the owner knows about an event that its publisher does not: the
        # session stamps who decided a permission ask on the harness's echo of
        # the reply, before the transcript or any mirror sees it. Applied once,
        # to every event, ahead of fan-out; ``None`` leaves events as published.
        self.rewrite: Callable[[Event], Event] | None = None
        # Events the owner adds AHEAD of one being published: a turn that ended
        # with nothing on screen gets its explanation just before the message's
        # completion. Each added event is rewritten like any other.
        self.precede: Callable[[Event], Sequence[Event]] | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def published_count(self) -> int:
        return self._published_count

    def sequence_for(self, event: Event) -> int | None:
        """Publish sequence for ``event`` when it is still in the recent window."""
        return self._event_sequences.get(id(event))

    # --- publisher side --------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Send `event` to every subscriber. Non-blocking — drops on
        a saturated subscriber rather than awaiting capacity.

        Raises `RuntimeError` if the bus has been closed.
        """
        if self._closed:
            raise RuntimeError("EventBus is closed")
        if self.precede is not None:
            for added in self.precede(event):
                self._fan_out(added)
        self._fan_out(event)

    def _fan_out(self, event: Event) -> None:
        if self.rewrite is not None:
            event = self.rewrite(event)
        self._published_count += 1
        key = id(event)
        self._event_sequences[key] = self._published_count
        weakref.finalize(event, self._event_sequences.pop, key, None)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                key = id(q)
                self._dropped[key] = self._dropped.get(key, 0) + 1
                # Throttle log spam — only log every Nth drop.
                if self._dropped[key] % 100 == 1:
                    logger.warning(
                        "EventBus subscriber dropped event (total drops on this subscriber: %d)",
                        self._dropped[key],
                    )

    # --- subscriber side -------------------------------------------------

    def subscribe(self, *, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> AsyncIterator[Event]:
        """Subscribe + return an async iterator over published events.

        The queue is registered IMMEDIATELY (synchronous side effect)
        so callers can do::

            sub = bus.subscribe()
            await bus.publish(ev)   # ev WILL hit `sub`
            async for ev in sub: ...

        Each call returns an independent stream; multiple subscribers
        receive every event. The iterator exits cleanly when `close()`
        is called.
        """
        if self._closed:
            raise RuntimeError("EventBus is closed")
        q: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return _Subscription(self, q)

    def drops_for_oldest_subscriber(self) -> int:
        """Drops on the first registered subscriber. Useful for tests."""
        if not self._subscribers:
            return 0
        return self._dropped.get(id(self._subscribers[0]), 0)

    # --- lifecycle -------------------------------------------------------

    async def close(self) -> None:
        """Stop the bus. Every subscriber's async iterator exits cleanly."""
        if self._closed:
            return
        self._closed = True
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # Subscriber is so saturated even the sentinel can't fit.
                # Drain one slot to make room.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    # --- internal: subscription unregistration -------------------------

    def _remove_subscriber(self, q: asyncio.Queue[Event | None]) -> None:
        """Called by `_Subscription` when its iterator exits or is GC'd."""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass
        self._dropped.pop(id(q), None)


class _Subscription:
    """Iterator wrapping one subscriber queue. Unregisters on exit so
    GC + early-cancel paths don't leak queues."""

    def __init__(self, bus: EventBus, queue: asyncio.Queue[Event | None]) -> None:
        self._bus = bus
        self._queue = queue
        self._done = False

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> Event:
        if self._done:
            raise StopAsyncIteration
        ev = await self._queue.get()
        if ev is None:
            self._done = True
            self._bus._remove_subscriber(self._queue)
            raise StopAsyncIteration
        return ev

    def __del__(self) -> None:
        # Best-effort cleanup if the consumer dropped the iterator
        # without iterating to completion. Destructors must never raise,
        # and there's nowhere meaningful to log from a __del__.
        with contextlib.suppress(Exception):
            self._bus._remove_subscriber(self._queue)


__all__ = ["DEFAULT_QUEUE_MAXSIZE", "EventBus"]
