"""The promoter: one request per file, a cap per folder, and a wait that ends on
the right event and on nothing else.

The promoter's two collaborators are the process's :class:`EventHub` — real
here — and the lane it publishes a request on. The lane is ``pg_notify`` in
production; here a scripted machine stands in for it, which is the boundary
that costs a second process: it records what was asked and answers the way a
box does, by publishing an ack (or the durable landing frame) back onto the
same hub. What is asserted is what the promoter decided from those events, and
how many requests left the process — never what the script was told to say.

The route that drives the promoter over a real socket and a real upload is
``apps/backend/tests/files/test_files_content_promote.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.config import Settings, get_settings
from alkera_core.events.hub import EventHub, HubEvent
from alkera_core.events.types import EventType
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_snapshots import HeldLease, LeaseFacet
from alkera_core.files.promotion import (
    MACHINE_ENTITY,
    PromoteOutcome,
    Promoter,
    machine_event,
)
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.realtime import MachineAck, MachineRequest

ORG = uuid.UUID("00000000-0000-4000-8000-00000000a001")
OTHER_ORG = uuid.UUID("00000000-0000-4000-8000-00000000a002")
MACHINE = "4b1d2c3e-0000-4000-8000-000000000b01"

#: Every wait below is bounded by these; a case asserts which event ended it,
#: and the slack only has to cover one loop hop.
ACK_SECONDS = 0.3
WAIT_SECONDS = 1.5


def settings_with(**overrides: Any) -> Settings:
    base = {
        "files_promote_ack_seconds": ACK_SECONDS,
        "files_promote_wait_seconds": WAIT_SECONDS,
        "files_promote_per_minute": 600,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def a_node(*, org: uuid.UUID = ORG, size: int | None = 12) -> FileNode:
    return FileNode(
        id=uuid.uuid4(),
        org_team_id=org,
        name=b"report.csv",
        holder_size=size,
        holder_mtime_ns=1_758_625_000_123_456_789 if size is not None else None,
    )


def a_lease(
    *,
    node_id: uuid.UUID | None = None,
    live: bool = True,
    served: str = "live",
    machine: str = MACHINE,
) -> LeaseFacet:
    now = datetime.now(UTC)
    return LeaseFacet(
        node_id=NodeId(node_id or uuid.uuid4()),
        holder_principal_id=uuid.uuid4(),
        machine=machine,
        purpose="chat",
        since=now,
        expires_at=now + timedelta(minutes=10),
        last_sync_at=now,
        mine=False,
        stale=False,
        live=live,
        served=served,
        epoch=41,
    )


@dataclass
class ScriptedMachine:
    """The box on the far side of the lane: it records every request that left
    the process and answers with whatever the case scripts, by publishing onto
    the hub the way a replica's listener would."""

    hub: EventHub
    answer: Callable[[MachineRequest], list[HubEvent]] = lambda _request: []
    asked: list[MachineRequest] = field(default_factory=list)

    async def publish(self, event: HubEvent) -> None:
        assert event.lane == "ephemeral"
        assert event.channel == f"machine:{event.entity_id}"
        request = MachineRequest.model_validate(event.payload)
        self.asked.append(request)
        replies = self.answer(request)
        loop = asyncio.get_running_loop()
        for reply in replies:
            loop.call_soon(self.hub.publish, reply)


def ack(request: MachineRequest, outcome: str, **extra: Any) -> HubEvent:
    return machine_event(
        ORG, MACHINE, MachineAck(request_id=request.request_id, outcome=outcome, **extra)
    )


def landed(
    node_id: uuid.UUID, *, org: uuid.UUID = ORG, reason: str | None = "live_saved"
) -> HubEvent:
    """The durable frame the content commit writes when the holder's bytes land."""
    return HubEvent(
        lane="durable",
        org_id=org,
        type=EventType.FILE_NODE_CHANGED.value,
        entity="file_node",
        entity_id=str(node_id),
        version=2,
        visibility="org",
        payload={"node_id": str(node_id), "reason": reason},
        id=1,
    )


