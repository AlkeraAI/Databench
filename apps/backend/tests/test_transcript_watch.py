"""Who is told that a chat's transcript moved: the hub consumer every replica
runs, and the registry a service (the Slack relay) wakes itself through.

Driven through a real ``EventHub`` with real ``HubEvent`` values; the watchers
are plain recorders because what is under test is which chat each is told
about, and that one failing watcher does not silence the others."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from alkera_core.events import EventHub
from alkera_core.events.hub import HubEvent
from backend.services.realtime import transcript_watch


def _event(*, type: str = "doc.op", entity_id: str, lane: str = "durable") -> HubEvent:
    return HubEvent(
        lane=lane,
        org_id=uuid4(),
        type=type,
        entity="doc",
        entity_id=entity_id,
        version=0,
        visibility="org",
        payload={},
        id=1 if lane == "durable" else None,
        channel=entity_id,
    )


CHAT = UUID("d6d800e9-5bce-4d9f-b0e0-dd5367ea4298")


@pytest.mark.parametrize(
    ("event", "chat"),
    [
        pytest.param(_event(entity_id=f"doc:chat:{CHAT}"), CHAT, id="a-chat-append"),
        pytest.param(_event(entity_id=f"doc:artifact:{CHAT}"), None, id="another-doc-type"),
        pytest.param(_event(entity_id="doc:chat:not-a-uuid"), None, id="a-malformed-id"),
        pytest.param(
            _event(type="chat.updated", entity_id=f"doc:chat:{CHAT}"), None, id="not-a-doc-op"
        ),
        pytest.param(
            _event(entity_id=f"doc:chat:{CHAT}", lane="ephemeral"), None, id="the-ephemeral-lane"
        ),
    ],
)
def test_only_a_committed_append_to_a_chat_names_a_chat(event: HubEvent, chat: UUID | None) -> None:
    assert transcript_watch.chat_of(event) == chat


@pytest.fixture
async def watching() -> AsyncIterator[EventHub]:
    hub = EventHub(queue_maxsize=4)
    task = transcript_watch.start(hub)
    await asyncio.sleep(0)
    try:
        yield hub
    finally:
        await transcript_watch.stop(task)
        for name in transcript_watch.registered():
            if name.startswith("test-"):
                transcript_watch.unregister(name)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_every_watcher_hears_the_chat_and_a_broken_one_silences_none(
    watching: EventHub,
) -> None:
    heard: list[UUID | None] = []

    def broken(_chat: UUID | None) -> None:
        raise RuntimeError("a watcher with a bug")

    transcript_watch.register("test-broken", broken)
    transcript_watch.register("test-heard", heard.append)
    watching.publish(_event(entity_id=f"doc:chat:{CHAT}"))
    watching.publish(_event(type="chat.updated", entity_id=str(CHAT)))
    await _settle()
    assert heard == [CHAT]


async def test_a_burst_the_hub_dropped_tells_every_watcher_anything_may_have_moved(
    watching: EventHub,
) -> None:
    """A slow consumer's queue overflows and the hub drops the burst; the
    watchers are told ``None`` -- re-check everything -- rather than nothing."""
    heard: list[UUID | None] = []
    transcript_watch.register("test-heard", heard.append)
    for _ in range(12):
        watching.publish(_event(entity_id=f"doc:chat:{CHAT}"))
    await _settle()
    assert None in heard
    # And it keeps listening after the reset.
    heard.clear()
    watching.publish(_event(entity_id=f"doc:chat:{CHAT}"))
    await _settle()
    assert heard == [CHAT]


async def test_an_unregistered_watcher_hears_nothing(watching: EventHub) -> None:
    heard: list[UUID | None] = []
    transcript_watch.register("test-heard", heard.append)
    transcript_watch.unregister("test-heard")
    transcript_watch.unregister("test-heard")  # idempotent
    watching.publish(_event(entity_id=f"doc:chat:{CHAT}"))
    await _settle()
    assert heard == []


async def test_stopping_the_consumer_takes_its_subscription_off_the_hub() -> None:
    hub = EventHub()
    task = transcript_watch.start(hub)
    await asyncio.sleep(0)
    assert hub.subscriber_count == 1
    await transcript_watch.stop(task)
    assert hub.subscriber_count == 0
    with contextlib.suppress(Exception):
        await transcript_watch.stop(None)
