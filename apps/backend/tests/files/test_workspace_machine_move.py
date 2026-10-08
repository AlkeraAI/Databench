"""A workspace moving between machines, one step at a time, against real
Postgres and a real drive.

Every step of :mod:`alkera_core.compute.workspace_move` is driven directly, and
the box is scripted through the same rows a real box writes: its heartbeat on
its allocation, its turn on the chat's document, its leases on the folders,
its word on a chat's session. What a person would check after a move is what
is asserted: where every chat is bound, who holds which folder, what each chat
says about its ending, where the workspace is pinned, and that nothing that
held a turn or a file was cut off while it still answered.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from alkera_core.compute import workspace_move, workspace_move_fence
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events.hub import EventHub, HubEvent
from alkera_core.files.lease_snapshots import HeldLease
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.files.promotion import Promoter, machine_event
from alkera_core.models import (
    ComputeAllocation,
    EventOutbox,
    RealtimeDoc,
    User,
    WorkspaceObject,
)
from alkera_core.models.compute import POOL_TENANCY
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.tree import FileNode
from alkera_core.models.org_machines import OrgMachine, WorkspaceMachineMove
from alkera_core.schemas.realtime.machine import MachineAck
from backend.services.workspaces import machine_move
from freezegun import freeze_time
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import make_org_machine

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

GRACE = 120
WAKE_TIMEOUT = 600


@dataclass
class Rig:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    workspace_id: uuid.UUID
    folder_id: uuid.UUID
    chats: list[uuid.UUID]
    chat_folders: list[uuid.UUID]
    a: OrgMachine
    a_alloc: ComputeAllocation
    b: OrgMachine
    b_alloc: ComputeAllocation
    move_id: uuid.UUID


async def _object(
    session: AsyncSession, org_id: uuid.UUID, owner: uuid.UUID, **columns: Any
) -> uuid.UUID:
    row = WorkspaceObject(
        org_team_id=org_id,
        logical_id=uuid.uuid4().hex,
        owner_user_id=owner,
        visibility_scope="private",
        version=1,
        status="ready",
        **columns,
    )
    session.add(row)
    await session.flush()
    return uuid.UUID(str(row.id))


async def _lease(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    machine: uuid.UUID,
    purpose: str,
) -> None:
    session.add(
        FileLease(
            node_id=node_id,
            org_team_id=org_id,
            epoch=1,
            holder_principal_kind="user",
            holder_kind="machine",
            holder_principal_id=machine,
            holder_instance_id=f"{machine}:{node_id}",
            machine_id=str(machine),
            purpose=purpose,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    await session.commit()


async def _rig(
    fx: FilesFixtures,
    session: AsyncSession,
    files_org: FilesOrgFixture,
    *,
    b_look: str = "running",
    to_default: bool = False,
    stop_running: bool = False,
    a_flushes: bool = True,
) -> Rig:
    """A workspace pinned to org machine A, its folder and two chats awake on
    A under A's leases, and a move to B (or to the default) just asked for:
    the row ``requested`` and the pin already on the target, as the request
    leaves them."""
    org_id = files_org.org.org_id
    owner = files_org.org.admin_id
    a, a_alloc = await make_org_machine(session, org_id=org_id, operator_id=owner, name="A")
    # What A's build said it can do on its last heartbeat.
    a_alloc.capabilities_json = [BoxCapability.FOLDER_FLUSH_V1] if a_flushes else []
    await session.commit()
    b, b_alloc = await make_org_machine(
        session,
        org_id=org_id,
        operator_id=owner,
        name="B",
        look=b_look,  # type: ignore[arg-type]
    )
    workspace_id = await _object(
        session,
        org_id,
        owner,
        type="workspace",
        title="Training",
        spec={"layout": "native", "kind": "project", "machine_pin": str(a.id)},
    )
    folder = await fx.node(
        b"Training.alkeraworkspace",
        kind="folder",
        subtype=WORKSPACE_TYPE,
        target_object_id=workspace_id,
        parent=await fx.home(),
    )
    await fx.node(b"files", kind="folder", parent=folder)
    chats_folder = await fx.node(b".chats", kind="folder", parent=folder)
    await _lease(session, org_id=org_id, node_id=folder.id, machine=a_alloc.id, purpose="workspace")
    chats: list[uuid.UUID] = []
    chat_folders: list[uuid.UUID] = []
    for index in range(2):
        chat_id = await _object(
            session,
            org_id,
            owner,
            type="chat",
            title=f"Run {index}",
            spec={
                "machine_id": str(a_alloc.id),
                "machine_status": "ready",
                "mirror_state": "awake",
                "workspace_id": str(workspace_id),
            },
        )
        chat_folder = await fx.node(
            f"Run {index}.alkerachat".encode(),
            kind="folder",
            subtype=CHAT_TYPE,
            target_object_id=chat_id,
            parent=chats_folder,
        )
        await _lease(
            session, org_id=org_id, node_id=chat_folder.id, machine=a_alloc.id, purpose="chat"
        )
        chats.append(chat_id)
        chat_folders.append(chat_folder.id)
    move = WorkspaceMachineMove(
        org_team_id=org_id,
        workspace_id=workspace_id,
        from_org_machine_id=a.id,
        to_org_machine_id=None if to_default else b.id,
        state="requested",
        stop_running=stop_running,
        requested_by=owner,
    )
    session.add(move)
    workspace = await session.get(WorkspaceObject, workspace_id)
    assert workspace is not None
    workspace.spec = {**workspace.spec, "machine_pin": None if to_default else str(b.id)}
    await session.commit()
    return Rig(
        org_id=org_id,
        owner_id=owner,
        workspace_id=workspace_id,
        folder_id=folder.id,
        chats=chats,
        chat_folders=chat_folders,
        a=a,
        a_alloc=a_alloc,
        b=b,
        b_alloc=b_alloc,
        move_id=move.id,
    )


async def _step(
    rig: Rig, step: str, *, default: uuid.UUID | None = None, **over: Any
) -> workspace_move.StepOutcome:
    async with AsyncSessionLocal() as db:
        outcome = await workspace_move.run_step(
            db,
            workspace_move.StepRequest(
                move_id=str(rig.move_id),
                org_team_id=str(rig.org_id),
                step=step,
                default_machine_id=str(default) if default else None,
                **over,
            ),
            grace_seconds=GRACE,
            wake_timeout_seconds=WAKE_TIMEOUT,
        )
        await db.commit()
    return outcome


async def _spec(object_id: uuid.UUID) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(WorkspaceObject.spec).where(WorkspaceObject.id == object_id))
        ).scalar_one()
        return dict(row or {})


async def _move(rig: Rig) -> WorkspaceMachineMove:
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceMachineMove, rig.move_id)
        assert row is not None
        return row


async def _live_lease_holders(*node_ids: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
    """Who holds a live lease on each node; a node nobody holds is absent."""
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        rows = await db.execute(
            select(FileLease.node_id, FileLease.holder_principal_id).where(
                FileLease.node_id.in_(node_ids), FileLease.released_at.is_(None)
            )
        )
        return {node: holder for node, holder in rows.all()}


async def _box_writes(sql: str, **params: Any) -> None:
    """A row the box writes (its heartbeat, its turn, its word on a chat)."""
    async with AsyncSessionLocal() as db:
        await db.execute(text(sql), params)
        await db.commit()


async def _working(rig: Rig, chat_id: uuid.UUID, state: str = "working") -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            RealtimeDoc(org_id=rig.org_id, doc_type="chat", doc_id=str(chat_id), turn_state=state)
        )
        await db.commit()


async def _turn_ends(chat_id: uuid.UUID) -> None:
    await _box_writes(
        "UPDATE realtime_docs SET turn_state = 'idle' WHERE doc_id = :id", id=str(chat_id)
    )


async def _stops_sent(rig: Rig) -> list[str]:
    """The chats a Stop relay was put on the channel for."""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox.entity_id, EventOutbox.payload).where(
                EventOutbox.org_id == rig.org_id, EventOutbox.type == "doc.op"
            )
        )
        stopped: list[str] = []
        for entity_id, payload in rows.all():
            events = payload["envelope"]["payload"]["events"]
            if any(event.get("kind") == "stop" for event in events):
                stopped.append(entity_id.removeprefix("doc:chat:"))
        return sorted(stopped)


async def _goes_quiet(alloc: ComputeAllocation) -> None:
    await _box_writes(
        "UPDATE compute_allocations SET last_heartbeat_at = now() - interval '1 hour' "
        "WHERE id = :id",
        id=alloc.id,
    )


async def _says_awake(chat_id: uuid.UUID) -> None:
    await _box_writes(
        'UPDATE workspace_objects SET spec = spec || \'{"mirror_state": "awake"}\'::jsonb '
        "WHERE id = :id",
        id=chat_id,
    )


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


async def test_a_two_chat_workspace_moves_from_a_to_b(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)

    begun = await _step(rig, workspace_move.STEP_BEGIN)
    assert begun.state == "draining"
    assert begun.origin == {str(c): str(rig.a_alloc.id) for c in rig.chats}
    assert (begun.grace_seconds, begun.wake_timeout_seconds) == (GRACE, WAKE_TIMEOUT)
    # Nothing runs in either chat: nothing to stop, and the drain is over.
    assert await _stops_sent(rig) == []
    assert (await _step(rig, workspace_move.STEP_DRAIN_POLL)).ready is True

    switched = await _step(rig, workspace_move.STEP_SWITCH)
    assert switched.state == "switching"
    for chat_id in rig.chats:
        spec = await _spec(chat_id)
        assert spec["machine_id"] == str(rig.b_alloc.id)
        # The departed box's word on the session is not B's.
        assert "mirror_state" not in spec
    # Both chats were awake on A, so both are woken on B.
    assert all([(await _spec(c)).get("wake_requested_at") for c in rig.chats])
    # A still answers: it keeps its leases and hands each folder back itself,
    # with the push of the last turn it ran.
    assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == {
        node: rig.a_alloc.id for node in (rig.folder_id, *rig.chat_folders)
    }

    assert (await _step(rig, workspace_move.STEP_WAKE_BEGIN)).state == "waking"
    waiting = await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)
    assert (waiting.state, waiting.ready) == ("waking", False)
    # B opens the first chat and says so.
    await _says_awake(rig.chats[0])
    done = await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)
    assert (done.state, done.ready) == ("done", True)
    move = await _move(rig)
    assert move.finished_at is not None
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.b.id)


async def test_a_step_run_twice_does_its_work_once(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """A retried activity repeats its step: the row says it is done."""
    rig = await _rig(fx, real_session, files_org)
    await _step(rig, workspace_move.STEP_BEGIN)
    await _step(rig, workspace_move.STEP_SWITCH)
    async with AsyncSessionLocal() as db:
        before = (
            await db.execute(
                select(WorkspaceObject.version, WorkspaceObject.spec).where(
                    WorkspaceObject.id.in_(rig.chats)
                )
            )
        ).all()
    again = await _step(rig, workspace_move.STEP_SWITCH)
    begun_again = await _step(rig, workspace_move.STEP_BEGIN)
    assert again.state == "switching"
    assert begun_again.state == "switching"
    async with AsyncSessionLocal() as db:
        after = (
            await db.execute(
                select(WorkspaceObject.version, WorkspaceObject.spec).where(
                    WorkspaceObject.id.in_(rig.chats)
                )
            )
        ).all()
    assert sorted(map(str, before)) == sorted(map(str, after))


# --------------------------------------------------------------------------- #
# running turns
# --------------------------------------------------------------------------- #


async def test_stop_running_stops_the_running_turn_at_once(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org, stop_running=True)
    busy, idle = rig.chats
    await _working(rig, busy)
    await _working(rig, idle, state="idle")
    await _step(rig, workspace_move.STEP_BEGIN)
    # The stop is the relay a reader's Stop sends, for the running chat only.
    assert await _stops_sent(rig) == [str(busy)]
    assert (await _step(rig, workspace_move.STEP_DRAIN_POLL)).ready is False
    # Nothing moves while the turn is still ending.
    assert (await _spec(busy))["machine_id"] == str(rig.a_alloc.id)
    await _turn_ends(busy)
    assert (await _step(rig, workspace_move.STEP_DRAIN_POLL)).ready is True


async def test_without_stop_running_a_turn_is_left_to_finish_until_asked(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)
    busy, _idle = rig.chats
    await _working(rig, busy)
    await _step(rig, workspace_move.STEP_BEGIN)
    assert await _stops_sent(rig) == []
    assert (await _step(rig, workspace_move.STEP_DRAIN_POLL)).ready is False
    # Past the grace, the workflow asks what is left to stop.
    await _step(rig, workspace_move.STEP_STOP_BUSY)
    assert await _stops_sent(rig) == [str(busy)]


async def test_a_box_that_stops_answering_is_fenced_at_the_switch(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """A box that went quiet mid-turn will never finish it or hand anything
    back: the drain does not wait for it, and the switch ends its leases so B
    can take the folders and A, if it ever returns, is refused."""
    rig = await _rig(fx, real_session, files_org)
    await _working(rig, rig.chats[0])
    await _step(rig, workspace_move.STEP_BEGIN)
    assert (await _step(rig, workspace_move.STEP_DRAIN_POLL)).ready is False
    await _goes_quiet(rig.a_alloc)
    assert (await _step(rig, workspace_move.STEP_DRAIN_POLL)).ready is True
    await _step(rig, workspace_move.STEP_SWITCH)
    assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == {}
    for chat_id in rig.chats:
        spec = await _spec(chat_id)
        assert spec["machine_id"] == str(rig.b_alloc.id)
        assert spec["ended_reason"] == "moved"
        assert spec["end_seq"] == 1


# --------------------------------------------------------------------------- #
# failures and cancels
# --------------------------------------------------------------------------- #


async def test_no_hardware_on_the_target_puts_everything_back_on_a(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)
    begun = await _step(rig, workspace_move.STEP_BEGIN)
    await _step(rig, workspace_move.STEP_SWITCH)
    await _step(rig, workspace_move.STEP_WAKE_BEGIN)
    await _box_writes(
        "UPDATE compute_allocations SET failure_kind = 'capacity' WHERE id = :id",
        id=rig.b_alloc.id,
    )
    failed = await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)
    assert failed.state == "failed"
    move = await _move(rig)
    assert move.error_code == "target_capacity"
    assert move.error == workspace_move.ERROR_WORDS["target_capacity"]
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.a_alloc.id)
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.a.id)


async def test_a_failure_with_the_old_machine_gone_leaves_the_chats_on_the_target(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """Putting the chats back on a machine that no longer runs would strand
    them: they stay where something can serve them, and so does the pin."""
    rig = await _rig(fx, real_session, files_org)
    begun = await _step(rig, workspace_move.STEP_BEGIN)
    await _step(rig, workspace_move.STEP_SWITCH)
    await _step(rig, workspace_move.STEP_WAKE_BEGIN)
    await _box_writes(
        "UPDATE compute_allocations SET state = 'released' WHERE id = :id", id=rig.a_alloc.id
    )
    failed = await _step(
        rig, workspace_move.STEP_FAIL, origin=begun.origin, error_code="wake_timeout"
    )
    assert failed.state == "failed"
    assert (await _move(rig)).error_code == "wake_timeout"
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.b_alloc.id)
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.b.id)


async def test_a_failure_with_no_memory_of_the_origin_goes_back_to_the_old_machine(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """A run started again after the switch has no record of where each chat
    was; the machine the move came from still says it."""
    rig = await _rig(fx, real_session, files_org)
    await _step(rig, workspace_move.STEP_BEGIN)
    await _step(rig, workspace_move.STEP_SWITCH)
    await _step(rig, workspace_move.STEP_FAIL, origin={}, error_code="target_boot_failed")
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.a_alloc.id)
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.a.id)


async def test_a_target_deleted_before_the_switch_fails_with_nothing_moved(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)
    await _step(rig, workspace_move.STEP_BEGIN)
    await _box_writes("UPDATE org_machines SET deleted_at = now() WHERE id = :id", id=rig.b.id)
    assert (await _step(rig, workspace_move.STEP_SWITCH)).state == "failed"
    assert (await _move(rig)).error_code == "target_deleted"
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.a_alloc.id)
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.a.id)


async def test_cancel_while_draining_restores_everything(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)
    await _step(rig, workspace_move.STEP_BEGIN)
    owner = await real_session.get(User, rig.owner_id)
    assert owner is not None
    from alkera_core.authz import ActingContext

    ctx = ActingContext.for_user(user_id=owner.id, org_id=rig.org_id, email=owner.email)
    async with AsyncSessionLocal() as db:
        # The route's order: the move, then the workspace.
        unlocked = await workspace_move.load_workspace(db, rig.workspace_id, org_team_id=rig.org_id)
        assert unlocked is not None
        move = await machine_move.load_move(db, workspace=unlocked, move_id=rig.move_id)
        assert move is not None
        workspace = await workspace_move.load_workspace(
            db, rig.workspace_id, org_team_id=rig.org_id, lock=True
        )
        assert workspace is not None
        await machine_move.cancel_move(db, ctx=ctx, user=owner, workspace=workspace, move=move)
        await db.commit()
    # The workflow's next step finds the cancel and does nothing.
    assert (await _step(rig, workspace_move.STEP_SWITCH)).state == "canceled"
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.a_alloc.id)
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.a.id)
    assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == {
        node: rig.a_alloc.id for node in (rig.folder_id, *rig.chat_folders)
    }


# --------------------------------------------------------------------------- #
# the default placement, and a stopped target
# --------------------------------------------------------------------------- #


async def _pool_box(session: AsyncSession, files_org: FilesOrgFixture) -> ComputeAllocation:
    await session.execute(
        update(ComputeAllocation)
        .where(
            ComputeAllocation.tenancy == POOL_TENANCY,
            ComputeAllocation.org_machine_id.is_(None),
        )
        .values(state="released")
    )
    await session.commit()
    mt = await make_machine_type(session, provider="ec2")
    now = datetime.now(UTC)
    box = ComputeAllocation(
        user_id=files_org.org.admin_id,
        org_team_id=files_org.org.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        name=f"pool-{uuid.uuid4().hex[:6]}",
        tenancy=POOL_TENANCY,
        sandbox="gvisor",
        capacity=6,
        state="ready",
        provider_machine_id=f"i-{uuid.uuid4().hex[:8]}",
        created_at=now,
        ready_at=now,
        state_changed_at=now,
        last_heartbeat_at=now,
        capabilities_json=[BoxCapability.WORKSPACES],
    )
    session.add(box)
    await session.commit()
    await hold_with_credential(session, box)
    return box


async def test_a_move_to_the_default_binds_where_placement_answered(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org, to_default=True)
    pool = await _pool_box(real_session, files_org)
    await _step(rig, workspace_move.STEP_BEGIN, default=pool.id)
    assert (await _step(rig, workspace_move.STEP_SWITCH, default=pool.id)).state == "switching"
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(pool.id)
    assert (await _spec(rig.workspace_id))["machine_pin"] is None


async def test_a_default_that_no_longer_serves_the_org_fails_with_nothing_moved(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org, to_default=True)
    pool = await _pool_box(real_session, files_org)
    await _step(rig, workspace_move.STEP_BEGIN, default=pool.id)
    await _box_writes(
        "UPDATE compute_allocations SET state = 'released' WHERE id = :id", id=pool.id
    )
    assert (await _step(rig, workspace_move.STEP_SWITCH, default=pool.id)).state == "failed"
    assert (await _move(rig)).error_code == "target_capacity"
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.a_alloc.id)
    # The pin goes back to the machine the workspace came from.
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.a.id)


async def test_a_stopped_target_is_asked_to_start_and_its_chats_wait_for_it(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org, b_look="stopped")
    begun = await _step(rig, workspace_move.STEP_BEGIN)
    await _step(rig, workspace_move.STEP_SWITCH)
    # The most recent chat is woken on B even though B reads asleep: without
    # the wake a box coming up would leave every chat it finds asleep alone.
    assert all([(await _spec(c)).get("wake_requested_at") for c in rig.chats])
    assert {(await _spec(c))["machine_status"] for c in rig.chats} == {"asleep"}
    await _step(rig, workspace_move.STEP_WAKE_BEGIN)
    async with AsyncSessionLocal() as db:
        b = await db.get(OrgMachine, rig.b.id)
        alloc = await db.get(ComputeAllocation, rig.b_alloc.id)
        assert b is not None and alloc is not None
        assert (b.desired_power, b.stop_reason) == ("on", "")
        assert alloc.wake_requested_at is not None
    assert (await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)).ready is False


async def test_the_workspace_report_of_the_target_completes_the_move(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """A box that runs workspaces reports the sandbox itself: its word that the
    workspace is awake on it is the target taking the workspace, and the
    departed box's earlier report never counts."""
    rig = await _rig(fx, real_session, files_org)
    await _box_writes(
        "UPDATE workspace_objects SET spec = spec || jsonb_build_object("
        "'binding_authority', 'workspace', 'machine_id', CAST(:a AS text), "
        "'sandbox_state', 'awake') WHERE id = :id",
        a=str(rig.a_alloc.id),
        id=rig.workspace_id,
    )
    begun = await _step(rig, workspace_move.STEP_BEGIN)
    await _step(rig, workspace_move.STEP_SWITCH)
    await _step(rig, workspace_move.STEP_WAKE_BEGIN)
    assert (await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)).ready is False
    await _box_writes(
        "UPDATE workspace_objects SET spec = spec || jsonb_build_object("
        "'machine_id', CAST(:b AS text), 'sandbox_state', 'awake') WHERE id = :id",
        b=str(rig.b_alloc.id),
        id=rig.workspace_id,
    )
    assert (await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)).state == "done"


