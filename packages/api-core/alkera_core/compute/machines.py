"""How a workspace machine's reachability is judged, and the sweep that
announces a machine going quiet.

The daemon on a workspace machine heartbeats the cloud. The machine's status
is DERIVED from the last stamp against one window rather than stored, so the
backend (answering ``/machines/current``, binding a chat) and the worker (the
sweep) agree by construction:

- ``starting`` — registered, never heartbeated;
- ``ready`` — heartbeated within the window;
- ``draining`` — heartbeated within the window, and the box has been told to
  stop: it is finishing the turns it holds and will take no new chat;
- ``restarting`` — draining because the box's supervisor is restarting the
  daemon in place: it takes no new chat, keeps the ones it holds, and a fresh
  registration puts it back;
- ``unreachable`` — the last heartbeat is older than the window;
- ``asleep`` — stopped on purpose and kept: the box is off at the provider,
  so its silence is not unreachability; the chats bound to it stay bound and
  a wake starts it back;
- ``none`` — there is no live workspace machine (a released or failed row is
  not a machine).

The window is ``settings.compute_heartbeat_ready_seconds``, which defaults to
the heartbeat contract in :mod:`alkera_core.compute.liveness` — three missed
beats at the interval the daemon actually beats at, stated once so the two
halves cannot drift. A heartbeat announces a transition the moment it lands;
the periodic sweep announces the transition a heartbeat cannot — the machine
that stopped heartbeating — by comparing the derived status with the one last
announced. Nothing is emitted for a status that has not changed, so the sweep
runs often enough to name a dead box quickly and costs one indexed read a tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.machine_credential_standing import owner_stands_clause
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.events import announce_machine
from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    ASLEEP as ASLEEP_STATE,
)
from alkera_core.models.compute import (
    COMPUTE_ACTIVE_STATES,
    DEDICATED_TENANCY,
    DRAIN_RESTART,
    GVISOR_SANDBOX,
    ORG_TENANCY,
    PERSONAL_TENANCY,
    POOL_TENANCY,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.compute import (
    DRAINING as DRAINING_STATE,
)
from alkera_core.models.machine_credential import MachineCredential, OrgComputeAssignment

log = get_logger(__name__)

MachineStatus = Literal["starting", "ready", "draining", "restarting", "unreachable", "asleep"]
"""The reachability of a live workspace machine."""

MachineState = Literal[
    "starting", "ready", "draining", "restarting", "unreachable", "asleep", "none"
]
"""What the machine banner shows: a live machine's status, or that there is none."""

STARTING: MachineStatus = "starting"
READY: MachineStatus = "ready"
DRAINING: MachineStatus = "draining"
RESTARTING: MachineStatus = "restarting"
UNREACHABLE: MachineStatus = "unreachable"
ASLEEP: MachineStatus = "asleep"
NONE: MachineState = "none"

WORKSPACE = "workspace"


def ready_window() -> timedelta:
    return timedelta(seconds=settings.compute_heartbeat_ready_seconds)


def machine_status(alloc: ComputeAllocation, *, now: datetime | None = None) -> MachineStatus:
    """The reachability of a live workspace machine at ``now``.

    Silence outranks draining: a box that was told to stop AND has stopped
    beating is unreachable, which is the more urgent thing to say and the one
    the meter acts on. A box still beating while it finishes its turns reads
    ``draining`` — it is answering, and it is taking nothing new — or
    ``restarting`` when the drain is its supervisor restarting the daemon.

    A sleeping box outranks everything its heartbeat could say: it was stopped
    on purpose, so the silence that follows is the sleep, not a box that died,
    and a chat parked on it is waiting for a wake rather than for a move.
    """
    if alloc.state == ASLEEP_STATE:
        return ASLEEP
    leaving: MachineStatus = RESTARTING if alloc.drain_kind == DRAIN_RESTART else DRAINING
    if alloc.last_heartbeat_at is None:
        return leaving if alloc.state == DRAINING_STATE else STARTING
    moment = now or datetime.now(UTC)
    if moment - alloc.last_heartbeat_at >= ready_window():
        return UNREACHABLE
    return leaving if alloc.state == DRAINING_STATE else READY


