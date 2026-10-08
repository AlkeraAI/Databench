"""The facts an object or chat route resolves before the policy decides.

A policy is pure: the route resolves every fact first and the engine only
decides. This module is where those facts come from, spelled once for the two
surfaces — chats and objects — so "is this person on the team" cannot be
resolved two different ways, and the fact set the policies require is met by
construction: roles at the ORG ROOT (membership of the org is what the policies
ask about; the object's scope narrows the audience afterwards), the reader's
materialized memberships, the row's owner and scope, and whether the reader's
email is verified.

The tenancy floor is deliberately the ENGINE's, not a WHERE clause. An object
is loaded by its id alone and the :class:`~alkera_core.authz.Resource` names
the org the row actually belongs to; ``authorize()`` then refuses a row from
another org as an opaque not-found **before any policy runs**, and leaves the
denial on record. Filtering by org in the query instead would give the same
answer to the caller and no answer at all to an auditor. The request's session
is held to its orgs by row-level security, so a by-id load that misses there
reads the row once more across orgs (:func:`load_object`) to put that refusal
on record.

A listing is the one place the policy's READ answer is computed without a
decision row: a page of fifty objects must not put fifty rows on the audit
lane. :func:`readable_objects` is that answer, and it routes each row to the
predicate its TYPE is decided by — a scope string for an object with an
audience, the chat's own read predicate for a chat, which has none. Every
by-id route goes through ``enforce()``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from alkera_core.authz import (
    ActingContext,
    Action,
    Resource,
    ResourceType,
    Role,
    RoleResolver,
    authorize,
    expand_roles,
    scope_for_team,
    scope_readable,
)
from alkera_core.authz.chat_scope import SCOPE_PRIVATE, chat_visible
from alkera_core.compute.machines import verify_machine_assertion
from alkera_core.compute.workspace_lease import holds_workspace
from alkera_core.config import settings
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.tenant_session import bound_org_ids
from alkera_core.files.authz.decider import CHAT_SUBTYPE as CHAT_TYPE
from alkera_core.files.authz.ladder import ROLE_OWNER
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import MembershipStatus, OrgMembership, User, WorkspaceObject
from alkera_core.objects.workspaces import workspace_spec_of
from alkera_core.schemas.objects import ChatSpec, ResultSpec
from alkera_core.verification import is_verified
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.membership_tokens import billed_user_of
from backend.services.org import ancestor_chain
from backend.services.realtime.filters import load_entitlements
from backend.services.sharing import node_role

#: The object kind a promotion mints: the one kind a box ever fills.
RESULT_TYPE = "result"


@dataclass(frozen=True, slots=True)
class Reader:
    """Who is asking, resolved once per request: the acting context, their
    roles at the org root (closed under the ladder), their memberships, and
    whether their email is verified."""

    ctx: ActingContext
    in_org: bool
    roles: frozenset[Role]
    is_org_admin: bool
    team_ids: frozenset[UUID]
    email_verified: bool
    #: Whether the agent assertion on this request was proven to be the
    #: machine it names — a live workspace machine of the org registered by
    #: the delegating user with the very credential this request carries.
    #: ``False`` for a person, and fail-closed by default:
    #: an agent id is only ever compared with a chat's binding through
    #: :meth:`is_machine`, which needs this to be ``True``.
    asserted_machine_verified: bool = False

    @property
    def is_member(self) -> bool:
        return Role.MEMBER in self.roles

    def owns(self, owner_user_id: UUID | None) -> bool:
        """Whether the user whose permissions apply — the delegating user
        behind an agent or a personal access token — is ``owner_user_id``."""
        effective = self.ctx.effective_user_id
        return owner_user_id is not None and effective is not None and effective == owner_user_id

    def is_machine(self, machine_id: str | None) -> bool:
        """Whether this request is ``machine_id`` speaking: the asserted agent
        IS that id and the assertion verified. A machine's id is on every chat
        it serves, so the id alone — a header any member can send on their own
        session — never makes a caller the machine."""
        if not machine_id:
            return False
        if self.ctx.is_machine:
            # The credential proved the machine; nothing was asserted.
            return self.ctx.acting_principal.id == machine_id
        return (
            self.asserted_machine_verified
            and self.ctx.is_agent
            and self.ctx.acting_principal.id == machine_id
        )


def machine_reader(ctx: ActingContext) -> Reader:
    """The facts for a box on its own machine credential: a member of no org,
    an admin of nothing, on no team, with no email to verify, and no assertion
    to have verified — its standing is the credential, and the one thing a
    policy admits it for is holding the resource."""
    if not ctx.is_machine:
        raise ValueError("machine_reader is for a machine principal")
    return Reader(
        ctx=ctx,
        in_org=False,
        roles=frozenset(),
        is_org_admin=False,
        team_ids=frozenset(),
        email_verified=False,
        asserted_machine_verified=False,
    )


async def resolve_reader(
    db: AsyncSession, *, ctx: ActingContext, roles: RoleResolver, user: User
) -> Reader:
    """Resolve the caller's facts once. Roles come from the request org's ROOT team, the
    memberships from the same snapshot the socket's channel rules use, so the
    two surfaces read one set of facts — and so is the machine assertion,
    verified here once per request through the same read the socket makes at
    admission (only an agent pays for it)."""
    # The org is the request's (its credential's), never the person's own: a
    # member of two orgs reads each one's objects from that org's session.
    team = await roles.for_team(ctx.org_id)
    ent = await load_entitlements(db, user, org_id=ctx.org_id)
    machine_verified = ctx.is_agent and await verify_machine_assertion(
        db,
        machine_id=ctx.acting_principal.id,
        org_id=ctx.org_id,
        operator_user_id=ctx.effective_user_id,
        credential_id=ctx.credential_id,
    )
    return Reader(
        ctx=ctx,
        in_org=team.in_org,
        roles=expand_roles(team.roles),
        is_org_admin=await roles.is_org_admin(),
        team_ids=ent.team_ids,
        email_verified=is_verified(user),
        asserted_machine_verified=machine_verified,
    )


async def load_object(
    db: AsyncSession, object_id: UUID, *, type: str | None = None
) -> WorkspaceObject | None:
    """The live object with this id, from any org (see the module docstring).

    ``type`` narrows to one discriminator, so ``/chats/{id}`` cannot be handed
    the id of a saved query and answer about it.
    """
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.id == object_id, WorkspaceObject.deleted_at == 0
    )
    if type is not None:
        stmt = stmt.where(WorkspaceObject.type == type)
    found = (await db.execute(stmt)).scalar_one_or_none()
    if found is not None or bound_org_ids(db) is None:
        return found
    # The request's session sees only its own orgs' rows. A miss there may be
    # another org's id, and that probe is the engine's to refuse and record, so
    # the row is read once more across orgs, by id alone, for the decision.
    async with cross_tenant_write(db, reason="objects.load_by_id.probe"):
        return (await db.execute(stmt)).scalar_one_or_none()


def _facts(
    reader: Reader,
    *,
    owner_user_id: UUID | None,
    visibility_scope: str,
    held_by_machine: bool = False,
) -> dict[str, object]:
    return {
        "in_org": reader.in_org,
        "roles": reader.roles,
        "is_org_admin": reader.is_org_admin,
        "owner_user_id": str(owner_user_id) if owner_user_id else "",
        "visibility_scope": visibility_scope,
        "team_ids": reader.team_ids,
        "email_verified": reader.email_verified,
        "held_by_machine": held_by_machine,
    }


def object_attrs(
    obj: WorkspaceObject, reader: Reader, *, held_by_machine: bool = False
) -> dict[str, object]:
    """Every fact ``objects.access`` reads, for ``obj``. ``held_by_machine`` is
    the one fact about the caller rather than the row — whether the box asking
    runs the chat the object came out of (:func:`machine_holds_object`) — and
    ``False`` for everyone who is not a box, which no read is paid for."""
    return _facts(
        reader,
        owner_user_id=obj.owner_user_id,
        visibility_scope=obj.visibility_scope,
        held_by_machine=held_by_machine,
    )


async def machine_holds_object(db: AsyncSession, obj: WorkspaceObject, reader: Reader) -> bool:
    """Whether the box asking runs the chat ``obj`` came out of.

    A result names its source chat in its spec; the chat's binding says which
    machine runs it. The chat is looked up by id across orgs and must be in the
    object's own org — a promotion never crosses one — and bound to the acting
    machine through the same comparison every chat door makes. ``False``
    without a read for anyone who is not a box, for an object that is not a
    result, and for a result that names no chat (one a person saved by hand).
    """
    if not reader.ctx.is_machine or obj.type != RESULT_TYPE:
        return False
    try:
        source = ResultSpec.model_validate(obj.spec or {}).source_chat_id
        chat_id = UUID(source) if source else None
    except (ValidationError, ValueError):
        return False
    if chat_id is None:
        return False
    chat = await load_object(db, chat_id, type=CHAT_TYPE)
    return (
        chat is not None
        and chat.org_team_id == obj.org_team_id
        and reader.is_machine(bound_machine(chat, reader))
    )


async def chat_attrs(db: AsyncSession, chat: WorkspaceObject, reader: Reader) -> dict[str, object]:
    """Every fact ``chat.access`` reads: the object facts, the team column —
    which the policy checks agrees with the scope, two spellings of one row —
    and the rung the chat's node in the file tree gives this caller.

    The rung is resolved HERE rather than at each door because both doors that
    carry a message (``POST /chats/{id}/messages`` and the socket's
    ``user_message``) build their facts through this one function. Narrowing
    ``SEND`` in the policy and resolving its fact in one place is what keeps
    the two doors from drifting apart: a door that forgot the rung would be
    denied by the engine rather than admitted by a stale branch.

    The chat's own author and the machine it is bound to are the two callers
    whose rung cannot change an answer: every branch that reads it admits them
    first, so resolving it for them would be a ``file_acls`` read that no
    decision can turn on — paid on every message the product sends and every
    turn the box publishes. Anybody else's request still resolves the rung,
    because for them it is the whole gate: a chat is private until shared.
    """
    caller = reader.ctx.effective_user_id
    bound = bound_machine(chat, reader)
    role = (
        None
        if _rung_cannot_decide(chat, reader, bound=bound)
        else await node_role.shared_object_role(
            db,
            org_id=chat.org_team_id,
            object_id=chat.id,
            user_id=caller,
            team_ids=reader.team_ids,
        )
    )
    standings = await workspace_standings(db, [chat], reader)
    departed = await departed_owners(db, [chat], reader)
    payers = await payer_standings(db, [chat], reader, standings)
    return chat_facts(
        chat,
        reader,
        shared_role=role,
        standing=standings[chat.id],
        owner_departed=chat.id in departed,
        payer_stands=payers.get(chat.id),
    )


async def departed_owners(
    db: AsyncSession, objects: Sequence[WorkspaceObject], reader: Reader
) -> frozenset[UUID]:
    """The ids of the objects among ``objects`` whose owner has left: their
    user is deactivated, gone, or no longer in the object's org. That is what
    offboarding is, and what lets an org admin delete a private chat or
    workspace they cannot read.

    Read only for an org admin, the one caller a policy branch consults it
    for: everyone else is answered ``False`` without a ``users`` read, which
    can only withhold, never grant."""
    if not reader.is_org_admin or not objects:
        return frozenset()
    owner_ids = {obj.owner_user_id for obj in objects}
    rows = await db.execute(select(User.id, User.is_active).where(User.id.in_(owner_ids)))
    active = {row.id for row in rows if row.is_active}
    # In the object's org means an active membership there: a person who
    # belongs to several orgs has left only the ones they were removed from.
    standing = await db.execute(
        select(OrgMembership.user_id, OrgMembership.org_team_id).where(
            OrgMembership.user_id.in_(owner_ids),
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
    )
    members = {(row.user_id, row.org_team_id) for row in standing}
    departed: set[UUID] = set()
    for obj in objects:
        owner = obj.owner_user_id
        if owner not in active or (owner, obj.org_team_id) not in members:
            departed.add(obj.id)
    return frozenset(departed)


async def payer_standings(
    db: AsyncSession,
    chats: Sequence[WorkspaceObject],
    reader: Reader,
    standings: Mapping[UUID, WorkspaceStanding],
) -> dict[UUID, bool]:
    """Whether each chat's payer (:func:`billed_user_of`) may still read it:
    an active user with an active membership in the chat's org whom the chat's
    read predicate admits, on the workspace for a chat in a workspace of
    several (so a starter removed from the workspace stops standing), on the
    chat itself otherwise (which its owner always reads).

    Empty for a box on its own credential: it never sends, and the policy
    reads this fact on ``SEND`` alone. The caller's own chats are answered
    from the reader and ``standings`` already resolved, with no read; anybody
    else's payer costs one read for the page, plus their roles and rungs for
    the chats of theirs that sit in a workspace."""
    if reader.ctx.is_machine or not chats:
        return {}
    out: dict[UUID, bool] = {}
    others: list[WorkspaceObject] = []
    for chat in chats:
        if reader.owns(billed_user_of(chat)):
            out[chat.id] = (
                reader.in_org
                and reader.is_member
                and _payer_reads(
                    standings.get(chat.id, OWN_FOLDER), is_org_admin=reader.is_org_admin
                )
            )
        else:
            others.append(chat)
    return {**out, **await _payers_stand(db, others)}


async def _payers_stand(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> dict[UUID, bool]:
    """:func:`payer_standings` for chats whose payer is not the caller: one
    read of the payers' accounts and memberships, then each payer's own roles
    and rungs for the chats of theirs that sit in a workspace."""
    if not chats:
        return {}
    payer_ids = {billed_user_of(chat) for chat in chats}
    rows = await db.execute(
        select(User, OrgMembership.org_team_id)
        .join(OrgMembership, OrgMembership.user_id == User.id)
        .where(
            User.id.in_(payer_ids),
            User.is_active.is_(True),
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
    )
    standing_in: dict[tuple[UUID, UUID], User] = {
        (user.id, org_id): user for user, org_id in rows.tuples()
    }
    out: dict[UUID, bool] = {}
    standing = [chat for chat in chats if (billed_user_of(chat), chat.org_team_id) in standing_in]
    kept = {chat.id for chat in standing}
    out.update({chat.id: False for chat in chats if chat.id not in kept})
    shared = await _in_shared_workspace(db, standing)
    in_workspace: dict[tuple[UUID, UUID], list[WorkspaceObject]] = {}
    for chat in standing:
        if chat.id in shared:
            in_workspace.setdefault((billed_user_of(chat), chat.org_team_id), []).append(chat)
        else:
            out[chat.id] = True  # a workspace of one: its owner reads the chat
    for (payer_id, org_id), group in in_workspace.items():
        payer = standing_in[(payer_id, org_id)]
        ctx = ActingContext.for_user(user_id=payer.id, org_id=org_id, email=payer.email)
        roles = RoleResolver(db, ctx, ancestor_chain=ancestor_chain)
        payer_reader = await resolve_reader(db, ctx=ctx, roles=roles, user=payer)
        theirs = await workspace_standings(db, group, payer_reader)
        for chat in group:
            out[chat.id] = payer_reader.is_member and _payer_reads(
                theirs[chat.id], is_org_admin=payer_reader.is_org_admin
            )
    return out


async def _in_shared_workspace(
    db: AsyncSession, chats: Sequence[WorkspaceObject]
) -> frozenset[UUID]:
    """The ids of the chats among ``chats`` that sit in a workspace of several,
    in one read of the workspaces they name."""
    named: dict[UUID, UUID] = {}
    for chat in chats:
        raw = ChatSpec.model_validate(chat.spec or {}).workspace_id
        try:
            if raw:
                named[chat.id] = UUID(raw)
        except ValueError:
            continue
    if not named:
        return frozenset()
    rows = await db.execute(
        select(WorkspaceObject).where(
            WorkspaceObject.id.in_(set(named.values())), WorkspaceObject.type == WORKSPACE_TYPE
        )
    )
    native = {ws.id for ws in rows.scalars() if workspace_spec_of(ws.spec).layout == "native"}
    return frozenset(chat_id for chat_id, ws_id in named.items() if ws_id in native)


def _payer_reads(standing: WorkspaceStanding, *, is_org_admin: bool) -> bool:
    """The chat's read predicate for its own payer: its owner, unless the chat
    sits in a workspace of several, where the rung on the workspace decides."""
    if not standing.multi_chat:
        return True
    return chat_visible(
        is_owner=standing.role == ROLE_OWNER,
        is_bound_machine=False,
        shared_role=standing.role or None,
        is_org_admin=is_org_admin,
        admin_reads_private=admin_reads_private(),
    )


async def payer_may_read(db: AsyncSession, chat: WorkspaceObject) -> bool:
    """:func:`payer_standings` for one chat, asked with no person calling: by
    a box on its machine credential before it mints a token that bills the
    payer."""
    return (await _payers_stand(db, [chat])).get(chat.id, False)


@dataclass(frozen=True, slots=True)
class WorkspaceStanding:
    """Where a chat's workspace leaves the caller: whether the workspace holds
    several chats (so its agent works in a tree shared beyond the chat), and
    the caller's rung on that workspace's folder."""

    multi_chat: bool
    role: str


#: A chat in a workspace of one, or one whose workspace is not on record: the
#: chat's own rung decides, as it always has.
OWN_FOLDER = WorkspaceStanding(multi_chat=False, role="")


async def workspace_standings(
    db: AsyncSession, chats: Sequence[WorkspaceObject], reader: Reader
) -> dict[UUID, WorkspaceStanding]:
    """Each chat's :class:`WorkspaceStanding` for this caller, for a page in at
    most two reads: the workspaces the page names, then the caller's rungs on
    the multi-chat ones they do not own. A machine on its own credential gets
    :data:`OWN_FOLDER` for every chat: it drives nothing, and the policy's
    machine branch never reads the standing."""
    named: dict[UUID, UUID] = {}
    for chat in chats:
        workspace_id = ChatSpec.model_validate(chat.spec or {}).workspace_id
        if workspace_id and not reader.ctx.is_machine:
            try:
                named[chat.id] = UUID(workspace_id)
            except ValueError:
                continue
    multi: dict[UUID, WorkspaceObject] = {}
    if named:
        rows = await db.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.id.in_(set(named.values())),
                WorkspaceObject.type == WORKSPACE_TYPE,
            )
        )
        multi = {
            ws.id: ws for ws in rows.scalars() if workspace_spec_of(ws.spec).layout == "native"
        }
    unowned = [ws.id for ws in multi.values() if not reader.owns(ws.owner_user_id)]
    rungs = (
        await node_role.shared_object_roles(
            db,
            org_id=reader.ctx.org_id,
            object_ids=unowned,
            user_id=reader.ctx.effective_user_id,
            team_ids=reader.team_ids,
        )
        if unowned and reader.ctx.org_id is not None
        else {}
    )
    standings: dict[UUID, WorkspaceStanding] = {}
    for chat in chats:
        ws = multi.get(named.get(chat.id, chat.id))
        if ws is None:
            standings[chat.id] = OWN_FOLDER
        elif reader.owns(ws.owner_user_id):
            standings[chat.id] = WorkspaceStanding(multi_chat=True, role=ROLE_OWNER)
        else:
            standings[chat.id] = WorkspaceStanding(multi_chat=True, role=rungs.get(ws.id, ""))
    return standings


async def chat_standing_for(
    *,
    workspace: tuple[UUID, UUID] | None,
    user_id: UUID,
    rung: Callable[[UUID], Awaitable[str | None]],
) -> WorkspaceStanding:
    """One chat's :class:`WorkspaceStanding` for one person, for a door that
    holds no :class:`Reader` (the socket), given the chat's workspace
    (``ChatDocScope.workspace``). ``rung`` answers the person's rung on
    an object, through the connection's cache when it has one, so a share
    change the connection hears about is read afresh."""
    if workspace is None:
        return OWN_FOLDER
    workspace_id, owner = workspace
    if owner == user_id:
        return WorkspaceStanding(multi_chat=True, role=ROLE_OWNER)
    return WorkspaceStanding(multi_chat=True, role=await rung(workspace_id) or "")


def bound_machine(chat: WorkspaceObject, reader: Reader) -> str:
    """The machine the chat is bound to, as THIS reader may count it. A
    person's own box holds no chat but its owner's, whatever the binding says:
    placement never binds anyone else's chat to it, and a binding that did
    would still not hand it over."""
    owner = reader.ctx.personal_owner_id
    if owner is not None and chat.owner_user_id != owner:
        return ""
    return ChatSpec.model_validate(chat.spec or {}).machine_id or ""


def _rung_cannot_decide(chat: WorkspaceObject, reader: Reader, *, bound: str) -> bool:
    """Whether resolving the chat's rung for this caller could change nothing.

    The chat's own author and the machine it is bound to are admitted first by
    every branch that reads a rung, so the ``file_acls`` read would be paid on
    every message the product sends and every turn the box publishes without a
    decision ever turning on it.
    """
    return reader.owns(chat.owner_user_id) or reader.is_machine(bound)


def chat_facts(
    chat: WorkspaceObject,
    reader: Reader,
    *,
    shared_role: str | None,
    standing: WorkspaceStanding,
    owner_departed: bool,
    payer_stands: bool | None = None,
) -> dict[str, object]:
    """The ``chat.access`` fact set, with the rung already resolved.

    Pure, so the by-id doors (which resolve one rung) and a listing page (which
    resolves a page of them in one read) build the SAME facts. A door that
    assembled its own would be one edit away from deciding on a different
    question than the policy was written for. ``payer_stands``
    (:func:`payer_standings`) is left out when it was not resolved (a box,
    which never sends), and a ``SEND`` decided without it is refused.
    """
    payer = {} if payer_stands is None else {"payer_stands": payer_stands}
    return {
        **payer,
        **object_attrs(chat, reader),
        "team_id": str(chat.team_id) if chat.team_id else "",
        "shared_role": shared_role or "",
        "bound_machine_id": bound_machine(chat, reader),
        "asserted_machine_verified": reader.asserted_machine_verified,
        "admin_reads_private": admin_reads_private(),
        "in_multi_chat_workspace": standing.multi_chat,
        "workspace_role": standing.role,
        "owner_departed": owner_departed,
    }


def admin_reads_private() -> bool:
    """Whether this deployment lets an org admin open a chat nobody shared with
    them — read HERE and nowhere else, so every door onto a chat asks the same
    question of the same setting. A route that resolved it for itself is a
    route that can answer differently from the one next to it, which is the
    whole shape of bug this function exists to make unreachable."""
    return settings.chat_org_admin_reads_private


async def readable_chats(
    db: AsyncSession, chats: Sequence[WorkspaceObject], reader: Reader
) -> list[WorkspaceObject]:
    """The chats of a listing page this caller may read, in page order — the
    policy's READ answer without a decision row, over the same facts.

    A chat is private until shared, so the page is cut by ownership, by the
    bound machine, and by the rung the chat's node gives the caller, resolved
    for the whole page in ONE Files read (:func:`node_role.shared_object_roles`)
    rather than one per row. The owner's and the machine's chats never reach
    that read.
    """
    return [chat for chat, _facts in await readable_chat_facts(db, chats, reader)]


async def readable_chat_facts(
    db: AsyncSession, chats: Sequence[WorkspaceObject], reader: Reader
) -> list[tuple[WorkspaceObject, dict[str, object]]]:
    """The readable chats of a listing page, each with the facts a policy
    decides on — so a list can answer a SECOND question about a row (may this
    caller speak in it?) without a second Files read per row, and answer it
    through the policy rather than through a predicate copied beside it.
    """
    if reader.ctx.is_machine:
        # A box on its own credential is a member of no org, so the membership
        # predicate below would hide every row from it. What admits it is the
        # policy's own machine branch — holding the chat, inside the engine's
        # tenancy floor — asked here per row, without a decision row, exactly
        # as the chat route asks it with one. The box's chats never reach the
        # rung read: its rung cannot change the answer.
        return [
            (chat, facts)
            for chat, facts in (
                (
                    chat,
                    chat_facts(
                        chat, reader, shared_role=None, standing=OWN_FOLDER, owner_departed=False
                    ),
                )
                for chat in chats
            )
            if authorize(
                reader.ctx, Action.READ, object_resource(chat, type=ResourceType.CHAT), facts
            ).allowed
        ]

    caller = reader.ctx.effective_user_id
    org_id = reader.ctx.org_id

    standings = await workspace_standings(db, chats, reader)

    def _visible(chat: WorkspaceObject, role: str | None) -> bool:
        # A chat in a workspace of several answers to the workspace: its owner
        # is the workspace's owner and its rung the workspace's, exactly as the
        # policy substitutes them, so a person removed from the workspace stops
        # seeing the chat they started there.
        standing = standings[chat.id]
        return (
            chat.org_team_id == org_id
            and reader.in_org
            and reader.is_member
            # The listing answers the policy's READ question without a decision
            # row, so it asks the very predicate the policy asks, the admin
            # branch included. A list with its own spelling of the rule is a
            # list that hands an admin a chat the chat route refuses them, and
            # hides from a shared reader a chat the chat route opens.
            and chat_visible(
                is_owner=standing.role == ROLE_OWNER
                if standing.multi_chat
                else reader.owns(chat.owner_user_id),
                is_bound_machine=reader.is_machine(bound_machine(chat, reader)),
                shared_role=(standing.role or None) if standing.multi_chat else role,
                is_org_admin=reader.is_org_admin,
                admin_reads_private=admin_reads_private(),
            )
        )

    undecided = [
        chat
        for chat in chats
        if not _rung_cannot_decide(chat, reader, bound=bound_machine(chat, reader))
    ]
    roles = (
        await node_role.shared_object_roles(
            db,
            org_id=org_id,
            object_ids=[chat.id for chat in undecided],
            user_id=caller,
            team_ids=reader.team_ids,
        )
        if undecided and org_id is not None
        else {}
    )
    visible = [chat for chat in chats if _visible(chat, roles.get(chat.id))]
    departed = await departed_owners(db, visible, reader)
    payers = await payer_standings(db, visible, reader, standings)
    return [
        (
            chat,
            chat_facts(
                chat,
                reader,
                shared_role=roles.get(chat.id),
                standing=standings[chat.id],
                owner_departed=chat.id in departed,
                payer_stands=payers.get(chat.id),
            ),
        )
        for chat in visible
    ]


def _workspace_folder_object(workspace: WorkspaceObject) -> UUID:
    """The object whose node is the workspace's folder: its own, or for a
    workspace of one its chat's."""
    spec = workspace_spec_of(workspace.spec)
    if spec.layout == "adopted" and spec.adopted_chat_id:
        try:
            return UUID(spec.adopted_chat_id)
        except ValueError:
            return workspace.id
    return workspace.id


