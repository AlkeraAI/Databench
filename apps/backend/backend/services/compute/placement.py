"""Which running machine serves a chat — the single chat-to-machine binding.

Every chat-start path calls :func:`resolve_machine_for` and persists the
``machine_id`` it answers; nothing else picks a machine. The rule, in order:

0. **A person's own private chat runs on their personal box** while that box
   answers, unless its workspace is pinned to an org machine (rule 1, asked
   first): the box they registered through the device flow, bound to this
   org, which takes nobody else's chat and no shared workspace's
   (:func:`personal_machine_for`). Down or absent, the rules below apply.
1. **A workspace pinned to an org machine** runs on that machine and only that
   machine (:func:`pinned_machine`): the pin names an org machine of the
   chat's org (read with the org in the SQL, so a pin naming another org's
   machine finds nothing), not deleted, whose audience holds the chat's owner
   (a pool machine's audience is the whole org). Running, it is bound;
   stopped or stopping, it is bound and the message wakes it; starting,
   waiting for hardware or failed, it is bound and the chat reads the
   machine's state. The person chose this machine, so the chat never falls
   back to another one. A pin to a machine deleted or out of the owner's
   reach is a lost machine (:mod:`.machine_lost`): a person opening it picks.
2. **An org with a box of its own** (registered by a member under the org's
   grant, ``tenancy = org``) runs on it, preferring the org's live machine of
   ``settings.compute_web_chat_provider`` and falling back to its other live
   workspace machine — the rule every deployment ran before the pool existed.
3. **The org pool**, only for an org on an Enterprise plan or a self-hosted
   deployment (read at placement time, so a downgrade needs no data change):
   the org's org machines in ``pool`` use mode, the least loaded running one
   first, else one that is starting, else a stopped one (bound, and the
   message wakes it), else the shared pool when the org's
   ``shared_pool_fallback`` allows it (the default with no settings row),
   else nothing: the chat waits for the org's own machines.
4. **Everyone else runs on the shared pool**: the live pool box with the most
   room (``chats_served`` against the ``capacity`` its heartbeat carries), and
   the box that already holds the chat's folder when it is still live — a chat
   is moved only when the box serving it is gone.

An org machine in ``assigned`` use mode is reached only through a pin: no
regular chat of anyone in its audience lands on it, and a box coming up never
takes an unpinned chat onto it. A pinned chat is never taken by the pool or
the org's other boxes while its machine is down.

The signature admits what comes later — per-team machines, machine types by
purpose, a start-on-demand — because the caller hands over the acting context,
the team and the purpose, and receives a binding rather than a row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.machines import (
    ASLEEP,
    DRAINING,
    NONE,
    READY,
    RESTARTING,
    STARTING,
    UNREACHABLE,
    MachineState,
    MachineStatus,
    current_machine,
    live_workspace_machines,
    machine_state,
    machine_status,
    may_serve,
    personal_machines,
    pool_machines,
)
from alkera_core.compute.org_machines import (
    may_use,
    new_workspace_pin,
    team_ids_of,
)
from alkera_core.compute.unservable import is_unhealthy
from alkera_core.compute.workspace_lease import workspace_lease_holder
from alkera_core.config import settings
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.events import org_root_for_team
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.logging import get_logger
from alkera_core.models import (
    ComputeAllocation,
    WorkspaceObject,
)
from alkera_core.models.compute import (
    COMPUTE_ACTIVE_STATES,
    GVISOR_SANDBOX,
    ORG_TENANCY,
    PERSONAL_TENANCY,
    POOL_TENANCY,
)
from alkera_core.models.org_machines import OrgMachine
from alkera_core.objects import chat_end
from alkera_core.permission_presentation import PERMISSION_MODES, WRITING_MODES
from alkera_core.schemas.objects.specs import ChatSpec, CloudPermissionMode
from alkera_core.schemas.objects.specs import MachineStatus as ChatMachineStatus
from sqlalchemy import Uuid, case, cast, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.services import chats as chat_domain
from backend.services.compute import machine_lost as lost
from backend.services.compute.box_room import (
    bound_orgs,
    has_capability,
    has_worker_room,
    isolates_orgs,
    org_slots_free,
    org_worker_capacity,
    rolled_back_from_org_workers,
    runs_org_workers,
)
from backend.services.compute.grants import ComputeRefusedError
from backend.services.compute.org_machine_pins import (
    POOL_USE_MODE,
    PinAction,
    choose_org_pool,
    org_pool_applies,
    org_pool_machines,
    pin_action,
    pinned_machine,
    pins_of,
    shared_pool_fallback,
    workspace_pin,
)

log = get_logger(__name__)

MachinePurpose = Literal["chat"]

#: The permission stances in which the agent can run or modify code (untrusted
#: execution). A writable chat on the shared pool needs a real sandbox (gVisor).
#: Read from the mode vocabulary's owner, so a writing stance added there is
#: sandboxed here with no second list to update.
WRITABLE_PERMISSION_MODES: frozenset[str] = frozenset(WRITING_MODES)

_KNOWN_PERMISSION_MODES: frozenset[str] = frozenset(mode.value for mode in PERMISSION_MODES)


def chat_is_writable(permission_mode: str | None) -> bool:
    """Whether a chat in ``permission_mode`` can run or modify code. Unknown or
    missing reads as writable, the fail-safe answer, so a mode we do not
    recognise is bounded rather than waved onto a ``none`` pool box."""
    if permission_mode is None or permission_mode not in _KNOWN_PERMISSION_MODES:
        return True
    return permission_mode in WRITABLE_PERMISSION_MODES


def may_place_chat(alloc: ComputeAllocation, *, writable: bool) -> bool:
    """Whether a chat may bind to ``alloc``.

    A writable chat (the agent can run or modify code) on the shared
    multi-tenant pool needs gVisor: a pool box that does not report ``gvisor``
    is refused, so a second org's untrusted code never lands on a box with no
    real boundary. A single-tenant box (``org`` or ``dedicated``) carries only
    its one tenant's code, so ``none`` is fine there; and a read-only chat runs
    no untrusted mutations, so it is not gated. The allocator already keeps
    ``none`` boxes out of the pool (:func:`pool_machines`); this is the guard
    for a chat handed a box some other way — a preferred box, a stale binding.

    A box rolled back from running a worker per org takes no chat at all
    (:func:`rolled_back_from_org_workers`), and neither does one whose last
    beat said no worker of it can serve (``alkera_core.compute.unservable``)."""
    if writable and alloc.tenancy == POOL_TENANCY and alloc.sandbox != GVISOR_SANDBOX:
        return False
    return not rolled_back_from_org_workers(alloc) and not is_unhealthy(alloc)


UNSERVEABLE: frozenset[MachineState] = frozenset({NONE, UNREACHABLE, DRAINING, ASLEEP})
"""The machine states under which no box will answer a chat bound to it.

``draining`` belongs here even though the box is up and answering: it is
finishing what it already holds and taking nothing new, so a chat bound to it
that is NOT mid-turn has to move, and the same two moments that move a chat off
a dead box move it off a draining one — its next message, and the next box that
comes up. That is the hand-over: the draining box puts the chat to sleep (its
folder pushed, its lease given back) and placement puts it somewhere else.