def promoter_for(
    machine: ScriptedMachine, *, clock: Callable[[], float] | None = None, **overrides: Any
) -> Promoter:
    kwargs: dict[str, Any] = {"publish": machine.publish, "settings": settings_with(**overrides)}
    if clock is not None:
        kwargs["clock"] = clock
    return Promoter(machine.hub, **kwargs)


# ---------------------------------------------------------------------------
# One request per file
# ---------------------------------------------------------------------------


#: A promote deadline no loaded event loop reaches: the probes below are about
#: what the promoter decides, never about how long a scripted ack takes.
LOADED_LOOP = 30.0


async def test_two_readers_of_one_file_share_one_request_and_one_landing() -> None:
    hub = EventHub()
    node, lease = a_node(), a_lease()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "accepted")])
    promoter = promoter_for(machine)

    first = asyncio.create_task(promoter.promote(node, lease, deadline=WAIT_SECONDS, path="a"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(promoter.promote(node, lease, deadline=WAIT_SECONDS, path="a"))
    await asyncio.sleep(0.05)
    assert len(machine.asked) == 1, "the second reader joined the first reader's request"
    hub.publish(landed(node.id))

    assert await asyncio.wait_for(asyncio.gather(first, second), WAIT_SECONDS) == [
        PromoteOutcome.LANDED,
        PromoteOutcome.LANDED,
    ]
    assert len(machine.asked) == 1
    assert promoter.in_flight == 0, "the flight is forgotten once it settles"


async def test_the_request_names_the_file_and_what_the_drive_expects_of_it() -> None:
    hub = EventHub()
    node, lease = a_node(size=4096), a_lease()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "missing")])
    outcome = await promoter_for(machine).promote(
        node, lease, deadline=WAIT_SECONDS, path="src/app/main.py"
    )
    assert outcome is PromoteOutcome.MISSING
    (request,) = machine.asked
    assert (
        request.kind,
        request.path,
        request.node_id,
        request.lease_node_id,
        request.epoch,
    ) == ("promote", "src/app/main.py", node.id, lease.node_id, 41)
    assert (request.expected.size, request.expected.mtime_ns) == (
        4096,
        node.holder_mtime_ns,
    )
    assert 0 < request.deadline_ms <= WAIT_SECONDS * 1000


async def test_a_file_asked_for_again_after_it_settled_is_a_new_request() -> None:
    hub = EventHub()
    node, lease = a_node(), a_lease()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "busy")])
    promoter = promoter_for(machine)
    assert await promoter.promote(node, lease, deadline=1, path="a") is PromoteOutcome.BUSY
    assert await promoter.promote(node, lease, deadline=1, path="a") is PromoteOutcome.BUSY
    assert len({request.request_id for request in machine.asked}) == 2


# ---------------------------------------------------------------------------
# The cap per folder
# ---------------------------------------------------------------------------


async def test_a_folder_past_its_cap_is_throttled_without_asking_and_recovers_a_minute_later() -> (
    None
):
    hub = EventHub()
    now = [1_000.0]
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "missing")])
    promoter = promoter_for(machine, clock=lambda: now[0], files_promote_per_minute=2)
    lease = a_lease()

    outcomes = [await promoter.promote(a_node(), lease, deadline=1, path="f") for _ in range(3)]
    assert outcomes == [
        PromoteOutcome.MISSING,
        PromoteOutcome.MISSING,
        PromoteOutcome.THROTTLED,
    ]
    assert len(machine.asked) == 2, "a throttled reader asks nothing"

    # Another folder has a budget of its own.
    assert (
        await promoter.promote(a_node(), a_lease(), deadline=1, path="f") is PromoteOutcome.MISSING
    )
    # A minute on, the first folder may ask again.
    now[0] += 60.5
    assert await promoter.promote(a_node(), lease, deadline=1, path="f") is PromoteOutcome.MISSING
    assert len(machine.asked) == 4