def workspace_facts(
    workspace: WorkspaceObject,
    reader: Reader,
    *,
    shared_role: str | None,
    owner_departed: bool,
    bound_machine_id: str,
) -> dict[str, object]:
    """The ``workspace.access`` fact set, with the rung and the holding box
    already resolved. Pure, so a by-id door and a listing page build the same
    facts."""
    return {
        **object_attrs(workspace, reader),
        "team_id": str(workspace.team_id) if workspace.team_id else "",
        "shared_role": shared_role or "",
        "admin_reads_private": admin_reads_private(),
        "is_main": workspace_spec_of(workspace.spec).kind == "main",
        "owner_departed": owner_departed,
        "bound_machine_id": bound_machine_id,
    }


async def workspace_machine_id(db: AsyncSession, workspace: WorkspaceObject) -> str:
    """The machine that holds the workspace NOW, ``""`` when none does: the
    box that reported it, while :func:`~alkera_core.compute.workspace_lease.
    workspace_held_by` says it still holds it.

    This is the one fact a box is admitted on for the workspace's
    connections and their credentials, so a pool box that once held a
    workspace of another org reaches nothing of it once it has moved on.
    """
    spec = workspace_spec_of(workspace.spec)
    if spec.binding_authority != "workspace" or not spec.machine_id:
        return ""
    reported = str(spec.machine_id)
    held = await holds_workspace(db, workspace_id=workspace.id, machine_id=reported)
    return reported if held else ""