``asleep`` belongs here because a stopped box answers nothing — but what
follows differs by tenancy. A pool box is interchangeable, so a chat parked on
a sleeping one moves to a pool box that is up. An org machine is the one its
pinned workspaces chose, or the org pool's: placement hands those chats back
to it even asleep (a pin always, the org pool when nothing else in it runs),
so a pinned chat is never taken by a box coming up, and the next message on
one of them wakes the machine instead of moving the chat.
"""


def chat_machine_status(
    spec: ChatSpec, machine: ComputeAllocation | None, *, now: datetime | None = None
) -> ChatMachineStatus:
    """What a chat says about the machine serving it, now — derived from the
    machine's own row, never from the word the chat's spec recorded.

    The spec's ``machine_status`` is a snapshot of the binding when it was
    made (or last restated); the machine moves on without it. So every reader
    — the chat page, the list, the console's chat rows, the org insight — asks
    this, and a chat stored as ``ready`` on a box that has since been released
    reads ``stranded`` everywhere without a sweep having touched the row.

    In order: a machine that said it cannot publish the chat overrides the
    binding (a live box that will not answer is not ready), unless that word
    predates the wake or the machine life the chat now waits on
    (:func:`refusal_stands`). A chat that was
    never placed reads ``none``. A chat placed on a box that is no longer on
    the plane, or no longer serves its org, reads ``stranded`` — it is waiting
    for the next box that comes up, or for its next message to place it. A box
    that is asleep reads ``asleep``, and so does a ready box that put THIS
    chat's session to sleep: only the box can say whether it holds the chat,
    and its last word stands while it answers. A restarting box reads as the
    drain it is, the chat's vocabulary having no word of its own for it.
    """
    if not spec.machine_id:
        return "refused" if spec.publisher_refusal else "none"
    try:
        UUID(spec.machine_id)
    except ValueError:
        return "refused" if spec.publisher_refusal else "none"
    state = machine_state(machine, now=now)
    if refusal_stands(spec, machine, state):
        return "refused"
    if state == READY and spec.mirror_state == "asleep":
        return "asleep"
    return _CHAT_WORD_FOR[state]


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def _instant(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def refusal_stands(spec: ChatSpec, machine: ComputeAllocation | None, state: MachineState) -> bool:
    """Whether the box's refusal on ``spec`` is its word NOW, rather than one
    said before the wake or the machine life the chat is waiting on.

    A wake or a restart is a state the chat passes through, and the box has
    not looked at the chat again since: a reader who asked for a wake after
    the refusal is owed that wake, and a machine whose state changed after the
    refusal was said (it slept, it was started again, it came back up) will
    say it again if it still holds. In each the chat reads as the machine's
    own state. A refusal said in the machine's current state stands, whatever
    that state. One with no stamp (written before the stamp existed) stands
    unless the machine is starting or asleep.
    """
    if not spec.publisher_refusal:
        return False
    said = _instant(spec.publisher_refusal_at)
    if said is None:
        return state not in (STARTING, ASLEEP)
    wake = _instant(spec.wake_requested_at)
    if wake is not None and wake > said:
        return False
    since = _aware(machine.state_changed_at) if machine is not None else None
    return since is None or since <= said


#: A machine's state in the chat's own vocabulary. Total over
#: :data:`MachineState`, so a state this table does not name fails at once
#: rather than reaching a reader as a word the chat has no slot for.
_CHAT_WORD_FOR: dict[MachineState, ChatMachineStatus] = {
    STARTING: "starting",
    READY: "ready",
    DRAINING: "draining",
    RESTARTING: "draining",
    UNREACHABLE: "unreachable",
    ASLEEP: "asleep",
    NONE: "stranded",
}


class LiveMachines:
    """An org's live workspace machines, read once per request.

    A chat's stored ``machine_status`` records what the BINDING looked like when
    the chat was created and is never written again — so served verbatim it says
    ``ready`` over a box that died an hour ago and ``starting`` forever for a
    chat opened while the box was coming up. The binding is the durable fact;
    the status is derived from the machine's own heartbeat here, the same
    judgment ``GET /machines/current`` and the sweep make, so all three agree by
    construction.
    """

    def __init__(self, allocations: Sequence[ComputeAllocation]) -> None:
        self._by_id = {alloc.id: alloc for alloc in allocations}

    def machine_for(self, machine_id: str | None) -> ComputeAllocation | None:
        """The live machine a chat is bound to, or ``None`` when it is bound
        to nothing, to a box that is no longer live, or to one that no longer
        serves the org: no box will serve such a chat — the daemon only serves
        chats bound to the machine it registered as — and
        :func:`chat_machine_status` says which of those it is."""
        if not machine_id:
            return None
        try:
            bound = UUID(machine_id)
        except ValueError:
            return None
        return self._by_id.get(bound)

    def status_of(self, spec: ChatSpec) -> ChatMachineStatus:
        """What the chat says about the machine serving it, now."""
        return chat_machine_status(spec, self.machine_for(spec.machine_id))


async def live_machines(db: AsyncSession, *, org_id: UUID) -> LiveMachines:
    """``org_id``'s live workspace machines: the chat's org, never the caller's."""
    rows = await db.execute(live_workspace_machines(org_id, with_personal=True))
    return LiveMachines(rows.scalars().all())


@dataclass(frozen=True, slots=True)
class MachineBinding:
    """The machine a chat is bound to at creation."""

    machine_id: UUID
    status: MachineStatus
    name: str
    #: Chosen by the workspace's pin. The pin's own read established that the
    #: machine is the chat's org's and that the owner may use it, whatever
    #: state it is in, so the live-box checks a binding otherwise meets
    #: (:func:`may_serve`, the sandbox guard, capabilities) do not apply: the
    #: chat waits on the machine its workspace was put on rather than move.
    pinned: bool = False
    #: Bound to a stopped machine the message should start.
    wake: bool = False

    @property
    def chat_status(self) -> ChatMachineStatus:
        """The status in the chat record's vocabulary. Placement never binds to
        a restarting box (it is out of placement), so the word the record has
        no slot for is only ever spelled as the drain it is."""
        return "draining" if self.status == "restarting" else self.status


def _binding(alloc: ComputeAllocation) -> MachineBinding:
    return MachineBinding(machine_id=alloc.id, status=machine_status(alloc), name=alloc.name)


