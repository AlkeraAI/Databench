"""Who may see which event, decided per frame without a database hit.

An event names its org, its audience (``visibility``) and, when it concerns
one team, that team in ``payload["team_id"]``. A subscriber is admitted to a
frame when all three agree with a snapshot of what the user is entitled to —
their memberships, whether they administer the org, whether they are platform
staff. The snapshot is loaded once when the connection opens and refreshed on
every keepalive tick, so a membership change takes effect within one tick and
no frame ever costs a query.

The rule is the same one the REST surface applies. Team-scoped rows follow the
membership chain (a member of a sub-team holds materialized rows for every
ancestor, so the snapshot's ``team_ids`` already contains them), an org admin
sees every team, and anything the snapshot cannot vouch for is refused.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.events import (
    ACCESS_CHANGED_KEY,
    BOUND_MACHINE_KEY,
    VISIBILITY_ORG,
    VISIBILITY_PLATFORM,
    EventType,
    HubEvent,
    is_realtime_event_type,
    user_id_of_visibility,
)
from alkera_core.models import TeamMembership, TeamRole, User, WorkspaceObject
from alkera_core.schemas.realtime import machine_channel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    """What one user was entitled to in one org at one instant."""

    #: The org the snapshot was taken in: the org of the credential the
    #: connection was opened on. Every org-scoped decision the connection makes
    #: (which document rows it may find, which org its grants are addressed to)
    #: reads it here, so no decision can fall back to an org of the person's
    #: choosing. ``None`` only for a box, which is a member of no org and is
    #: judged by its machine scope instead; a decision that needs an org refuses
    #: it.
    org_id: UUID | None
    #: Every team the user holds a membership row in, in that org. Chain rows
    #: are materialized, so a sub-team member also holds the ancestors.
    team_ids: frozenset[UUID]
    #: ADMIN of the org root, which by descent is admin everywhere in the org.
    org_admin: bool
    #: Holds any platform role.
    platform: bool


async def load_entitlements(db: AsyncSession, user: User, *, org_id: UUID) -> EntitlementSnapshot:
    """One membership read answers every field. ``org_id`` is the connection's
    org, from its credential: a team the person holds in any other org is not
    in the snapshot, so a frame addressed to that team is refused."""
    rows = await db.execute(
        select(TeamMembership.team_id, TeamMembership.role).where(
            TeamMembership.user_id == user.id, TeamMembership.org_team_id == org_id
        )
    )
    memberships = rows.all()
    return EntitlementSnapshot(
        org_id=org_id,
        team_ids=frozenset(team_id for team_id, _role in memberships),
        org_admin=any(
            team_id == org_id and role == TeamRole.ADMIN for team_id, role in memberships
        ),
        platform=user.platform_role is not None,
    )


class EntitlementRef:
    """A mutable holder the keepalive tick refreshes, so a predicate captured
    when the connection opened reads the latest snapshot."""

    __slots__ = ("value",)

    def __init__(self, value: EntitlementSnapshot) -> None:
        self.value = value


def visible_to(event: HubEvent, *, user_id: UUID, org_id: UUID, ent: EntitlementSnapshot) -> bool:
    """Whether ``event`` may reach this user. Anything the rule does not
    positively admit — a foreign org, an unknown visibility grammar, a team the
    user is not in — is refused. The snapshot must have been taken in the same
    org: team ids from one org never vouch for a frame of another."""
    if event.org_id != org_id or ent.org_id != org_id:
        return False
    visibility = event.visibility
    if visibility == VISIBILITY_ORG:
        pass
    elif visibility == VISIBILITY_PLATFORM:
        if not ent.platform:
            return False
    else:
        addressed = user_id_of_visibility(visibility)
        if addressed is None or addressed != user_id:
            return False
    team_id = event.team_id
    if team_id is not None and not (ent.org_admin or team_id in ent.team_ids):
        return False
    return True


#: The event types whose arrival always means "decide this socket's channels
#: again". A share made or revoked is the standing case; the others carry the
#: marker above, or name the socket's own user.
_ALWAYS_REAUTHORIZES: frozenset[str] = frozenset({EventType.FILE_NODE_CHANGED.value})


def reauthorizes(event: HubEvent, *, user_id: UUID) -> bool:
    """Whether ``event`` can have changed what this user may read.

    Three ways it can, and they are different questions, which is why this is
    one predicate rather than a membership test on a set of types:

    * a node's access changed — a share made, revoked or moved;
    * the thing itself went away or came back (a chat deleted, trashed or
      restored), which the producer marks with :data:`ACCESS_CHANGED_KEY`
      because the same event type also carries ordinary edits;
    * this user's own standing moved — a role downgrade, a removal from a team,
      a deactivation — which is announced as a membership change naming them.

    Everything else is content, and content never re-opens a decision.
    """
    if event.type in _ALWAYS_REAUTHORIZES:
        return True
    if event.payload.get(ACCESS_CHANGED_KEY) is True:
        return True
    if event.type != EventType.MEMBERSHIP_CHANGED.value:
        return False
    named = event.payload.get("user_id")
    return isinstance(named, str) and named == str(user_id)


def stream_predicate(
    *, user_id: UUID, org_id: UUID, ref: EntitlementRef
) -> Callable[[HubEvent], bool]:
    """The hub predicate for an event-stream subscriber: durable rows only, of
    a type a client may receive, visible to this user under the snapshot held
    in ``ref`` at the moment the event arrives."""

    def _accept(event: HubEvent) -> bool:
        return (
            event.lane == "durable"
            and is_realtime_event_type(event.type)
            and visible_to(event, user_id=user_id, org_id=org_id, ent=ref.value)
        )

    return _accept


# ---------------------------------------------------------------------------
# A box on its own machine credential
# ---------------------------------------------------------------------------

CHAT_CHANNEL_PREFIX = "doc:chat:"


@dataclass(frozen=True, slots=True)
class MachineScope:
    """What a box's connection may hear at one instant: the credential's
    context (the orgs it serves) and the chats bound to its machine — their
    ids, the orgs they live in, and the Files nodes of their folders.

    A box is a member of no org, holds no rung and has no platform role, so
    nothing an entitlement snapshot says applies to it. What admits a frame is
    that the frame concerns one of ITS chats; everything else — another box's
    chat, an org's own traffic, a platform stream, a frame addressed to a
    person — is refused, whatever org it is in. Loaded when the connection
    opens and refreshed on every keepalive tick, so a chat rebound elsewhere
    stops reaching the box within one tick and a chat newly bound to it is
    heard from the announcement that bound it.
    """

    ctx: ActingContext
    chat_ids: frozenset[str]
    org_ids: frozenset[UUID]
    node_ids: frozenset[str]
    #: The ``doc:notebook:<item_id>`` channels the box's socket was granted as
    #: the machine holding each notebook's folder, re-decided on every tick.
    notebook_channels: frozenset[str] = frozenset()

    @property
    def machine_id(self) -> str:
        return self.ctx.acting_principal.id

    @property
    def machine_channel(self) -> str:
        return machine_channel(self.machine_id)

    def holds_channel(self, channel: str | None) -> bool:
        """Whether ``channel`` is one of the box's own: its machine channel,
        the document of a chat bound to it, or a notebook it was granted."""
        if channel is None:
            return False
        if channel == self.machine_channel or channel in self.notebook_channels:
            return True
        return channel.startswith(CHAT_CHANNEL_PREFIX) and (
            channel.removeprefix(CHAT_CHANNEL_PREFIX) in self.chat_ids
        )


class MachineScopeRef:
    """A mutable holder the keepalive tick refreshes, so a predicate captured
    when the connection opened reads the latest scope."""

    __slots__ = ("value",)

    def __init__(self, value: MachineScope) -> None:
        self.value = value


#: The events that concern a box only when they name its own machine's row.
_OWN_ROW_EVENT_TYPES: frozenset[str] = frozenset({EventType.COMPUTE_MACHINE_CHANGED.value})
#: The events that concern a chat's folder: they name the node — the folder
#: itself, or a node under it whose lease root is the folder.
_FOLDER_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.FILE_LEASE_CHANGED.value,
        EventType.FILE_NODE_CHANGED.value,
        EventType.FILE_OPERATION_CHANGED.value,
    }
)
#: The events a box hears for the whole org of a chat it holds: a connection
#: or a membership moving changes which connections the chat's members may
#: use, and the box's schema cards follow. Ids only, and the routes that
#: answer the refetch decide what the box may read of them.
_ORG_WIDE_EVENT_TYPES: frozenset[str] = frozenset(
    {EventType.TEAM_CONNECTION_UPDATED.value, EventType.MEMBERSHIP_CHANGED.value}
)


async def load_machine_scope(db: AsyncSession, ctx: ActingContext) -> MachineScope:
    """The box's scope now: one read of the chats bound to it and one of their
    folders' nodes."""
    from alkera_core.files.authz.decider import CHAT_SUBTYPE, WORKSPACE_SUBTYPE
    from alkera_core.models.files.tree import FileNode

    from backend.services.objects.object_service import chats_bound_to

    # Only the chats in an org this context serves: an org-bound worker's
    # connection holds that org's chats and is never told the others exist.
    bound = [
        (chat_id, org_id)
        for chat_id, org_id in await chats_bound_to(db, machine_id=ctx.acting_principal.id)
        if ctx.serves(org_id)
    ]
    chat_ids = [chat_id for chat_id, _org in bound]
    node_ids: frozenset[str] = frozenset()
    if chat_ids:
        rows = await db.execute(
            select(FileNode.id).where(
                FileNode.target_object_id.in_(chat_ids),
                FileNode.subtype == CHAT_SUBTYPE,
                FileNode.trashed_at.is_(None),
            )
        )
        node_ids = frozenset(str(node_id) for node_id in rows.scalars().all())
        # A chat in a workspace that owns a folder is served under the box's
        # lease on that folder: a drop into its shared tree names the
        # workspace's folder as its lease root, and the box must hear it.
        workspace_ids = (
            await db.execute(
                select(WorkspaceObject.spec["workspace_id"].astext).where(
                    WorkspaceObject.id.in_(chat_ids),
                    WorkspaceObject.spec["workspace_id"].astext.is_not(None),
                )
            )
        ).scalars()
        wanted = [UUID(raw) for raw in workspace_ids if _is_uuid(raw)]
        if wanted:
            rows = await db.execute(
                select(FileNode.id).where(
                    FileNode.target_object_id.in_(wanted),
                    FileNode.subtype == WORKSPACE_SUBTYPE,
                    FileNode.trashed_at.is_(None),
                )
            )
            node_ids |= frozenset(str(node_id) for node_id in rows.scalars().all())
    return MachineScope(
        ctx=ctx,
        chat_ids=frozenset(str(chat_id) for chat_id in chat_ids),
        org_ids=frozenset(org_id for _chat, org_id in bound),
        node_ids=node_ids,
    )