async def workspace_attrs(
    db: AsyncSession, workspace: WorkspaceObject, reader: Reader
) -> dict[str, object]:
    """Every fact ``workspace.access`` reads: the object facts and the rung the
    workspace's folder gives this caller. The owner's rung cannot change any
    answer, so it is not read for them."""
    role = (
        None
        if reader.owns(workspace.owner_user_id)
        else await node_role.shared_object_role(
            db,
            org_id=workspace.org_team_id,
            object_id=_workspace_folder_object(workspace),
            user_id=reader.ctx.effective_user_id,
            team_ids=reader.team_ids,
        )
    )
    departed = await departed_owners(db, [workspace], reader)
    # Only a machine's decision reads the holder; a person's never does, so a
    # person's door does not pay for the read.
    holder = await workspace_machine_id(db, workspace) if reader.ctx.is_machine else ""
    return workspace_facts(
        workspace,
        reader,
        shared_role=role,
        owner_departed=workspace.id in departed,
        bound_machine_id=holder,
    )


async def readable_workspace_facts(
    db: AsyncSession, workspaces: Sequence[WorkspaceObject], reader: Reader
) -> list[tuple[WorkspaceObject, dict[str, object]]]:
    """The readable workspaces of a listing page, each with its facts: the
    policy's READ answer without a decision row, the rungs resolved for the
    whole page in one Files read."""
    if reader.ctx.is_machine:
        return []
    org_id = reader.ctx.org_id
    undecided = [ws for ws in workspaces if not reader.owns(ws.owner_user_id)]
    by_folder = {_workspace_folder_object(ws): ws.id for ws in undecided}
    rungs = (
        await node_role.shared_object_roles(
            db,
            org_id=org_id,
            object_ids=list(by_folder),
            user_id=reader.ctx.effective_user_id,
            team_ids=reader.team_ids,
        )
        if by_folder and org_id is not None
        else {}
    )
    role_of = {by_folder[folder]: rung for folder, rung in rungs.items() if folder in by_folder}
    departed = await departed_owners(db, workspaces, reader)
    kept: list[tuple[WorkspaceObject, dict[str, object]]] = []
    for ws in workspaces:
        facts = workspace_facts(
            ws,
            reader,
            shared_role=role_of.get(ws.id),
            owner_departed=ws.id in departed,
            # A listing never admits a machine (it returned above), and a
            # person's decision never reads the holder.
            bound_machine_id="",
        )
        if authorize(
            reader.ctx, Action.READ, object_resource(ws, type=ResourceType.WORKSPACE), facts
        ).allowed:
            kept.append((ws, facts))
    return kept