def silent_since(alloc: ComputeAllocation, *, now: datetime | None = None) -> datetime | None:
    """When a registered machine's daemon was last heard from, if that is
    already past the ready window — ``None`` while its heartbeat is fresh.

    The moment is the last heartbeat or, for a box that has never beaten, the
    moment it (re)registered: a daemon that registers and dies before its first
    beat would otherwise be metered for ever, since nothing else can say it is
    gone. The meter reaps a registered machine from this, and bills it up to
    this moment — the last one the box was known to be alive — never past it.
    """
    moment = now or datetime.now(UTC)
    last_heard = alloc.last_heartbeat_at or alloc.ready_at or alloc.created_at
    if last_heard is None or moment - last_heard < ready_window():
        return None
    return last_heard


def machine_name_for_reader(machine: ComputeAllocation | None) -> str:
    """The machine's name where it is the org's to read. Which shared box
    serves a chat is the platform's, so a pool box has no name here."""
    if machine is None or machine.tenancy == POOL_TENANCY:
        return ""
    return machine.name


def machine_state(alloc: ComputeAllocation | None, *, now: datetime | None = None) -> MachineState:
    """The banner state for an org's machine row: ``none`` for no row and for
    a row that has left the active states — released, failed, lost, and
    ``releasing`` too. A releasing box is off the plane the moment the release
    is decided (placement no longer reads it, its credential is revoked), so
    a chat bound to it is waiting, not served, however fresh the last beat."""
    if alloc is None or alloc.state not in COMPUTE_ACTIVE_STATES:
        return NONE
    return machine_status(alloc, now=now)