# --------------------------------------------------------------------------- #
# the leaving box pushes before anything moves
# --------------------------------------------------------------------------- #


@dataclass
class ScriptedBox:
    """Box A on its machine channel, answering a flush the way the daemon
    does: ``accepted`` at once, then its push, then ``flushed`` (or ``busy``
    when the push did not land whole). ``silent`` never answers.

    The requests reach it through the promoter's ``publish`` seam and its acks
    go back on the same hub the promoter waits on, which is what the replica
    holding a box's socket does with them."""

    hub: EventHub
    org_id: uuid.UUID
    answer: str = "flushed"
    push: Callable[[uuid.UUID], Awaitable[None]] | None = None
    asked: list[uuid.UUID] = field(default_factory=list)

    async def publish(self, event: HubEvent) -> None:
        request = event.payload
        assert request["kind"] == "flush"
        lease_node = uuid.UUID(request["lease_node_id"])
        self.asked.append(lease_node)
        if self.answer == "silent":
            return
        rid = uuid.UUID(request["request_id"])

        def ack(outcome: str) -> None:
            self.hub.publish(
                machine_event(
                    self.org_id, event.entity_id, MachineAck(request_id=rid, outcome=outcome)
                )
            )

        if self.answer == "not_holder":
            ack("not_holder")
            return
        ack("accepted")
        if self.answer == "flushed" and self.push is not None:
            await self.push(lease_node)
        ack(self.answer)