def new_workspace_attrs(reader: Reader) -> dict[str, object]:
    """The facts for a workspace that does not exist yet: addressed to its
    owner-to-be alone, with no folder and so no rung."""
    return {
        **_facts(
            reader, owner_user_id=reader.ctx.effective_user_id, visibility_scope=SCOPE_PRIVATE
        ),
        "team_id": "",
        "shared_role": "",
        "admin_reads_private": admin_reads_private(),
        "is_main": False,
        "owner_departed": False,
        "bound_machine_id": "",
    }


def new_object_attrs(
    reader: Reader, *, team_id: UUID | None = None, visibility_scope: str | None = None
) -> dict[str, object]:
    """The facts for an object that does not exist yet: addressed to
    ``team_id``'s audience (the whole org when ``None``), or to
    ``visibility_scope`` when the creator names the audience outright, with
    the acting user as its owner-to-be. The policies check that the creator
    is in the audience they are creating into."""
    return _facts(
        reader,
        owner_user_id=reader.ctx.effective_user_id,
        visibility_scope=visibility_scope or scope_for_team(team_id),
    )


def new_chat_attrs(reader: Reader) -> dict[str, object]:
    """The facts for a chat that does not exist yet. A chat is created PRIVATE
    — addressed to its owner alone, shared afterwards through its node — so
    the audience the creator must be in is themself. It has no node yet and
    no machine, so the rung and the binding are empty; ``CREATE`` consults
    neither."""
    return {
        **_facts(
            reader, owner_user_id=reader.ctx.effective_user_id, visibility_scope=SCOPE_PRIVATE
        ),
        "team_id": "",
        "shared_role": "",
        "bound_machine_id": "",
        "asserted_machine_verified": reader.asserted_machine_verified,
        "admin_reads_private": admin_reads_private(),
        "in_multi_chat_workspace": False,
        "workspace_role": "",
        "owner_departed": False,
    }