def registrant_is_org_admin() -> ColumnElement[bool]:
    """Whether the operator a box registered under still holds admin at the org
    root the box is scoped to — admin on the root row itself, the same thing
    the org-admin dependency means, by membership or by a live role grant —
    through an ACTIVE membership of that org. A deactivated membership keeps
    its seats (so a reactivation restores them) but stands behind nothing, and
    the operator's standing in any other org has no bearing on this one.

    A workspace box is where an org's chats run: it receives their prompts and
    attachments and is leased the connections they reach. Only an org admin may
    stand one up, and this is the other half of that rule — placement asks the
    question again on every read, so a box whose operator has since been
    demoted, or one registered before the rule existed, stops taking chats
    instead of serving on. Expressed in SQL because placement reads a set of
    machines, not one.
    """
    # Imported here rather than at module level: the membership and assignment
    # models pull in the authorization enums, which import compute state.
    from alkera_core.authz.enums import PrincipalKind, Role
    from alkera_core.models._enums import MembershipStatus, TeamRole
    from alkera_core.models.org_membership import OrgMembership
    from alkera_core.models.role_assignment import RoleAssignment
    from alkera_core.models.team_membership import TeamMembership

    member_of_org = (
        select(OrgMembership.id)
        .where(
            OrgMembership.user_id == ComputeAllocation.user_id,
            OrgMembership.org_team_id == ComputeAllocation.org_team_id,
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
        .exists()
    )
    membership = (
        select(TeamMembership.id)
        .where(
            TeamMembership.user_id == ComputeAllocation.user_id,
            TeamMembership.org_team_id == ComputeAllocation.org_team_id,
            TeamMembership.team_id == ComputeAllocation.org_team_id,
            TeamMembership.role == TeamRole.ADMIN,
        )
        .exists()
    )
    granted = (
        select(RoleAssignment.id)
        .where(
            RoleAssignment.principal_kind == PrincipalKind.USER,
            RoleAssignment.principal_id == ComputeAllocation.user_id,
            RoleAssignment.scope_id == ComputeAllocation.org_team_id,
            RoleAssignment.role.in_([Role.OWNER, Role.ADMIN]),
            RoleAssignment.revoked_at.is_(None),
        )
        .exists()
    )
    return and_(member_of_org, or_(membership, granted))


def holds_a_live_credential() -> ColumnElement[bool]:
    """Whether a platform box's machine is still held by a live (unrevoked)
    machine credential.

    A pool or dedicated box exists only because the platform minted a
    credential for it; the credential IS the box's standing. Revoking it (or
    rotating it away: a claim under a new credential retires the old one) takes
    that standing away at once, on every surface, without waiting for the box
    to present the credential again — most of what a box does (reading a chat,
    publishing its events, leasing its folder) rides its session, not the
    credential header. Expressed in SQL for the same reason as
    :func:`registrant_is_org_admin`."""
    return (
        select(MachineCredential.id)
        .where(
            MachineCredential.machine_id == ComputeAllocation.id,
            MachineCredential.revoked_at.is_(None),
        )
        .exists()
    )


def owner_stands_in_its_org() -> ColumnElement[bool]:
    """Whether a personal box's owner (the row's ``user_id``, the person the
    credential was minted for) still stands behind the box in the org it is
    bound to, by the one rule every door reads
    (:func:`~alkera_core.auth.machine_credential_standing.owner_stands_clause`):
    an owner who was deactivated, banned, deleted or moved out of the org
    stops standing behind the box at once, on every read."""
    return owner_stands_clause(ComputeAllocation.user_id, ComputeAllocation.org_team_id)


def stands_behind_its_org() -> ColumnElement[bool]:
    """Whether somebody with the authority to put an org's chats on this box
    still stands behind it.

    This is THE eligibility question, asked in one place. A pool or dedicated
    box passes while the machine credential the platform minted for it is
    live — authority no org holds, and the platform's to withdraw. An org's
    own box passes only while its registrant is an admin of the org root — the
    same authority registering one takes. Asked on every read rather than
    stored, so a box registered before that rule existed, a box whose operator
    was demoted and a box whose credential was revoked all stop taking chats
    without a migration or a sweep. A personal box passes while its credential
    is live AND its owner is still an active member of its org. Any other
    tenancy passes nothing.
    """
    return or_(
        and_(
            ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)),
            holds_a_live_credential(),
        ),
        and_(ComputeAllocation.tenancy == ORG_TENANCY, registrant_is_org_admin()),
        and_(
            ComputeAllocation.tenancy == PERSONAL_TENANCY,
            holds_a_live_credential(),
            owner_stands_in_its_org(),
        ),
    )


def _serves_org(org_id: UUID) -> ColumnElement[bool]:
    """The machines scoped to ``org_id``: the org's own boxes, every pool box,
    the box dedicated to it, and the provider machines of its org machines. A
    platform box carries the operator org in ``org_team_id``, so the org column
    alone would make the pool invisible to every tenant and the operator's own.

    Scope only — whether a box in scope may actually be used is
    :func:`stands_behind_its_org`, which every placement read applies whether
    or not it names an org.

    A personal box is in no org's scope: it serves one person, so nothing that
    places an ORG's chats may ever pick it. Placement reaches it only through
    :func:`personal_machines`, naming the owner.
    """
    dedicated = select(OrgComputeAssignment.machine_id).where(
        OrgComputeAssignment.org_team_id == org_id
    )
    return or_(
        and_(ComputeAllocation.tenancy == ORG_TENANCY, ComputeAllocation.org_team_id == org_id),
        ComputeAllocation.tenancy == POOL_TENANCY,
        and_(ComputeAllocation.tenancy == DEDICATED_TENANCY, ComputeAllocation.id.in_(dedicated)),
        # An org machine's provider machine: its tenant is set at creation and
        # can never change, so it serves exactly that org.
        and_(
            ComputeAllocation.tenancy == DEDICATED_TENANCY,
            ComputeAllocation.org_machine_id.is_not(None),
            ComputeAllocation.tenant_org_id == org_id,
        ),
    )


