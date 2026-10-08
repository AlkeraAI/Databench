"""What a workspace's status is decided on, gathered for a whole page at once.

The listing row already holds where the workspace was placed (its chats'
bindings, grouped). This adds what was reported about it since: the live
machine rows, the stamp of every turn its chats are running, its unfinished
move, and the lease its folder's box keeps beating. Three plain reads for a
page, however many chats the workspaces hold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from alkera_core.compute.machines import machine_name_for_reader, machine_state
from alkera_core.config import settings
from alkera_core.models import RealtimeDoc, WorkspaceObject
from alkera_core.models.org_machines import MOVE_FINISHED_STATES, OrgMachine, WorkspaceMachineMove
from alkera_core.objects.instants import instant
from alkera_core.objects.workspaces import WorkspaceBinding
from alkera_core.status import (
    StatusFact,
    WorkspaceBounds,
    WorkspaceEvidence,
    workspace_status,
)
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute import LiveMachines, live_machines
from backend.services.workspaces.folders import WorkspaceNodes

#: What a move to the default placement is called, where no machine is named.
DEFAULT_PLACEMENT_NAME = "shared machines"


@dataclass(frozen=True, slots=True)
class MoveInFlight:
    state: str
    target: str
    since: datetime


@dataclass(frozen=True, slots=True)
class WorkspaceProgress:
    """A page of workspaces' reported progress."""

    machines: LiveMachines
    #: The turn stamp of each chat whose document says a turn is running.
    working: Mapping[UUID, tuple[datetime | None, ...]]
    moves: Mapping[UUID, MoveInFlight]


async def gather(
    db: AsyncSession, *, org_id: UUID, workspace_ids: Sequence[UUID]
) -> WorkspaceProgress:
    """The progress facts for ``workspace_ids``, all of one org."""
    machines = await live_machines(db, org_id=org_id)
    if not workspace_ids:
        return WorkspaceProgress(machines=machines, working={}, moves={})
    keys = [str(workspace_id) for workspace_id in workspace_ids]
    in_workspace = WorkspaceObject.spec["workspace_id"].astext
    turns = await db.execute(
        select(in_workspace, RealtimeDoc.turn_state_at)
        .select_from(WorkspaceObject)
        .join(
            RealtimeDoc,
            (RealtimeDoc.org_id == WorkspaceObject.org_team_id)
            & (RealtimeDoc.doc_type == "chat")
            & (RealtimeDoc.doc_id == func.cast(WorkspaceObject.id, String)),
        )
        .where(
            WorkspaceObject.org_team_id == org_id,
            WorkspaceObject.type == "chat",
            WorkspaceObject.deleted_at == 0,
            in_workspace.in_(keys),
            RealtimeDoc.turn_state == "working",
        )
    )
    working: dict[UUID, tuple[datetime | None, ...]] = {}
    for key, stamped_at in turns.all():
        workspace_id = UUID(key)
        working[workspace_id] = (*working.get(workspace_id, ()), instant(stamped_at))
    rows = await db.execute(
        select(WorkspaceMachineMove, OrgMachine.name)
        .outerjoin(
            OrgMachine,
            (OrgMachine.id == WorkspaceMachineMove.to_org_machine_id)
            & (OrgMachine.org_team_id == WorkspaceMachineMove.org_team_id),
        )
        .where(
            WorkspaceMachineMove.org_team_id == org_id,
            WorkspaceMachineMove.workspace_id.in_(list(workspace_ids)),
            WorkspaceMachineMove.state.not_in(MOVE_FINISHED_STATES),
        )
    )
    moves = {
        move.workspace_id: MoveInFlight(
            state=move.state,
            target=name or DEFAULT_PLACEMENT_NAME,
            since=move.updated_at,
        )
        for move, name in rows.all()
    }
    return WorkspaceProgress(machines=machines, working=working, moves=moves)


def workspace_bounds() -> WorkspaceBounds:
    return WorkspaceBounds(
        turn_silence=timedelta(seconds=settings.chat_turn_silence_seconds),
        sync_pause=timedelta(seconds=settings.files_sync_pause_seconds),
    )


def status_fact(
    workspace_id: UUID,
    binding: WorkspaceBinding,
    *,
    chat_count: int,
    node: WorkspaceNodes | None,
    progress: WorkspaceProgress,
    now: datetime | None = None,
) -> StatusFact | None:
    """The workspace's status from its binding, its folder's lease and the
    page's progress facts."""
    moment = now or datetime.now(UTC)
    machine = progress.machines.machine_for(binding.machine_id)
    holder = (
        progress.machines.machine_for(node.lease_machine_id)
        if node is not None and node.lease_machine_id
        else None
    )
    move = progress.moves.get(workspace_id)
    return workspace_status(
        WorkspaceEvidence(
            chat_count=chat_count,
            machine_state=machine_state(machine, now=moment) if binding.machine_id else "",
            machine_name=machine_name_for_reader(machine),
            awake=binding.mirror_state == "awake",
            wake_requested_at=instant(binding.wake_requested_at),
            working_stamps=progress.working.get(workspace_id, ()),
            move_state=move.state if move else None,
            move_target=move.target if move else "",
            move_since=move.since if move else None,
            lease_held=node is not None and node.lease_machine_id is not None,
            lease_beat_at=node.lease_beat_at if node is not None else None,
            lease_machine_name=machine_name_for_reader(holder),
        ),
        now=moment,
        bounds=workspace_bounds(),
    )


__all__ = ["WorkspaceProgress", "gather", "status_fact", "workspace_bounds"]