async def test_the_default_cap_asks_a_folder_600_times_a_minute_and_throttles_the_601st() -> None:
    """At the shipped setting: six hundred distinct files of one folder asked
    for inside a minute each reach the machine; the next is ``throttled`` and
    asks nothing, and another folder is still asked. The deadline is generous on
    purpose: six hundred flights share one loop with the rest of the suite, and
    this probe is about the cap, not about how long an ack takes to arrive."""
    assert get_settings().files_promote_per_minute == 600
    hub = EventHub()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "missing")])
    promoter = Promoter(
        hub,
        publish=machine.publish,
        clock=lambda: 1_000.0,
        # The ack window is generous too: a scripted machine answering six hundred
        # flights on a shared loop is not "a box that has gone quiet".
        settings=get_settings().model_copy(update={"files_promote_ack_seconds": LOADED_LOOP}),
    )
    lease = a_lease()

    outcomes = await asyncio.gather(
        *(promoter.promote(a_node(), lease, deadline=LOADED_LOOP, path="f") for _ in range(600))
    )
    assert set(outcomes) == {PromoteOutcome.MISSING}
    assert len(machine.asked) == 600
    assert (
        await promoter.promote(a_node(), lease, deadline=LOADED_LOOP, path="f")
        is PromoteOutcome.THROTTLED
    )
    assert len(machine.asked) == 600
    assert (
        await promoter.promote(a_node(), a_lease(), deadline=LOADED_LOOP, path="f")
        is PromoteOutcome.MISSING
    )


async def test_a_reader_joining_a_flight_spends_no_budget() -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub)
    promoter = promoter_for(machine, files_promote_per_minute=1)
    node, lease = a_node(), a_lease()
    first = asyncio.create_task(promoter.promote(node, lease, deadline=0.5, path="a"))
    await asyncio.sleep(0.05)
    joined = await promoter.promote(node, lease, deadline=0.5, path="a")
    assert joined is PromoteOutcome.TIMED_OUT, "the joiner waited on the flight, not the cap"
    assert await first is PromoteOutcome.TIMED_OUT
    assert len(machine.asked) == 1


# ---------------------------------------------------------------------------
# A machine that is not serving is never asked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "served"),
    [
        pytest.param(False, "offline", id="not-a-live-lease"),
        pytest.param(True, "offline", id="holder-stopped-beating"),
    ],
)
async def test_an_offline_folder_answers_at_once_without_publishing(
    live: bool, served: str
) -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub)
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await promoter_for(machine).promote(
        a_node(), a_lease(live=live, served=served), deadline=WAIT_SECONDS, path="a"
    )
    assert outcome is PromoteOutcome.OFFLINE
    assert machine.asked == []
    assert loop.time() - started < ACK_SECONDS, "nothing was waited for"


async def test_a_file_the_machine_never_reported_is_not_asked_for() -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub)
    outcome = await promoter_for(machine).promote(
        a_node(size=None), a_lease(), deadline=WAIT_SECONDS, path="a"
    )
    assert outcome is PromoteOutcome.MISSING
    assert machine.asked == []


# ---------------------------------------------------------------------------
# What ends the wait
# ---------------------------------------------------------------------------


async def test_the_landing_frame_resolves_the_wait_and_its_lookalikes_do_not() -> None:
    hub = EventHub()
    node, lease = a_node(), a_lease()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "accepted")])
    task = asyncio.create_task(
        promoter_for(machine).promote(node, lease, deadline=WAIT_SECONDS, path="a")
    )
    await asyncio.sleep(0.05)
    # The same node in another org, another node, and the node changing for a
    # reason other than the holder's bytes landing: none of them is the answer.
    hub.publish(landed(node.id, org=OTHER_ORG))
    hub.publish(landed(uuid.uuid4()))
    hub.publish(landed(node.id, reason=None))
    await asyncio.sleep(0.1)
    assert not task.done()
    hub.publish(landed(node.id))
    assert await asyncio.wait_for(task, WAIT_SECONDS) is PromoteOutcome.LANDED