async def _flush(
    rig: Rig, box: ScriptedBox, *, deadline: float = 5.0
) -> workspace_move.StepOutcome:
    promoter = Promoter(box.hub, publish=box.publish)

    async def flusher(holders: Sequence[HeldLease], wait: float) -> Mapping[uuid.UUID, str]:
        return await workspace_move.flush_with(promoter, holders, wait)

    return await workspace_move.flush_departing(
        workspace_move.StepRequest(
            move_id=str(rig.move_id), org_team_id=str(rig.org_id), step=workspace_move.STEP_FLUSH
        ),
        flusher=flusher,
        session_factory=AsyncSessionLocal,
        deadline=deadline,
    )


async def _edit_on_drive(name: bytes) -> FileNode | None:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        return (
            await db.execute(select(FileNode).where(FileNode.name == name))
        ).scalar_one_or_none()


async def test_an_unpushed_edit_on_the_old_box_is_on_the_drive_before_the_switch(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)
    edit = f"notes-{uuid.uuid4().hex[:6]}.md".encode()

    async def push(lease_node: uuid.UUID) -> None:
        # The edit a turn on A wrote and A had not pushed yet: its push of the
        # chat's folder lands it through the drive.
        if lease_node == rig.chat_folders[0]:
            folder = await fx.folder(rig.chat_folders[0])
            await fx.version(await fx.node(edit, parent=folder))

    box = ScriptedBox(EventHub(), rig.org_id, push=push)
    await _step(rig, workspace_move.STEP_BEGIN)
    assert await _edit_on_drive(edit) is None
    flushed = await _flush(rig, box)
    assert (flushed.state, flushed.ready) == ("draining", True)
    # Every folder A holds of the workspace was asked for, and only those.
    assert sorted(box.asked) == sorted([rig.folder_id, *rig.chat_folders])
    landed = await _edit_on_drive(edit)
    assert landed is not None and landed.head_version_id is not None
    # Nothing moved yet: the switch comes after the push, never before it.
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.a_alloc.id)
    await _step(rig, workspace_move.STEP_SWITCH)
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.b_alloc.id)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("busy", id="the-push-did-not-land-whole"),
        pytest.param("silent", id="the-box-never-answers"),
    ],
)
async def test_a_box_that_cannot_save_what_it_holds_fails_the_move_with_nothing_moved(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture, answer: str
) -> None:
    rig = await _rig(fx, real_session, files_org)
    box = ScriptedBox(EventHub(), rig.org_id, answer=answer)
    begun = await _step(rig, workspace_move.STEP_BEGIN)
    assert begun.state == "draining"
    failed = await _flush(rig, box, deadline=3.0)
    assert (failed.state, failed.ready) == ("failed", False)
    move = await _move(rig)
    assert move.error_code == "drain_timeout"
    assert move.error == workspace_move.FLUSH_FAILED_WORDS
    assert move.flushed_before_switch is False
    # The switch the workflow would run next finds the move ended.
    assert (await _step(rig, workspace_move.STEP_SWITCH)).state == "failed"
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.a_alloc.id)
    assert (await _spec(rig.workspace_id))["machine_pin"] == str(rig.a.id)
    assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == {
        node: rig.a_alloc.id for node in (rig.folder_id, *rig.chat_folders)
    }


