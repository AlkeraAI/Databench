"""Where a workspace runs, and moving it somewhere else.

A workspace runs where its pin says (``WorkspaceSpec.machine_pin``, an org
machine) or, with no pin, where the org's default placement puts it. Changing
that is a move: one ``workspace_machine_moves`` row, the pin written at once so
every chat started from here on lands on the target, and a durable workflow
that sleeps the workspace where it runs and wakes it on the target. The states
and every step's effect are :mod:`alkera_core.compute.workspace_move`, shared
with the worker that drives them; this module is the request side: what a
person may ask for, what is refused before anything changes, the cancel, and
the read the workspace header draws.

Who may ask is ``workspace_machine.move``: Full access or Owner on the
workspace, and use of the target (in its audience; any member for a pool
machine). Beyond that a move is refused when someone with a chat in the
workspace could not use the target (their chats would land on a machine they
may not run on), while another move of the workspace is under way, or when a
target that is not running cannot be admitted (its funding cannot start it).
Registered :data:`~alkera_core.compute.workspace_move.MoveCheck` rules run last.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast
from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.authz.engine import authorize as decide_policy
from alkera_core.authz.policies import org_machine as org_machine_policy
from alkera_core.authz.policies import workspace_machine as policy
from alkera_core.compute import workspace_move
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.machines import machine_state
from alkera_core.compute.org_machines import in_audience, org_machine_state
from alkera_core.compute.workspace_move import (
    CANCELABLE_STATES,
    MoveInput,
    MoveSubject,
    active_move,
    announce_move,
    last_finished_move,
    may_cancel,
    run_move_checks,
    workspace_chats,
    write_pin,
)
from alkera_core.models import ComputeAllocation, User, WorkspaceMachineMove, WorkspaceObject
from alkera_core.models.compute import ORG_TENANCY
from alkera_core.models.org_machines import (
    MOVE_CANCELED,
    MOVE_REQUESTED,
    USE_POOL,
    OrgMachine,
)
from alkera_core.objects.workspaces import workspace_spec_of
from alkera_core.schemas.objects.specs import ChatSpec
from alkera_core.schemas.org_machines import (
    LostMachineRead,
    MachineCard,
    MachineUnavailableRead,
    MoveStateLiteral,
    OrgMachineState,
    WorkspaceMachineMoveRead,
    WorkspaceMachineRead,
)
from alkera_core.status import MachineEvidence
from alkera_core.status import machine_status as machine_status_fact
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import compute
from backend.services.audit import record_org_audit
from backend.services.infra import start_workspace_machine_move

#: The audit action of a move asked for.
MOVED: Final = "workspace.machine_moved"
#: The audit action of a move canceled.
MOVE_CANCELED_ACTION: Final = "workspace.machine_move_canceled"

MOVE_IN_PROGRESS: Final = "move_in_progress"
TARGET_NOT_SHARED: Final = "move_target_not_shared"
#: A move to the default placement while nothing serves the org's regular chats.
DEFAULT_UNAVAILABLE: Final = "move_default_unavailable"
NOT_CANCELABLE: Final = "move_not_cancelable"
#: What the default placement is called: the org's own pool where it runs one,
#: else the machines Alkera shares.
SHARED_NAME: Final = "Standard"
ORG_POOL_NAME: Final = "Org machines"
#: How long a move may sit without its workflow advancing it before a reader
#: starts the workflow again (a start the orchestrator never took).
REARM_AFTER_SECONDS: Final = 60


class MoveRefusedError(Exception):
    """A move refused before anything changed: ``status`` and ``{code,
    message}``, plus the people a refusal names."""

    def __init__(self, status: int, code: str, message: str, *, names: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.names = list(names)

    def detail(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.names:
            body["names"] = self.names
        return body


@dataclass(frozen=True, slots=True)
class Target:
    """Where a move goes: an org machine of the caller's org (with what it runs
    on now), or the default placement (``row`` ``None``)."""

    row: compute.OrgMachineRow | None
    usable: bool

    @property
    def kind(self) -> str:
        return policy.TARGET_DEFAULT if self.row is None else policy.TARGET_ORG_MACHINE

    @property
    def machine(self) -> OrgMachine | None:
        return self.row.machine if self.row is not None else None


# ---------------------------------------------------------------------------
# The target and the facts the policy decides from
# ---------------------------------------------------------------------------


async def target_for(
    db: AsyncSession, viewer: compute.OrgMachineViewer, row: compute.OrgMachineRow | None
) -> Target:
    """The target and whether the caller may use it, by the ``org_machine``
    policy's own answer for ``use``."""
    if row is None:
        return Target(row=None, usable=True)
    grants = await compute.org_machine_audiences(
        db, org_id=viewer.org_id, machine_ids=[row.machine.id]
    )
    attrs = await compute.org_machine_attrs(
        viewer, row.machine, grants[row.machine.id], purpose=org_machine_policy.USE_PURPOSE
    )
    return Target(
        row=row, usable=compute.org_machine_allowed(viewer, Action.READ, row.machine, attrs)
    )