async def test_a_landing_that_races_the_request_is_not_missed() -> None:
    """The flight subscribes before it publishes: bytes that land while the
    request is still being published still end the wait."""
    hub = EventHub()
    node, lease = a_node(), a_lease()
    machine = ScriptedMachine(hub, answer=lambda _request: [landed(node.id)])
    outcome = await promoter_for(machine).promote(node, lease, deadline=WAIT_SECONDS, path="a")
    assert outcome is PromoteOutcome.LANDED


@pytest.mark.parametrize(
    ("answer", "extra", "expected"),
    [
        pytest.param("not_holder", {}, PromoteOutcome.NOT_HOLDER, id="not-holder"),
        pytest.param("missing", {}, PromoteOutcome.MISSING, id="missing"),
        pytest.param(
            "changed",
            {"observed": {"size": 13, "mtime_ns": 1}},
            PromoteOutcome.CHANGED,
            id="changed",
        ),
        pytest.param("busy", {}, PromoteOutcome.BUSY, id="busy"),
    ],
)
async def test_each_refusing_ack_resolves_the_wait_with_its_outcome(
    answer: str, extra: dict[str, Any], expected: PromoteOutcome
) -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, answer, **extra)])
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await promoter_for(machine).promote(
        a_node(), a_lease(), deadline=WAIT_SECONDS, path="a"
    )
    assert outcome is expected
    assert loop.time() - started < ACK_SECONDS, "a refusal ends the wait at once"


async def test_an_ack_for_another_request_or_machine_or_org_is_not_the_answer() -> None:
    hub = EventHub()
    node, lease = a_node(), a_lease()

    def strangers(request: MachineRequest) -> list[HubEvent]:
        other_request = MachineAck(request_id=uuid.uuid4(), outcome="missing")
        mine = MachineAck(request_id=request.request_id, outcome="missing")
        return [
            machine_event(ORG, MACHINE, other_request),
            machine_event(ORG, "another-machine", mine),
            machine_event(OTHER_ORG, MACHINE, mine),
        ]

    machine = ScriptedMachine(hub, answer=strangers)
    outcome = await promoter_for(machine).promote(node, lease, deadline=WAIT_SECONDS, path="a")
    assert outcome is PromoteOutcome.TIMED_OUT


async def test_a_machine_that_never_answers_ends_the_wait_at_the_ack_window() -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub)
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await promoter_for(machine).promote(
        a_node(), a_lease(), deadline=WAIT_SECONDS, path="a"
    )
    elapsed = loop.time() - started
    assert outcome is PromoteOutcome.TIMED_OUT
    assert ACK_SECONDS * 0.9 <= elapsed < WAIT_SECONDS, (
        "no answer at all ends the wait at the ack window, not the deadline"
    )


async def test_an_accepted_request_waits_for_the_bytes_until_the_readers_deadline() -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "accepted")])
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = ACK_SECONDS * 3
    outcome = await promoter_for(machine).promote(a_node(), a_lease(), deadline=deadline, path="a")
    elapsed = loop.time() - started
    assert outcome is PromoteOutcome.ACCEPTED
    assert elapsed >= deadline * 0.9, "an accepted request is waited on past the ack window"


async def test_a_reader_with_a_shorter_deadline_leaves_while_the_flight_carries_on() -> None:
    hub = EventHub()
    node, lease = a_node(), a_lease()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "accepted")])
    promoter = promoter_for(machine)
    patient = asyncio.create_task(promoter.promote(node, lease, deadline=WAIT_SECONDS, path="a"))
    await asyncio.sleep(0.05)
    hasty = await promoter.promote(node, lease, deadline=0.1, path="a")
    assert hasty is PromoteOutcome.ACCEPTED
    assert not patient.done()
    hub.publish(landed(node.id))
    assert await asyncio.wait_for(patient, WAIT_SECONDS) is PromoteOutcome.LANDED


async def test_a_publish_that_fails_is_a_machine_never_asked() -> None:
    hub = EventHub()

    async def broken(_event: HubEvent) -> None:
        raise ConnectionError("the database is down")

    promoter = Promoter(hub, publish=broken, settings=settings_with())
    outcome = await promoter.promote(a_node(), a_lease(), deadline=WAIT_SECONDS, path="a")
    assert outcome is PromoteOutcome.TIMED_OUT
    assert hub.subscriber_count == 0, "the flight's subscription is dropped on the way out"