#: Where a box's beat names the orgs whose worker keeps failing on it
#: (``MachineResources.org_workers_failing_ids``).
FAILING_ORGS_KEY: Final = "org_workers_failing_ids"


def failing_orgs(alloc: ComputeAllocation) -> frozenset[str]:
    """The orgs whose worker is crash-looping on the box, as its last beat
    said: the box is up, but it serves none of those orgs' chats."""
    raw = (alloc.resources_json or {}).get(FAILING_ORGS_KEY)
    return frozenset(str(org) for org in raw) if isinstance(raw, list) else frozenset()


def _not_failing(org_id: UUID) -> ColumnElement[bool]:
    """:func:`failing_orgs` in SQL: the box's last beat does not name the org.
    A box that names none (or an older box) is not gated."""
    named = func.coalesce(
        ComputeAllocation.resources_json.op("->")(FAILING_ORGS_KEY), cast(literal("[]"), JSONB)
    )
    return ~named.op("@>")(func.jsonb_build_array(str(org_id)))


def live_workspace_machines(
    org_id: UUID | None = None,
    *,
    provider: str | None = None,
    tenancy: str | None = None,
    placeable: bool = False,
    eligible: bool = True,
    with_personal: bool = False,
) -> Select[tuple[ComputeAllocation]]:
    """The live workspace machines that may serve ``org_id``'s chats (every
    live machine when ``org_id`` is None), the ones answering now first and the
    longest-standing of those first.

    The order is deliberately not "whoever beat most recently": every box
    beats on its own schedule, so a box with a faster timer would take the
    org's next chat from one that has served it for a month. Inside the
    answering group the oldest allocation wins, a stable choice no timer can
    change.

    ``provider`` narrows to the machines whose catalog type belongs to that
    provider kind, so placement can ask for "the org's container machine"
    without learning where a provider name is stored. ``tenancy``
    narrows to one tenancy: ``org`` for the org's OWN boxes (what the banner
    and the org-preferred placement mean by "the org's machine"), ``pool`` for
    the shared pool, ``dedicated`` for the box assigned to the org.

    ``placeable`` drops the boxes that are draining. A draining box is live
    (metered, answering the turns it holds, resolving the banner over those
    chats) but stopping, so nothing may be placed on it. Placement passes
    ``True``; the banner, the sweep and the meter read every live box and must
    not.

    ``eligible`` applies :func:`stands_behind_its_org` and is on by default,
    so a reader that has not thought about it gets the safe answer. The two readers
    that must see a box nobody stands behind say so: the reachability sweep
    (it announces every live box) and the meter (it has to stop one).

    ``with_personal`` adds the org's personal boxes to its scope, for the
    reads that say how the box a chat is ALREADY bound to stands (the banner,
    a listing's status, the rebind's "is it still up"). Never for a read that
    chooses a box: a personal box is chosen only by
    :func:`personal_machines`."""
    stmt = select(ComputeAllocation).where(
        ComputeAllocation.lifecycle == WORKSPACE,
        ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES),
    )
    if placeable:
        stmt = stmt.where(ComputeAllocation.state != DRAINING_STATE)
    if eligible:
        stmt = stmt.where(stands_behind_its_org())
    if org_id is not None and with_personal:
        stmt = stmt.where(
            or_(
                _serves_org(org_id),
                and_(
                    ComputeAllocation.tenancy == PERSONAL_TENANCY,
                    ComputeAllocation.org_team_id == org_id,
                ),
            )
        )
    elif org_id is not None:
        stmt = stmt.where(_serves_org(org_id))
    if org_id is not None:
        # A box whose worker for the org keeps failing serves none of its
        # chats: they read stranded there, move on their next message, and no
        # new one is placed on it.
        stmt = stmt.where(_not_failing(org_id))
    if tenancy is not None:
        stmt = stmt.where(ComputeAllocation.tenancy == tenancy)
    if provider is not None:
        stmt = stmt.join(
            ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id
        ).where(ComputeMachineType.provider == provider)
    answering = (
        ComputeAllocation.last_heartbeat_at.isnot(None)
        & (ComputeAllocation.last_heartbeat_at > func.now() - ready_window())
    ).desc()
    return stmt.order_by(answering, ComputeAllocation.created_at.asc())


