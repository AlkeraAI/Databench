"""Re-emit the events a box posts to the backend's own log.

A box that cannot write to the deployment's log store itself posts its
supervisor's events here. Each is put through the same allowlist the box
applied (``alkera_core.compute.box_logs.sanitize``): a box changed to send
more still lands nothing outside it. What passes is logged under its own
event name with the machine id the credential proved, never one the body
names, so the app log group's metric filters count it exactly as they count
the backend's own events. Its org is the one the event names (the org whose
worker it describes) or none, never the request's.

Each machine has an event budget (:class:`EventBudget`), so a box that floods
cannot fill the log: past it events are dropped, counted in the answer, and
one throttle line says so. The budget is per backend task, like the request
limiter's buckets.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from alkera_core.compute.box_logs import sanitize
from alkera_core.logging import get_logger
from alkera_core.schemas.box_logs import BoxLogBatch

log = get_logger(__name__)

#: A machine's sustained events per minute, and the most it may post at once.
EVENTS_PER_MINUTE: Final = 300
EVENTS_BURST: Final = 300
#: How many machines' budgets a task remembers; the least recently used go.
TRACKED_MACHINES: Final = 10_000
#: The line logged when a machine's events were dropped past its budget.
THROTTLED_EVENT: Final = "compute.box_logs.throttled"


@dataclass
class _Bucket:
    tokens: float
    at: float


class EventBudget:
    """A token bucket of events per machine, on an injected clock."""

    def __init__(
        self,
        *,
        per_minute: int = EVENTS_PER_MINUTE,
        burst: int = EVENTS_BURST,
        clock: Callable[[], float] = time.monotonic,
        tracked: int = TRACKED_MACHINES,
    ) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._tracked = tracked
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def take(self, machine_id: str) -> bool:
        """Spend one event of ``machine_id``'s budget; whether there was one."""
        now = self._clock()
        bucket = self._buckets.pop(machine_id, None) or _Bucket(self._burst, now)
        bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.at) * self._rate)
        bucket.at = now
        self._buckets[machine_id] = bucket
        while len(self._buckets) > self._tracked:
            self._buckets.popitem(last=False)
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True


_BUDGET = EventBudget()


def event_budget() -> EventBudget:
    """The task's budget. Exposed so a test can swap in one on its own clock."""
    return _BUDGET


def ingest(
    machine_id: str, batch: BoxLogBatch, *, budget: EventBudget | None = None
) -> tuple[int, int]:
    """Log each allowed event of ``batch`` as ``machine_id``'s, against
    ``budget`` (the task's own by default); ``(accepted, dropped)``."""
    budget = budget if budget is not None else event_budget()
    accepted = dropped = throttled = 0
    for item in batch.events:
        clean = sanitize(item.event, item.level, item.timestamp, item.fields)
        if clean is None:
            dropped += 1
            continue
        if not budget.take(machine_id):
            dropped += 1
            throttled += 1
            continue
        level = str(clean.pop("level"))
        event = str(clean.pop("event"))
        stamp = clean.pop("timestamp")
        # The org is the event's own, or none: never the org the request
        # that carried it is bound to, which is whatever the credential's
        # context holds and has nothing to do with the worker described.
        org_id = clean.pop("org_id", None)
        getattr(log, level)(
            event,
            origin="box",
            machine_id=machine_id,
            box_timestamp=stamp,
            org_id=org_id,
            **clean,
        )
        accepted += 1
    if throttled:
        log.warning(THROTTLED_EVENT, machine_id=machine_id, count=throttled)
    return accepted, dropped


__all__ = [
    "EVENTS_BURST",
    "EVENTS_PER_MINUTE",
    "THROTTLED_EVENT",
    "TRACKED_MACHINES",
    "EventBudget",
    "event_budget",
    "ingest",
]
