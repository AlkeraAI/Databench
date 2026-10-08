"""A workspace move as a Temporal workflow: the real workflow and the real
activity through a real Worker on the local dev server, against the test
database, with the boxes scripted through the rows they write.

The happy path runs end to end (drained, switched, the target takes the
workspace, done), and a worker that stops in the middle of the drain is
replaced by another that resumes from the durable timer without running any
step twice: the drain is begun once, nothing is stopped that was not asked to
be, and every chat moves once.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.workspace_move import MoveInput, StalledMove, StalledMoves
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import drives
from alkera_core.files.ids import OrgScope
from alkera_core.files.names import name_key
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models import (
    ComputeAllocation,
    ComputeMachineType,
    EventOutbox,
    RealtimeDoc,
    Team,
    TeamMembership,
    TeamRole,
    User,
    WorkspaceObject,
)
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.stores import FileStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.org_machines import OrgMachine, WorkspaceMachineMove
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType, keyed_workflow_id
from sqlalchemy import select, text
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from worker.activities.workspace_machine import workspace_machine_move
from worker.temporal import queues
from worker.temporal.retry import TRANSIENT_RETRY, policy_for
from worker.workflows.workspace_machine import WorkspaceMachineMove as MoveWorkflow
from worker.workflows.workspace_machine import WorkspaceMachineMoveRecover

pytestmark = pytest.mark.temporal


@pytest.fixture(autouse=True)
def _bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Long enough that no test reaches either bound by waiting.
    monkeypatch.setattr(settings, "move_turn_grace_seconds", 600)
    monkeypatch.setattr(settings, "move_wake_timeout_seconds", 600)


@dataclass
class Rig:
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    chats: list[uuid.UUID]
    a_alloc: uuid.UUID
    b: uuid.UUID
    b_alloc: uuid.UUID
    move_id: uuid.UUID


async def _machine(
    s: Any, *, org: uuid.UUID, user: uuid.UUID, offering: ComputeOffering, name: str
) -> tuple[OrgMachine, ComputeAllocation]:
    om = OrgMachine(
        org_team_id=org,
        owner_team_id=org,
        offering_id=offering.id,
        name=name,
        acquisition="purchased",
        use_mode="assigned",
        storage_gb=50,
    )
    s.add(om)
    await s.flush()
    now = datetime.now(UTC)
    alloc = ComputeAllocation(
        user_id=user,
        org_team_id=org,
        machine_type_id=offering.machine_type_id,
        lifecycle="workspace",
        origin="provisioned",
        name=name,
        tenancy="dedicated",
        sandbox="gvisor",
        state="ready",
        provider_machine_id=f"pod-{uuid.uuid4().hex[:8]}",
        created_at=now,
        ready_at=now,
        last_metered_at=now,
        last_heartbeat_at=now,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=0,
        tenant_org_id=org,
        org_machine_id=om.id,
    )
    s.add(alloc)
    await s.flush()
    om.current_allocation_id = alloc.id
    await s.flush()
    return om, alloc


async def _rig(*, busy: bool, stop_running: bool = False) -> Rig:
    """A workspace on org machine A with two chats awake there, one of them
    running a turn when ``busy``, and a move to org machine B just asked for."""
    async with AsyncSessionLocal() as s:
        team = Team(name=f"move-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"m-{secrets.token_hex(6)}@alkera.dev",
            first_name="Mo",
            last_name="Ver",
        )
        s.add(user)
        await s.flush()
        s.add(TeamMembership(user_id=user.id, team_id=team.id, role=TeamRole.ADMIN))
        mt = ComputeMachineType(
            provider="runpod",
            provider_type_id=f"TEST {uuid.uuid4().hex[:10]}",
            display_name="Test GPU",
            compute_class="gpu",
            vcpu=8,
            provider_price_per_minute_nanos=0,
        )
        s.add(mt)
        await s.flush()
        offering = ComputeOffering(
            machine_type_id=mt.id,
            name="GPU",
            pricing_mode="fixed",
            fixed_rate_per_minute_nanos=0,
            storage_gb_default=50,
            storage_gb_max=500,
            audience="all",
        )
        s.add(offering)
        await s.flush()
        a, a_alloc = await _machine(s, org=team.id, user=user.id, offering=offering, name="A")
        b, b_alloc = await _machine(s, org=team.id, user=user.id, offering=offering, name="B")
        workspace = WorkspaceObject(
            org_team_id=team.id,
            logical_id=uuid.uuid4().hex,
            owner_user_id=user.id,
            visibility_scope="private",
            type="workspace",
            title="Training",
            version=1,
            status="ready",
            spec={"layout": "native", "kind": "project", "machine_pin": str(b.id)},
        )
        s.add(workspace)
        await s.flush()
        chats: list[uuid.UUID] = []
        for index in range(2):
            chat = WorkspaceObject(
                org_team_id=team.id,
                logical_id=uuid.uuid4().hex,
                owner_user_id=user.id,
                visibility_scope="private",
                type="chat",
                title=f"Run {index}",
                version=1,
                status="ready",
                spec={
                    "machine_id": str(a_alloc.id),
                    "machine_status": "ready",
                    "mirror_state": "awake",
                    "workspace_id": str(workspace.id),
                },
            )
            s.add(chat)
            await s.flush()
            chats.append(chat.id)
        if busy:
            s.add(
                RealtimeDoc(
                    org_id=team.id, doc_type="chat", doc_id=str(chats[0]), turn_state="working"
                )
            )
        move = WorkspaceMachineMove(
            org_team_id=team.id,
            workspace_id=workspace.id,
            from_org_machine_id=a.id,
            to_org_machine_id=b.id,
            state="requested",
            stop_running=stop_running,
            requested_by=user.id,
        )
        s.add(move)
        await s.commit()
        return Rig(
            org_id=team.id,
            workspace_id=workspace.id,
            chats=chats,
            a_alloc=a_alloc.id,
            b=b.id,
            b_alloc=b_alloc.id,
            move_id=move.id,
        )


async def _move_state(rig: Rig) -> str:
    async with AsyncSessionLocal() as s:
        move = await s.get(WorkspaceMachineMove, rig.move_id)
        assert move is not None
        return move.state


async def _bound(rig: Rig) -> list[str | None]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(WorkspaceObject.spec).where(WorkspaceObject.id.in_(rig.chats))
        )
        return [(spec or {}).get("machine_id") for spec in rows.scalars().all()]


async def _frames(rig: Rig) -> list[str]:
    """The states the move announced, in order."""
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(EventOutbox.payload)
            .where(
                EventOutbox.org_id == rig.org_id,
                EventOutbox.type == "workspace.machine_move",
                EventOutbox.entity_id == str(rig.workspace_id),
            )
            .order_by(EventOutbox.id)
        )
        return [payload["state"] for payload in rows.scalars().all()]


async def _stops(rig: Rig) -> int:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(EventOutbox.payload).where(
                EventOutbox.org_id == rig.org_id, EventOutbox.type == "doc.op"
            )
        )
        return sum(
            1
            for payload in rows.scalars().all()
            if any(e.get("kind") == "stop" for e in payload["envelope"]["payload"]["events"])
        )


async def _until(check: Callable[[], Awaitable[bool]], *, seconds: float = 30) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the rows never reached the state the test waits for")
        await asyncio.sleep(0.2)


async def _target_takes_the_workspace(rig: Rig) -> None:
    """Box B: once a chat is bound to it, it opens the chat and says so."""

    async def bound_to_b() -> bool:
        return str(rig.b_alloc) in await _bound(rig)

    await _until(bound_to_b)
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                'UPDATE workspace_objects SET spec = spec || \'{"mirror_state": "awake"}\'::jsonb '
                "WHERE id = :id"
            ),
            {"id": rig.chats[0]},
        )
        await s.commit()


def _args(rig: Rig) -> list[str]:
    return MoveInput(move_id=str(rig.move_id), org_team_id=str(rig.org_id)).args()


async def test_a_move_runs_end_to_end(temporal_worker: Any, temporal_client: Client) -> None:
    rig = await _rig(busy=False)
    async with temporal_worker(
        workflows=[MoveWorkflow], activities=[workspace_machine_move]
    ) as running:
        handle = await temporal_client.start_workflow(
            MoveWorkflow.run,
            args=_args(rig),
            id=f"workspace.machine_move:{rig.move_id}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=80),
        )
        await _target_takes_the_workspace(rig)
        assert await handle.result() == "done"
    assert await _bound(rig) == [str(rig.b_alloc)] * 2
    assert await _frames(rig) == ["draining", "switching", "waking", "done"]
    assert await _stops(rig) == 0


async def test_a_worker_restart_mid_drain_resumes_without_doing_anything_twice(
    temporal_worker: Any, temporal_client: Client
) -> None:
    rig = await _rig(busy=True)
    queue = f"test-move-restart-{uuid.uuid4().hex[:8]}"
    async with temporal_worker(
        workflows=[MoveWorkflow], activities=[workspace_machine_move], queue=queue
    ):
        handle = await temporal_client.start_workflow(
            MoveWorkflow.run,
            args=_args(rig),
            id=f"workspace.machine_move:{rig.move_id}",
            task_queue=queue,
            execution_timeout=timedelta(seconds=80),
        )

        async def draining() -> bool:
            return await _move_state(rig) == "draining"

        await _until(draining)
        # A poll or two while the turn runs: nothing moves.
        await asyncio.sleep(3)
        assert await _bound(rig) == [str(rig.a_alloc)] * 2
    # The worker is gone. The turn ends while nobody is watching.
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("UPDATE realtime_docs SET turn_state = 'idle' WHERE doc_id = :id"),
            {"id": str(rig.chats[0])},
        )
        await s.commit()
    assert await _move_state(rig) == "draining"
    async with temporal_worker(
        workflows=[MoveWorkflow], activities=[workspace_machine_move], queue=queue
    ):
        await _target_takes_the_workspace(rig)
        assert await handle.result() == "done"
    assert await _bound(rig) == [str(rig.b_alloc)] * 2
    # Begun once, switched once: each state was entered exactly one time.
    assert await _frames(rig) == ["draining", "switching", "waking", "done"]
    # The turn finished inside the grace: nothing was ever stopped.
    assert await _stops(rig) == 0


async def test_a_leaving_box_that_cannot_save_stops_the_move_before_any_chat_moves(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flush runs between the drain and the switch: when the leaving box
    answers ``busy`` the move fails there, and the switch never runs."""
    from alkera_core.compute import workspace_move

    rig = await _rig(busy=False)
    asked: list[str] = []

    async def busy_box(holders: Any, deadline: float) -> dict[uuid.UUID, str]:
        return {holder.lease_node_id: "busy" for holder in holders}

    async def one_held_folder(db: Any, move: Any, *, target: Any, now: Any) -> list[Any]:
        asked.append(str(move.id))
        from alkera_core.files.lease_snapshots import HeldLease

        return [
            HeldLease(
                org_id=rig.org_id, lease_node_id=uuid.uuid4(), epoch=1, machine=str(rig.a_alloc)
            )
        ]

    async with AsyncSessionLocal() as s:
        alloc = await s.get(ComputeAllocation, rig.a_alloc)
        assert alloc is not None
        alloc.capabilities_json = ["folder_flush_v1"]
        await s.commit()
    monkeypatch.setattr(workspace_move, "promoter_flusher", busy_box)
    monkeypatch.setattr(workspace_move, "departing_holders", one_held_folder)
    async with temporal_worker(
        workflows=[MoveWorkflow], activities=[workspace_machine_move]
    ) as running:
        result = await temporal_client.execute_workflow(
            MoveWorkflow.run,
            args=_args(rig),
            id=f"workspace.machine_move:{rig.move_id}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )
    assert result == "failed"
    assert asked == [str(rig.move_id)]
    assert await _bound(rig) == [str(rig.a_alloc)] * 2
    assert await _frames(rig) == ["draining", "failed"]


def test_the_move_is_on_the_default_queue_and_its_step_retries() -> None:
    assert queues.workflow_type_name(MoveWorkflow) == WorkflowType.WORKSPACE_MACHINE_MOVE.value
    assert queues.queue_for_activity(WorkflowType.WORKSPACE_MACHINE_MOVE.value) is TaskQueue.DEFAULT
    assert WorkflowType.WORKSPACE_MACHINE_MOVE.value in queues.served_workflow_types()
    policy = policy_for(WorkflowType.WORKSPACE_MACHINE_MOVE.value)
    assert policy.retry is TRANSIENT_RETRY
    assert policy.start_to_close < timedelta(minutes=5)


# ---- recovery ------------------------------------------------------------------


async def _quiet_for(rig: Rig, *, minutes: int, state: str | None = None) -> None:
    async with AsyncSessionLocal() as s:
        move = await s.get(WorkspaceMachineMove, rig.move_id)
        assert move is not None
        move.updated_at = datetime.now(UTC) - timedelta(minutes=minutes)
        if state is not None:
            move.state = state
        await s.commit()


@pytest.mark.parametrize(
    ("minutes", "state", "found"),
    [
        pytest.param(3, None, True, id="quiet-and-not-ended-is-offered"),
        pytest.param(3, "draining", True, id="quiet-mid-drain-is-offered"),
        pytest.param(0, None, False, id="a-move-still-stepping-is-left-alone"),
        pytest.param(5, "done", False, id="an-ended-move-is-left-alone"),
        pytest.param(5, "failed", False, id="a-failed-move-is-left-alone"),
    ],
)
async def test_the_recovery_sweep_finds_only_quiet_moves_that_have_not_ended(
    minutes: int, state: str | None, found: bool
) -> None:
    from worker.tasks.workspace_machine import stalled_moves

    rig = await _rig(busy=False)
    await _quiet_for(rig, minutes=minutes, state=state)
    page = await stalled_moves()
    ours = [m for m in page.moves if m.move_id == str(rig.move_id)]
    assert bool(ours) is found
    if found:
        assert ours[0].org_team_id == str(rig.org_id)


async def _pin(rig: Rig) -> str | None:
    async with AsyncSessionLocal() as s:
        workspace = await s.get(WorkspaceObject, rig.workspace_id)
        assert workspace is not None
        pin = (workspace.spec or {}).get("machine_pin")
        return None if pin is None else str(pin)


@pytest.mark.parametrize(
    ("state", "bound_seconds", "code"),
    [
        pytest.param("requested", 600, "not_started", id="never-started"),
        # The suite holds the turn grace and wake timeout at 600 s.
        pytest.param("draining", 600 + 30 + 60 + 60, "drain_timeout", id="drain-never-ended"),
        pytest.param("switching", 120, "wake_timeout", id="switch-never-ended"),
        pytest.param("waking", 600, "wake_timeout", id="target-never-took-it"),
    ],
)
async def test_a_move_past_its_bound_is_failed_with_its_code_whatever_its_runner_does(
    state: str, bound_seconds: int, code: str
) -> None:
    """A re-armed runner starts its own deadlines over, so a move whose
    runners kept dying never ended. The bound is measured from the row's last
    state change: one second short of it the move is offered a runner, at it
    the move ends failed with the bound's code and the pin goes back."""
    from worker.tasks.workspace_machine import stalled_moves

    rig = await _rig(busy=False)
    await _quiet_for(rig, minutes=0, state=state)
    async with AsyncSessionLocal() as s:
        move = await s.get(WorkspaceMachineMove, rig.move_id)
        assert move is not None
        changed = move.updated_at
        pin_before_move = str(move.from_org_machine_id)

    await stalled_moves(now=changed + timedelta(seconds=bound_seconds - 1))
    assert await _move_state(rig) == state

    await stalled_moves(now=changed + timedelta(seconds=bound_seconds))
    async with AsyncSessionLocal() as s:
        move = await s.get(WorkspaceMachineMove, rig.move_id)
        assert move is not None
        assert (move.state, move.error_code) == ("failed", code)
    assert await _pin(rig) == pin_before_move
    assert await _bound(rig) == [str(rig.a_alloc)] * 2


async def test_the_recovery_sweep_gives_a_runner_only_to_a_move_that_has_none(
    temporal_worker: Any, temporal_client: Client
) -> None:
    """A move whose workflow went with Temporal's history is offered one under
    the id the request uses, bound to the move workflow's arguments by name; a
    move whose runner is alive is left to it."""
    org = str(uuid.uuid4())
    gone, alive = str(uuid.uuid4()), str(uuid.uuid4())
    runner = WorkflowType.WORKSPACE_MACHINE_MOVE

    @activity.defn(name=WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER.value)
    async def _found() -> StalledMoves:
        return StalledMoves(
            moves=[
                StalledMove(move_id=gone, org_team_id=org),
                StalledMove(move_id=alive, org_team_id=org),
            ]
        )

    already = await temporal_client.start_workflow(
        runner.value,
        args=MoveInput(move_id=alive, org_team_id=org).args(),
        id=keyed_workflow_id(runner, alive),
        task_queue=QUEUE_FOR[runner].value,
    )
    started_handle = temporal_client.get_workflow_handle(keyed_workflow_id(runner, gone))
    try:
        async with temporal_worker(
            workflows=[WorkspaceMachineMoveRecover], activities=[_found]
        ) as running:
            started = await temporal_client.execute_workflow(
                WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER.value,
                id=f"recover-moves-{uuid.uuid4()}",
                task_queue=running.task_queue,
            )
        assert started == 1
        described = await started_handle.describe()
        assert described.status is WorkflowExecutionStatus.RUNNING
        assert described.workflow_type == runner.value
        assert described.task_queue == QUEUE_FOR[runner].value
        history = await started_handle.fetch_history()
        first = history.events[0].workflow_execution_started_event_attributes
        sent = await temporal_client.data_converter.decode(first.input.payloads)
        bound = inspect.signature(MoveWorkflow.run).bind(None, *sent)
        assert (bound.arguments["move_id"], bound.arguments["org_team_id"]) == (gone, org)
    finally:
        await started_handle.terminate("the case has read what it needed")
        await already.terminate("the case has read what it needed")


# ---- a departed box that never hands the workspace back -------------------------


async def _workspace_folder_held_by_a(rig: Rig) -> uuid.UUID:
    """The workspace's folder on the org's drive, leased by box A as a box
    that is still beating holds it."""
    async with AsyncSessionLocal() as s:
        store = FileStore(
            id=uuid.uuid4(),
            driver="filesystem",
            bucket=f"move-{uuid.uuid4().hex[:8]}",
            endpoint="",
            region="",
            capabilities={},
            transfer_modes=["single"],
        )
        s.add(store)
        await s.flush()
        repo = FilesRepo(s, OrgScope(org_team_id=rig.org_id))
        owner = (
            await s.execute(
                select(WorkspaceObject.owner_user_id).where(WorkspaceObject.id == rig.workspace_id)
            )
        ).scalar_one()
        ctx = ActingContext.for_user(user_id=owner, org_id=rig.org_id, email="mover@test")
        async with repo.transaction():
            drive = await drives.ensure_org_drive(repo, ctx, rig.org_id, store_id=store.id)
        await s.refresh(drive)
        root = await s.get(FileNode, drive.root_node_id)
        assert root is not None
        ino = drive.next_ino
        drive.next_ino += 1
        name = b"Training.alkeraworkspace"
        folder = FileNode(
            id=uuid.uuid4(),
            ino=ino,
            drive_id=drive.id,
            org_team_id=rig.org_id,
            parent_id=root.id,
            kind="folder",
            subtype=WORKSPACE_TYPE,
            target_object_id=rig.workspace_id,
            name=name,
            name_display=name.decode(),
            name_key=name_key(name),
            path_ids=f"{root.path_ids}.{ino_label(ino)}",
            depth=root.depth + 1,
        )
        s.add(folder)
        await s.flush()
        s.add(
            FileLease(
                node_id=folder.id,
                org_team_id=rig.org_id,
                epoch=1,
                holder_principal_kind="user",
                holder_kind="machine",
                holder_principal_id=rig.a_alloc,
                holder_instance_id=f"{rig.a_alloc}:{folder.id}",
                machine_id=str(rig.a_alloc),
                purpose="workspace",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await s.commit()
        return folder.id


async def _a_holds(node_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as s:
        await s.execute(text("SET LOCAL row_security = off"))
        held = await s.execute(
            select(FileLease.node_id).where(
                FileLease.node_id == node_id, FileLease.released_at.is_(None)
            )
        )
        return held.first() is not None


async def test_a_stop_running_move_off_a_box_that_never_lets_go_still_finishes(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move stops the running turn and switches; box A keeps beating but
    never hands the workspace's folder back (a process the stopped turn left
    running holds it, or its worker ended without a hand-back). Box B can
    take the workspace only once that lease is gone. The wait fences A after
    its hand-back time instead of timing the move out and putting the chats
    back on A."""
    from alkera_core.compute import workspace_move_fence

    monkeypatch.setattr(workspace_move_fence, "HAND_BACK_SECONDS", 3)
    rig = await _rig(busy=True, stop_running=True)
    folder = await _workspace_folder_held_by_a(rig)

    async def stop_landed() -> bool:
        return await _stops(rig) == 1

    async def a_let_go() -> bool:
        return not await _a_holds(folder)

    async with temporal_worker(
        workflows=[MoveWorkflow], activities=[workspace_machine_move]
    ) as running:
        handle = await temporal_client.start_workflow(
            MoveWorkflow.run,
            args=_args(rig),
            id=f"workspace.machine_move:{rig.move_id}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=80),
        )
        # Box A: the Stop ends the turn at once.
        await _until(stop_landed)
        async with AsyncSessionLocal() as s:
            await s.execute(
                text("UPDATE realtime_docs SET turn_state = 'idle' WHERE doc_id = :id"),
                {"id": str(rig.chats[0])},
            )
            await s.commit()
        # Box B: takes the workspace once nobody else holds its folder.
        await _until(a_let_go, seconds=60)
        await _target_takes_the_workspace(rig)
        assert await handle.result() == "done"
    assert await _bound(rig) == [str(rig.b_alloc)] * 2
    assert await _frames(rig) == ["draining", "switching", "waking", "done"]