async def test_a_folder_the_box_already_handed_back_needs_no_push(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)
    box = ScriptedBox(EventHub(), rig.org_id, answer="not_holder")
    await _step(rig, workspace_move.STEP_BEGIN)
    assert (await _flush(rig, box)).ready is True


async def test_a_box_that_stopped_beating_is_not_asked_and_is_fenced_at_the_switch(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org)
    await _box_writes(
        "UPDATE file_leases SET heartbeat_at = now() - interval '10 minutes' "
        "WHERE holder_principal_id = :a",
        a=rig.a_alloc.id,
    )
    await _goes_quiet(rig.a_alloc)
    box = ScriptedBox(EventHub(), rig.org_id, answer="busy")
    await _step(rig, workspace_move.STEP_BEGIN)
    assert (await _flush(rig, box)).ready is True
    assert box.asked == []
    # No live box held anything left to push: nothing was skipped.
    assert (await _move(rig)).flushed_before_switch is True
    await _step(rig, workspace_move.STEP_SWITCH)
    assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == {}


async def test_a_box_on_an_older_build_is_moved_as_before(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """A box that never said it answers a flush is not asked (it would never
    answer, and every move off it would fail): its turns finish, the switch
    goes ahead, and it pushes each folder as it lets the chat go."""
    rig = await _rig(fx, real_session, files_org, a_flushes=False)
    box = ScriptedBox(EventHub(), rig.org_id, answer="silent")
    begun = await _step(rig, workspace_move.STEP_BEGIN)
    flushed = await _flush(rig, box, deadline=3.0)
    assert (flushed.state, flushed.ready) == ("draining", True)
    assert box.asked == []
    # A move that went ahead is not an error; it says it ran without a flush.
    noted = await _move(rig)
    assert (noted.error, noted.flushed_before_switch) == ("", False)
    await _step(rig, workspace_move.STEP_SWITCH)
    for chat_id in rig.chats:
        assert (await _spec(chat_id))["machine_id"] == str(rig.b_alloc.id)
    # A still answers, so it keeps its leases to push as it lets each chat go.
    assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == {
        node: rig.a_alloc.id for node in (rig.folder_id, *rig.chat_folders)
    }
    await _step(rig, workspace_move.STEP_WAKE_BEGIN)
    await _says_awake(rig.chats[0])
    done = await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)
    assert done.state == "done"
    move = await _move(rig)
    assert (move.error_code, move.error) == ("", "")


