"""Bounded per-client event queues and the hub that fans events out.

Each attached client reads from its own :class:`ClientQueue`. The queue keeps
memory bounded no matter how slowly a client reads:

- a pending ``cell.output`` with ``mode="replace"`` is replaced in place by a
  newer one for the same cell (the older was never seen, so nothing is lost);
- a pending ``cell.status`` is replaced by a newer one for the same cell when
  nothing else about that cell came between them, and never when it says
  ``running``: that one tells a client the cell's previous outputs are gone,
  so dropping it would leave them beside the new run's;
- when the queue is still full, everything pending is dropped and the client
  receives a single ``resync`` carrying a full view (built when the client
  reads it, not when the overflow happened).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from alkera_notebook.engine.models import NotebookView
from alkera_notebook.events.models import (
    AnyEvent,
    CellOutputEvent,
    CellStatusEvent,
    Resync,
)

ViewFactory = Callable[[], Awaitable[NotebookView]]


class ClientQueue:
    def __init__(
        self, maxlen: int, view: ViewFactory, current_seq: Callable[[], int] = lambda: 0
    ) -> None:
        if maxlen < 2:
            raise ValueError("maxlen must be at least 2")
        self._maxlen = maxlen
        self._view = view
        self._current_seq = current_seq
        self._items: deque[AnyEvent] = deque()
        self._overflowed = False
        self._closed = False
        self._wakeup = asyncio.Event()
        self.overflows = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def closed(self) -> bool:
        return self._closed

    def put(self, event: AnyEvent) -> None:
        if self._closed:
            return
        if self._overflowed:
            # Everything up to the resync will be covered by the full view.
            self._wakeup.set()
            return
        if self._coalesce(event):
            self._wakeup.set()
            return
        if len(self._items) >= self._maxlen:
            self._items.clear()
            self._overflowed = True
            self.overflows += 1
        else:
            self._items.append(event)
        self._wakeup.set()

    def _coalesce(self, event: AnyEvent) -> bool:
        if isinstance(event, CellStatusEvent):
            for i in range(len(self._items) - 1, -1, -1):
                pending = self._items[i]
                if getattr(pending, "cell_id", None) != event.cell_id:
                    continue
                if not isinstance(pending, CellStatusEvent) or pending.status == "running":
                    # Something the client must see first, in its place.
                    return False
                del self._items[i]
                self._items.append(event)
                return True
        elif isinstance(event, CellOutputEvent) and event.mode == "replace":
            for i in range(len(self._items) - 1, -1, -1):
                pending = self._items[i]
                if isinstance(pending, CellOutputEvent) and pending.cell_id == event.cell_id:
                    if pending.mode == "replace" and pending.run_id == event.run_id:
                        self._items[i] = event
                        return True
                    # An append after a replace must keep its order.
                    return False
        return False

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()

    async def get(self) -> AnyEvent | None:
        """The next event, or None once closed and drained."""
        while True:
            if self._overflowed:
                self._overflowed = False
                seq = self._current_seq()
                view = await self._view()
                return Resync(reason="overflow", view=view, seq=seq)
            if self._items:
                return self._items.popleft()
            if self._closed:
                return None
            self._wakeup.clear()
            await self._wakeup.wait()

    async def __aiter__(self) -> AsyncIterator[AnyEvent]:
        while True:
            event = await self.get()
            if event is None:
                return
            yield event


class EventHub:
    """Stamps each event with the session's next ``seq`` and fans it out."""

    def __init__(self) -> None:
        self._queues: list[ClientQueue] = []
        self.seq = 0
        self._listeners: list[Callable[[AnyEvent], Any]] = []

    def subscribe(self, maxlen: int, view: ViewFactory) -> ClientQueue:
        q = ClientQueue(maxlen, view, lambda: self.seq)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: ClientQueue) -> None:
        q.close()
        if q in self._queues:
            self._queues.remove(q)

    def listen(self, fn: Callable[[AnyEvent], Any]) -> None:
        """In-process observers called synchronously for every event."""
        self._listeners.append(fn)

    def publish(self, event: AnyEvent) -> AnyEvent:
        self.seq += 1
        event.seq = self.seq
        for fn in self._listeners:
            fn(event)
        for q in list(self._queues):
            q.put(event)
        return event

    def publish_to(self, queue: ClientQueue, event: AnyEvent) -> AnyEvent:
        """An event for one client only (its widget frames)."""
        self.seq += 1
        event.seq = self.seq
        for fn in self._listeners:
            fn(event)
        queue.put(event)
        return event

    def close(self) -> None:
        for q in self._queues:
            q.close()
        self._queues.clear()

    @property
    def queues(self) -> list[ClientQueue]:
        return list(self._queues)