def _is_uuid(raw: object) -> bool:
    try:
        UUID(str(raw))
    except ValueError:
        return False
    return True


def _names_a_folder(event: HubEvent, scope: MachineScope) -> bool:
    if event.entity_id in scope.node_ids:
        return True
    lease_root = event.payload.get("lease_node_id")
    return isinstance(lease_root, str) and lease_root in scope.node_ids


def machine_visible_to(event: HubEvent, *, scope: MachineScope) -> bool:
    """Whether ``event`` may reach a box holding ``scope``.

    The one predicate for both of a box's connections — its socket and its
    event stream — so the two cannot drift. Positively admitted, and nothing
    else: the frame's org is one the credential serves, the frame is addressed
    to the org (never to a person, never to platform staff), and it concerns
    one of the box's own chats — a document or machine channel it holds, a
    chat bound (or being bound) to it, a folder of one of those chats, the
    box's own machine row, or the org-wide connection and membership traffic
    of an org it holds a chat in.
    """
    if not scope.ctx.serves(event.org_id):
        return False
    if event.visibility != VISIBILITY_ORG:
        return False
    if event.channel is not None:
        return scope.holds_channel(event.channel)
    if event.type == EventType.CHAT_UPDATED.value:
        return (
            event.entity_id in scope.chat_ids
            or event.payload.get(BOUND_MACHINE_KEY) == scope.machine_id
        )
    if event.type in _OWN_ROW_EVENT_TYPES:
        return event.entity_id == scope.machine_id
    if event.type in _FOLDER_EVENT_TYPES:
        return _names_a_folder(event, scope)
    if event.type in _ORG_WIDE_EVENT_TYPES:
        return event.org_id in scope.org_ids
    return False


