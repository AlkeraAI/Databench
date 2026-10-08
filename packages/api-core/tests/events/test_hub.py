"""The in-process event hub: predicate routing, bounded queues, overflow →
reset marker, and the metrics it reports. Pure — no database, no listener."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.events import EventHub, HubEvent, ResetMarker, get_hub
from alkera_core.events.hub import DEFAULT_QUEUE_MAXSIZE, RESET_REASON_OVERFLOW
from alkera_core.models import EventOutbox
from prometheus_client import REGISTRY


def _event(
    org_id: UUID | None = None,
    *,
    lane: str = "durable",
    type: str = "kb_item.changed",
    entity_id: str = "item",
    payload: dict[str, Any] | None = None,
    id: int | None = 1,
) -> HubEvent:
    return HubEvent(
        lane=lane,  # type: ignore[arg-type]
        org_id=org_id or uuid4(),
        type=type,
        entity="kb_item",
        entity_id=entity_id,
        version=0,
        visibility="org",
        payload=payload or {},
        id=id,
    )


def _gauge(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_subscribe_receives_only_matching_events() -> None:
    hub = EventHub()
    org_a, org_b = uuid4(), uuid4()
    sub_a = hub.subscribe(lambda e: e.org_id == org_a, label="a")
    sub_b = hub.subscribe(lambda e: e.org_id == org_b, label="b")
    event = _event(org_a)
    assert hub.publish(event) == 1
    assert sub_a.queue.get_nowait() is event
    assert sub_b.queue.empty()


def test_publish_returns_how_many_queues_it_reached() -> None:
    hub = EventHub()
    hub.subscribe(lambda e: True)
    hub.subscribe(lambda e: True)
    hub.subscribe(lambda e: False)
    assert hub.publish(_event()) == 2
    assert hub.publish(_event()) == 2


def test_unsubscribe_stops_delivery_and_count() -> None:
    hub = EventHub()
    sub = hub.subscribe(lambda e: True)
    assert hub.subscriber_count == 1
    hub.unsubscribe(sub)
    assert hub.subscriber_count == 0
    assert hub.publish(_event()) == 0
    assert sub.queue.empty()
    hub.unsubscribe(sub)  # idempotent
    assert hub.subscriber_count == 0


def test_publish_never_raises_when_a_predicate_raises() -> None:
    hub = EventHub()

    def broken(_event: HubEvent) -> bool:
        raise RuntimeError("subscriber bug")

    bad = hub.subscribe(broken, label="bad")
    good = hub.subscribe(lambda e: True, label="good")
    event = _event()
    assert hub.publish(event) == 1
    assert good.queue.get_nowait() is event
    assert bad.queue.empty()
    assert hub.subscriber_count == 2  # a raising predicate is skipped, not evicted


def test_reset_for_tests_clears_everything() -> None:
    hub = EventHub()
    sub = hub.subscribe(lambda e: True)
    hub.reset_for_tests()
    assert hub.subscriber_count == 0
    assert hub.publish(_event()) == 0
    assert sub.queue.empty()


def test_get_hub_is_a_process_singleton() -> None:
    assert get_hub() is get_hub()
    assert isinstance(get_hub(), EventHub)


# ---------------------------------------------------------------------------
# Bounded queues and overflow
# ---------------------------------------------------------------------------


def test_the_default_queue_size_and_a_per_subscription_override() -> None:
    hub = EventHub()
    default = hub.subscribe(lambda e: True)
    small = hub.subscribe(lambda e: True, maxsize=3)
    assert default.queue.maxsize == DEFAULT_QUEUE_MAXSIZE
    assert small.queue.maxsize == 3


@pytest.mark.parametrize("size", [0, -1], ids=["zero", "negative"])
def test_a_queue_that_cannot_hold_a_reset_is_refused(size: int) -> None:
    with pytest.raises(ValueError, match="queue_maxsize must be >= 1"):
        EventHub(queue_maxsize=size)
    with pytest.raises(ValueError, match="maxsize must be >= 1"):
        EventHub().subscribe(lambda e: True, maxsize=size)


def test_overflow_replaces_the_queue_with_one_reset_marker() -> None:
    hub = EventHub(queue_maxsize=2)
    sub = hub.subscribe(lambda e: True, label="slow")
    delivered = [hub.publish(_event(id=i)) for i in range(1, 6)]
    # Two fit; the third overflowed and reset the queue; the last two were dropped.
    assert delivered == [1, 1, 0, 0, 0]
    assert sub.queue.qsize() == 1
    marker = sub.queue.get_nowait()
    # The two that were discarded to make room plus the one that did not fit.
    assert marker == ResetMarker(reason=RESET_REASON_OVERFLOW, dropped=3)
    assert sub.needs_reset is True


def test_events_after_overflow_are_dropped_until_ack_reset() -> None:
    hub = EventHub(queue_maxsize=1)
    sub = hub.subscribe(lambda e: True)
    hub.publish(_event(id=1))
    hub.publish(_event(id=2))  # overflow
    marker = sub.queue.get_nowait()
    assert isinstance(marker, ResetMarker)
    # Still awaiting the acknowledgement: nothing is queued.
    assert hub.publish(_event(id=3)) == 0
    assert sub.queue.empty()
    hub.ack_reset(sub)
    assert sub.needs_reset is False
    late = _event(id=4)
    assert hub.publish(late) == 1
    assert sub.queue.get_nowait() is late


def test_one_slow_subscriber_does_not_affect_the_others() -> None:
    hub = EventHub(queue_maxsize=1)
    slow = hub.subscribe(lambda e: True, label="slow")
    fast = hub.subscribe(lambda e: True, label="fast", maxsize=10)
    for i in range(4):
        hub.publish(_event(id=i))
    assert isinstance(slow.queue.get_nowait(), ResetMarker)
    assert [e.id for e in _drain(fast)] == [0, 1, 2, 3]


def _drain(sub: Any) -> list[HubEvent]:
    items: list[HubEvent] = []
    while not sub.queue.empty():
        items.append(sub.queue.get_nowait())
    return items


# ---------------------------------------------------------------------------
# HubEvent
# ---------------------------------------------------------------------------


def _row(**overrides: Any) -> EventOutbox:
    base: dict[str, Any] = {
        "id": 42,
        "org_id": uuid4(),
        "type": "kb_item.changed",
        "entity": "kb_item",
        "entity_id": "item-1",
        "version": 3,
        "visibility": "org",
        "payload": {"team_id": str(uuid4())},
        "actor": {},
    }
    base.update(overrides)
    return EventOutbox(**base)


def test_from_outbox_copies_every_field_on_the_durable_lane() -> None:
    row = _row()
    event = HubEvent.from_outbox(row)
    assert event.lane == "durable"
    assert event.id == 42
    assert event.org_id == row.org_id
    assert event.type == "kb_item.changed"
    assert event.entity == "kb_item"
    assert event.entity_id == "item-1"
    assert event.version == 3
    assert event.visibility == "org"
    assert event.payload == row.payload
    assert event.channel is None
    # The event owns its payload: a later mutation of the row does not leak in.
    row.payload["team_id"] = "changed"
    assert event.payload != row.payload


def test_from_outbox_sets_channel_for_doc_ops_only() -> None:
    doc = HubEvent.from_outbox(_row(type="doc.op", entity="doc", entity_id="doc:chat:abc"))
    assert doc.channel == "doc:chat:abc"
    other = HubEvent.from_outbox(_row(type="chat.updated", entity="chat", entity_id="abc"))
    assert other.channel is None


def test_from_outbox_tolerates_a_null_payload() -> None:
    assert HubEvent.from_outbox(_row(payload=None)).payload == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            {"team_id": "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f"},
            UUID("0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f"),
            id="uuid",
        ),
        pytest.param({}, None, id="absent"),
        pytest.param({"team_id": None}, None, id="null"),
        pytest.param({"team_id": "not-a-uuid"}, None, id="garbage"),
        pytest.param({"team_id": 7}, None, id="not-a-string"),
    ],
)
def test_team_id_is_parsed_from_the_payload_or_none(
    payload: dict[str, Any], expected: UUID | None
) -> None:
    assert _event(payload=payload).team_id == expected


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_subscriber_gauge_tracks_subscriptions() -> None:
    hub = EventHub()
    a = hub.subscribe(lambda e: True)
    assert _gauge("alkera_realtime_hub_subscribers") == 1.0
    hub.subscribe(lambda e: True)
    assert _gauge("alkera_realtime_hub_subscribers") == 2.0
    hub.unsubscribe(a)
    assert _gauge("alkera_realtime_hub_subscribers") == 1.0
    hub.reset_for_tests()
    assert _gauge("alkera_realtime_hub_subscribers") == 0.0


def test_published_and_overflow_counters_move() -> None:
    hub = EventHub(queue_maxsize=1)
    hub.subscribe(lambda e: True)
    durable_before = _gauge("alkera_realtime_events_published_total", lane="durable")
    ephemeral_before = _gauge("alkera_realtime_events_published_total", lane="ephemeral")
    overflow_before = _gauge("alkera_realtime_hub_overflow_total")
    hub.publish(_event(lane="durable"))
    hub.publish(_event(lane="durable"))  # overflows the size-1 queue
    hub.publish(_event(lane="ephemeral", id=None))
    assert _gauge("alkera_realtime_events_published_total", lane="durable") == durable_before + 2
    assert (
        _gauge("alkera_realtime_events_published_total", lane="ephemeral") == ephemeral_before + 1
    )
    assert _gauge("alkera_realtime_hub_overflow_total") == overflow_before + 1