async def _pool_machine(
    db: AsyncSession,
    *,
    prefer: UUID | None,
    capability: str | None = None,
    org_id: UUID | None = None,
) -> ComputeAllocation | None:
    """The pool box for a chat: the one that already holds it when that box
    is still ready, else the least loaded ready box. A box past its capacity
    is passed over while another has room, and still taken when every box is
    past it — a busy pool answers slowly, it does not strand the chat. A box
    that registered and has not beaten yet is taken only when no box is
    ready: it will beat, and the chat reads ``starting`` until it does.

    ``capability`` is what the chat needs of its box beyond running a chat (a
    chat of a shared workspace needs a box that runs the workspace in one
    sandbox). Boxes that said they can are the only candidates while any is
    up; when none can, every box stays one and :func:`resolve_machine_for`
    refuses the incapable answer. ``prefer`` (the box already holding the
    chat, or its workspace) is chosen among those candidates, so an incapable
    preferred box never shadows a capable one.

    A pool box that does not run a worker per org takes a chat of an org only
    while no other org's chat is bound to it (:func:`has_worker_room`)."""
    rows = (await db.execute(pool_machines())).scalars().all()
    boxes = [alloc for alloc in rows if machine_status(alloc) == READY]
    if not boxes:
        boxes = [alloc for alloc in rows if machine_status(alloc) == STARTING]
    # A box whose worker budget is spent takes no new org: each org costs a
    # process of its own there, and the memory for it is the box's to give.
    orgs_on = await bound_orgs(db, [alloc.id for alloc in boxes])
    boxes = [a for a in boxes if has_worker_room(a, orgs_on.get(a.id, set()), org_id)]
    if not boxes:
        return None
    if capability is not None:
        able = [alloc for alloc in boxes if has_capability(alloc, capability)]
        # Asked before ``prefer``: a preferred box that cannot do what the chat
        # needs would be refused by ``resolve_machine_for`` while a box that
        # can stands ready. With none able, every box stays a candidate and the
        # refusal is the caller's to say.
        boxes = able or boxes
    if prefer is not None:
        for alloc in boxes:
            if alloc.id == prefer:
                return alloc
    with_room = [alloc for alloc in boxes if alloc.chats_served < alloc.capacity]
    return with_room[0] if with_room else boxes[0]


def _pinned_binding(alloc: ComputeAllocation, action: PinAction) -> MachineBinding:
    """A pinned chat's binding. A machine off the plane (pending, failed) has no
    reachability of its own; the chat records ``starting`` and every reader
    derives the truth from the machine's row."""
    status: MachineStatus = (
        machine_status(alloc) if alloc.state in COMPUTE_ACTIVE_STATES else STARTING
    )
    return MachineBinding(
        machine_id=alloc.id, status=status, name=alloc.name, pinned=True, wake=action == "wake"
    )


async def place_for_org(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    prefer: UUID | None = None,
    capability: str | None = None,
) -> ComputeAllocation | None:
    """The machine that serves ``org_team_id``'s regular (unpinned) chats now,
    by rules 2 to 4 of the module docstring, or ``None`` when nothing does.
    ``prefer`` is the box a chat is already on — kept when it is still a live
    box the org may use."""
    own = await current_machine(db, org_id=org_team_id, provider=settings.compute_web_chat_provider)
    if own is None:
        # No machine of the preferred provider: the org's other live workspace
        # machine still serves the chat rather than leaving the reader with a
        # dead banner over compute that exists.
        own = await current_machine(db, org_id=org_team_id)
    if own is not None:
        return own
    if await org_pool_applies(db, org_id=org_team_id):
        org_pool = choose_org_pool(await org_pool_machines(db, org_id=org_team_id), prefer=prefer)
        if org_pool is not None:
            return org_pool
        if not await shared_pool_fallback(db, org_id=org_team_id):
            return None
    return await _pool_machine(db, prefer=prefer, capability=capability, org_id=org_team_id)


async def personal_machine_for(
    db: AsyncSession, *, org_team_id: UUID, owner_user_id: UUID
) -> ComputeAllocation | None:
    """The owner's personal box in this org while it answers, or ``None``.

    The one way a chat reaches a personal box: it names the chat's OWNER, and
    the box must be one that person registered in this very org and still
    stands behind (credential live, owner an active member). A box that is
    asleep, unreachable or draining is passed over, so the chat runs where the
    org's rules put it instead of waiting on a laptop that is shut."""
    rows = (
        await db.execute(personal_machines(org_id=org_team_id, owner_user_id=owner_user_id))
    ).scalars()
    for alloc in rows:
        if machine_state(alloc) not in UNSERVEABLE:
            return alloc
    return None


async def resolve_machine_for(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_team_id: UUID,
    purpose: MachinePurpose,
    prefer: UUID | None = None,
    permission_mode: CloudPermissionMode | None = None,
    capability: str | None = None,
    owner_user_id: UUID | None = None,
    workspace_pin: UUID | None = None,
) -> MachineBinding | None:
    """The machine that serves ``purpose`` for ``org_team_id`` on behalf of
    ``ctx``, or ``None`` when none is available. A team outside the caller's
    org resolves to nothing: another tenant's machine is never bound.

    ``prefer`` is the box the chat, or the workspace it is in, is already on;
    ``capability`` what the chat needs of whatever box it is placed on: a box
    that has not said it can is never bound (boxes that can are preferred in
    the pool, see :func:`_pool_machine`).

    A writable chat (``permission_mode``; unknown or missing is treated as
    writable) is never bound to a non-gVisor pool box — the sandbox placement
    guard. ``None`` for a read-only or planning chat, which runs no untrusted
    mutations and so is not gated.

    ``workspace_pin`` is the org machine the chat's workspace is pinned to and
    ``owner_user_id`` the chat's owner, whose place in the machine's audience
    the pin needs. A pin that holds is the answer whatever state its machine
    is in; one that does not counts as no pin.

    ``owner_user_id`` is the chat's owner when the chat is that person's own
    (not in a shared workspace: ``capability`` is ``None``); their personal
    box is asked next, after a workspace pin. Nobody else's chat ever names it."""
    if await org_root_for_team(db, org_team_id) != ctx.org_id:
        return None
    if workspace_pin is not None:
        pinned = await pinned_machine(
            db, org_id=ctx.org_id, pin=workspace_pin, owner_user_id=owner_user_id
        )
        if pinned is not None:
            _, pinned_alloc, action = pinned
            return _pinned_binding(pinned_alloc, action)
    if owner_user_id is not None and capability is None:
        personal = await personal_machine_for(
            db, org_team_id=ctx.org_id, owner_user_id=owner_user_id
        )
        if personal is not None:
            return _binding(personal)
    # Compute is held at the org root — a team below it runs on the org's box.
    alloc = await place_for_org(db, org_team_id=ctx.org_id, prefer=prefer, capability=capability)
    if alloc is None or not await may_serve(db, machine_id=alloc.id, org_id=ctx.org_id):
        return None
    if not may_place_chat(alloc, writable=chat_is_writable(permission_mode)):
        return None
    if capability is not None and not has_capability(alloc, capability):
        # A chat of a shared workspace on a box that cannot run one would be
        # served without its shared tree, silently. Nothing is placed instead;
        # the caller says why (:class:`CapabilityMissingError`).
        return None
    return _binding(alloc)


async def bound_machine_state(
    db: AsyncSession, *, org_team_id: UUID, machine_id: str | None
) -> MachineState:
    """What the machine this chat is bound to looks like now.

    ``none`` for a chat that never bound and for one whose machine is no longer
    live — the same judgment the chat routes serve, so "no workspace" means the
    same thing to a reader and to the rebind below."""
    if not machine_id:
        return "none"
    try:
        bound = UUID(machine_id)
    except ValueError:
        return "none"
    stmt = live_workspace_machines(org_team_id, with_personal=True).where(
        ComputeAllocation.id == bound
    )
    return machine_state((await db.execute(stmt)).scalars().first())