def move_attrs(workspace_attrs: Mapping[str, object], target: Target) -> dict[str, object]:
    """The facts ``workspace_machine.move`` decides from: the workspace's, and
    the target's."""
    keep = (
        "in_org",
        "roles",
        "is_org_admin",
        "owner_user_id",
        "visibility_scope",
        "team_ids",
        "email_verified",
        policy.SHARED_ROLE_ATTR,
        policy.ADMIN_READS_PRIVATE_ATTR,
    )
    attrs = {key: workspace_attrs[key] for key in keep if key in workspace_attrs}
    shared_role = attrs.get(policy.SHARED_ROLE_ATTR)
    # The workspace policy carries "no rung" as None for its owner; this one
    # reads it as the empty rung.
    attrs[policy.SHARED_ROLE_ATTR] = shared_role if isinstance(shared_role, str) else ""
    attrs[policy.TARGET_KIND_ATTR] = target.kind
    attrs[policy.TARGET_USABLE_ATTR] = target.usable
    return attrs


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def members_left_out(
    db: AsyncSession, workspace: WorkspaceObject, target: Target
) -> list[str]:
    """Who has a live chat in the workspace and could not use the target, by
    display name (their address when they gave no name), sorted. Nobody for
    the default placement, which is everyone's."""
    om = target.machine
    if om is None or om.use_mode == USE_POOL:
        return []
    chats = await workspace_chats(db, workspace_id=workspace.id, org_team_id=workspace.org_team_id)
    owners = {chat.owner_user_id for chat in chats if chat.owner_user_id is not None}
    if not owners:
        return []
    grants = (await compute.org_machine_audiences(db, org_id=om.org_team_id, machine_ids=[om.id]))[
        om.id
    ]
    left_out: list[str] = []
    for owner_id in sorted(owners, key=str):
        teams = await compute.team_ids_of(db, user_id=owner_id, org_id=om.org_team_id)
        if in_audience(om, grants, user_id=owner_id, user_team_ids=teams):
            continue
        user = await db.get(User, owner_id)
        if user is None:
            continue
        left_out.append(user.display_name or user.email)
    return sorted(left_out)


async def admit(db: AsyncSession, *, ctx: ActingContext, target: Target) -> None:
    """Admission for a target that is not running: the org machine's funding
    must be able to start it. A running target is already admitted."""
    row = target.row
    if row is None:
        return
    state, _step = org_machine_state(row.machine, row.allocation, now=datetime.now(UTC))
    if state in ("running", "starting", "stopping", "unreachable"):
        return
    # The org already holds the machine: its plan's quota is not the question,
    # whether its funding can start it is.
    await compute.admit_org_machine_start(
        db,
        ctx=ctx,
        org_machine=row.machine,
        offering=row.offering,
        machine_type=row.machine_type,
    )


# ---------------------------------------------------------------------------
# The default placement
# ---------------------------------------------------------------------------


