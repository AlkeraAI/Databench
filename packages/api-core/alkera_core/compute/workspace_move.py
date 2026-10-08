"""Moving a workspace from one machine to another: the states a move passes
through, and each step's effect on the rows.

A move sleeps the workspace where it runs and wakes it where it is going. It is
one ``workspace_machine_moves`` row driven by a durable workflow through
``requested → draining → switching → waking → done``; ``failed`` is reachable
from every state that has not ended, and ``canceled`` only before the chats
have moved (``requested``, ``draining``).

Nothing a move does may lose a file or a turn:

* **draining** lets every turn running in the workspace finish (up to the
  grace the deployment allows) or, when the person asked for it, stops it now
  through the same relay a reader's Stop sends. No chat is touched while a turn
  runs in it.
* then, still ``draining``, every box leaving the workspace is asked over its
  machine channel to **flush**: push everything it holds of the workspace's
  folders. The move waits, bounded, for each to say the push ended with
  everything sent; a box that answers ``busy`` or does not answer fails the
  move with nothing moved.
* **switching** rebinds every chat of the workspace onto the target in one
  transaction. A box that is still beating keeps its lease on the chat's folder
  and hands it back itself, pushing what the last turn wrote: the box lets go
  of a chat the moment it is bound elsewhere, and its push is the only copy of
  that turn. A box that stopped beating, sleeps or left the plane will never
  hand anything back, so its lease is ended here and the box, if it ever
  returns, is fenced. The chats' transcripts are rows: they move with the
  binding.
* **waking** powers the target on when it is stopped and waits, bounded, until
  the target took the workspace. A departed box that has not handed the
  workspace back a minute into the wait is fenced
  (:mod:`alkera_core.compute.workspace_move_fence`), so a box that will never
  let go cannot hold the target off until the wake times out.
* **failed** puts the chats back on the machine they came from when that
  machine still runs, and the pin with them.

Everything here runs in the caller's transaction and filters by the move's org
explicitly: the worker reads as the platform login, so no row policy narrows a
read for it. Each step is keyed by the move id and idempotent, so a step a
retried activity runs twice does its work once.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Final
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from alkera_core.files.promotion import Promoter

from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.machines import (
    ASLEEP,
    NONE,
    READY,
    RESTARTING,
    UNREACHABLE,
    MachineState,
    machine_state,
    may_serve,
)
from alkera_core.compute.workspace_move_fence import fence_departed, hand_back_overdue
from alkera_core.config import get_settings
from alkera_core.db.cross_tenant import cross_tenant_read
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import BOUND_MACHINE_KEY, Entity, EventType, actor_system, emit
from alkera_core.files import chat_workspace_id
from alkera_core.files.lease_snapshots import SERVED_HEARTBEATS, HeldLease
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.lifecycle import Bound
from alkera_core.lifecycle.machines import MOVE_OUTCOMES, WORKSPACE_MOVE
from alkera_core.logging import get_logger
from alkera_core.models import ComputeAllocation, RealtimeDoc, WorkspaceObject
from alkera_core.models.compute import (
    FAILED as ALLOCATION_FAILED,
)
from alkera_core.models.compute import (
    FAILURE_CAPACITY,
    TERMINATED_BOOT_FAILED,
    TERMINATED_PROVIDER_CAPACITY,
)
from alkera_core.models.org_machines import (
    MOVE_CANCELED,
    MOVE_DONE,
    MOVE_DRAINING,
    MOVE_FAILED,
    MOVE_FINISHED_STATES,
    MOVE_REQUESTED,
    MOVE_STATES,
    MOVE_SWITCHING,
    MOVE_WAKING,
    POWER_OFF,
    OrgMachine,
    WorkspaceMachineMove,
)
from alkera_core.objects import chat_end
from alkera_core.objects.publisher_report import clear_refusal
from alkera_core.objects.workspaces import workspace_spec_of
from alkera_core.schemas.objects.specs import MachineStatus as ChatMachineStatus
from alkera_core.schemas.objects.transcript import StopRelay
from alkera_core.schemas.realtime import SERVER_PEER_ID, DocEnvelope, OpPayload
from alkera_core.versioning import VersionedModel

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# The state machine (pure)
# ---------------------------------------------------------------------------

#: The target has no hardware for the workspace right now.
TARGET_CAPACITY: Final = "target_capacity"
#: The target was started and never came up.
TARGET_BOOT_FAILED: Final = "target_boot_failed"
#: The chats could not be brought to rest on the machine they ran on.
DRAIN_TIMEOUT: Final = "drain_timeout"
#: Something the move needs is no longer allowed: the requester lost use of
#: the target, or a registered move check refused.
NOT_ALLOWED: Final = "not_allowed"
#: The target was deleted while the move ran.
TARGET_DELETED: Final = "target_deleted"
#: The target came up and did not take the workspace in time.
WAKE_TIMEOUT: Final = "wake_timeout"
#: No runner ever began the move.
NOT_STARTED: Final = "not_started"
MOVE_ERROR_CODES: tuple[str, ...] = (
    TARGET_CAPACITY,
    TARGET_BOOT_FAILED,
    DRAIN_TIMEOUT,
    NOT_ALLOWED,
    TARGET_DELETED,
    WAKE_TIMEOUT,
    NOT_STARTED,
)

#: What each code says to the person who asked for the move: the words of its
#: one outcome.
ERROR_WORDS: Mapping[str, str] = {code: MOVE_OUTCOMES[code].words for code in MOVE_ERROR_CODES}

#: Every legal edge. A state not listed as a key has no way out.
TRANSITIONS: Mapping[str, frozenset[str]] = {
    MOVE_REQUESTED: frozenset({MOVE_DRAINING, MOVE_FAILED, MOVE_CANCELED}),
    MOVE_DRAINING: frozenset({MOVE_SWITCHING, MOVE_FAILED, MOVE_CANCELED}),
    MOVE_SWITCHING: frozenset({MOVE_WAKING, MOVE_FAILED}),
    MOVE_WAKING: frozenset({MOVE_DONE, MOVE_FAILED}),
    MOVE_DONE: frozenset(),
    MOVE_FAILED: frozenset(),
    MOVE_CANCELED: frozenset(),
}
#: The states a move may still be canceled from: nothing has moved yet.
CANCELABLE_STATES: tuple[str, ...] = (MOVE_REQUESTED, MOVE_DRAINING)
#: The states in which the chats may already be on the target.
CHATS_MAY_HAVE_MOVED: tuple[str, ...] = (MOVE_SWITCHING, MOVE_WAKING)


class MoveTransitionError(ValueError):
    """An edge the state machine does not have."""

    def __init__(self, source: str, target: str) -> None:
        super().__init__(f"a move cannot go from {source!r} to {target!r}")
        self.source = source
        self.target = target


def may_transition(source: str, target: str) -> bool:
    """Whether a move in ``source`` may go to ``target``."""
    return target in TRANSITIONS.get(source, frozenset())


def check_transition(source: str, target: str) -> None:
    """Refuse an edge the state machine does not have."""
    if source not in MOVE_STATES or target not in MOVE_STATES:
        raise MoveTransitionError(source, target)
    if not may_transition(source, target):
        raise MoveTransitionError(source, target)


def is_active(state: str) -> bool:
    """Whether a move in ``state`` has not ended."""
    return state in MOVE_STATES and state not in MOVE_FINISHED_STATES


def may_cancel(state: str) -> bool:
    """Whether a move in ``state`` may still be canceled."""
    return state in CANCELABLE_STATES


# ---------------------------------------------------------------------------
# Checks a move must pass (a registry; empty today)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoveSubject:
    """What a move check is asked about: the workspace and where it would go.
    ``target`` is ``None`` for the org's default placement."""

    workspace: WorkspaceObject
    target: OrgMachine | None
    target_allocation: ComputeAllocation | None