def may_read(obj: WorkspaceObject, reader: Reader) -> bool:
    """The ``objects.access`` READ answer for a listing, without a decision row:
    in the org, a member of it, and in the object's audience — the same
    predicate that policy decides through.

    **Not for chats.** A chat is not read by its audience; it is read by its
    owner, its machine, a rung on its node and — where the deployment says so —
    an org admin, none of which a scope string can answer. Asking this about a
    chat is the bug that handed an unshared org admin a member's private title,
    owner and spec through the objects list while it hid the same chat from the
    colleague holding "Can view", so it refuses outright rather than answering
    from the wrong question. :func:`readable_objects` is the caller that routes
    each row to the predicate its type is decided by.
    """
    if obj.type in (CHAT_TYPE, WORKSPACE_TYPE):
        return False
    return (
        obj.org_team_id == reader.ctx.org_id
        and reader.in_org
        and reader.is_member
        and scope_readable(
            is_org_admin=reader.is_org_admin,
            team_ids=reader.team_ids,
            visibility_scope=obj.visibility_scope,
            is_owner=reader.owns(obj.owner_user_id),
        )
    )


async def readable_objects(
    db: AsyncSession, objects: Sequence[WorkspaceObject], reader: Reader
) -> list[WorkspaceObject]:
    """One listing page cut to what this caller may read, in page order, with
    each row decided by the policy its TYPE is decided by.

    A page of workspace objects can hold both kinds: a promoted result, whose
    audience is a scope string, and a chat, which has no audience and is read
    through its node. The chats are cut in ONE batched Files read
    (:func:`readable_chats`) and everything else by :func:`may_read`, and the
    page keeps its order so a cursor still means what it meant.
    """
    chats = [obj for obj in objects if obj.type == CHAT_TYPE]
    workspaces = [obj for obj in objects if obj.type == WORKSPACE_TYPE]
    visible: set[UUID] = (
        {obj.id for obj in await readable_chats(db, chats, reader)} if chats else set()
    )
    if workspaces:
        visible |= {ws.id for ws, _ in await readable_workspace_facts(db, workspaces, reader)}

    def _readable(obj: WorkspaceObject) -> bool:
        if obj.type in (CHAT_TYPE, WORKSPACE_TYPE):
            return obj.id in visible
        return may_read(obj, reader)

    return [obj for obj in objects if _readable(obj)]


def object_resource(obj: WorkspaceObject, *, type: ResourceType) -> Resource:
    """The resource a decision about ``obj`` is filed under."""
    return Resource(
        type,
        id=str(obj.id),
        org_id=obj.org_team_id,
        team_id=obj.team_id,
    )


__all__ = [
    "Reader",
    "admin_reads_private",
    "bound_machine",
    "chat_attrs",
    "chat_facts",
    "load_object",
    "machine_holds_object",
    "may_read",
    "new_chat_attrs",
    "new_object_attrs",
    "new_workspace_attrs",
    "object_attrs",
    "object_resource",
    "payer_may_read",
    "payer_standings",
    "readable_chat_facts",
    "readable_chats",
    "readable_objects",
    "readable_workspace_facts",
    "resolve_reader",
    "workspace_attrs",
    "workspace_facts",
]