async def default_machine_for(
    db: AsyncSession, *, ctx: ActingContext, workspace: WorkspaceObject
) -> UUID | None:
    """The machine the org's default placement gives the workspace's chats
    now, asked as placement would with no pin: one that can run a shared
    workspace, writable when any chat may write."""
    chats = await workspace_chats(db, workspace_id=workspace.id, org_team_id=workspace.org_team_id)
    specs = [ChatSpec.model_validate(chat.spec or {}) for chat in chats]
    writable = next(
        (spec.permission_mode for spec in specs if compute.chat_is_writable(spec.permission_mode)),
        specs[0].permission_mode if specs else None,
    )
    capability = (
        BoxCapability.WORKSPACES if workspace_spec_of(workspace.spec).layout == "native" else None
    )
    binding = await compute.resolve_machine_for(
        db,
        ctx=ctx,
        org_team_id=workspace.org_team_id,
        purpose="chat",
        permission_mode=writable,
        capability=capability,
    )
    if binding is None and capability is not None:
        binding = await compute.resolve_machine_for(
            db,
            ctx=ctx,
            org_team_id=workspace.org_team_id,
            purpose="chat",
            permission_mode=writable,
        )
    return binding.machine_id if binding is not None else None


# ---------------------------------------------------------------------------
# Starting the workflow
# ---------------------------------------------------------------------------


async def start_workflow(move: WorkspaceMachineMove, default_machine_id: UUID | None) -> bool:
    """Ask the worker to run the move now. Best-effort: a start the
    orchestrator does not take leaves the row ``requested`` (or wherever the
    last run left it), and the next read of the workspace's machine starts it
    again."""
    return await start_workspace_machine_move(
        move.id,
        MoveInput(
            move_id=str(move.id),
            org_team_id=str(move.org_team_id),
            default_machine_id=str(default_machine_id) if default_machine_id else None,
        ).args(),
    )


def stalled(move: WorkspaceMachineMove, *, now: datetime) -> bool:
    """Whether a move that has not ended has gone quiet long enough that its
    workflow may be gone."""
    if not workspace_move.is_active(move.state):
        return False
    return (now - move.updated_at).total_seconds() >= REARM_AFTER_SECONDS


# ---------------------------------------------------------------------------
# Asking for a move, canceling one
# ---------------------------------------------------------------------------


def move_read(move: WorkspaceMachineMove) -> WorkspaceMachineMoveRead:
    return WorkspaceMachineMoveRead(
        id=str(move.id),
        workspace_id=str(move.workspace_id),
        from_org_machine_id=str(move.from_org_machine_id) if move.from_org_machine_id else None,
        to_org_machine_id=str(move.to_org_machine_id) if move.to_org_machine_id else None,
        state=cast(MoveStateLiteral, move.state),
        error_code=move.error_code,
        error=move.error,
        requested_at=move.requested_at,
        finished_at=move.finished_at,
        flushed_before_switch=move.flushed_before_switch,
    )


