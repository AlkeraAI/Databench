"""A sleeping box and the chats parked on it.

A dedicated box is put to sleep to stop costing while its org is quiet; its
disk is kept and a wake starts it back. The chats bound to it are the org's
whole history on that box, and nothing else may take them when the org has
no pool fallback — so they stay bound and must READ that way: ``asleep``, not
``ready`` (the box answers nothing) and not ``unreachable`` (nothing is wrong).
The org's next chat binds to the sleeping box too, and a message on any of
them is what wakes it — the one signal a sleeping box waits for.

Driven through the routes an admin and a tenant use: the console's sleep and
wake, ``machines/current``, the chat page, and ``POST /chats/{id}/messages``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.machines import MachineState, machine_state, machine_status
from alkera_core.compute.provider import ComputeProviderError
from alkera_core.models import OrgComputeAssignment
from alkera_core.models.compute import POOL_TENANCY, ComputeAllocation
from alkera_core.schemas.objects.specs import ChatSpec
from alkera_core.schemas.objects.specs import MachineStatus as ChatMachineStatus
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from backend.services.compute import provisioning
from backend.services.compute.placement import chat_machine_status
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._placement_helpers import (
    assign,
    beat,
    chat_frames,
    console_rows,
    current_machine,
    lifecycle_action,
    listed_chat,
    machine_edges,
    machine_row,
    open_chat,
    platform_box,
    read_chat,
    send_message,
    spec_of,
)
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


@pytest.fixture(autouse=True)
async def _quiet_plane(real_session: AsyncSession) -> None:
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.state.not_in(("released", "failed")))
        .values(state="released")
    )
    await real_session.commit()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeNodeProvider:
    provider = FakeNodeProvider(kind="ec2")
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: provider)
    return provider


async def _sleep(client: AsyncClient, admin: OrgWithAdmin, box: ComputeAllocation) -> None:
    await lifecycle_action(client, admin, box.id, "sleep")


# --------------------------------------------------------------------------- #
# what a sleeping box reads as
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "beat",
    [
        pytest.param("fresh", id="beat-inside-the-window"),
        pytest.param("stale", id="silent-past-the-window"),
        pytest.param("never", id="never-beat"),
    ],
)
async def test_a_sleeping_box_reads_asleep_whatever_its_heartbeat_says(
    real_session: AsyncSession, platform_admin: OrgWithAdmin, fake: FakeNodeProvider, beat: str
) -> None:
    """The sleep outranks the heartbeat: a box stopped on purpose is not
    ``unreachable`` once its last beat ages out, and not ``ready`` while that
    beat is still fresh."""
    box = await platform_box(
        real_session, fake, operator=platform_admin, name="dozing", state="asleep", fresh=False
    )
    stamps = {
        "fresh": datetime.now(UTC),
        "stale": datetime.now(UTC) - timedelta(hours=1),
        "never": None,
    }
    box.last_heartbeat_at = stamps[beat]
    assert machine_status(box) == "asleep"
    assert machine_state(box) == "asleep"


# --------------------------------------------------------------------------- #
# sleep: the chats stay bound and say so
# --------------------------------------------------------------------------- #


async def test_sleep_parks_the_chats_nothing_else_may_take_and_they_read_asleep(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """A dedicated org with no fallback: sleeping its box moves nothing. In
    the sleep's own transaction each chat is restated ``asleep`` and announced;
    the chat page, the list, ``machines/current`` and the console all read the
    box as asleep — never as a ready box carrying load."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="night")
    await assign(real_session, org=org_admin, box=box)
    chats = [await open_chat(org_admin, f"parked {i}") for i in range(2)]
    frames_before = {c["id"]: await chat_frames(c["id"]) for c in chats}

    await _sleep(client, platform_admin, box)

    assert fake.nodes["i-night"].stopped is True
    # Counted before anyone reads the chats: a chat parked on a sleeping box reads
    # asleep, and a reader opening it asks for its wake (one frame of its own).
    for chat in chats:
        assert await chat_frames(chat["id"]) == frames_before[chat["id"]] + 1
    for chat in chats:
        spec = await spec_of(chat["id"])
        assert (spec["machine_id"], spec["machine_status"]) == (str(box.id), "asleep")
        read = await read_chat(org_admin, chat["id"])
        assert (read["machine_id"], read["machine_status"]) == (str(box.id), "asleep")
        assert (await listed_chat(org_admin, chat["id"]))["machine_status"] == "asleep"
    current = await current_machine(org_admin)
    assert (current["machine_id"], current["status"]) == (str(box.id), "asleep")
    row = (await console_rows(client, platform_admin))[str(box.id)]
    assert (row["liveness"], row["chats_served"], row["chats_asleep"]) == ("asleep", 0, 2)