async def current_machine(
    db: AsyncSession, *, org_id: UUID, provider: str | None = None
) -> ComputeAllocation | None:
    """The org's OWN workspace machine: the longest-standing one that is
    answering and whose operator is an admin of the org, optionally restricted
    to one provider kind. A pool or dedicated box is never an org's own machine
    — placement reaches those by tenancy. A draining box is not one either:
    this is a placement read."""
    return (
        await db.execute(
            live_workspace_machines(
                org_id, provider=provider, tenancy=ORG_TENANCY, placeable=True
            ).limit(1)
        )
    ).scalar_one_or_none()


def personal_machines(*, org_id: UUID, owner_user_id: UUID) -> Select[tuple[ComputeAllocation]]:
    """The live, placeable personal boxes ``owner_user_id`` registered in
    ``org_id`` and still stands behind: the only boxes that person's own
    chats may be placed on beyond the org's, and boxes nobody else's chat is
    ever placed on. The answering one first, as :func:`live_workspace_machines`
    orders."""
    return live_workspace_machines(tenancy=PERSONAL_TENANCY, placeable=True).where(
        ComputeAllocation.org_team_id == org_id,
        ComputeAllocation.user_id == owner_user_id,
    )


async def may_serve(db: AsyncSession, *, machine_id: UUID, org_id: UUID) -> bool:
    """Whether ``org_id``'s chats may be placed on ``machine_id`` right now.

    The one question every bind asks, whatever chose the target: the row is a
    live workspace machine, in this org's scope, not draining, and somebody
    with the authority to place the org's chats stands behind it. A caller that
    picked its target through :func:`live_workspace_machines` gets the same
    answer twice; a caller handed a machine from somewhere else — a binding
    passed in, a box announcing itself ready — is checked here rather than
    trusted. One indexed read.
    """
    stmt = live_workspace_machines(org_id, placeable=True).where(ComputeAllocation.id == machine_id)
    return (await db.execute(stmt.limit(1))).scalars().first() is not None


def pool_machines() -> Select[tuple[ComputeAllocation]]:
    """Every pool box a chat may be placed on, least loaded first
    (``chats_served`` against ``capacity``), then most recently heard from —
    the order placement spreads new chats in. A draining box is left out: it
    is going away and must not be handed the chat it would have to hand on
    again, and so is a box whose credential was revoked
    (:func:`stands_behind_its_org`).

    A box that does not report gVisor is left out too: the shared pool runs
    many tenants' untrusted code, so its boundary must be gVisor (runsc). A
    ``none`` box is single-tenant only and never joins the pool — the server
    guard that closes the placement hole (a ``none`` node serving a second
    org's chat)."""
    load = ComputeAllocation.chats_served * 1000 / ComputeAllocation.capacity
    return (
        select(ComputeAllocation)
        .where(
            ComputeAllocation.lifecycle == WORKSPACE,
            ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES),
            ComputeAllocation.state != DRAINING_STATE,
            ComputeAllocation.tenancy == POOL_TENANCY,
            ComputeAllocation.sandbox == GVISOR_SANDBOX,
            stands_behind_its_org(),
        )
        .order_by(
            load.asc(),
            ComputeAllocation.last_heartbeat_at.desc().nulls_last(),
            ComputeAllocation.created_at.desc(),
        )
    )