async def request_move(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User,
    workspace: WorkspaceObject,
    target: Target,
    stop_running: bool,
) -> WorkspaceMachineMove:
    """Write the move, set the pin and put it on record, in the caller's
    transaction; the caller commits and then starts the workflow. The caller
    has decided the policy, admitted the target (:func:`admit`, before it
    locked the workspace) and holds the workspace's row lock.

    Raises :class:`MoveRefusedError` for a move that cannot start."""
    if target.row is None and not await default_serves(
        db, ctx=ctx, org_id=workspace.org_team_id, user_id=user.id
    ):
        raise MoveRefusedError(
            409,
            DEFAULT_UNAVAILABLE,
            "Nothing in your organization can run this workspace's chats without a machine "
            "of its own right now.",
        )
    left_out = await members_left_out(db, workspace, target)
    if left_out:
        raise MoveRefusedError(
            409,
            TARGET_NOT_SHARED,
            "That machine isn't shared with everyone who has a chat in this workspace.",
            names=left_out,
        )
    if await active_move(db, workspace_id=workspace.id, org_team_id=workspace.org_team_id):
        raise _in_progress()
    refused = await run_move_checks(
        db,
        MoveSubject(
            workspace=workspace,
            target=target.machine,
            target_allocation=target.row.allocation if target.row is not None else None,
        ),
    )
    if refused is not None:
        raise MoveRefusedError(409, refused.code, refused.message)
    spec = workspace_spec_of(workspace.spec)
    previous = _uuid(spec.machine_pin)
    to = target.machine.id if target.machine is not None else None
    move = WorkspaceMachineMove(
        org_team_id=workspace.org_team_id,
        workspace_id=workspace.id,
        from_org_machine_id=previous,
        to_org_machine_id=to,
        state=MOVE_REQUESTED,
        stop_running=stop_running,
        requested_by=user.id,
    )
    try:
        async with db.begin_nested():
            db.add(move)
            await db.flush()
    except IntegrityError as exc:
        # Two requests raced past the read above: the partial unique index
        # on the workspace's active move is the real guard.
        raise _in_progress() from exc
    await write_pin(db, workspace, to, actor=ctx.audit_dict())
    await announce_move(db, move, actor=ctx.audit_dict())
    await record_org_audit(
        db,
        org_id=workspace.org_team_id,
        actor=user,
        action=MOVED,
        target=str(workspace.id),
        detail={
            "move_id": str(move.id),
            "from": str(previous) if previous else None,
            "to": str(to) if to else None,
            "stop_running": stop_running,
        },
        acting=ctx,
    )
    return move


def _in_progress() -> MoveRefusedError:
    return MoveRefusedError(
        409, MOVE_IN_PROGRESS, "This workspace is already moving to another machine."
    )


async def load_move(
    db: AsyncSession, *, workspace: WorkspaceObject, move_id: UUID
) -> WorkspaceMachineMove | None:
    """The workspace's move with that id, locked; ``None`` for any other id."""
    move = await workspace_move.lock_move(db, move_id, org_team_id=workspace.org_team_id)
    if move is None or move.workspace_id != workspace.id:
        return None
    return move


async def cancel_move(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User,
    workspace: WorkspaceObject,
    move: WorkspaceMachineMove,
) -> WorkspaceMachineMove:
    """Cancel ``move`` (locked) before its chats moved: the pin goes back to
    where it was. The workflow sees the cancel at its next step. The caller
    holds the workspace's row lock and commits."""
    if not may_cancel(move.state):
        raise MoveRefusedError(
            409,
            NOT_CANCELABLE,
            "This move can't be canceled any more: its chats are already moving.",
        )
    await workspace_move.advance(db, move, MOVE_CANCELED, actor=ctx.audit_dict())
    await write_pin(db, workspace, move.from_org_machine_id, actor=ctx.audit_dict())
    await record_org_audit(
        db,
        org_id=workspace.org_team_id,
        actor=user,
        action=MOVE_CANCELED_ACTION,
        target=str(workspace.id),
        detail={"move_id": str(move.id)},
        acting=ctx,
    )
    return move


# ---------------------------------------------------------------------------
# The read the workspace header draws
# ---------------------------------------------------------------------------


def shared_card(*, org_pool: bool) -> MachineCard:
    """The default placement as one card: which box it picks is not the
    reader's to see."""
    name = ORG_POOL_NAME if org_pool else SHARED_NAME
    return MachineCard(
        kind="shared",
        org_machine_id=None,
        name=name,
        spec=None,
        state="shared",
        status=machine_status_fact(
            MachineEvidence(state="shared", name=name), now=datetime.now(UTC)
        ),
    )


#: An org box's state in the card's vocabulary.
_BOX_STATE: Mapping[str, OrgMachineState] = {
    "starting": "starting",
    "ready": "running",
    "draining": "stopping",
    "restarting": "starting",
    "unreachable": "unreachable",
    "asleep": "stopped",
    "none": "failed",
}