def machine_reauthorizes(event: HubEvent, *, scope: MachineScope) -> bool:
    """Whether ``event`` can have changed which chats the box holds: a chat's
    own doorbell (a rebinding, a deletion, a claim) is the only thing that
    can. A share moves nothing for a box — it is the publisher whatever the
    node's grants say — and no membership names it."""
    return event.type == EventType.CHAT_UPDATED.value and (
        event.entity_id in scope.chat_ids
        or event.payload.get(BOUND_MACHINE_KEY) == scope.machine_id
    )


def machine_routing_visible_to(event: HubEvent, *, scope: MachineScope) -> bool:
    """Whether ``event`` may reach a box's ROOT process, which routes chats to
    each org's process and holds nothing of any tenant's: the doorbell of a
    chat bound (or being bound) to its machine, and its machine's own row.
    No document channel, no folder traffic, no org-wide connection or
    membership traffic: those belong to the org's process, on its own
    credential. A subset of :func:`machine_visible_to` by construction."""
    if not machine_visible_to(event, scope=scope):
        return False
    if event.channel is not None:
        return False
    return event.type == EventType.CHAT_UPDATED.value or event.type in _OWN_ROW_EVENT_TYPES


def machine_routing_predicate(*, ref: MachineScopeRef) -> Callable[[HubEvent], bool]:
    """The hub predicate for a box's routing stream (see
    :func:`machine_routing_visible_to`)."""

    def _accept(event: HubEvent) -> bool:
        return (
            event.lane == "durable"
            and is_realtime_event_type(event.type)
            and machine_routing_visible_to(event, scope=ref.value)
        )

    return _accept


def machine_stream_predicate(*, ref: MachineScopeRef) -> Callable[[HubEvent], bool]:
    """The hub predicate for a box's event stream: durable rows only, of a
    type a client may receive, concerning the box's chats under the scope held
    in ``ref`` at the moment the event arrives."""

    def _accept(event: HubEvent) -> bool:
        return (
            event.lane == "durable"
            and is_realtime_event_type(event.type)
            and machine_visible_to(event, scope=ref.value)
        )

    return _accept


__all__ = [
    "ACCESS_CHANGED_KEY",
    "BOUND_MACHINE_KEY",
    "CHAT_CHANNEL_PREFIX",
    "EntitlementRef",
    "EntitlementSnapshot",
    "MachineScope",
    "MachineScopeRef",
    "load_entitlements",
    "load_machine_scope",
    "machine_reauthorizes",
    "machine_routing_predicate",
    "machine_routing_visible_to",
    "machine_stream_predicate",
    "machine_visible_to",
    "reauthorizes",
    "stream_predicate",
    "visible_to",
]