async def test_a_box_that_answers_flush_is_asked_and_the_move_carries_no_note(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    rig = await _rig(fx, real_session, files_org, a_flushes=True)
    box = ScriptedBox(EventHub(), rig.org_id)
    await _step(rig, workspace_move.STEP_BEGIN)
    assert (await _flush(rig, box)).ready is True
    assert sorted(box.asked) == sorted([rig.folder_id, *rig.chat_folders])
    flushed = await _move(rig)
    assert (flushed.error, flushed.flushed_before_switch) == ("", True)


# --------------------------------------------------------------------------- #
# a departed box that never hands the workspace back
# --------------------------------------------------------------------------- #


async def _lease_on_b(rig: Rig, node_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await _lease(db, org_id=rig.org_id, node_id=node_id, machine=rig.b_alloc.id, purpose="chat")


async def test_a_departed_box_that_keeps_the_workspace_is_fenced_once_its_time_is_up(
    fx: FilesFixtures, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """A stopped turn's process kept A holding the shared tree, or A's worker
    ended without handing anything back: A still beats, so the switch spares
    its leases, and B can never take the workspace while they stand. A minute
    into the wait they are ended, B's own lease is untouched, and B taking the
    workspace finishes the move instead of a wake timeout."""
    rig = await _rig(fx, real_session, files_org, stop_running=True, a_flushes=False)
    extra = await fx.node(b"scratch", kind="folder", parent=await fx.folder(rig.folder_id))
    await _lease_on_b(rig, extra.id)
    start = datetime.now(UTC)
    with freeze_time(start, real_asyncio=True) as frozen:
        begun = await _step(rig, workspace_move.STEP_BEGIN)
        await _step(rig, workspace_move.STEP_DRAIN_POLL)
        await _step(rig, workspace_move.STEP_SWITCH)
        await _step(rig, workspace_move.STEP_WAKE_BEGIN)
        held = {node: rig.a_alloc.id for node in (rig.folder_id, *rig.chat_folders)}

        frozen.move_to(start + timedelta(seconds=workspace_move_fence.HAND_BACK_SECONDS - 1))
        waiting = await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)
        assert (waiting.state, waiting.ready) == ("waking", False)
        # Inside its time, A is left to hand back itself.
        assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == held

        frozen.move_to(start + timedelta(seconds=workspace_move_fence.HAND_BACK_SECONDS))
        fenced = await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)
        assert fenced.state == "waking"
        assert await _live_lease_holders(rig.folder_id, *rig.chat_folders) == {}
        # Only the departed box's leases end: the target keeps what it holds.
        assert await _live_lease_holders(extra.id) == {extra.id: rig.b_alloc.id}

        await _says_awake(rig.chats[0])
        done = await _step(rig, workspace_move.STEP_WAKE_POLL, origin=begun.origin)
    assert (done.state, done.ready) == ("done", True)
    assert (await _move(rig)).error_code == ""