async def test_closing_the_promoter_answers_every_waiter() -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "accepted")])
    promoter = promoter_for(machine)
    waiting = asyncio.create_task(
        promoter.promote(a_node(), a_lease(), deadline=WAIT_SECONDS, path="a")
    )
    await asyncio.sleep(0.05)
    await promoter.close()
    assert await asyncio.wait_for(waiting, 1.0) is PromoteOutcome.TIMED_OUT
    assert hub.subscriber_count == 0


def test_a_machine_event_travels_on_the_machines_channel_as_the_frame_itself() -> None:
    request_id = uuid.uuid4()
    event = machine_event(ORG, MACHINE, MachineAck(request_id=request_id, outcome="busy"))
    assert (event.lane, event.entity, event.entity_id, event.channel) == (
        "ephemeral",
        MACHINE_ENTITY,
        MACHINE,
        f"machine:{MACHINE}",
    )
    assert event.type == "machine.ack"
    assert event.payload == {
        "t": "machine.ack",
        "request_id": str(request_id),
        "outcome": "busy",
        "observed": None,
    }


def test_the_promoters_own_spellings_are_the_vocabularys() -> None:
    """The promoter spells the frame tags and the landing event itself, so the
    Files library need not import the events barrel to know them."""
    from alkera_core.files import promotion
    from alkera_core.schemas.realtime import MACHINE_ACK_TAG, MACHINE_REQUEST_TAG

    assert promotion.MACHINE_ACK_TAG == MACHINE_ACK_TAG == MachineAck.model_fields["t"].default
    assert (
        promotion.MACHINE_REQUEST_TAG
        == MACHINE_REQUEST_TAG
        == MachineRequest.model_fields["t"].default
    )
    assert promotion.FILE_NODE_CHANGED == EventType.FILE_NODE_CHANGED.value


# ---------------------------------------------------------------------------
# A flush before a trash
# ---------------------------------------------------------------------------


def a_holder() -> HeldLease:
    return HeldLease(org_id=ORG, lease_node_id=uuid.uuid4(), epoch=41, machine=MACHINE)


async def test_a_flush_waits_past_accepted_for_the_machine_to_say_the_push_ended() -> None:
    """A push outlasts the ack window: ``accepted`` keeps the wait open, a
    landing of some file under the folder does not end it, and the machine's
    ``flushed`` does."""
    hub = EventHub()
    holder = a_holder()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "accepted")])
    promoter = promoter_for(machine)

    waiting = asyncio.create_task(promoter.flush(holder, deadline=LOADED_LOOP))
    await asyncio.sleep(ACK_SECONDS + 0.2)
    hub.publish(landed(holder.lease_node_id))
    await asyncio.sleep(0.1)
    assert not waiting.done(), "a flush ended before the machine said its push had"
    [request] = machine.asked
    assert (request.kind, request.lease_node_id, request.epoch, request.path) == (
        "flush",
        holder.lease_node_id,
        41,
        None,
    )
    hub.publish(ack(request, "flushed"))

    assert await asyncio.wait_for(waiting, WAIT_SECONDS) == PromoteOutcome.LANDED
    assert promoter.in_flight == 0


async def test_a_flush_nobody_answers_ends_at_the_ack_window() -> None:
    hub = EventHub()
    promoter = promoter_for(ScriptedMachine(hub))

    started = asyncio.get_running_loop().time()
    outcome = await promoter.flush(a_holder(), deadline=LOADED_LOOP)

    assert outcome == PromoteOutcome.TIMED_OUT
    assert asyncio.get_running_loop().time() - started < LOADED_LOOP / 2


async def test_a_flush_the_machine_cannot_serve_ends_at_its_answer() -> None:
    hub = EventHub()
    machine = ScriptedMachine(hub, answer=lambda request: [ack(request, "not_holder")])
    promoter = promoter_for(machine)

    assert await promoter.flush(a_holder(), deadline=LOADED_LOOP) == PromoteOutcome.NOT_HOLDER
