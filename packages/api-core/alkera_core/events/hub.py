"""In-process fan-out of events to subscribers, with bounded queues.

One :class:`EventHub` per process. The listener publishes every outbox row it
reads (the durable lane) and every ephemeral notification it receives; each
subscriber owns a bounded ``asyncio.Queue`` and a predicate that decides which
events it wants. Everything here is synchronous and lock-free: it runs on the
one event loop that owns the queues, and ``publish`` never blocks and never
raises.

A slow consumer cannot back up the producer. When a subscriber's queue is
full, its contents are discarded and replaced by a single :class:`ResetMarker`;
until the consumer acknowledges the marker, further events for that subscriber
are dropped. The consumer's contract is to re-synchronise from the outbox by
cursor after a reset, which is always possible because the table, not the
queue, is the source of truth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from alkera_core.doc_type_names import respell_channel, respell_payload
from alkera_core.events.types import EventType
from alkera_core.logging import get_logger
from alkera_core.models.event_outbox import EventOutbox
from alkera_core.observability.metrics import (
    record_event_published,
    record_hub_overflow,
    record_hub_subscribers,
)

log = get_logger(__name__)

Lane = Literal["durable", "ephemeral"]
RESET_REASON_OVERFLOW = "overflow"
DEFAULT_QUEUE_MAXSIZE = 256
#: The durable event types addressed to a socket channel, whose ``entity_id``
#: is that channel's name (``doc:<type>:<id>``, ``nb:<item_id>``).
_CHANNEL_EVENT_TYPES: frozenset[str] = frozenset(
    {EventType.DOC_OP.value, EventType.NOTEBOOK_EVENT.value}
)


@dataclass(frozen=True, slots=True)
class HubEvent:
    """One event as a subscriber sees it.

    ``id`` is the outbox row id on the durable lane and ``None`` on the
    ephemeral lane. ``channel`` is ``doc:<type>:<id>`` for doc-sync traffic and
    ``None`` otherwise. The team a row concerns, when it has one, travels in
    ``payload["team_id"]`` — see :attr:`team_id`.
    """

    lane: Lane
    org_id: UUID
    type: str
    entity: str
    entity_id: str
    version: int
    visibility: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    channel: str | None = None

    @property
    def team_id(self) -> UUID | None:
        raw = self.payload.get("team_id")
        if not isinstance(raw, str):
            return None
        try:
            return UUID(raw)
        except ValueError:
            return None

    @classmethod
    def from_outbox(cls, row: EventOutbox) -> HubEvent:
        """The row as this build spells it: a renamed document type a replica
        on either build wrote is read back under its current name."""
        entity_id = respell_channel(row.entity_id, "from_replicas") or row.entity_id
        return cls(
            lane="durable",
            org_id=row.org_id,
            type=row.type,
            entity=row.entity,
            entity_id=entity_id,
            version=row.version,
            visibility=row.visibility,
            payload=respell_payload(row.payload or {}, "from_replicas"),
            id=row.id,
            channel=entity_id if row.type in _CHANNEL_EVENT_TYPES else None,
        )


@dataclass(frozen=True, slots=True)
class ResetMarker:
    """Left in a subscriber's queue in place of the events it could not keep.

    ``dropped`` counts the events this subscriber will never see from the
    queue: the ones discarded to make room plus the one that did not fit.
    Events published before the consumer acknowledges the marker are dropped
    on top of that.
    """

    reason: str
    dropped: int


Predicate = Callable[[HubEvent], bool]
QueueItem = HubEvent | ResetMarker


class Subscription:
    """A subscriber's queue plus the predicate that feeds it."""

    __slots__ = ("label", "needs_reset", "predicate", "queue")

    def __init__(self, predicate: Predicate, *, label: str, maxsize: int) -> None:
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=maxsize)
        self.predicate = predicate
        self.label = label
        self.needs_reset = False


class EventHub:
    def __init__(self, *, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        if queue_maxsize < 1:
            raise ValueError(
                "queue_maxsize must be >= 1 (a zero-size queue could never hold a reset)"
            )
        self._queue_maxsize = queue_maxsize
        self._subscriptions: list[Subscription] = []

    def subscribe(
        self, predicate: Predicate, *, label: str = "", maxsize: int | None = None
    ) -> Subscription:
        size = self._queue_maxsize if maxsize is None else maxsize
        if size < 1:
            raise ValueError("maxsize must be >= 1")
        sub = Subscription(predicate, label=label, maxsize=size)
        self._subscriptions.append(sub)
        record_hub_subscribers(len(self._subscriptions))
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Stop delivering to ``sub``. Idempotent."""
        try:
            self._subscriptions.remove(sub)
        except ValueError:
            return
        record_hub_subscribers(len(self._subscriptions))

    def publish(self, event: HubEvent) -> int:
        """Offer ``event`` to every subscriber whose predicate accepts it.

        Returns the number of queues it was enqueued on. A predicate that raises
        is logged and skipped; a full queue is reset (see :class:`ResetMarker`);
        a subscriber awaiting a reset acknowledgement is skipped. Never raises.
        """
        record_event_published(event.lane)
        delivered = 0
        for sub in list(self._subscriptions):
            try:
                wanted = sub.predicate(event)
            except Exception as exc:  # a subscriber bug must not stall the others
                log.warning(
                    "realtime.hub.predicate_failed",
                    label=sub.label,
                    error=str(exc),
                    event_type=event.type,
                )
                continue
            if not wanted or sub.needs_reset:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                self._overflow(sub)
            else:
                delivered += 1
        return delivered

    def ack_reset(self, sub: Subscription) -> None:
        """The consumer dequeued the reset marker and is re-synchronising;
        resume delivery."""
        sub.needs_reset = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def reset_for_tests(self) -> None:
        """Drop every subscription (test isolation between cases)."""
        self._subscriptions.clear()
        record_hub_subscribers(0)

    def _overflow(self, sub: Subscription) -> None:
        dropped = 1  # the event that did not fit
        while True:
            try:
                sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        # The queue is empty now and every size is at least one, so this fits.
        sub.queue.put_nowait(ResetMarker(RESET_REASON_OVERFLOW, dropped))
        sub.needs_reset = True
        record_hub_overflow()
        log.warning("realtime.hub.overflow", label=sub.label, dropped=dropped)


_hub = EventHub()


def get_hub() -> EventHub:
    """The process-wide hub."""
    return _hub


__all__ = [
    "DEFAULT_QUEUE_MAXSIZE",
    "RESET_REASON_OVERFLOW",
    "EventHub",
    "HubEvent",
    "Lane",
    "Predicate",
    "ResetMarker",
    "Subscription",
    "get_hub",
]