class CapabilityMissingError(Exception):
    """No box that can do what a chat needs is available, though a box is."""

    code = "workspace_box_unsupported"

    def __init__(self) -> None:
        super().__init__(
            "No machine that can run a shared workspace is available to this "
            "organization yet; start this chat in a workspace of its own"
        )


async def native_workspace(
    db: AsyncSession, workspace_id: str | UUID | None
) -> WorkspaceObject | None:
    """The live workspace ``workspace_id`` names when it owns a folder (its
    ``layout`` is ``native``): the kind whose chats share one sandbox and one
    lease, and so one box. ``None`` for anything else."""
    if not workspace_id:
        return None
    try:
        key = workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))
    except ValueError:
        return None
    workspace = await db.get(WorkspaceObject, key)
    if (
        workspace is None
        or workspace.type != WORKSPACE_TYPE
        or workspace.deleted_at != 0
        or (workspace.spec or {}).get("layout") != "native"
    ):
        return None
    return workspace


async def serving_lease_holder(db: AsyncSession, workspace: WorkspaceObject) -> UUID | None:
    """:func:`workspace_lease_holder` while that box still answers. A box that
    stopped beating, sleeps or drains will not serve the workspace's next
    chat, so its lease says nothing about where that chat should go."""
    holder = await workspace_lease_holder(
        db, org_team_id=workspace.org_team_id, workspace_id=workspace.id
    )
    if holder is None:
        return None
    if machine_state(await db.get(ComputeAllocation, holder)) in UNSERVEABLE:
        return None
    return holder


async def workspace_placement(
    db: AsyncSession, workspace_id: str | UUID | None, *, exclude: UUID | None = None
) -> tuple[UUID | None, str | None]:
    """``(prefer, capability)`` for a chat in ``workspace_id``.

    A workspace that owns a folder runs every chat in it in one sandbox under
    one lease on that folder, so its chats belong on one box, and a box newly
    chosen for it should be one that said it can run a workspace. The box
    preferred is the one holding the workspace's lease while it answers — no
    other box can serve a chat of it — and, failing that, the box a sibling
    chat is bound to. A workspace of one (or no workspace) asks nothing beyond
    what a chat always asked: ``(None, None)``. ``exclude`` is the chat being
    placed, whose own stale binding is not a sibling's.
    """
    workspace = await native_workspace(db, workspace_id)
    if workspace is None:
        return None, None
    holder = await serving_lease_holder(db, workspace)
    if holder is not None:
        return holder, BoxCapability.WORKSPACES
    bound_to = WorkspaceObject.spec["machine_id"].astext
    stmt = (
        select(bound_to)
        .where(
            WorkspaceObject.type == CHAT_TYPE,
            WorkspaceObject.deleted_at == 0,
            WorkspaceObject.org_team_id == workspace.org_team_id,
            WorkspaceObject.spec["workspace_id"].astext == str(workspace.id),
            bound_to.is_not(None),
            bound_to != "",
        )
        .order_by(WorkspaceObject.updated_at.desc())
        .limit(1)
    )
    if exclude is not None:
        stmt = stmt.where(WorkspaceObject.id != exclude)
    sibling = (await db.execute(stmt)).scalar_one_or_none()
    prefer: UUID | None = None
    if sibling:
        try:
            prefer = UUID(str(sibling))
        except ValueError:
            prefer = None
    return prefer, BoxCapability.WORKSPACES


async def place_new_chat(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: Any,
    mode: CloudPermissionMode | None,
    workspace: WorkspaceObject | None,
) -> MachineBinding | None:
    """The machine a new chat is bound to. A chat in a workspace that owns a
    folder goes where its siblings are, on a box that can run a workspace;
    raises :class:`CapabilityMissingError` when a box is up but none can, since
    the chat would run without the workspace's files."""
    org_team_id: UUID = ctx.org_id
    workspace_id = workspace.id if workspace is not None else None
    prefer, capability = await workspace_placement(db, workspace_id)
    pin = await workspace_pin(db, workspace_id, org_id=org_team_id)
    owner = getattr(user, "id", None)
    binding = await resolve_machine_for(
        db,
        ctx=ctx,
        org_team_id=org_team_id,
        purpose="chat",
        permission_mode=mode,
        prefer=prefer,
        capability=capability,
        owner_user_id=owner,
        workspace_pin=pin,
    )
    if binding is None and capability is not None:
        incapable = await resolve_machine_for(
            db,
            ctx=ctx,
            org_team_id=org_team_id,
            purpose="chat",
            permission_mode=mode,
            owner_user_id=owner,
            workspace_pin=pin,
        )
        if incapable is not None:
            raise CapabilityMissingError
    if binding is not None and not binding.pinned:
        await lost.fall_back_if_lost(db, workspace, owner_user_id=owner, actor=ctx.audit_dict())
    return binding


async def rebind_if_stranded(
    db: AsyncSession, *, chat: WorkspaceObject, ctx: ActingContext, org_team_id: UUID
) -> tuple[WorkspaceObject, ComputeRefusedError | None]:
    """Move a chat off a machine that is no longer live, on its next message.

    The binding is made once, at create. A box is a thing that sleeps, is
    replaced, or simply dies, and a chat whose box is gone is unserveable: a
    daemon serves only the chats bound to the machine it registered as, so the
    prompt is recorded, relayed at a machine id nobody answers to, and the
    reader waits forever. Placement runs again here because a message is the
    moment the answer matters — whether the person typed it in the browser or
    mentioned the bot in a Slack thread.

    A box that has stopped heartbeating counts as gone here, not just one whose
    row is off the plane. The row outlives the process: a killed daemon leaves
    ``state="ready"`` in the database for as long as the meter takes to reap it,
    so a rebind that only looked at the row would leave the chat pinned to a
    machine nobody is running and the prompt would be relayed into the void —
    the exact wait this function exists to end. ``unreachable`` and ``none`` are
    therefore the same answer to "can this box still serve the chat".

    A chat whose machine is still answering is not touched, and neither is one
    whose org has no reachable machine at all — there is nothing to move it to,
    and clearing the binding would only lose which box last had it.

    A chat whose only serving option is a box that is asleep — the org
    machine its workspace is pinned to, an org pool machine stopped to save its
    cost, or the one pool box, slept — is not moved but woken: the message is
    the signal a sleeping box waits for, and it is recorded either way, so the
    box coming up finds it. The wake is best-effort here; a provider that
    refuses the start, or credit that does not cover an org machine's start,
    leaves the box asleep and the chat parked on it, exactly as it was, for the
    next message or an admin to try again.

    A chat in a pinned workspace goes nowhere but its pinned machine: while the
    machine is down the chat stays bound to it and reads the machine's state.

    Returns the chat as it is bound now, and the admission refusal that left
    the box it needs stopped, if one did.
    """
    spec = chat_domain.chat_spec_of(chat)
    state = await bound_machine_state(db, org_team_id=org_team_id, machine_id=spec.machine_id)
    if state not in UNSERVEABLE:
        joined = await _join_workspace_holder(db, chat=chat, ctx=ctx, org_team_id=org_team_id)
        return joined, None
    prefer, capability = await workspace_placement(db, spec.workspace_id, exclude=chat.id)
    binding = await resolve_machine_for(
        db,
        ctx=ctx,
        org_team_id=org_team_id,
        purpose="chat",
        permission_mode=spec.permission_mode,
        prefer=prefer,
        capability=capability,
        # The chat's owner, never the sender: whether a pinned machine serves
        # the chat is the owner's place in its audience, not a colleague's.
        owner_user_id=chat.owner_user_id,
        workspace_pin=await workspace_pin(db, spec.workspace_id, org_id=org_team_id),
    )
    if binding is None:
        if state == ASLEEP and spec.machine_id and not await _is_org_machine(db, spec.machine_id):
            # Nothing else serves the org: the sleeping box the chat is on is
            # the one that will answer, so the message starts it. An org
            # machine is started only through a pin or the org pool, which
            # answered above; one that answered nothing is not this chat's.
            return chat, await _wake_for_chat(db, UUID(spec.machine_id), ctx=ctx)
        return chat, None
    moves = str(binding.machine_id) != (spec.machine_id or None)
    if (
        moves
        and not binding.pinned
        and not await may_serve(db, machine_id=binding.machine_id, org_id=org_team_id)
        and not await _owners_personal_box(db, binding.machine_id, chat=chat)
    ):
        return chat, None
    refused: ComputeRefusedError | None = None
    if binding.wake or binding.status == ASLEEP:
        # Before the chat's row is taken: a machine's rows come before the
        # chats bound to it, the order a sleep, a reconcile and a heartbeat
        # take them in, so the send never holds the chat while it waits for
        # the machine.
        refused = await _wake_for_chat(db, binding.machine_id, ctx=ctx)
    if moves:
        await lost.chat_fell_back_if_lost(db, chat, actor=ctx.audit_dict())
        chat = await _rebind(db, chat=chat, departed=spec.machine_id, binding=binding, ctx=ctx)
    return chat, refused