async def _default_card(
    db: AsyncSession, viewer: compute.OrgMachineViewer, workspace: WorkspaceObject, *, now: datetime
) -> MachineCard:
    """What the workspace runs on with no pin: the org machine its chats are on
    when that is an org pool machine, a box a member registered for the org,
    else the shared machines."""
    org_pool = await _org_pool_offered(db, org_id=workspace.org_team_id)
    chats = await workspace_chats(db, workspace_id=workspace.id, org_team_id=workspace.org_team_id)
    bound = {(chat.spec or {}).get("machine_id") for chat in chats} - {None}
    if len(bound) == 1:
        alloc = await db.get(ComputeAllocation, _uuid(next(iter(bound))))
        if alloc is not None and alloc.org_machine_id is not None:
            row = await compute.load_org_machine(
                db, org_id=workspace.org_team_id, machine_id=alloc.org_machine_id
            )
            if row is not None:
                return await _card(db, viewer, row, now=now)
        if alloc is not None and alloc.tenancy == ORG_TENANCY:
            box_state = _BOX_STATE[machine_state(alloc, now=now)]
            return MachineCard(
                kind="org_box",
                org_machine_id=None,
                name=alloc.name,
                spec=None,
                state=box_state,
                status=machine_status_fact(
                    MachineEvidence(state=box_state, name=alloc.name), now=now
                ),
            )
    return shared_card(org_pool=org_pool)


async def default_serves(
    db: AsyncSession, *, ctx: ActingContext, org_id: UUID, user_id: UUID
) -> bool:
    """Whether anything would serve the org's regular chats: the answer
    ``GET /machines/current`` gives with no workspace. A workspace is neither
    offered nor moved to the default placement when a chat there would find
    no machine; the read and the move ask this one question."""
    read = await compute.machine_state_read(db, ctx=ctx, org_id=org_id, owner_user_id=user_id)
    return read.status != "none"


async def _org_pool_offered(db: AsyncSession, *, org_id: UUID) -> bool:
    """Whether the org's default placement is its own pool: honoured for the
    org, and it holds a pool machine."""
    if not await compute.org_pool_applies(db, org_id=org_id):
        return False
    found = await db.execute(
        select(OrgMachine.id)
        .where(
            OrgMachine.org_team_id == org_id,
            OrgMachine.use_mode == USE_POOL,
            OrgMachine.deleted_at.is_(None),
        )
        .limit(1)
    )
    return found.scalar_one_or_none() is not None


async def _card(
    db: AsyncSession, viewer: compute.OrgMachineViewer, row: compute.OrgMachineRow, *, now: datetime
) -> MachineCard:
    """An org machine's card; its rates only for someone who may use it."""
    target = await target_for(db, viewer, row)
    return await compute.org_machine_card(
        db, ctx=viewer.ctx, row=row, now=now, include_rate=target.usable
    )


async def machine_read(
    db: AsyncSession,
    *,
    viewer: compute.OrgMachineViewer,
    workspace: WorkspaceObject,
    can_move: bool,
    now: datetime | None = None,
) -> WorkspaceMachineRead:
    """The workspace's machine: the card of where it runs, its pin, the move
    under way and the last one that ended, whether the caller may move it, and
    where they may move it to (the default first when something serves the
    org's regular chats, then every org machine they may use)."""
    moment = now or datetime.now(UTC)
    spec = workspace_spec_of(workspace.spec)
    pin = _uuid(spec.machine_pin)
    pinned = (
        await compute.load_org_machine(db, org_id=workspace.org_team_id, machine_id=pin)
        if pin is not None
        else None
    )
    card = (
        await _card(db, viewer, pinned, now=moment)
        if pinned is not None
        else await _default_card(db, viewer, workspace, now=moment)
    )
    move = await active_move(db, workspace_id=workspace.id, org_team_id=workspace.org_team_id)
    last = await last_finished_move(
        db, workspace_id=workspace.id, org_team_id=workspace.org_team_id
    )
    lost = await compute.lost_machine(db, workspace, owner_user_id=viewer.user.id)
    targets = await _targets(db, viewer, now=moment) if can_move else []
    return WorkspaceMachineRead(
        card=card,
        pin=str(pin) if pin is not None and pinned is not None else None,
        lost=lost_read(lost) if lost is not None else None,
        active_move=move_read(move) if move is not None else None,
        last_move=move_read(last) if last is not None else None,
        can_move=can_move,
        targets=targets,
    )