async def test_a_new_chat_for_an_org_whose_box_sleeps_binds_to_it_asleep(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """The sleeping box is still the org's box: a chat opened now binds to it
    and says ``asleep`` — composable, since the send is what wakes it — rather
    than telling the reader nothing can serve them."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="dormant")
    await assign(real_session, org=org_admin, box=box)
    await _sleep(client, platform_admin, box)

    chat = await open_chat(org_admin, "opened while it slept")

    assert (chat["machine_id"], chat["machine_status"]) == (str(box.id), "asleep")


async def test_a_pool_box_coming_up_leaves_the_chats_parked_on_a_sleeping_dedicated_box(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """Parked is not stranded: with no fallback, a pool box announcing itself
    ready takes none of the sleeping box's chats — they wait for the wake."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="kept")
    await assign(real_session, org=org_admin, box=box)
    chat = await open_chat(org_admin, "stays parked")
    await _sleep(client, platform_admin, box)
    pool = await platform_box(
        real_session,
        fake,
        operator=platform_admin,
        name="pool-up",
        tenancy=POOL_TENANCY,
        fresh=False,
    )

    await beat(pool.id, operator=platform_admin)

    read = await read_chat(org_admin, chat["id"])
    assert (read["machine_id"], read["machine_status"]) == (str(box.id), "asleep")


async def test_a_pool_chat_on_a_sleeping_pool_box_moves_to_the_next_pool_box(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """A pool box is interchangeable, so a chat parked on a sleeping one is
    stranded for placement: the next pool box that comes up takes it."""
    first = await platform_box(
        real_session, fake, operator=platform_admin, name="pool-1", tenancy=POOL_TENANCY
    )
    chat = await open_chat(org_admin, "pool chat")
    assert chat["machine_id"] == str(first.id)
    await _sleep(client, platform_admin, first)
    assert (await read_chat(org_admin, chat["id"]))["machine_status"] == "asleep"
    second = await platform_box(
        real_session,
        fake,
        operator=platform_admin,
        name="pool-2",
        tenancy=POOL_TENANCY,
        fresh=False,
    )

    await beat(second.id, operator=platform_admin)

    read = await read_chat(org_admin, chat["id"])
    assert (read["machine_id"], read["machine_status"]) == (str(second.id), "ready")


async def test_a_chat_its_box_parked_reads_ready_on_the_box_it_moves_to(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """A ready box closes a chat's session and says so; the box then sleeps and
    the chat moves to the next pool box. The closed session was the old box's
    word: on the new box the chat reads ready, and the console counts it as
    served there, not parked."""
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import WorkspaceObject
    from sqlalchemy.orm.attributes import flag_modified

    first = await platform_box(
        real_session, fake, operator=platform_admin, name="pool-p1", tenancy=POOL_TENANCY
    )
    chat = await open_chat(org_admin, "parked then moved")
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat["id"]))
        assert row is not None
        row.spec = {**row.spec, "mirror_state": "asleep"}
        flag_modified(row, "spec")
        await session.commit()
    assert (await read_chat(org_admin, chat["id"]))["machine_status"] == "asleep"
    assert (await console_rows(client, platform_admin))[str(first.id)]["chats_asleep"] == 1

    await _sleep(client, platform_admin, first)
    second = await platform_box(
        real_session,
        fake,
        operator=platform_admin,
        name="pool-p2",
        tenancy=POOL_TENANCY,
        fresh=False,
    )
    await beat(second.id, operator=platform_admin)

    read = await read_chat(org_admin, chat["id"])
    assert (read["machine_id"], read["machine_status"]) == (str(second.id), "ready")
    row2 = (await console_rows(client, platform_admin))[str(second.id)]
    assert row2["chats_asleep"] == 0 and row2["chats_served"] >= 1


async def test_a_message_on_a_pool_chat_whose_only_pool_box_sleeps_wakes_that_box(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """The wake rule is not a dedicated-box privilege: when the sleeping pool
    box is the only thing that could answer, the message starts it rather
    than leaving the reader with a recorded prompt and no box coming."""
    only = await platform_box(
        real_session, fake, operator=platform_admin, name="pool-only", tenancy=POOL_TENANCY
    )
    chat = await open_chat(org_admin, "only pool box")
    await _sleep(client, platform_admin, only)
    assert fake.nodes["i-pool-only"].stopped is True

    status, _body = await send_message(org_admin, chat["id"], "anyone?")

    assert status == 201
    assert fake.nodes["i-pool-only"].stopped is False
    read = await read_chat(org_admin, chat["id"])
    assert (read["machine_id"], read["machine_status"]) == (str(only.id), "starting")


# --------------------------------------------------------------------------- #
# wake: a message on a parked chat starts the box
# --------------------------------------------------------------------------- #


async def test_a_message_on_a_chat_parked_on_a_sleeping_box_wakes_it(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """The send is the signal: the box is started at the provider, the row
    is ``ready`` with its stale heartbeat cleared so it reads ``starting``
    until the daemon beats, the parked chats are restated and announced the
    same way, the message is recorded, and the machine's history names the
    member whose message woke it."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="waking")
    await assign(real_session, org=org_admin, box=box)
    chat = await open_chat(org_admin, "wakes the box")
    other = await open_chat(org_admin, "parked beside it")
    await _sleep(client, platform_admin, box)
    assert fake.nodes["i-waking"].stopped is True
    frames_before = await chat_frames(other["id"])

    status, message = await send_message(org_admin, chat["id"], "are you there?")

    assert status == 201, message
    assert fake.nodes["i-waking"].stopped is False, "the provider was asked to start it"
    # Counted before anyone reads the chats: a chat parked on a sleeping box reads
    # asleep, and a reader opening it asks for its wake (one frame of its own).
    assert await chat_frames(other["id"]) == frames_before + 1
    row = await machine_row(box.id)
    assert (row.state, row.last_heartbeat_at) == ("ready", None)
    for chat_id in (chat["id"], other["id"]):
        assert (await spec_of(chat_id))["machine_status"] == "starting"
        read = await read_chat(org_admin, chat_id)
        assert (read["machine_id"], read["machine_status"]) == (str(box.id), "starting")
    woke = [
        e for e in await machine_edges(box.id) if (e.from_state, e.to_state) == ("asleep", "ready")
    ]
    assert len(woke) == 1
    assert woke[0].actor == {"kind": "member", "email": org_admin.admin_email, "name": None}
    assert (await current_machine(org_admin))["status"] == "starting"


async def test_a_second_message_while_the_box_starts_does_not_start_it_again(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    box = await platform_box(real_session, fake, operator=platform_admin, name="once")
    await assign(real_session, org=org_admin, box=box)
    chat = await open_chat(org_admin, "two messages")
    await _sleep(client, platform_admin, box)

    assert (await send_message(org_admin, chat["id"], "first"))[0] == 201
    assert (await send_message(org_admin, chat["id"], "second"))[0] == 201

    edges = [e for e in await machine_edges(box.id) if e.to_state == "ready"]
    assert len(edges) == 1, "one wake for the two messages"


async def test_a_wake_the_provider_refuses_leaves_the_box_asleep_and_the_message_recorded(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-safe: the row does not flip to ``ready`` for a machine that did
    not start. The message is still recorded — the transcript is the durable
    thing — and the chat keeps reading ``asleep`` for the next attempt."""

    class _NoStart(FakeNodeProvider):
        async def start(self, machine_id: str) -> None:
            raise ComputeProviderError("start refused")

    fake = _NoStart(kind="ec2")
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    box = await platform_box(real_session, fake, operator=platform_admin, name="stuck")
    await assign(real_session, org=org_admin, box=box)
    chat = await open_chat(org_admin, "will not wake")
    await _sleep(client, platform_admin, box)

    status, _message = await send_message(org_admin, chat["id"], "hello?")

    assert status == 201
    assert (await machine_row(box.id)).state == "asleep"
    assert (await read_chat(org_admin, chat["id"]))["machine_status"] == "asleep"
    assert (await read_chat(org_admin, chat["id"]))["last_seq"] == 1, "the message was recorded"


async def test_an_org_pool_machine_asleep_is_woken_by_a_message_before_any_fallback(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """An org pool machine that sleeps is the org's machine still: its chats
    stay with it, the org's next chat is placed on it, and a message wakes it,
    whatever the shared-pool fallback says. The fallback is for an org with no
    pool machine that can take a chat at all."""
    await platform_box(
        real_session, fake, operator=platform_admin, name="pool-f", tenancy=POOL_TENANCY
    )
    box = await platform_box(real_session, fake, operator=platform_admin, name="spared")
    await assign(real_session, org=org_admin, box=box, fallback=True)
    chat = await open_chat(org_admin, "stays with its machine")
    assert chat["machine_id"] == str(box.id)

    await _sleep(client, platform_admin, box)

    parked = await read_chat(org_admin, chat["id"])
    assert parked["machine_id"] == str(box.id)
    fresh = await open_chat(org_admin, "placed on the sleeping machine")
    assert fresh["machine_id"] == str(box.id)
    assert (await send_message(org_admin, chat["id"], "wake up"))[0] == 201
    assert fake.nodes["i-spared"].stopped is False, "the message woke the machine"


async def test_an_admin_wake_restates_the_parked_chats_as_starting(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """The console's wake is the same wake: the parked chats are restated
    ``starting`` and announced, and read that way until the daemon beats."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="morning")
    await assign(real_session, org=org_admin, box=box)
    chat = await open_chat(org_admin, "resumes")
    await _sleep(client, platform_admin, box)
    frames = await chat_frames(chat["id"])

    row = await lifecycle_action(client, platform_admin, box.id, "wake")

    assert (row["state"], row["liveness"]) == ("ready", "starting")
    # Counted before anyone reads the chats: a chat parked on a sleeping box reads
    # asleep, and a reader opening it asks for its wake (one frame of its own).
    assert await chat_frames(chat["id"]) == frames + 1
    assert (await spec_of(chat["id"]))["machine_status"] == "starting"
    assert (await read_chat(org_admin, chat["id"]))["machine_status"] == "starting"


# --------------------------------------------------------------------------- #
# every machine state has one word in the chat's vocabulary
# --------------------------------------------------------------------------- #


def _machine_in(state: MachineState) -> ComputeAllocation | None:
    """An unsaved row whose derived state is ``state``."""
    now = datetime.now(UTC)
    if state == "none":
        return None
    row = ComputeAllocation(state="ready", last_heartbeat_at=now)
    if state == "starting":
        row.last_heartbeat_at = None
    elif state == "unreachable":
        row.last_heartbeat_at = now - timedelta(hours=1)
    elif state == "draining":
        row.state = "draining"
    elif state == "restarting":
        row.state = "draining"
        row.drain_kind = "restart"
    elif state == "asleep":
        row.state = "asleep"
        row.last_heartbeat_at = now - timedelta(hours=1)
    return row


@pytest.mark.parametrize("state", sorted(get_args(MachineState)))
def test_every_machine_state_reads_as_one_word_of_the_chats_vocabulary(state: MachineState) -> None:
    """The derivation is total: a chat bound to a machine in any state the
    plane can report reads exactly one chat-vocabulary word, and the two that
    matter here are pinned — a machine that is off the plane reads
    ``stranded``, a sleeping one ``asleep``. A machine state the table does
    not name fails here before it reaches a reader."""
    machine = _machine_in(state)
    assert machine_state(machine) == state, "the fixture is in the state it claims"
    spec = ChatSpec(machine_id=str(uuid4()), machine_status="ready")
    word = chat_machine_status(spec, machine)
    assert word in get_args(ChatMachineStatus)
    assert word == {"none": "stranded", "asleep": "asleep", "restarting": "draining"}.get(
        state, state
    )


def test_a_chat_never_placed_reads_none_whatever_machine_it_is_shown() -> None:
    """``stranded`` is for a chat that HAD a machine: one bound to nothing
    reads ``none`` even beside a live box, and a refusal outranks both."""
    assert chat_machine_status(ChatSpec(), _machine_in("ready")) == "none"
    assert chat_machine_status(ChatSpec(), None) == "none"
    refused = ChatSpec(machine_id=str(uuid4()), publisher_refusal="no grant")
    assert chat_machine_status(refused, None) == "refused"