async def _is_org_machine(db: AsyncSession, machine_id: str) -> bool:
    try:
        key = UUID(machine_id)
    except ValueError:
        return False
    alloc = await db.get(ComputeAllocation, key)
    return alloc is not None and alloc.org_machine_id is not None


async def _wake_for_chat(
    db: AsyncSession, machine_id: UUID, *, ctx: ActingContext
) -> ComputeRefusedError | None:
    """A message reached a chat bound to a stopped machine: start it. An org
    machine is admitted first and its intent turned on, so the reconcile does
    not stop it again; a refused admission leaves it stopped.

    The wake runs in transactions of its own, after the caller's work so far
    is committed: a machine's rows come before the chats bound to it in the
    one lock order, and a caller that has already recorded an answer holds
    that chat. What the caller wrote is kept either way."""
    # Imported here: the org machines service and provisioning both import
    # this module for the hand-off a stop or a release makes.
    from backend.services.compute import org_machines as org_machine_service

    await db.commit()
    return (await org_machine_service.wake_for_chat(db, machine_id, ctx=ctx)).refused


async def _rebind(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    departed: str | None,
    binding: MachineBinding,
    ctx: ActingContext,
) -> WorkspaceObject:
    """Bind ``chat`` to ``binding``, end its service on the box it left, and
    put the move on record. Which box holds a chat decides who sees its
    prompts and its files, so the move is announced naming whoever's message
    caused it — the same frame a move made when a box comes up writes."""
    chat = await chat_domain.rebind_machine(
        db, chat=chat, machine_id=str(binding.machine_id), machine_status=binding.chat_status
    )
    await hand_over_folder(db, chat=chat, departed=departed, actor=ctx.audit_dict())
    await chat_domain.announce_chat(db, chat=chat, actor=ctx.audit_dict())
    return chat


async def _join_workspace_holder(
    db: AsyncSession, *, chat: WorkspaceObject, ctx: ActingContext, org_team_id: UUID
) -> WorkspaceObject:
    """Move a chat whose box still answers onto the box that now holds its
    workspace, when that is another box.

    A box serving a chat of a shared workspace can lose the workspace to a
    second box (the second took the lease while the first was unreachable).
    The first still answers, so nothing calls the chat stranded, yet it can no
    longer serve it: the folder is held elsewhere, and every message would be
    refused there until the second box put the workspace away. The lease
    holder is the one box that can serve the chat, so the chat goes there —
    only there: a chat is never moved off a box that answers to a third box.
    """
    spec = chat_domain.chat_spec_of(chat)
    workspace = await native_workspace(db, spec.workspace_id)
    if workspace is None:
        return chat
    holder = await serving_lease_holder(db, workspace)
    if holder is None or _same_machine(holder, spec.machine_id):
        return chat
    binding = await resolve_machine_for(
        db,
        ctx=ctx,
        org_team_id=org_team_id,
        purpose="chat",
        permission_mode=spec.permission_mode,
        prefer=holder,
        capability=BoxCapability.WORKSPACES,
        owner_user_id=chat.owner_user_id,
        workspace_pin=await workspace_pin(db, spec.workspace_id, org_id=org_team_id),
    )
    if binding is None or binding.machine_id != holder:
        return chat
    return await _rebind(db, chat=chat, departed=spec.machine_id, binding=binding, ctx=ctx)


def _same_machine(machine_id: UUID, bound: str | None) -> bool:
    try:
        return bound is not None and UUID(bound) == machine_id
    except ValueError:
        return False


#: The departed-box states in which the box will not hand a folder back itself:
#: it has stopped beating, been put to sleep, or left the plane. A draining box
#: is beating and finishing; its hand-back is its own to make, and lands under
#: the lease it still holds (the Files decider keeps a live holder its rung
#: after the binding moves), so the lease is left to it.
_CANNOT_RELEASE: frozenset[MachineState] = frozenset({NONE, UNREACHABLE, ASLEEP})


async def hand_over_folder(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    departed: str | None,
    actor: Mapping[str, Any] | None = None,
) -> bool:
    """End the chat's service on the box it just moved off of, through the one
    chat-end transition. True when a lease was ended.

    A move rebinds the chat, and the box it moves to takes the chat's folder
    under a lease of its own — refused for as long as the departed box's lease
    is live. A box that has stopped beating, is asleep or has left the plane
    will never hand the folder back, so its lease is ended here, in the move's
    own transaction: the frame that tells the new box about the chat finds the
    folder free, and the departed box, if it ever comes back, is fenced. A box
    that is still beating (draining) hands the folder back itself — its last
    push is the only copy of its last turn — so its lease is spared; the next
    grant fences it all the same.
    """
    departed_id: UUID | None = None
    if departed:
        try:
            departed_id = UUID(departed)
        except ValueError:
            departed_id = None
    serving = (
        departed_id is not None
        and machine_state(await db.get(ComputeAllocation, departed_id)) not in _CANNOT_RELEASE
    )
    ended = await chat_end.end_chat(
        db,
        chat.id,
        chat_end.ChatEndReason.MOVED,
        actor=actor,
        spare_serving_holder=departed_id if serving else None,
        # The move rings the chat's doorbell itself, once, and holds its row.
        announce=False,
        locked=chat,
    )
    released = list(ended.released)
    if departed_id is not None and not serving:
        released += await _end_departed_workspace_lease(db, chat, departed_id)
    if released:
        log.info(
            "compute.placement.departed_lease_released",
            chat_id=str(chat.id),
            machine_id=departed,
            node_ids=[str(node) for node in released],
        )
    return bool(released)