@dataclass(frozen=True, slots=True)
class MoveRefusal:
    """Why a move check refuses: a code the client can branch on and a sentence."""

    code: str
    message: str


MoveCheck = Callable[[AsyncSession, MoveSubject], Awaitable[MoveRefusal | None]]
"""One rule a move must pass before it is accepted, beyond permissions and
admission: a workspace whose notebook needs a GPU may only go to a machine whose
sandbox can hand one over, a workspace whose environment cannot be rebuilt on
another provider stays where it is. Registered, never coded at the route."""

_CHECKS: list[MoveCheck] = []


def register_move_check(check: MoveCheck) -> MoveCheck:
    """Add a check every move request runs. A check is registered once."""
    if check in _CHECKS:
        raise ValueError(f"move check {check!r} is already registered")
    _CHECKS.append(check)
    return check


def move_checks() -> tuple[MoveCheck, ...]:
    return tuple(_CHECKS)


async def run_move_checks(db: AsyncSession, subject: MoveSubject) -> MoveRefusal | None:
    """The first refusal of the registered checks, in registration order."""
    for check in _CHECKS:
        refused = await check(db, subject)
        if refused is not None:
            return refused
    return None


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

#: How long a move that has not ended may go without a step before the
#: recovery sweep offers it a runner again. A runner that is alive (waiting on
#: a wake, say) is left alone: the offer finds it and does nothing.
STALLED_AFTER: Final = timedelta(minutes=2)
#: How many stalled moves one sweep offers a runner; the next tick takes the rest.
RECOVERY_PAGE: Final = 200


class StalledMove(VersionedModel):
    """A move the recovery sweep offers a runner. Crosses workflow history."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    move_id: str
    org_team_id: str


class StalledMoves(VersionedModel):
    """What one recovery sweep found. Crosses workflow history."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    moves: list[StalledMove] = Field(default_factory=list)


async def stalled_moves(
    db: AsyncSession, *, now: datetime | None = None, limit: int = RECOVERY_PAGE
) -> StalledMoves:
    """Every org's moves that have not ended and have gone quiet for
    :data:`STALLED_AFTER`, oldest first. Writes nothing: whether a runner is
    still there is the orchestrator's answer, not the database's."""
    moment = now or datetime.now(UTC)
    async with cross_tenant_read(db, reason="workspace.machine_move.recover") as read:
        rows = (
            await read.execute(
                select(WorkspaceMachineMove.id, WorkspaceMachineMove.org_team_id)
                .where(
                    WorkspaceMachineMove.state.not_in(MOVE_FINISHED_STATES),
                    WorkspaceMachineMove.updated_at <= moment - STALLED_AFTER,
                )
                .order_by(WorkspaceMachineMove.updated_at)
                .limit(limit)
            )
        ).all()
    return StalledMoves(
        moves=[StalledMove(move_id=str(move_id), org_team_id=str(org)) for move_id, org in rows]
    )


async def end_overdue_moves(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Fail every move that has sat in one state past its registered bound,
    with that bound's code, whether or not a runner is still there.

    The workflow's own deadlines count from when a runner started them, so a
    move re-armed by the recovery sweep started its clock again, and a move
    whose runners kept dying never ended. A bound here is measured from the
    row's last state change, which no runner resets. Commits per move.
    """
    moment = now or datetime.now(UTC)
    async with cross_tenant_read(db, reason="workspace.machine_move.overdue") as read:
        rows = (
            await read.execute(
                select(
                    WorkspaceMachineMove.id,
                    WorkspaceMachineMove.org_team_id,
                    WorkspaceMachineMove.state,
                    WorkspaceMachineMove.updated_at,
                )
                .where(WorkspaceMachineMove.state.not_in(MOVE_FINISHED_STATES))
                .order_by(WorkspaceMachineMove.updated_at)
                .limit(RECOVERY_PAGE)
            )
        ).all()
    await db.rollback()
    ended = 0
    for move_id, org_team_id, state, updated_at in rows:
        bound = WORKSPACE_MOVE.bounds.get(state)
        if not isinstance(bound, Bound) or moment - updated_at < bound.duration:
            continue
        move = await lock_move(db, move_id, org_team_id=org_team_id)
        if move is None or move.state != state or move.updated_at != updated_at:
            await db.rollback()
            continue
        await fail(
            db,
            move=move,
            error_code=bound.outcome,
            origin={},
            error=WORKSPACE_MOVE.outcomes[bound.outcome].words,
            now=moment,
        )
        await db.commit()
        ended += 1
    return ended


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoveInput:
    """What the move workflow is started with. ``default_machine_id`` is the
    machine the org's default placement named when the move was asked for (or
    re-armed), for a move to the default: placement is the backend's, and the
    step that binds the chats checks that this machine still serves the org.

    The workflow takes these as positional strings across a process boundary;
    every starter builds them through :meth:`args`."""

    move_id: str
    org_team_id: str
    default_machine_id: str | None = None

    def args(self) -> list[str]:
        """The positional args, in the order the workflow's ``run`` declares them."""
        return [self.move_id, self.org_team_id, self.default_machine_id or ""]