class MachineStanding(StrEnum):
    """What an agent assertion naming a machine turns out to be."""

    #: The machine speaking: see :func:`verify_machine_assertion`.
    SPEAKS = "speaks"
    #: Not the machine — a person, another session, a name that is no machine,
    #: a released row. Such a request is simply not the machine; it goes on as
    #: whoever it is.
    NOT_THE_MACHINE = "not_the_machine"
    #: The box's OWN session (the credential the machine registered with, its
    #: operator, its org) naming a platform machine whose machine credential is
    #: no longer live. This is the box the platform took away, and the request
    #: is refused outright rather than downgraded to the box user.
    REFUSED = "refused"


async def machine_standing(
    db: AsyncSession,
    *,
    machine_id: str | None,
    org_id: UUID,
    operator_user_id: UUID | None,
    credential_id: str | None,
) -> MachineStanding:
    """Resolve an agent assertion naming ``machine_id`` to one of three
    answers (:class:`MachineStanding`) in one indexed read. The seam every
    surface asks: REST access facts, the socket's admission and its tick, the
    Files lease holder, and the request dependency that refuses a box whose
    credential was taken away."""
    if not machine_id or operator_user_id is None or not credential_id:
        return MachineStanding.NOT_THE_MACHINE
    try:
        row_id = UUID(machine_id)
    except ValueError:
        return MachineStanding.NOT_THE_MACHINE
    stmt = select(
        ComputeAllocation.tenancy,
        ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES),
        stands_behind_its_org(),
        holds_a_live_credential(),
    ).where(
        ComputeAllocation.id == row_id,
        ComputeAllocation.org_team_id == org_id,
        ComputeAllocation.user_id == operator_user_id,
        ComputeAllocation.registered_jti == credential_id,
        ComputeAllocation.lifecycle == WORKSPACE,
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return MachineStanding.NOT_THE_MACHINE
    tenancy, live, eligible, credential_live = row
    if tenancy != ORG_TENANCY and not credential_live:
        return MachineStanding.REFUSED
    if live and eligible:
        return MachineStanding.SPEAKS
    return MachineStanding.NOT_THE_MACHINE


async def machine_still_stands(db: AsyncSession, *, machine_id: str) -> bool:
    """Whether ``machine_id`` is still a live workspace machine that somebody
    stands behind (its credential live, for a platform box).

    For the one door that cannot re-verify an assertion because it has no
    session to verify: the content origin, redeeming a URL a box minted. The
    mint proved the machine then; this says whether it still is one now, so a
    URL minted before a revoke does not carry the box's standing past it. The
    id is the server's own (it rides a signed claim), so it is not re-scoped
    to an org here: a pool box serves orgs other than the one it belongs to."""
    try:
        row_id = UUID(machine_id)
    except ValueError:
        return False
    stmt = select(ComputeAllocation.id).where(
        ComputeAllocation.id == row_id,
        ComputeAllocation.lifecycle == WORKSPACE,
        ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES),
        stands_behind_its_org(),
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def verify_machine_assertion(
    db: AsyncSession,
    *,
    machine_id: str | None,
    org_id: UUID,
    operator_user_id: UUID | None,
    credential_id: str | None,
) -> bool:
    """Whether an agent assertion naming ``machine_id`` is that machine
    speaking: a LIVE workspace machine of ``org_id`` whose registered operator
    is ``operator_user_id``, whose registration was made with the very
    credential this request carries (``credential_id``, the session token's
    ``jti``), and that somebody with the authority to place the org's chats
    still stands behind (:func:`stands_behind_its_org`) — a box nobody stands
    behind speaks for no chat, on any surface, however it came to be named on
    one.

    The assertion is a header any member can put on their own session, and a
    machine's id is on every chat it serves, so the id alone proves nothing.
    Nor does the operator's identity: the box holds ONE device token, and the
    operator's browser session or a token minted on another laptop is the
    operator, not the box. What proves it is the registration — only the
    credential the box registered with, in that org, while the row is on the
    plane, is the box. A released or failed row is no machine; another org's
    machine, a colleague's session, the operator's other sessions, a name that
    is not a machine id at all, and a request with no human or no revocable
    token behind it all read as unverified, and so does a platform box whose
    machine credential was revoked or rotated away. One indexed read — the
    caller resolves it once per request (or once per socket, re-asked on the
    socket's tick) and passes the answer on as a fact.
    """
    standing = await machine_standing(
        db,
        machine_id=machine_id,
        org_id=org_id,
        operator_user_id=operator_user_id,
        credential_id=credential_id,
    )
    return standing is MachineStanding.SPEAKS


PLACE_AGAIN = "place_again"
"""What a box's ``last_reported_status`` is set to when another box was lost:
its next heartbeat then reads as the transition to ready, which is the moment
a box binds the chats nothing serves. The sweep leaves it alone on a box that
is ready, or it would announce the box first and take that moment away."""


#: The statuses of a box that was serving chats when last announced: one of
#: these turning ``unreachable`` is a heartbeat gap on a live box, not a box
#: that never came up or was put to sleep.
_SERVING: frozenset[str | None] = frozenset({READY, DRAINING, RESTARTING, PLACE_AGAIN})


async def sweep_reachability(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Announce every live workspace machine whose derived status differs from
    the one last announced — the machine that went quiet, or one whose recovery
    heartbeat landed without a frame. Returns how many frames were emitted.
    Commits."""
    moment = now or datetime.now(UTC)
    # Every live box, eligible or not: a box nobody stands behind still goes
    # quiet, and saying so is how the meter and an operator learn it is there.
    rows = (await db.execute(live_workspace_machines(eligible=False))).scalars().all()
    changed = 0
    for alloc in rows:
        status = machine_status(alloc, now=moment)
        if status == alloc.last_reported_status or (
            status == READY and alloc.last_reported_status == PLACE_AGAIN
        ):
            continue
        previous = alloc.last_reported_status
        await announce_machine(db, alloc, status=status, reason=None)
        changed += 1
        log.info(
            "compute.machine.reachability",
            allocation_id=str(alloc.id),
            org_id=str(alloc.org_team_id),
            status=status,
        )
        if status == UNREACHABLE and previous in _SERVING:
            # A box that was serving chats went quiet past the ready window:
            # the figure the heartbeat-gap alarm counts.
            last_heard = silent_since(alloc, now=moment)
            log.warning(
                "compute.machine.heartbeat_gap",
                allocation_id=str(alloc.id),
                org_id=str(alloc.org_team_id),
                previous=previous,
                silent_seconds=(
                    None if last_heard is None else round((moment - last_heard).total_seconds())
                ),
            )
    await db.commit()
    return changed


def has_run_org_workers(alloc: ComputeAllocation) -> bool:
    """Whether the box runs, or has ever run, a worker per org: its last beat
    said ``org_workers``, or an earlier one did (``ran_org_workers_at`` is
    never cleared). A box rolled back to a build that serves every org from
    one process is still held to what the newer build was."""
    return alloc.ran_org_workers_at is not None or BoxCapability.ORG_WORKERS in (
        alloc.capabilities_json or ()
    )


__all__ = [
    "ASLEEP",
    "DRAINING",
    "FAILING_ORGS_KEY",
    "NONE",
    "PLACE_AGAIN",
    "READY",
    "RESTARTING",
    "STARTING",
    "UNREACHABLE",
    "WORKSPACE",
    "MachineStanding",
    "MachineState",
    "MachineStatus",
    "current_machine",
    "failing_orgs",
    "has_run_org_workers",
    "holds_a_live_credential",
    "live_workspace_machines",
    "machine_name_for_reader",
    "machine_standing",
    "machine_state",
    "machine_status",
    "machine_still_stands",
    "may_serve",
    "owner_stands_in_its_org",
    "personal_machines",
    "pool_machines",
    "ready_window",
    "registrant_is_org_admin",
    "silent_since",
    "stands_behind_its_org",
    "sweep_reachability",
    "verify_machine_assertion",
]