async def _targets(
    db: AsyncSession, viewer: compute.OrgMachineViewer, *, now: datetime
) -> list[MachineCard]:
    """Where the viewer may move a workspace: the default placement first when
    something serves the org's regular chats, then every org machine they may
    use."""
    targets: list[MachineCard] = []
    if await default_serves(db, ctx=viewer.ctx, org_id=viewer.org_id, user_id=viewer.user.id):
        targets.append(shared_card(org_pool=await _org_pool_offered(db, org_id=viewer.org_id)))
    for row in await compute.load_org_machines(db, org_id=viewer.org_id):
        target = await target_for(db, viewer, row)
        if target.usable:
            targets.append(
                await compute.org_machine_card(
                    db, ctx=viewer.ctx, row=row, now=now, include_rate=True
                )
            )
    return targets


def lost_read(lost: compute.LostMachine) -> LostMachineRead:
    return LostMachineRead(
        org_machine_id=str(lost.org_machine_id),
        name=lost.name,
        reason=lost.reason,
        fell_back=lost.fell_back,
    )


async def wake_held_for_choice(
    db: AsyncSession,
    *,
    viewer: compute.OrgMachineViewer,
    chat: WorkspaceObject,
    workspace_attrs: Callable[[WorkspaceObject], Awaitable[Mapping[str, object]]],
) -> MachineUnavailableRead | None:
    """The answer to a person opening ``chat`` when its workspace's own machine
    is gone and nobody has chosen where it runs since: what was lost, and the
    targets the opener may move the workspace to, the default placement first
    and preselected when it serves the org. ``None`` when the wake may go
    ahead. Nothing is woken or moved here: the opener's pick is a move.

    ``workspace_attrs`` reads the workspace's facts for the opener, which
    decide whether they may move it at all (no choices when they may not)."""
    lost = await compute.lost_machine_of_chat(db, chat)
    if lost is None or not lost.awaiting_choice:
        return None
    workspace = await workspace_move.load_workspace(
        db, lost.workspace_id, org_team_id=chat.org_team_id
    )
    if workspace is None:
        return None
    attrs = move_attrs(await workspace_attrs(workspace), Target(row=None, usable=True))
    resource = Resource(
        ResourceType.WORKSPACE_MACHINE, id=str(workspace.id), org_id=workspace.org_team_id
    )
    may_move = decide_policy(viewer.ctx, Action.WRITE, resource, attrs).allowed
    choices = await _targets(db, viewer, now=datetime.now(UTC)) if may_move else []
    return MachineUnavailableRead(
        workspace_id=str(workspace.id),
        lost=lost_read(lost),
        choices=choices,
        preselect_default=bool(choices) and choices[0].org_machine_id is None,
    )


def _uuid(raw: object) -> UUID | None:
    if not raw:
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except ValueError:
        return None


__all__ = [
    "CANCELABLE_STATES",
    "MOVED",
    "MOVE_CANCELED_ACTION",
    "MOVE_IN_PROGRESS",
    "NOT_CANCELABLE",
    "TARGET_NOT_SHARED",
    "MoveRefusedError",
    "Target",
    "admit",
    "cancel_move",
    "default_machine_for",
    "load_move",
    "lost_read",
    "machine_read",
    "members_left_out",
    "move_attrs",
    "move_read",
    "request_move",
    "shared_card",
    "stalled",
    "start_workflow",
    "target_for",
    "wake_held_for_choice",
]