async def lock_move(
    db: AsyncSession, move_id: uuid.UUID, *, org_team_id: uuid.UUID
) -> WorkspaceMachineMove | None:
    """The move row, locked for update; ``None`` when no move of this org has
    that id."""
    return (
        await lock_rows(
            db,
            LockRank.WORKSPACE_MOVE,
            select(WorkspaceMachineMove)
            .where(
                WorkspaceMachineMove.id == move_id,
                WorkspaceMachineMove.org_team_id == org_team_id,
            )
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()


async def active_move(
    db: AsyncSession, *, workspace_id: uuid.UUID, org_team_id: uuid.UUID
) -> WorkspaceMachineMove | None:
    """The workspace's move that has not ended, if any."""
    return (
        await db.execute(
            select(WorkspaceMachineMove).where(
                WorkspaceMachineMove.workspace_id == workspace_id,
                WorkspaceMachineMove.org_team_id == org_team_id,
                WorkspaceMachineMove.state.not_in(MOVE_FINISHED_STATES),
            )
        )
    ).scalar_one_or_none()


async def last_finished_move(
    db: AsyncSession, *, workspace_id: uuid.UUID, org_team_id: uuid.UUID
) -> WorkspaceMachineMove | None:
    """The workspace's most recent move that ended, if any."""
    return (
        await db.execute(
            select(WorkspaceMachineMove)
            .where(
                WorkspaceMachineMove.workspace_id == workspace_id,
                WorkspaceMachineMove.org_team_id == org_team_id,
                WorkspaceMachineMove.state.in_(MOVE_FINISHED_STATES),
            )
            .order_by(
                WorkspaceMachineMove.finished_at.desc().nulls_last(),
                WorkspaceMachineMove.requested_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def workspace_chats(
    db: AsyncSession, *, workspace_id: uuid.UUID, org_team_id: uuid.UUID, lock: bool = False
) -> list[WorkspaceObject]:
    """The live chats of the workspace, most recently active first. ``lock``
    takes their rows in id order (the order every multi-chat writer takes)."""
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.type == CHAT_TYPE,
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.org_team_id == org_team_id,
        WorkspaceObject.spec["workspace_id"].astext == str(workspace_id),
    )
    if lock:
        locked = (
            (
                await lock_rows(
                    db,
                    LockRank.WORKSPACE_OBJECT,
                    stmt.order_by(WorkspaceObject.id).execution_options(populate_existing=True),
                )
            )
            .scalars()
            .all()
        )
        return sorted(locked, key=lambda chat: (chat.updated_at, chat.created_at), reverse=True)
    rows = await db.execute(
        stmt.order_by(WorkspaceObject.updated_at.desc(), WorkspaceObject.created_at.desc())
    )
    return list(rows.scalars().all())


async def load_workspace(
    db: AsyncSession, workspace_id: uuid.UUID, *, org_team_id: uuid.UUID, lock: bool = False
) -> WorkspaceObject | None:
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.id == workspace_id,
        WorkspaceObject.org_team_id == org_team_id,
        WorkspaceObject.type == WORKSPACE_TYPE,
        WorkspaceObject.deleted_at == 0,
    )
    if lock:
        stmt = stmt.execution_options(populate_existing=True)
        return (await lock_rows(db, LockRank.WORKSPACE_OBJECT, stmt)).scalar_one_or_none()
    return (await db.execute(stmt)).scalar_one_or_none()


async def load_org_machine(
    db: AsyncSession, org_machine_id: uuid.UUID | None, *, org_team_id: uuid.UUID
) -> OrgMachine | None:
    """The org machine of THIS org with that id, deleted or not."""
    if org_machine_id is None:
        return None
    return (
        await db.execute(
            select(OrgMachine).where(
                OrgMachine.id == org_machine_id, OrgMachine.org_team_id == org_team_id
            )
        )
    ).scalar_one_or_none()


async def current_allocation(db: AsyncSession, om: OrgMachine | None) -> ComputeAllocation | None:
    """The provider machine backing ``om`` now, held by the same org."""
    if om is None or om.current_allocation_id is None:
        return None
    return (
        await db.execute(
            select(ComputeAllocation).where(
                ComputeAllocation.id == om.current_allocation_id,
                ComputeAllocation.tenant_org_id == om.org_team_id,
            )
        )
    ).scalar_one_or_none()


def _parse(raw: object) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


async def _allocation(db: AsyncSession, machine_id: object) -> ComputeAllocation | None:
    parsed = _parse(machine_id)
    return await db.get(ComputeAllocation, parsed) if parsed is not None else None


#: The states of a machine that will not finish anything it holds: it stopped
#: beating, it is asleep, or it left the plane.
_NOT_ANSWERING: frozenset[MachineState] = frozenset({NONE, UNREACHABLE, ASLEEP})


async def working_chats(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> set[uuid.UUID]:
    """The chats whose document says a turn is running right now."""
    if not chats:
        return set()
    by_org: dict[uuid.UUID, list[str]] = {}
    for chat in chats:
        by_org.setdefault(chat.org_team_id, []).append(str(chat.id))
    working: set[uuid.UUID] = set()
    for org_id, ids in by_org.items():
        rows = await db.execute(
            select(RealtimeDoc.doc_id).where(
                RealtimeDoc.org_id == org_id,
                RealtimeDoc.doc_type == "chat",
                RealtimeDoc.doc_id.in_(ids),
                (RealtimeDoc.turn_state == "working")
                | (
                    RealtimeDoc.turn_state.is_(None)
                    & (RealtimeDoc.state["meta"]["turn_state"]["state"].astext == "working")
                ),
            )
        )
        working |= {uuid.UUID(doc_id) for doc_id in rows.scalars().all()}
    return working


# ---------------------------------------------------------------------------
# Writes shared by the steps and the routes
# ---------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


async def announce_move(
    db: AsyncSession, move: WorkspaceMachineMove, *, actor: Mapping[str, Any] | None
) -> None:
    """The workspace's frame for a move that advanced: its id, its state and
    why it failed. Never a machine's price."""
    await emit(
        db,
        org_id=move.org_team_id,
        type=EventType.WORKSPACE_MACHINE_MOVE,
        entity=Entity.WORKSPACE_OBJECT,
        entity_id=str(move.workspace_id),
        payload={"move_id": str(move.id), "state": move.state, "error_code": move.error_code},
        actor=dict(actor) if actor else None,
    )


async def advance(
    db: AsyncSession,
    move: WorkspaceMachineMove,
    state: str,
    *,
    error_code: str = "",
    error: str = "",
    actor: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Move ``move`` (locked by the caller) to ``state`` and announce it.
    Refuses an edge the state machine does not have."""
    check_transition(move.state, state)
    if error_code and error_code not in MOVE_ERROR_CODES:
        raise ValueError(f"unknown move error code {error_code!r}")
    moment = now or datetime.now(UTC)
    move.state = state
    move.updated_at = moment
    if error_code:
        move.error_code = error_code
        move.error = (error or ERROR_WORDS[error_code])[:512]
    if state in MOVE_FINISHED_STATES:
        move.finished_at = moment
    await db.flush()
    await announce_move(db, move, actor=actor)


async def write_pin(
    db: AsyncSession,
    workspace: WorkspaceObject,
    pin: uuid.UUID | None,
    *,
    actor: Mapping[str, Any] | None,
) -> bool:
    """Set the workspace's pin on its locked row: the version moves and the
    workspace is announced. A machine the workspace lost is forgotten: a move
    is the answer to where it runs now. Returns whether anything changed."""
    spec = workspace_spec_of(workspace.spec)
    wanted = str(pin) if pin is not None else None
    lost = spec.lost_machine_id is not None or spec.fell_back_at is not None
    if spec.machine_pin == wanted and not lost:
        return False
    workspace.spec = spec.model_copy(
        update={"machine_pin": wanted, "lost_machine_id": None, "fell_back_at": None}
    ).model_dump(mode="json")
    workspace.version += 1
    workspace.content_updated_at = datetime.now(UTC).timestamp()
    await db.flush()
    await emit(
        db,
        org_id=workspace.org_team_id,
        type=EventType.WORKSPACE_OBJECT_CHANGED,
        entity=Entity.WORKSPACE_OBJECT,
        entity_id=str(workspace.id),
        version=workspace.version,
        payload={"type": workspace.type, "version": workspace.version},
        actor=dict(actor) if actor else None,
    )
    return True


async def announce_chat(
    db: AsyncSession, chat: WorkspaceObject, *, actor: Mapping[str, Any] | None
) -> None:
    """The chat's doorbell, naming the box it is bound to now: the frame the
    box it moved to must hear first."""
    await emit(
        db,
        org_id=chat.org_team_id,
        type=EventType.CHAT_UPDATED,
        entity=Entity.CHAT,
        entity_id=str(chat.id),
        version=chat.version,
        payload={
            "team_id": str(chat.team_id) if chat.team_id else None,
            BOUND_MACHINE_KEY: (chat.spec or {}).get("machine_id"),
        },
        actor=dict(actor) if actor else None,
    )


#: A machine's state in the chat record's vocabulary; a restarting box is the
#: drain it is.
_CHAT_WORD_FOR: Mapping[MachineState, ChatMachineStatus] = {
    "starting": "starting",
    READY: "ready",
    "draining": "draining",
    RESTARTING: "draining",
    UNREACHABLE: "unreachable",
    ASLEEP: "asleep",
    NONE: "none",
}


def chat_status_of(alloc: ComputeAllocation) -> ChatMachineStatus:
    """The machine's state in the chat record's vocabulary."""
    state = machine_state(alloc)
    return _CHAT_WORD_FOR[state]


async def send_stop(db: AsyncSession, chat: WorkspaceObject, *, user_id: uuid.UUID | None) -> None:
    """Ask the box running ``chat`` to end its turn now: the relay a reader's
    Stop sends, on the chat's channel. A box not running it ignores it."""
    doc = await db.get(RealtimeDoc, (chat.org_team_id, "chat", str(chat.id)))
    envelope = DocEnvelope(
        doc_id=str(chat.id),
        doc_type="chat",
        epoch=doc.epoch if doc else 1,
        peer_id=SERVER_PEER_ID,
        seq=0,
        kind="op",
        payload=OpPayload(
            op_id=f"srv-{uuid4().hex[:12]}",
            intent="user_message",
            events=[StopRelay(user_id=str(user_id) if user_id else "").model_dump(mode="json")],
        ).model_dump(mode="json"),
    )
    await emit(
        db,
        org_id=chat.org_team_id,
        type=EventType.DOC_OP,
        entity=Entity.DOC,
        entity_id=f"doc:chat:{chat.id}",
        version=doc.seq if doc else 0,
        payload={
            "envelope": envelope.model_dump(mode="json"),
            "team_id": str(doc.team_id) if doc and doc.team_id else None,
            "relay": True,
        },
    )


async def rebind(
    db: AsyncSession,
    chat: WorkspaceObject,
    alloc: ComputeAllocation,
    *,
    wake: bool,
    actor: Mapping[str, Any] | None,
    now: datetime,
) -> bool:
    """Bind the locked ``chat`` to ``alloc`` and end its service on the box it
    leaves. Returns whether the binding changed.

    The departed box's word on the chat's session goes with the binding, as
    does a refusal it reported; a wake stands, and ``wake`` stamps one so the
    box it moves to opens the chat even while that box reads asleep. A box that
    is still beating keeps its lease and hands the folder back itself (its last
    push is the only copy of its last turn); one that is not is fenced here, on
    the chat's folder and on the workspace's.
    """
    spec = dict(chat.spec or {})
    departed = spec.get("machine_id") or None
    if departed == str(alloc.id):
        return False
    spec["machine_id"] = str(alloc.id)
    spec["machine_status"] = chat_status_of(alloc)
    clear_refusal(spec)
    spec.pop("mirror_state", None)
    if wake and not spec.get("wake_requested_at"):
        spec["wake_requested_at"] = _iso(now)
    chat.spec = spec
    await db.flush()
    await hand_over(db, chat, departed=departed, actor=actor)
    await announce_chat(db, chat, actor=actor)
    return True


async def hand_over(
    db: AsyncSession,
    chat: WorkspaceObject,
    *,
    departed: str | None,
    actor: Mapping[str, Any] | None,
) -> tuple[uuid.UUID, ...]:
    """End the chat's service on ``departed`` with ``moved``: spare the lease of
    a box that still answers, fence one that does not. Returns the folders
    whose lease ended."""
    departed_id = _parse(departed)
    serving = (
        departed_id is not None
        and machine_state(await db.get(ComputeAllocation, departed_id)) not in _NOT_ANSWERING
    )
    ended = await chat_end.end_chat(
        db,
        chat.id,
        chat_end.ChatEndReason.MOVED,
        actor=actor,
        spare_serving_holder=departed_id if serving else None,
        announce=False,
        locked=chat,
    )
    released = list(ended.released)
    if departed_id is not None and not serving:
        released += await _end_workspace_lease(db, chat, departed_id)
    return tuple(released)


async def _end_workspace_lease(
    db: AsyncSession, chat: WorkspaceObject, departed: uuid.UUID
) -> list[uuid.UUID]:
    """End the lease a departed box that will not answer holds on the folder
    of the chat's workspace: only that box's, only on that folder."""
    workspace_id = chat_workspace_id(chat)
    if workspace_id is None:
        return []
    rows = (
        await db.execute(
            text(
                "UPDATE file_leases SET released_at = now(), grantable_after = now() "
                "WHERE org_team_id = :org AND holder_kind = 'machine' "
                "AND holder_principal_id = :departed "
                "AND released_at IS NULL AND reaped_at IS NULL AND expires_at > now() "
                "AND node_id IN (SELECT n.id FROM file_nodes n WHERE n.org_team_id = :org "
                "AND n.target_object_id = CAST(:workspace AS uuid) AND n.subtype = :subtype "
                "AND n.kind = 'folder') "
                "RETURNING node_id"
            ),
            {
                "org": chat.org_team_id,
                "departed": departed,
                "workspace": str(workspace_id),
                "subtype": WORKSPACE_TYPE,
            },
        )
    ).all()
    return [uuid.UUID(str(row.node_id)) for row in rows]


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


class StepOutcome(VersionedModel):
    """What one step left behind. Crosses workflow history.

    ``state`` is the move's state after the step. ``ready`` means the step's
    wait is over (the chats are at rest, the target took the workspace).
    ``origin`` maps each chat to the machine it ran on before the move, which
    a failure puts it back on. ``grace_seconds`` and ``wake_timeout_seconds``
    are the deployment's bounds, read once by the first step so the workflow
    never reads settings itself."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    state: str = MOVE_CANCELED
    ready: bool = False
    origin: dict[str, str | None] = Field(default_factory=dict)
    grace_seconds: int = Field(default=0, ge=0)
    wake_timeout_seconds: int = Field(default=0, ge=0)


#: The steps the workflow runs, each an idempotent call keyed by the move id.
STEP_BEGIN: Final = "begin"
STEP_DRAIN_POLL: Final = "drain_poll"
STEP_STOP_BUSY: Final = "stop_busy"
STEP_FLUSH: Final = "flush"
STEP_SWITCH: Final = "switch"
STEP_WAKE_BEGIN: Final = "wake_begin"
STEP_WAKE_POLL: Final = "wake_poll"
STEP_FAIL: Final = "fail"
STEPS: tuple[str, ...] = (
    STEP_BEGIN,
    STEP_DRAIN_POLL,
    STEP_STOP_BUSY,
    STEP_FLUSH,
    STEP_SWITCH,
    STEP_WAKE_BEGIN,
    STEP_WAKE_POLL,
    STEP_FAIL,
)


class StepRequest(VersionedModel):
    """One step the workflow asks for. Crosses workflow history."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    move_id: str
    org_team_id: str
    step: str
    default_machine_id: str | None = None
    origin: dict[str, str | None] = Field(default_factory=dict)
    error_code: str = ""


#: Who the steps act as: the move runs on its own once it is accepted, and
#: the person who asked for it is on the move row and in the audit trail.
MOVE_ACTOR_LABEL: Final = "workspace.machine_move"


def _actor(_move: WorkspaceMachineMove) -> dict[str, Any]:
    return actor_system(MOVE_ACTOR_LABEL)


async def begin(
    db: AsyncSession,
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    grace_seconds: int,
    wake_timeout_seconds: int,
    now: datetime | None = None,
) -> StepOutcome:
    """``requested → draining``; with ``stop_running`` the turns running in the
    workspace are stopped now. Returns where every chat ran before the move."""
    moment = now or datetime.now(UTC)
    move = await lock_move(db, move_id, org_team_id=org_team_id)
    if move is None:
        return StepOutcome(state=MOVE_CANCELED)
    chats = await workspace_chats(db, workspace_id=move.workspace_id, org_team_id=org_team_id)
    origin = {str(chat.id): (chat.spec or {}).get("machine_id") or None for chat in chats}
    outcome = StepOutcome(
        state=move.state,
        origin=origin,
        grace_seconds=grace_seconds,
        wake_timeout_seconds=wake_timeout_seconds,
    )
    if move.state != MOVE_REQUESTED:
        return outcome
    await advance(db, move, MOVE_DRAINING, actor=_actor(move), now=moment)
    if move.stop_running:
        await stop_busy(db, move_id=move_id, org_team_id=org_team_id, locked=move)
    return StepOutcome(
        state=move.state,
        origin=origin,
        grace_seconds=grace_seconds,
        wake_timeout_seconds=wake_timeout_seconds,
    )


async def _target_allocation_id(
    db: AsyncSession, move: WorkspaceMachineMove, default_machine_id: str | None
) -> str | None:
    if move.to_org_machine_id is None:
        return default_machine_id
    om = await load_org_machine(db, move.to_org_machine_id, org_team_id=move.org_team_id)
    if om is None or om.current_allocation_id is None:
        return None
    return str(om.current_allocation_id)


async def _busy(
    db: AsyncSession, move: WorkspaceMachineMove, *, default_machine_id: str | None
) -> list[WorkspaceObject]:
    """The chats of the workspace that are running a turn on a machine that
    still answers, other than the target."""
    target = await _target_allocation_id(db, move, default_machine_id)
    chats = [
        chat
        for chat in await workspace_chats(
            db, workspace_id=move.workspace_id, org_team_id=move.org_team_id
        )
        if ((chat.spec or {}).get("machine_id") or None) not in (None, target)
    ]
    working = await working_chats(db, chats)
    busy: list[WorkspaceObject] = []
    for chat in chats:
        if chat.id not in working:
            continue
        alloc = await _allocation(db, (chat.spec or {}).get("machine_id"))
        if machine_state(alloc) in _NOT_ANSWERING:
            # Nothing runs it any more: the switch fences that box.
            continue
        busy.append(chat)
    return busy


async def drain_poll(
    db: AsyncSession,
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    default_machine_id: str | None = None,
) -> StepOutcome:
    """Whether every chat of the workspace is at rest: no turn runs in it on a
    box that still answers."""
    move = (
        await db.execute(
            select(WorkspaceMachineMove).where(
                WorkspaceMachineMove.id == move_id,
                WorkspaceMachineMove.org_team_id == org_team_id,
            )
        )
    ).scalar_one_or_none()
    if move is None:
        return StepOutcome(state=MOVE_CANCELED)
    if move.state != MOVE_DRAINING:
        return StepOutcome(state=move.state)
    busy = await _busy(db, move, default_machine_id=default_machine_id)
    return StepOutcome(state=move.state, ready=not busy)


async def stop_busy(
    db: AsyncSession,
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    default_machine_id: str | None = None,
    locked: WorkspaceMachineMove | None = None,
) -> StepOutcome:
    """Stop every turn still running in the workspace. A stop that reaches a
    box not running the turn is ignored, so sending it twice costs nothing."""
    move = locked or await lock_move(db, move_id, org_team_id=org_team_id)
    if move is None:
        return StepOutcome(state=MOVE_CANCELED)
    if move.state != MOVE_DRAINING:
        return StepOutcome(state=move.state)
    for chat in await _busy(db, move, default_machine_id=default_machine_id):
        await send_stop(db, chat, user_id=move.requested_by)
    return StepOutcome(state=move.state)


#: How long the boxes leaving a workspace get to say every folder they hold of
#: it has been pushed, before the move gives up and puts everything back.
FLUSH_SECONDS: Final = 60.0
#: How long the flush waits for its own listener to reach the event stream
#: before it gives up asking.
LISTEN_SECONDS: Final = 10.0
#: What the move says when a leaving box could not save what it holds.
FLUSH_FAILED_WORDS: Final = "The current machine couldn't save this workspace's files."

Flusher = Callable[[Sequence[HeldLease], float], Awaitable[Mapping[uuid.UUID, str]]]
"""Ask each holder's machine to push the folder it holds and wait, bounded,
for its answer: the outcome per lease node (``landed`` when the push ended
with everything sent). The live seam is :func:`promoter_flusher`; a test hands
a :class:`~alkera_core.files.promotion.Promoter` whose ``publish`` is a
scripted box."""

#: A flush answer that leaves nothing unsaved: the push ended with everything
#: sent, or the box no longer holds the folder (it already handed it back).
_FLUSH_SAVED: frozenset[str] = frozenset({"landed", "not_holder"})


async def flush_with(
    promoter: Promoter, holders: Sequence[HeldLease], deadline: float
) -> dict[uuid.UUID, str]:
    """Every holder flushed through ``promoter`` at once, each bounded by
    ``deadline`` seconds."""
    outcomes = await asyncio.gather(
        *(promoter.flush(holder, deadline=deadline) for holder in holders)
    )
    return {
        holder.lease_node_id: str(outcome)
        for holder, outcome in zip(holders, outcomes, strict=True)
    }


async def promoter_flusher(holders: Sequence[HeldLease], deadline: float) -> dict[uuid.UUID, str]:
    """Flush through the machine channel from a process that holds no socket:
    a listener of its own on the event stream hears the boxes' acks, which the
    replica holding each box's socket publishes there."""
    from alkera_core.events.hub import EventHub
    from alkera_core.events.listener import PgListener
    from alkera_core.files.promotion import Promoter

    hub = EventHub()
    listener = PgListener(hub)
    await listener.start()
    try:
        if not await listener.wait_connected(LISTEN_SECONDS):
            return {holder.lease_node_id: "timed_out" for holder in holders}
        return await flush_with(Promoter(hub), holders, deadline)
    finally:
        await listener.stop()


async def departing_holders(
    db: AsyncSession, move: WorkspaceMachineMove, *, target: str | None, now: datetime
) -> list[HeldLease]:
    """The live leases on the workspace's folder, on its chats' folders and on
    anything under them, held by a box other than the target that is still
    beating. A box that stopped beating cannot push and is fenced at the
    switch instead."""
    chats = await workspace_chats(db, workspace_id=move.workspace_id, org_team_id=move.org_team_id)
    roots = [move.workspace_id, *(chat.id for chat in chats)]
    grace = timedelta(seconds=SERVED_HEARTBEATS * get_settings().files_lease_heartbeat_seconds)
    rows = (
        await db.execute(
            text(
                "SELECT l.node_id, l.epoch, l.machine_id FROM file_leases l "
                "JOIN file_nodes n ON n.id = l.node_id "
                "WHERE l.org_team_id = :org AND l.holder_kind = 'machine' "
                "AND l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > :now "
                "AND l.heartbeat_at >= :beating "
                "AND l.machine_id <> COALESCE(:target, '') "
                "AND EXISTS (SELECT 1 FROM file_nodes r WHERE r.org_team_id = :org "
                "AND r.target_object_id = ANY(CAST(:roots AS uuid[])) AND r.kind = 'folder' "
                "AND r.subtype IN (:workspace, :chat) AND n.path_ids <@ r.path_ids) "
                "ORDER BY l.node_id"
            ),
            {
                "org": move.org_team_id,
                "now": now,
                "beating": now - grace,
                "target": target,
                "roots": [str(root) for root in roots],
                "workspace": WORKSPACE_TYPE,
                "chat": CHAT_TYPE,
            },
        )
    ).all()
    return [
        HeldLease(
            org_id=move.org_team_id,
            lease_node_id=uuid.UUID(str(row.node_id)),
            epoch=int(row.epoch),
            machine=str(row.machine_id),
        )
        for row in rows
    ]


async def flush_capable(db: AsyncSession, machines: set[str]) -> set[str]:
    """Which of ``machines`` said on their heartbeat that they answer a flush."""
    ids = [parsed for parsed in (_parse(machine) for machine in machines) if parsed is not None]
    if not ids:
        return set()
    rows = await db.execute(
        select(ComputeAllocation.id, ComputeAllocation.capabilities_json).where(
            ComputeAllocation.id.in_(ids)
        )
    )
    return {
        str(alloc_id)
        for alloc_id, capabilities in rows.all()
        if BoxCapability.FOLDER_FLUSH_V1 in (capabilities or ())
    }


async def flush_departing(
    request: StepRequest,
    *,
    flusher: Flusher,
    session_factory: Callable[[], AsyncSession],
    deadline: float = FLUSH_SECONDS,
    now: datetime | None = None,
) -> StepOutcome:
    """Before any chat moves, every box leaving the workspace pushes what it
    holds of it, and the move waits (bounded) for each to say it did: the
    switch fences those boxes, and anything they had not pushed would be left
    on a machine nothing serves the workspace from any more. A box that
    answers ``busy`` or does not answer fails the move with everything left
    where it was. No transaction is open while the boxes are asked.

    Only a box that names :data:`BoxCapability.FOLDER_FLUSH_V1` is asked; one on an
    older build is moved the way every move ran before the flush, and the
    worker's log says the move ran without one."""
    moment = now or datetime.now(UTC)
    move_id = uuid.UUID(request.move_id)
    org = uuid.UUID(request.org_team_id)
    async with session_factory() as db:
        move = (
            await db.execute(
                select(WorkspaceMachineMove).where(
                    WorkspaceMachineMove.id == move_id,
                    WorkspaceMachineMove.org_team_id == org,
                )
            )
        ).scalar_one_or_none()
        if move is None:
            return StepOutcome(state=MOVE_CANCELED)
        if move.state != MOVE_DRAINING:
            return StepOutcome(state=move.state)
        target = await _target_allocation_id(db, move, request.default_machine_id or None)
        found = await departing_holders(db, move, target=target, now=moment)
        capable = await flush_capable(db, {holder.machine for holder in found})
        await db.rollback()
    holders = [holder for holder in found if holder.machine in capable]
    unasked = sorted({holder.machine for holder in found if holder.machine not in capable})
    if unasked:
        # A box on a build that cannot be asked is not asked: the move goes on
        # as it did before the flush existed (its turns finished, it pushes
        # each folder as it lets the chat go).
        log.warning("workspace.machine_move.unflushed", move_id=str(move_id), machines=unasked)
    outcomes = await flusher(holders, deadline) if holders else {}
    unsaved = sorted(
        str(node)
        for node in (h.lease_node_id for h in holders)
        if outcomes.get(node) not in _FLUSH_SAVED
    )
    if not unsaved:
        # Flushed before the switch: every leaving box that holds a folder of
        # the workspace pushed it. Not, when a box too old to be asked was
        # moved off on the push it makes as it lets each chat go.
        return await _record_flush(
            session_factory, move_id=move_id, org_team_id=org, flushed=not unasked
        )
    log.warning(
        "workspace.machine_move.flush_incomplete",
        move_id=str(move_id),
        unsaved=unsaved,
        outcomes={str(k): v for k, v in outcomes.items()},
    )
    async with session_factory() as db:
        outcome = await fail_move(
            db,
            move_id=move_id,
            org_team_id=org,
            error_code=DRAIN_TIMEOUT,
            origin=request.origin,
            error=FLUSH_FAILED_WORDS,
        )
        failed = await lock_move(db, move_id, org_team_id=org)
        if failed is not None:
            failed.flushed_before_switch = False
        await db.commit()
    return outcome


async def _record_flush(
    session_factory: Callable[[], AsyncSession],
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    flushed: bool,
) -> StepOutcome:
    """Put on the move's row whether the leaving boxes flushed before the
    switch, unless a cancel ended it meanwhile; the drain is then over."""
    async with session_factory() as db:
        move = await lock_move(db, move_id, org_team_id=org_team_id)
        if move is None:
            return StepOutcome(state=MOVE_CANCELED)
        if move.state != MOVE_DRAINING:
            return StepOutcome(state=move.state)
        move.flushed_before_switch = flushed
        await db.commit()
    return StepOutcome(state=MOVE_DRAINING, ready=True)


async def switch(
    db: AsyncSession,
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    default_machine_id: str | None = None,
    now: datetime | None = None,
) -> StepOutcome:
    """``draining → switching``: every chat of the workspace onto the target,
    in one transaction. A target that is gone fails the move here, before any
    chat moved."""
    moment = now or datetime.now(UTC)
    move = await lock_move(db, move_id, org_team_id=org_team_id)
    if move is None:
        return StepOutcome(state=MOVE_CANCELED)
    if move.state != MOVE_DRAINING:
        return StepOutcome(state=move.state)
    target: ComputeAllocation | None
    if move.to_org_machine_id is not None:
        om = await load_org_machine(db, move.to_org_machine_id, org_team_id=org_team_id)
        if om is None or om.deleted_at is not None:
            await fail(db, move=move, error_code=TARGET_DELETED, origin={}, now=moment)
            return StepOutcome(state=move.state)
        target = await current_allocation(db, om)
        if target is None:
            await fail(db, move=move, error_code=TARGET_CAPACITY, origin={}, now=moment)
            return StepOutcome(state=move.state)
    else:
        target = await _allocation(db, default_machine_id)
        if (
            target is None
            or machine_state(target) == NONE
            or not await may_serve(db, machine_id=target.id, org_id=org_team_id)
        ):
            await fail(db, move=move, error_code=TARGET_CAPACITY, origin={}, now=moment)
            return StepOutcome(state=move.state)
    await advance(db, move, MOVE_SWITCHING, actor=_actor(move), now=moment)
    chats = await workspace_chats(
        db, workspace_id=move.workspace_id, org_team_id=org_team_id, lock=True
    )
    for index, chat in enumerate(chats):
        # Every chat a box held is woken where it lands, and the most recent
        # one always, so the workspace comes up on the target even while the
        # target reads asleep.
        was_awake = (chat.spec or {}).get("mirror_state") == "awake"
        await rebind(db, chat, target, wake=was_awake or index == 0, actor=_actor(move), now=moment)
    workspace = await load_workspace(db, move.workspace_id, org_team_id=org_team_id, lock=True)
    if workspace is not None:
        _forget_box_report(workspace)
    await db.flush()
    log.info(
        "workspace.machine_move.switched",
        move_id=str(move.id),
        workspace_id=str(move.workspace_id),
        machine_id=str(target.id),
        chats=len(chats),
    )
    return StepOutcome(state=move.state)


def _forget_box_report(workspace: WorkspaceObject) -> None:
    """The departed box's word on the workspace's sandbox is not the target's:
    cleared, so only the target's own report can say it took the workspace."""
    spec = workspace_spec_of(workspace.spec)
    if spec.binding_authority != "workspace":
        return
    workspace.spec = spec.model_copy(
        update={"machine_id": None, "sandbox_state": None, "sandbox_reported_at": None}
    ).model_dump(mode="json")


async def wake_begin(
    db: AsyncSession,
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    now: datetime | None = None,
) -> StepOutcome:
    """``switching → waking``; a target asked to be off is asked to be on."""
    moment = now or datetime.now(UTC)
    move = await lock_move(db, move_id, org_team_id=org_team_id)
    if move is None:
        return StepOutcome(state=MOVE_CANCELED)
    if move.state != MOVE_SWITCHING:
        return StepOutcome(state=move.state)
    await advance(db, move, MOVE_WAKING, actor=_actor(move), now=moment)
    om = await load_org_machine(db, move.to_org_machine_id, org_team_id=org_team_id)
    if om is not None and om.deleted_at is None:
        alloc = await current_allocation(db, om)
        if om.desired_power == POWER_OFF or (alloc is not None and alloc.state == ASLEEP):
            # Imported here: the org-machine power axis imports compute state
            # this module's importers already hold.
            from alkera_core.compute.org_machines import request_power

            await request_power(
                db, om, desired="on", reason="", drain_kind=None, deadline=None, actor=_actor(move)
            )
    return StepOutcome(state=move.state)


def _target_failure(om: OrgMachine, alloc: ComputeAllocation | None) -> str | None:
    """Why the target will not take the workspace, or ``None`` while it may."""
    if om.deleted_at is not None:
        return TARGET_DELETED
    if alloc is None:
        return None
    if alloc.failure_kind == FAILURE_CAPACITY or alloc.terminated_reason == (
        TERMINATED_PROVIDER_CAPACITY
    ):
        return TARGET_CAPACITY
    if alloc.state == ALLOCATION_FAILED or alloc.terminated_reason == TERMINATED_BOOT_FAILED:
        return TARGET_BOOT_FAILED
    return None


async def wake_poll(
    db: AsyncSession,
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    origin: Mapping[str, str | None],
    now: datetime | None = None,
) -> StepOutcome:
    """Whether the target took the workspace; ``waking → done`` when it did,
    ``failed`` (chats put back) when the target cannot."""
    moment = now or datetime.now(UTC)
    move = await lock_move(db, move_id, org_team_id=org_team_id)
    if move is None:
        return StepOutcome(state=MOVE_CANCELED)
    if move.state != MOVE_WAKING:
        return StepOutcome(state=move.state, ready=move.state == MOVE_DONE)
    chats = await workspace_chats(db, workspace_id=move.workspace_id, org_team_id=org_team_id)
    bound = {(chat.spec or {}).get("machine_id") for chat in chats} - {None}
    target = next(iter(bound)) if len(bound) == 1 else None
    if move.to_org_machine_id is not None:
        om = await load_org_machine(db, move.to_org_machine_id, org_team_id=org_team_id)
        alloc = await current_allocation(db, om)
        failure = TARGET_DELETED if om is None else _target_failure(om, alloc)
        if failure is not None:
            await fail(db, move=move, error_code=failure, origin=origin, now=moment)
            return StepOutcome(state=move.state)
        if alloc is not None:
            target = str(alloc.id)
    took = await _took_workspace(db, move, chats, target)
    if not took:
        if target is not None and hand_back_overdue(move, moment):
            # The departed box never handed the workspace back; the target
            # cannot take it while that box's leases stand.
            await fence_departed(db, move, chats, target=target)
        return StepOutcome(state=move.state)
    await advance(db, move, MOVE_DONE, actor=_actor(move), now=moment)
    return StepOutcome(state=move.state, ready=True)


async def _took_workspace(
    db: AsyncSession,
    move: WorkspaceMachineMove,
    chats: Sequence[WorkspaceObject],
    target: str | None,
) -> bool:
    """Whether the target says it holds the workspace: its own report on the
    workspace's sandbox, or a chat it reports awake. A workspace with no chat
    has nothing to take: it is moved once the target is up."""
    if target is None:
        return False
    if not chats:
        return machine_state(await _allocation(db, target)) == READY
    workspace = await load_workspace(db, move.workspace_id, org_team_id=move.org_team_id)
    if workspace is not None:
        spec = workspace_spec_of(workspace.spec)
        if spec.machine_id == target and spec.sandbox_state == "awake":
            return True
    return any(
        (chat.spec or {}).get("machine_id") == target
        and (chat.spec or {}).get("mirror_state") == "awake"
        for chat in chats
    )


async def _still_there(db: AsyncSession, machine_id: str | None) -> ComputeAllocation | None:
    """The machine when it is still on the plane, running or stopped."""
    alloc = await _allocation(db, machine_id)
    return None if machine_state(alloc) == NONE else alloc


async def fail(
    db: AsyncSession,
    *,
    move: WorkspaceMachineMove,
    error_code: str,
    origin: Mapping[str, str | None],
    error: str = "",
    now: datetime | None = None,
) -> None:
    """End ``move`` (locked) as failed and put things back, the same way for
    every failure code (the move's outcome table in
    :mod:`alkera_core.lifecycle.machines`).

    After the switch, every chat goes back to the machine it left, or to the
    previous org machine's current allocation, whether or not that machine is
    running: a chat back on a stopped machine reads asleep there, and its
    next message starts it. The pin goes back with them. Two moves that fail
    the same way end in the same place.

    The one exception is a machine that is gone for good (released, or its
    org machine deleted): there is nothing to go back to, so the chats stay
    on the target, the only machine left that serves them, and the pin stays
    with them rather than naming a machine the chats are not on."""
    moment = now or datetime.now(UTC)
    if move.state in MOVE_FINISHED_STATES:
        return
    moved = move.state in CHATS_MAY_HAVE_MOVED
    if moved:
        previous_om = await load_org_machine(
            db, move.from_org_machine_id, org_team_id=move.org_team_id
        )
        previous_live = previous_om is not None and previous_om.deleted_at is None
        # The machine the org machine runs on now: where a chat goes back to
        # when the workflow that moved it no longer remembers where it was.
        fallback = (
            str(previous_om.current_allocation_id)
            if previous_om is not None and previous_live and previous_om.current_allocation_id
            else None
        )
        chats = await workspace_chats(
            db, workspace_id=move.workspace_id, org_team_id=move.org_team_id, lock=True
        )
        home = await _still_there(db, fallback)
        stayed = 0
        for chat in chats:
            alloc = await _still_there(db, origin.get(str(chat.id))) or home
            if alloc is None:
                stayed += 1
                continue
            await rebind(db, chat, alloc, wake=False, actor=_actor(move), now=moment)
        # Nothing to go back to: the pin stays where the chats are.
        gone = home is None and (stayed > 0 or not chats)
    else:
        gone = False
    workspace = await load_workspace(db, move.workspace_id, org_team_id=move.org_team_id, lock=True)
    if workspace is not None and not gone:
        await write_pin(db, workspace, move.from_org_machine_id, actor=_actor(move))
    await advance(
        db, move, MOVE_FAILED, error_code=error_code, error=error, actor=_actor(move), now=moment
    )
    log.info(
        "workspace.machine_move.failed",
        move_id=str(move.id),
        workspace_id=str(move.workspace_id),
        error_code=error_code,
    )


async def fail_move(
    db: AsyncSession,
    *,
    move_id: uuid.UUID,
    org_team_id: uuid.UUID,
    error_code: str,
    origin: Mapping[str, str | None],
    error: str = "",
    now: datetime | None = None,
) -> StepOutcome:
    """:func:`fail` by id, for the workflow's own deadlines."""
    move = await lock_move(db, move_id, org_team_id=org_team_id)
    if move is None:
        return StepOutcome(state=MOVE_CANCELED)
    await fail(db, move=move, error_code=error_code, origin=origin, error=error, now=now)
    return StepOutcome(state=move.state)


async def run_step(
    db: AsyncSession,
    request: StepRequest,
    *,
    grace_seconds: int,
    wake_timeout_seconds: int,
    now: datetime | None = None,
) -> StepOutcome:
    """Run the step ``request`` names, in the caller's transaction."""
    move_id = uuid.UUID(request.move_id)
    org = uuid.UUID(request.org_team_id)
    default = request.default_machine_id or None
    if request.step == STEP_BEGIN:
        return await begin(
            db,
            move_id=move_id,
            org_team_id=org,
            grace_seconds=grace_seconds,
            wake_timeout_seconds=wake_timeout_seconds,
            now=now,
        )
    if request.step == STEP_DRAIN_POLL:
        return await drain_poll(db, move_id=move_id, org_team_id=org, default_machine_id=default)
    if request.step == STEP_STOP_BUSY:
        return await stop_busy(db, move_id=move_id, org_team_id=org, default_machine_id=default)
    if request.step == STEP_FLUSH:
        raise ValueError("the flush step opens its own sessions: call flush_departing")
    if request.step == STEP_SWITCH:
        return await switch(
            db, move_id=move_id, org_team_id=org, default_machine_id=default, now=now
        )
    if request.step == STEP_WAKE_BEGIN:
        return await wake_begin(db, move_id=move_id, org_team_id=org, now=now)
    if request.step == STEP_WAKE_POLL:
        return await wake_poll(db, move_id=move_id, org_team_id=org, origin=request.origin, now=now)
    if request.step == STEP_FAIL:
        return await fail_move(
            db,
            move_id=move_id,
            org_team_id=org,
            error_code=request.error_code,
            origin=request.origin,
            now=now,
        )
    raise ValueError(f"unknown move step {request.step!r}")


__all__ = [
    "CANCELABLE_STATES",
    "CHATS_MAY_HAVE_MOVED",
    "DRAIN_TIMEOUT",
    "ERROR_WORDS",
    "FLUSH_FAILED_WORDS",
    "FLUSH_SECONDS",
    "MOVE_ERROR_CODES",
    "NOT_ALLOWED",
    "STEPS",
    "STEP_BEGIN",
    "STEP_DRAIN_POLL",
    "STEP_FAIL",
    "STEP_FLUSH",
    "STEP_STOP_BUSY",
    "STEP_SWITCH",
    "STEP_WAKE_BEGIN",
    "STEP_WAKE_POLL",
    "TARGET_BOOT_FAILED",
    "TARGET_CAPACITY",
    "TARGET_DELETED",
    "TRANSITIONS",
    "WAKE_TIMEOUT",
    "Flusher",
    "MoveCheck",
    "MoveInput",
    "MoveRefusal",
    "MoveSubject",
    "MoveTransitionError",
    "StepOutcome",
    "StepRequest",
    "active_move",
    "advance",
    "announce_chat",
    "announce_move",
    "begin",
    "chat_status_of",
    "check_transition",
    "current_allocation",
    "departing_holders",
    "drain_poll",
    "fail",
    "fail_move",
    "flush_capable",
    "flush_departing",
    "flush_with",
    "hand_over",
    "is_active",
    "last_finished_move",
    "load_org_machine",
    "load_workspace",
    "lock_move",
    "may_cancel",
    "may_transition",
    "move_checks",
    "promoter_flusher",
    "rebind",
    "register_move_check",
    "run_move_checks",
    "run_step",
    "send_stop",
    "stop_busy",
    "switch",
    "wake_begin",
    "wake_poll",
    "working_chats",
    "workspace_chats",
    "write_pin",
]