async def _end_departed_workspace_lease(
    db: AsyncSession, chat: WorkspaceObject, departed: UUID
) -> list[UUID]:
    """End the lease a departed box holds on the folder of the chat's workspace.

    A chat in a workspace that owns a folder is served under the box's lease on
    that whole folder, which ending the chat's own folder lease never reaches
    (it sits above the chat's folder). A box that will not hand anything back
    would otherwise keep every chat of the workspace off its new box until the
    lease lapsed. Only that box's lease, only on the workspace's own folder:
    a live box's lease is never ended here, and neither is anybody else's.
    """
    workspace_id = chat_domain.chat_spec_of(chat).workspace_id
    if not workspace_id:
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
                "workspace": workspace_id,
                "subtype": WORKSPACE_TYPE,
            },
        )
    ).all()
    return [UUID(str(row.node_id)) for row in rows]


def _stranded_chats(org_team_id: UUID | None, serving: list[str]) -> Any:
    bound_to = WorkspaceObject.spec["machine_id"].astext
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.type == "chat",
        WorkspaceObject.deleted_at == 0,
        or_(bound_to.is_(None), bound_to.not_in(serving)),
    )
    if org_team_id is not None:
        stmt = stmt.where(WorkspaceObject.org_team_id == org_team_id)
    # Most recently active first. A box coming up may not have room for every
    # stranded chat, and the ones a reader is waiting on are the ones touched
    # moments ago — a question asked while no box was up, a turn cut off when
    # a box died — while a chat left behind by an old release has not moved in
    # weeks. ``updated_at`` moves with every transcript entry and every change
    # to the chat's record, so it is the chat's last sign of life.
    return stmt.order_by(WorkspaceObject.updated_at.desc(), WorkspaceObject.created_at.desc())


async def bind_stranded_chats(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    binding: MachineBinding,
    actor: Mapping[str, Any] | None,
) -> list[WorkspaceObject]:
    """Move every chat of the org that nothing serves onto ``binding``, when a
    machine comes up. Returns the chats moved; the caller commits.

    :func:`rebind_if_stranded` runs on a chat's next message, which is the
    moment an answer matters — but a chat that already holds a question and
    whose org had no machine when it was asked (a Slack mention while the box
    restarted, a browser chat while its row was reaped) has no next message
    coming: the reader is waiting on the one they sent. A box registering and
    a box's first heartbeat are the other moments placement has to run, and
    they run it for the whole org at once, on the same judgment the send path
    makes per chat — a chat is stranded when it has no machine or its machine
    is ``unreachable`` or gone, and a chat whose machine still answers is not
    touched, whichever box just came up.

    ``binding`` is not taken on trust: the box that just came up must be one of
    the boxes that read says serve the org, or nothing moves. That read is the
    eligibility question — it drops a box nobody with the authority to place
    the org's chats stands behind — so a heartbeat, which any operator drives,
    cannot make a box the org's by announcing itself.

    One read of the org's live machines and ONE read of its chats, whatever
    the org's history; only the chats moved cost a lock and a write each. Each
    move is announced the way the send path announces it, so an open browser
    refreshes and the box's next list adopts the chat. A shared workspace's
    chats move together, and only onto a box that can run a workspace and is
    not displacing the box the workspace already runs on (:class:`StrandedUnit`).
    """
    # The org's personal boxes count as serving the chats bound to them: an
    # org box coming up must not take a person's chat off their own box.
    machines = (
        (await db.execute(live_workspace_machines(org_team_id, with_personal=True))).scalars().all()
    )
    serving = [str(m.id) for m in machines if machine_state(m) not in UNSERVEABLE]
    if str(binding.machine_id) not in serving:
        # The box announcing itself is not one this org may use — it is
        # draining, off the plane, or nobody with the authority to place the
        # org's chats stands behind it. The same read that says which boxes
        # serve the org says so; taking the caller's word for the target
        # instead is what made an ordinary heartbeat a placement decision, and
        # let a box nobody stands behind harvest the org's waiting chats and
        # take one off a box that had merely missed a window.
        return []
    target = next(m for m in machines if m.id == binding.machine_id)
    rows = await db.execute(_stranded_with_workspace(org_team_id, serving))
    stranded = [(chat, workspace) for chat, workspace in rows.all()]
    # A pinned workspace's chats wait for its machine; a lost one's, for a choice.
    held = await lost.held_back(db, [chat for chat, _workspace in stranded])
    stranded = [row for row in stranded if row[0].id not in held]
    mine = [
        chat
        for unit in await stranded_units(db, stranded, serving)
        if unit.may_land_on(target)
        for chat in unit.chats
    ]
    return await _move(db, mine, binding=binding, actor=actor, org_id=org_team_id)


@dataclass(slots=True)
class StrandedUnit:
    """Stranded chats that move together: one chat on its own, or every
    stranded chat of one shared workspace, which runs in one sandbox under one
    lease and so is served from one box or not at all.

    ``capability`` is what the unit needs of its box; ``home`` the box that
    answers and already serves the workspace (it holds the workspace's lease,
    or a sibling chat is bound to it), where the unit belongs instead of on
    whichever box comes up next."""

    chats: list[WorkspaceObject]
    capability: str | None = None
    home: UUID | None = None

    def may_land_on(self, alloc: ComputeAllocation) -> bool:
        """Whether the unit may move onto ``alloc``: it is the workspace's
        home or the workspace has none, and the box can do what it needs."""
        if self.home is not None and self.home != alloc.id:
            return False
        return self.capability is None or has_capability(alloc, self.capability)


def _stranded_with_workspace(org_team_id: UUID | None, serving: list[str]) -> Any:
    """:func:`_stranded_chats`, each row carrying the id of the chat's shared
    workspace (``None`` when it is in none, or in a workspace of one). Read in
    the same statement, so a box coming up still reads the chats once however
    many wait."""
    workspace = aliased(WorkspaceObject)
    named = WorkspaceObject.spec["workspace_id"].astext
    # Only a well-formed id is cast: a chat naming something else is in no
    # shared workspace, not a failed read.
    key = case((named.op("~*")(_UUID_PATTERN), cast(named, Uuid)), else_=None)
    shared = (
        select(workspace.id)
        .where(
            workspace.id == key,
            workspace.type == WORKSPACE_TYPE,
            workspace.deleted_at == 0,
            workspace.spec["layout"].astext == "native",
        )
        .correlate(WorkspaceObject)
        .scalar_subquery()
    )
    return _stranded_chats(org_team_id, serving).add_columns(shared)


_UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


async def stranded_units(
    db: AsyncSession, stranded: Sequence[tuple[WorkspaceObject, UUID | None]], serving: list[str]
) -> list[StrandedUnit]:
    """The rows of :func:`_stranded_with_workspace` grouped into the units
    that move together, in the order of each unit's most recently active
    chat. ``serving`` is the ids of the boxes that answer, as the stranded
    read was made against. Only a shared workspace costs a read of its own."""
    units: list[StrandedUnit] = []
    by_workspace: dict[UUID, StrandedUnit] = {}
    for chat, workspace_id in stranded:
        if workspace_id is None:
            units.append(StrandedUnit(chats=[chat]))
            continue
        unit = by_workspace.get(workspace_id)
        if unit is None:
            home = await _workspace_home(
                db, org_team_id=chat.org_team_id, workspace_id=workspace_id, serving=serving
            )
            unit = StrandedUnit(chats=[], capability=BoxCapability.WORKSPACES, home=home)
            by_workspace[workspace_id] = unit
            units.append(unit)
        unit.chats.append(chat)
    return units


async def _workspace_home(
    db: AsyncSession, *, org_team_id: UUID, workspace_id: UUID, serving: list[str]
) -> UUID | None:
    """The answering box a shared workspace already runs on: the holder of
    its lease, else the box any of its chats is bound to (a chat bound to an
    answering box is not stranded, so it is a sibling still served there)."""
    holder = await workspace_lease_holder(db, org_team_id=org_team_id, workspace_id=workspace_id)
    if holder is not None and str(holder) in serving:
        return holder
    bound_to = WorkspaceObject.spec["machine_id"].astext
    found = (
        await db.execute(
            select(bound_to)
            .where(
                WorkspaceObject.type == CHAT_TYPE,
                WorkspaceObject.deleted_at == 0,
                WorkspaceObject.org_team_id == org_team_id,
                WorkspaceObject.spec["workspace_id"].astext == str(workspace_id),
                bound_to.in_(serving),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return UUID(str(found)) if found else None


def pool_room(alloc: ComputeAllocation, *, bound: int) -> int | None:
    """How many chats ``alloc`` takes when stranded chats are bound to it in
    bulk, or ``None`` for no bound. ``bound`` is how many live chats are bound
    to the box right now, counted from the chats.

    First placement spreads the pool by ``chats_served`` against ``capacity``
    one chat at a time, each at the moment a message needs an answer — which
    is why a pool where every box is full still answers. A box that comes up
    is handed every stranded chat at once and has no such moment per chat, so
    it takes chats up to its room — the most recently active first — and the
    rest stay stranded, in that order, for the next box or for their next
    message (which places them one at a time as before). The room is what the
    box's own report leaves, or what is already bound to it when that is more:
    a box's report lags — its registration precedes its first beat, and the
    chats a pass just bound are not in the count it sent — and the chats
    themselves say where they are. A single-tenant box is its org's only box
    and first placement binds to it whatever its load; so does this.
    """
    if alloc.tenancy != POOL_TENANCY:
        return None
    return max(alloc.capacity - max(alloc.chats_served, bound), 0)


async def bind_stranded_chats_platform(
    db: AsyncSession,
    *,
    alloc: ComputeAllocation,
    actor: Mapping[str, Any] | None,
) -> list[WorkspaceObject]:
    """A platform box came up: every stranded chat, in any org, whose placement
    now lands on this box is moved onto it, up to the box's room. Returns the
    chats moved, the most recently active first.

    A pool box serves whichever orgs have nothing else; an org machine's box
    serves its one org (:func:`_bind_stranded_org_machine`). A pool box's orgs
    are not known from its own ``org_team_id`` (that is the operator's), so
    the stranded chats are read across orgs — bounded by
    how many chats are stranded, never by how many orgs exist — and each is
    admitted by exactly the three questions the send path asks of a new chat
    (:func:`resolve_machine_for`): placement for its org lands on this box
    (:func:`place_for_org`), the box may serve the org (:func:`may_serve`),
    and the box may hold this chat (:func:`may_place_chat` — a writable chat
    needs gVisor on the pool). A chat that places elsewhere (its org has its
    own box, or an org pool machine, or an org pool that is down with no
    fallback) is left where it is, and so is every chat whose workspace is
    pinned.

    The pool's capacity binds here as it binds first placement
    (:func:`pool_room`): the most recently active stranded chats fill the room
    and the rest wait, in that same order, for the next box or their next
    message. The room counts what is already bound to the box, not only what
    its last report said, so a second pass — the box's first heartbeat after
    its registration, another box coming up a moment later — sees the room the
    first pass took; the box's report is never written by anyone but the box.

    The chats of one shared workspace move as one unit (:class:`StrandedUnit`):
    onto a box that can run a workspace, never away from the answering box the
    workspace already runs on, and only when the room takes all of them — a
    workspace split across two boxes is served by one and refused by the other.
    """
    async with cross_tenant_write(db, reason="compute.placement.rescue_stranded"):
        if alloc.org_machine_id is not None:
            return await _bind_stranded_org_machine(db, alloc, actor=actor)
        if alloc.tenancy == ORG_TENANCY:
            return await bind_stranded_chats(
                db, org_team_id=alloc.org_team_id, binding=_binding(alloc), actor=actor
            )
        if alloc.tenancy == PERSONAL_TENANCY:
            return await _bind_stranded_personal(db, alloc, actor=actor)
        # Eligible boxes only, the same read every other caller makes: a chat
        # pinned to a box nobody stands behind IS stranded, and a pool box coming
        # up is the moment that rescues it.
        live = (await db.execute(live_workspace_machines())).scalars().all()
        serving = [str(m.id) for m in live if machine_state(m) not in UNSERVEABLE]
        if str(alloc.id) not in serving:
            return []
        room: int | None = None
        if alloc.tenancy == POOL_TENANCY:
            # Imported here: the machines service imports this module for the
            # hand-off a registration and a heartbeat make.
            from backend.services.compute.machines import bound_chat_counts

            bound = (await bound_chat_counts(db, [alloc.id])).get(alloc.id, 0)
            room = pool_room(alloc, bound=bound)
        if room == 0:
            return []
        if alloc.tenancy != POOL_TENANCY:
            # A single-tenant box that belongs to no org machine is reached by
            # no placement rule: nothing is placed on it.
            return []
        rows = await db.execute(_stranded_with_workspace(None, serving))
        stranded = [(chat, workspace) for chat, workspace in rows.all()]
        # Pinned or waiting on a choice of machine: the shared pool never takes it.
        held = await lost.held_back(db, [chat for chat, _workspace in stranded])
        stranded = [row for row in stranded if row[0].id not in held]
        admitted: dict[UUID, bool] = {}
        mine: list[WorkspaceObject] = []
        # The orgs this box already runs a worker for, and those this pass adds:
        # a new org past the box's worker budget waits for another box.
        orgs_here = (await bound_orgs(db, [alloc.id])).get(alloc.id, set())
        for unit in await stranded_units(db, stranded, serving):
            if room is not None and len(mine) >= room:
                break
            if not unit.may_land_on(alloc):
                continue
            taken: list[WorkspaceObject] = []
            for chat in unit.chats:
                org = chat.org_team_id
                if org not in admitted:
                    target = await place_for_org(db, org_team_id=org)
                    admitted[org] = (
                        target is not None
                        and target.id == alloc.id
                        and await may_serve(db, machine_id=alloc.id, org_id=org)
                    )
                if not admitted[org] or not has_worker_room(alloc, orgs_here, org):
                    continue
                mode = chat_domain.chat_spec_of(chat).permission_mode
                if not may_place_chat(alloc, writable=chat_is_writable(mode)):
                    continue
                taken.append(chat)
            if room is not None and len(mine) + len(taken) > room:
                # A workspace is served from one box: the unit waits whole for
                # a box with room for all of it rather than being split.
                continue
            mine.extend(taken)
            orgs_here.update(chat.org_team_id for chat in taken)
        return await _move(db, mine, binding=_binding(alloc), actor=actor, org_id=None)


async def _owners_personal_box(
    db: AsyncSession, machine_id: UUID, *, chat: WorkspaceObject
) -> bool:
    """Whether ``machine_id`` is the chat owner's personal box in the chat's
    org, live and stood behind: the one target :func:`may_serve` (an ORG's
    scope) does not cover that a chat of its owner may still be moved to."""
    stmt = personal_machines(org_id=chat.org_team_id, owner_user_id=chat.owner_user_id)
    found = await db.execute(stmt.where(ComputeAllocation.id == machine_id).limit(1))
    return found.scalars().first() is not None


async def _bind_stranded_personal(
    db: AsyncSession, alloc: ComputeAllocation, *, actor: Mapping[str, Any] | None
) -> list[WorkspaceObject]:
    """A personal box came up: its OWNER's stranded chats in its org move onto
    it, and nothing else. A chat of a workspace that owns a folder is shared,
    so it is never one of them; neither is anybody else's chat, in any org."""
    target = await personal_machine_for(
        db, org_team_id=alloc.org_team_id, owner_user_id=alloc.user_id
    )
    if target is None or target.id != alloc.id:
        return []
    live = await db.execute(live_workspace_machines(alloc.org_team_id, with_personal=True))
    serving = [str(m.id) for m in live.scalars() if machine_state(m) not in UNSERVEABLE]
    stranded = await db.execute(
        _stranded_chats(alloc.org_team_id, serving).where(
            WorkspaceObject.owner_user_id == alloc.user_id
        )
    )
    mine = []
    for chat in stranded.scalars():
        workspace_id = chat_domain.chat_spec_of(chat).workspace_id
        if (await workspace_placement(db, workspace_id, exclude=chat.id))[1] is None:
            mine.append(chat)
    return await _move(db, mine, binding=_binding(alloc), actor=actor, org_id=alloc.org_team_id)


async def _bind_stranded_org_machine(
    db: AsyncSession, alloc: ComputeAllocation, *, actor: Mapping[str, Any] | None
) -> list[WorkspaceObject]:
    """An org machine's box came up: the chats it serves move onto it, and no
    others. Those are the stranded chats of its org whose workspace is pinned
    to this very machine (and whose owner may use it), and, for a machine in
    the org pool of an org whose plan has one, the org's unpinned stranded
    chats when the org's placement lands on this box. An assigned machine
    never takes an unpinned chat, and no org machine takes another org's chat:
    the machine is read with its org on both sides, and the chats are read in
    that org only."""
    machine = (
        await db.execute(
            select(OrgMachine).where(
                OrgMachine.id == alloc.org_machine_id,
                OrgMachine.org_team_id == alloc.tenant_org_id,
                OrgMachine.current_allocation_id == alloc.id,
                OrgMachine.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if machine is None:
        return []
    org = machine.org_team_id
    live = (await db.execute(live_workspace_machines(org))).scalars().all()
    serving = [str(m.id) for m in live if machine_state(m) not in UNSERVEABLE]
    if str(alloc.id) not in serving:
        return []
    stranded = await lost.not_waiting(
        db, (await db.execute(_stranded_chats(org, serving))).scalars().all()
    )
    pins = await pins_of(db, stranded)
    takes_unpinned = False
    if machine.use_mode == POOL_USE_MODE and any(chat.id not in pins for chat in stranded):
        target = await place_for_org(db, org_team_id=org)
        takes_unpinned = target is not None and target.id == alloc.id
    mine = [
        chat
        for chat in stranded
        if (chat.id in pins and pins[chat.id].id == machine.id)
        or (chat.id not in pins and takes_unpinned)
    ]
    return await _move(db, mine, binding=_binding(alloc), actor=actor, org_id=org)


async def _move(
    db: AsyncSession,
    chats: list[WorkspaceObject],
    *,
    binding: MachineBinding,
    actor: Mapping[str, Any] | None,
    org_id: UUID | None,
) -> list[WorkspaceObject]:
    # ``binding`` is a target its caller has already established the chats may
    # be placed on — :func:`bind_stranded_chats` from the org's own serving set,
    # the platform pass from ``place_for_org`` per org, the send path from
    # :func:`may_serve`. Nothing is re-asked per chat: a heartbeat is a hot path
    # and this loop grows with the org's history.
    moved: list[WorkspaceObject] = []
    for chat in chats:
        departed = chat_domain.chat_spec_of(chat).machine_id
        locked = await chat_domain.rebind_machine(
            db, chat=chat, machine_id=str(binding.machine_id), machine_status=binding.chat_status
        )
        await hand_over_folder(
            db, chat=locked, departed=departed, actor=dict(actor) if actor else None
        )
        await chat_domain.announce_chat(db, chat=locked, actor=dict(actor) if actor else None)
        moved.append(locked)
    if moved:
        log.info(
            "compute.placement.bound_on_readiness",
            org_id=str(org_id) if org_id is not None else "*",
            machine_id=str(binding.machine_id),
            chats=len(moved),
        )
    return moved


__all__ = [
    "UNSERVEABLE",
    "WRITABLE_PERMISSION_MODES",
    "CapabilityMissingError",
    "LiveMachines",
    "MachineBinding",
    "MachinePurpose",
    "StrandedUnit",
    "bind_stranded_chats",
    "bind_stranded_chats_platform",
    "bound_machine_state",
    "bound_orgs",
    "chat_is_writable",
    "chat_machine_status",
    "choose_org_pool",
    "hand_over_folder",
    "has_capability",
    "has_worker_room",
    "isolates_orgs",
    "live_machines",
    "may_place_chat",
    "may_use",
    "native_workspace",
    "new_workspace_pin",
    "org_pool_applies",
    "org_slots_free",
    "org_worker_capacity",
    "personal_machine_for",
    "pin_action",
    "pinned_machine",
    "pins_of",
    "place_for_org",
    "pool_room",
    "rebind_if_stranded",
    "refusal_stands",
    "resolve_machine_for",
    "rolled_back_from_org_workers",
    "runs_org_workers",
    "serving_lease_holder",
    "shared_pool_fallback",
    "stranded_units",
    "team_ids_of",
    "workspace_lease_holder",
    "workspace_pin",
    "workspace_placement",
]
