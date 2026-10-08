"""Channel names, and who may read or write the document behind one.

A channel is ``doc:<type>:<id>``. Subscribing to one is an authorization
decision made once per subscription from the same facts the REST surface
uses, and refreshed by the entitlement snapshot the keepalive tick keeps
current. Anything the rules do not positively admit is refused, and a
document the caller may not know exists is refused as ``not_found`` —
never ``forbidden``, which would confirm the id is live.

* A registered kind (:mod:`backend.services.realtime.doc_kinds`), such as a
  knowledge item's ``artifact``: granted by the rules the domain that owns it
  registered. A type nobody registered is ``not_found``. The chat rules
  below grant the socket's own types
  (:data:`~backend.services.realtime.doc_kinds.NATIVE_DOC_TYPES`): ``chat`` and
  the legacy spellings of a chat's documents, such as ``chat_workspace``.
* ``workspace``: ``doc:workspace:<workspace_id>``. Presence only: there is no
  document behind it, so no envelope names it (the envelope's document type
  has no such value) and the channel carries nothing but who is in the
  workspace. Read: the workspace policy's READ answer over the facts the
  REST routes decide on (its owner, a rung on its folder, an org admin where
  the deployment lets one read private work). Never writable. A box holds
  none.
* ``chat`` — ``doc:chat:<chat_id>``. Multi-user from the start. Read: a chat
  is private until its node is shared, decided by the predicate the REST
  policy decides through (:func:`alkera_core.authz.chat_visible`) — its
  owner, the machine it is bound to speaking as itself, or anyone holding a
  rung on the chat's Files node ("Can view" and up, granted on the node,
  inherited from a folder above it, or made to a team the caller is on).
  Nobody else in the org, org admins included. Write: the publishing peer,
  decided by :func:`alkera_core.authz.chat_writable` — the document's owner,
  or the machine the chat is bound to when the socket was opened as an agent
  whose asserted id is that machine's id (the daemon on the org's box
  registers a machine and speaks as it, so one box publishes every member's
  chat) — OR anyone the chat's node was shared with at ``writer`` or above,
  which is the second half of the same question the REST send gate asks. A
  reader who is neither relays ``user_message`` and writes nothing. A chat
  must be **declared** before it is spoken to
  (:mod:`backend.services.realtime.chat_lookup`): its declaration is the chat's
  workspace object, and an id nothing declared is ``not_found``, never a
  document the asker owns by being first. The scope is looked up within the
  caller's org and never across it.

A grant is what the subscribe decided from the rows as they were then, and a
socket keeps it for as long as the subscription lasts. The document row is the
authority: the registry re-reads the row's org, team and owner on every
``hello`` and every operation (:func:`doc_readable`, and the write predicate
against the row's owner and the chat's binding), so a grant minted before the
row existed — or before it was narrowed to a team — can never be more than a
guess about what the client will be told. It is never what decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Final, Literal, cast
from uuid import UUID

from alkera_core.authz import (
    PUBLISHER_ROLE,
    ActingContext,
    Action,
    ResourceType,
    RoleResolver,
    chat_readable,
    chat_writable,
    require_scope_team,
)
from alkera_core.authz import authorize as decide
from alkera_core.authz import team_of_scope as _team_of_scope
from alkera_core.authz.chat_scope import chat_visible
from alkera_core.files.authz.ladder import ROLE_OWNER
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import RealtimeDoc, User
from alkera_core.schemas.realtime import (
    CHANNEL_RE,
    CRDT_DOC_TYPES,
    RESERVED_DOC_TYPES,
    DocType,
    machine_of_channel,
)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import ancestor_chain
from backend.services.realtime.chat_lookup import ChatDocScope, lookup_chat_doc
from backend.services.realtime.doc_kinds import NATIVE_DOC_TYPES, doc_kind
from backend.services.realtime.filters import EntitlementSnapshot
from backend.services.sharing import (
    SharedRungCache,
    access,
    admin_reads_private,
    rung_writes,
    shared_object_role,
)

#: ``crdt_unsupported`` answers a CRDT channel this server cannot serve.
ChannelErrorCode = Literal["bad_channel", "not_found", "forbidden", "crdt_unsupported"]


class ChannelError(Exception):
    """A subscription refused; ``code`` is what the client is told."""

    def __init__(self, code: ChannelErrorCode, message: str = "") -> None:
        super().__init__(message or code)
        self.code: ChannelErrorCode = code
        self.message = message or code


@dataclass(frozen=True, slots=True)
class Channel:
    doc_type: DocType
    doc_id: str

    @property
    def key(self) -> str:
        return f"doc:{self.doc_type}:{self.doc_id}"


@dataclass(frozen=True, slots=True)
class ChannelGrant:
    """What one subscriber may do on one channel, and how the document's
    events are addressed (its org, its team, its visibility)."""

    channel: Channel
    org_id: UUID
    can_write: bool
    owner_user_id: UUID | None
    team_id: UUID | None
    visibility: str = "org"

    def settled(
        self, *, owner_user_id: UUID | None, team_id: UUID | None, can_write: bool
    ) -> ChannelGrant:
        """This grant re-derived from the document row the registry just
        judged: the row's team narrows its audience, and ``can_write`` is the
        registry's verdict on this socket against that row (the write
        predicate, not the subscribe's guess)."""
        return ChannelGrant(
            channel=self.channel,
            org_id=self.org_id,
            can_write=can_write,
            owner_user_id=owner_user_id,
            team_id=team_id,
            visibility=self.visibility,
        )


#: The presence channel of a workspace. Not a document type: no envelope can
#: name it, so it is spelled apart from the document grammar.
WORKSPACE_CHANNEL_TYPE: Final = "workspace"
WORKSPACE_CHANNEL_RE: Final = re.compile(r"^doc:workspace:([0-9A-Fa-f-]{36})$")


@dataclass(frozen=True, slots=True)
class WorkspaceChannel:
    """``doc:workspace:<id>``: who is in a workspace. Presence only: there is
    no document behind it, so nothing that speaks about a document can be
    handed one. ``doc_type`` and ``doc_id`` are what its presence rows are
    keyed by."""

    workspace_id: str
    doc_type: ClassVar[str] = WORKSPACE_CHANNEL_TYPE

    @property
    def doc_id(self) -> str:
        return self.workspace_id

    @property
    def key(self) -> str:
        return f"doc:{WORKSPACE_CHANNEL_TYPE}:{self.workspace_id}"


@dataclass(frozen=True, slots=True)
class WorkspaceGrant:
    """A person's place on a workspace's presence channel: its org, its owner,
    and nothing to write. No team narrows its audience; the sockets the policy
    admitted are the audience."""

    channel: WorkspaceChannel
    org_id: UUID
    owner_user_id: UUID | None
    can_write: ClassVar[bool] = False
    team_id: ClassVar[UUID | None] = None
    visibility: ClassVar[str] = "org"


#: A channel presence may be held on, and the grant that holds it.
PresenceChannel = Channel | WorkspaceChannel
PresenceGrant = ChannelGrant | WorkspaceGrant


def presence_grant_of_row(doc_type: str, doc_id: str, org_id: UUID) -> PresenceGrant:
    """The channel a presence row was keyed under, addressed to the org's
    subscribers of that channel and nothing narrower: what the server says
    about a row it removed on its own (a lapsed peer) has no socket's grant to
    ride, and the only fact it carries (which peer left) is one every
    subscriber was already shown on the roster."""
    if doc_type == WORKSPACE_CHANNEL_TYPE:
        return WorkspaceGrant(
            channel=WorkspaceChannel(workspace_id=doc_id), org_id=org_id, owner_user_id=None
        )
    return ChannelGrant(
        channel=Channel(doc_type=cast(DocType, doc_type), doc_id=doc_id),
        org_id=org_id,
        can_write=False,
        owner_user_id=None,
        team_id=None,
    )


@dataclass(frozen=True, slots=True)
class MachineChannel:
    """``machine:<allocation_id>``: the drive's line to one box. It sits outside
    the document state machine — no epoch, no sequence, no document row, no
    presence — and carries only ``machine.request`` frames to the box and its
    ``machine.ack`` answers back."""

    machine_id: str

    @property
    def key(self) -> str:
        return f"machine:{self.machine_id}"


#: The whole channel name is the outbox row's ``entity_id`` for its ``doc.op``
#: events, so it must fit that column.
MAX_CHANNEL_KEY_LENGTH = 255


def _canonical_workspace_id(raw: str) -> str:
    """``raw`` when it is a workspace id in its one spelling (a lower-case,
    hyphenated UUID), else ``ChannelError("bad_channel")``. Never respelled: a
    channel is a presence key, and two spellings of one id would be two rooms."""
    try:
        canonical = str(UUID(raw))
    except ValueError as exc:
        raise ChannelError("bad_channel", "a workspace channel names a workspace id") from exc
    if canonical != raw:
        raise ChannelError(
            "bad_channel", "a workspace id is spelled as a lower-case, hyphenated UUID"
        )
    return raw


def parse_channel(raw: str) -> Channel | MachineChannel | WorkspaceChannel:
    """``raw`` as a :class:`Channel`, a :class:`MachineChannel` or a
    :class:`WorkspaceChannel`, or ``ChannelError("bad_channel")``."""
    machine = machine_of_channel(raw)
    if machine is not None:
        return MachineChannel(machine_id=machine)
    workspace = WORKSPACE_CHANNEL_RE.fullmatch(raw)
    if workspace is not None:
        return WorkspaceChannel(workspace_id=_canonical_workspace_id(workspace.group(1)))
    match = CHANNEL_RE.fullmatch(raw)
    if match is None:
        raise ChannelError(
            "bad_channel", "channel must match doc:<chat|artifact>:<id> or machine:<id>"
        )
    if len(raw) > MAX_CHANNEL_KEY_LENGTH:
        raise ChannelError(
            "bad_channel", f"channel names are capped at {MAX_CHANNEL_KEY_LENGTH} characters"
        )
    if match.group(1) in RESERVED_DOC_TYPES:
        raise ChannelError("bad_channel", f"{match.group(1)} documents are not served here")
    doc_id = match.group(2)
    if _non_canonical_uuid(doc_id):
        # Every frame on a channel is addressed by its name, so a channel has
        # exactly one: an upper-case or braced spelling of the same id would be
        # a second name the server's own frames never use, and a client
        # matching replies by what it sent would never see one.
        raise ChannelError(
            "bad_channel", "a document id in a channel name is a lower-case hyphenated UUID"
        )
    return Channel(doc_type=cast(DocType, match.group(1)), doc_id=doc_id)


_HYPHENATED_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _non_canonical_uuid(doc_id: str) -> bool:
    """Whether ``doc_id`` is a hyphenated UUID with an upper-case digit.

    Only that shape: a legacy document id is any string the grammar admits
    (``sess-1``, a hex token), and none of those is a second spelling of a
    UUID the server names, so none of them is refused here.
    """
    return _HYPHENATED_UUID.fullmatch(doc_id) is not None and doc_id != doc_id.lower()


async def authorize_machine_chat(
    db: AsyncSession, ctx: ActingContext, channel: Channel
) -> ChannelGrant:
    """A box on its own machine credential's grant on ``channel``, or
    ``ChannelError``.

    A box holds exactly the chats bound to its machine, in whichever org they
    live, and nothing else: no artifact, no chat of its operator's, no chat
    another box runs. The decision is the chat policy's own machine branch —
    the tenancy floor over the orgs the credential serves, then the binding —
    asked through :func:`alkera_core.authz.authorize` over the same facts the
    chat route and the box's listing decide on, without a decision row (a
    subscribe leaves none, like every person's). The chat is looked up by id
    across orgs on purpose: the org is not what bounds a box, the policy is.
    Every refusal is ``not_found``, the answer a stranger gets.
    """
    if channel.doc_type != "chat":
        raise ChannelError("not_found")
    try:
        chat_id = UUID(channel.doc_id)
    except ValueError as exc:
        raise ChannelError("not_found") from exc
    chat = await access.load_object(db, chat_id, type="chat")
    if chat is None or not require_scope_team(chat.visibility_scope, chat.team_id):
        raise ChannelError("not_found")
    facts = access.chat_facts(
        chat,
        access.machine_reader(ctx),
        shared_role=None,
        standing=access.OWN_FOLDER,
        owner_departed=False,
    )
    decision = decide(ctx, Action.READ, access.object_resource(chat, type=ResourceType.CHAT), facts)
    if not decision.allowed:
        raise ChannelError("not_found")
    return ChannelGrant(
        channel=channel,
        org_id=chat.org_team_id,
        can_write=True,
        owner_user_id=chat.owner_user_id,
        team_id=chat.team_id,
    )


def authorize_machine(channel: MachineChannel, *, machine_id: str | None) -> MachineChannel:
    """Admit ``channel`` only to the socket that PROVED at admission it is that
    machine: ``machine_id`` is the session's verified machine, never the
    ticket's bare assertion, which any member can put on their own ticket.

    A person's socket and a machine's socket naming another machine are both
    refused ``forbidden``. The decision reads nothing, so the refusal is the
    same whether or not the named machine exists: it confirms nothing about
    anyone else's box."""
    if machine_id is None or machine_id != channel.machine_id:
        raise ChannelError("forbidden", "only the machine itself holds its channel")
    return channel


def team_of_scope(scope: str) -> UUID | None:
    """The team a ``team:<uuid>`` knowledge scope names; ``None`` for an org
    scope or anything unparseable. The scope grammar is the authorization
    package's, so the socket and the policies parse it the same way."""
    return _team_of_scope(scope)


async def authorize(
    db: AsyncSession,
    user: User,
    channel: Channel,
    *,
    ent: EntitlementSnapshot,
    agent_id: str | None = None,
    rungs: SharedRungCache | None = None,
) -> ChannelGrant:
    """The caller's grant on ``channel``, or ``ChannelError``. ``agent_id`` is
    the machine the socket was VERIFIED to speak as at admission — its
    ticket's assertion, admitted only when it named a live machine of the org
    registered by this very user — ``None`` for a person's own socket and for
    an assertion that did not verify; a chat bound to that machine reads it
    as itself. The session resolves it once; callers never pass the bare
    header, which any member can put on their own ticket.
    ``rungs`` is the connection's memory of the node rungs it has already
    read; omitting it reads the ACL afresh, which is what a one-shot caller
    wants."""
    if channel.doc_type in CRDT_DOC_TYPES:
        # A CRDT document is granted by its type's own rules (the CRDT
        # gateway asks them); it must never fall through to the chat grant.
        raise ChannelError("not_found")
    org_id = _org_of(ent)
    if channel.doc_type not in NATIVE_DOC_TYPES:
        # Any other type is a domain's, granted by the rules it registered;
        # one nobody registered is a document this server cannot find.
        kind = doc_kind(channel.doc_type)
        if kind is None:
            raise ChannelError("not_found")
        return await kind.authorize(db, user, channel, org_id=org_id)
    return await _authorize_chat(
        db, user, channel, org_id=org_id, ent=ent, agent_id=agent_id, rungs=rungs
    )


async def authorize_workspace(
    db: AsyncSession, user: User, channel: WorkspaceChannel, *, org_id: UUID | None
) -> WorkspaceGrant:
    """A person's grant on a workspace's presence: the workspace policy's READ
    answer, asked through the REST routes' own facts and without a decision
    row (a subscribe leaves none, like the listing's per-row answer). A
    workspace in another org, one nobody shared with the caller and one that
    does not exist are the same ``not_found``.

    The grant names no team: the audience of the channel's frames is the set
    of sockets the policy admitted, and a workspace shared outside its team
    must reach the person it was shared with. ``org_id`` is the connection's
    org (its ticket's), never the person's home org; ``None`` finds nothing."""
    if org_id is None:
        raise ChannelError("not_found")
    try:
        workspace_id = UUID(channel.workspace_id)
    except ValueError as exc:
        raise ChannelError("not_found") from exc
    workspace = await access.load_object(db, workspace_id, type=WORKSPACE_TYPE)
    if workspace is None or workspace.org_team_id != org_id:
        raise ChannelError("not_found")
    ctx = ActingContext.for_user(user_id=user.id, org_id=org_id, email=user.email)
    roles = RoleResolver(db, ctx, ancestor_chain=ancestor_chain)
    reader = await access.resolve_reader(db, ctx=ctx, roles=roles, user=user)
    attrs = await access.workspace_attrs(db, workspace, reader)
    resource = access.object_resource(workspace, type=ResourceType.WORKSPACE)
    if not decide(ctx, Action.READ, resource, attrs).allowed:
        raise ChannelError("not_found")
    return WorkspaceGrant(
        channel=channel, org_id=workspace.org_team_id, owner_user_id=workspace.owner_user_id
    )


def _org_of(ent: EntitlementSnapshot) -> UUID:
    """The org every document a connection may find lives in: the one its
    entitlement snapshot was taken in, which is its credential's. A snapshot
    with no org (a box's) finds no person's document."""
    if ent.org_id is None:
        raise ChannelError("not_found")
    return ent.org_id


def doc_readable(doc: RealtimeDoc, *, ent: EntitlementSnapshot) -> bool:
    """Whether the holder of ``ent`` may read ``doc`` as its row stands now: the
    row belongs to the org the snapshot was taken in (the connection's
    credential's) and, when it is narrowed to a team, the caller is on that
    team in that org or administers the org. The subscribe applies this to the row it
    finds; the registry applies it again to the row it locks, so a row that
    appeared or moved after the subscribe is judged as it is, not as it was.

    The row carries the team and nothing finer, so this re-check can narrow a
    grant by team but cannot see a share: whether the caller holds a rung on
    the chat's node is decided at the subscribe, from the chat's declaration,
    and a socket never granted the channel receives nothing from it."""
    if ent.org_id is None or doc.org_id != ent.org_id:
        return False
    return chat_readable(is_org_admin=ent.org_admin, team_ids=ent.team_ids, doc_team_id=doc.team_id)


async def _authorize_chat(
    db: AsyncSession,
    user: User,
    channel: Channel,
    *,
    org_id: UUID,
    ent: EntitlementSnapshot,
    agent_id: str | None,
    rungs: SharedRungCache | None = None,
) -> ChannelGrant:
    # Scoped to the connection's org: another tenant's row under the same id is
    # not this caller's document to find, and must not stop them holding theirs.
    scope = await lookup_chat_doc(db, org_id=org_id, chat_id=channel.doc_id)
    if scope is None:
        # Nothing declared this chat, so there is nothing to be admitted to.
        # Asking about an id is not how one comes into existence.
        raise ChannelError("not_found")
    # The scope string and the team column are two spellings of one row. The
    # REST policy refuses a row on which they disagree; so does the socket.
    if not require_scope_team(scope.visibility_scope, scope.team_id):
        raise ChannelError("not_found")
    # A chat is private until shared: the owner, the bound machine speaking as
    # itself (``agent_id`` is the machine the socket VERIFIED at admission — a
    # live machine of the org registered by this very user — never the bare
    # assertion a colleague can put on their own ticket), or a rung on the
    # chat's node — the REST policy's READ answer, through the same predicate
    # and the same rung read. The document registry judges every later frame
    # by this same standing, and what a reader is told about writing is
    # decided by the very standing that admitted them.
    standing = await chat_standing(
        db,
        scope,
        chat_id=channel.doc_id,
        user_id=user.id,
        agent_id=agent_id,
        org_id=org_id,
        ent=ent,
        rungs=rungs,
    )
    if not chat_visible(
        is_owner=standing.is_owner,
        is_bound_machine=standing.is_bound_machine,
        shared_role=standing.shared_role,
        # The deployment's answer to "may an org admin open a chat nobody shared
        # with them", read through the same accessor every REST door reads it
        # through. Without it the socket would be the one door that never heard
        # of the setting: an admin the deployment admits would open the chat
        # over REST and be told it does not exist the moment they subscribed.
        is_org_admin=ent.org_admin,
        admin_reads_private=admin_reads_private(),
    ):
        raise ChannelError("not_found")
    return ChannelGrant(
        channel=channel,
        org_id=org_id,
        can_write=standing.publishes or rung_writes(standing.shared_role),
        owner_user_id=scope.owner_user_id,
        team_id=scope.team_id,
    )


@dataclass(frozen=True, slots=True)
class ChatStanding:
    """Where a declared chat leaves one caller on its live document.

    ``is_owner`` is the chat's owner, or the workspace's owner when the chat
    sits in a workspace of several. ``is_bound_machine`` is the machine the
    chat is bound to, speaking as itself. ``shared_role`` is the rung that
    decides everything else: the workspace's rung in a workspace of several,
    the chat node's otherwise, and ``None`` for a publisher (whose rung changes
    nothing) or a caller nobody granted one. The socket's grant and the
    document registry both read this, so a subscribe and every later frame
    are judged by one answer.
    """

    is_owner: bool
    is_bound_machine: bool
    shared_role: str | None

    @property
    def publishes(self) -> bool:
        """Whether the caller is the document's PUBLISHER, the one identity
        that may append to the transcript, stream on the ephemeral lane and
        rebuild the state wholesale."""
        return self.is_owner or self.is_bound_machine

    @property
    def role(self) -> str | None:
        """The rung held on the document: the publisher's, else the shared
        one. ``None`` is a reader the audience already admitted."""
        return PUBLISHER_ROLE if self.publishes else self.shared_role


async def chat_standing(
    db: AsyncSession,
    scope: ChatDocScope,
    *,
    chat_id: str,
    user_id: UUID | None,
    agent_id: str | None,
    org_id: UUID,
    ent: EntitlementSnapshot,
    rungs: SharedRungCache | None = None,
) -> ChatStanding:
    """Where the chat ``scope`` declares leaves this caller, decided the way
    the REST chat policy decides it.

    A chat in a workspace of several answers to the workspace: whoever started
    it holds nothing extra, its owner is the workspace's owner, and a person
    demoted or removed there loses the chat's live document with it. Otherwise
    the chat's owner and its bound machine publish it, and anyone else holds
    the rung on the chat's node. ``user_id`` is ``None`` for a box on its own
    machine credential: only the binding admits it, and it holds no rung.

    The publisher of a chat of its own never pays for a Files read: their rung
    cannot change either answer, so the box and the author skip it.
    """
    is_bound_machine = bool(scope.machine_id) and bool(agent_id) and agent_id == scope.machine_id
    if user_id is None:
        return ChatStanding(is_owner=False, is_bound_machine=is_bound_machine, shared_role=None)
    workspace = await _workspace_standing(db, user_id, scope, org_id=org_id, ent=ent, rungs=rungs)
    if workspace.multi_chat:
        return ChatStanding(
            is_owner=workspace.role == ROLE_OWNER,
            is_bound_machine=is_bound_machine,
            shared_role=workspace.role or None,
        )
    if chat_writable(
        owner_user_id=scope.owner_user_id,
        user_id=user_id,
        bound_machine_id=scope.machine_id,
        agent_id=agent_id,
    ):
        return ChatStanding(
            is_owner=scope.owner_user_id == user_id,
            is_bound_machine=is_bound_machine,
            shared_role=None,
        )
    return ChatStanding(
        is_owner=False,
        is_bound_machine=False,
        shared_role=await _shared_rung(db, user_id, chat_id, org_id=org_id, ent=ent, rungs=rungs),
    )


async def _workspace_standing(
    db: AsyncSession,
    user_id: UUID,
    scope: ChatDocScope,
    *,
    org_id: UUID,
    ent: EntitlementSnapshot,
    rungs: SharedRungCache | None,
) -> access.WorkspaceStanding:
    """Where the chat's workspace leaves this person, the rung read through
    the connection's memory when it has one."""

    async def rung(object_id: UUID) -> str | None:
        if rungs is not None:
            return await rungs.rung(
                db,
                org_id=org_id,
                object_id=object_id,
                user_id=user_id,
                team_ids=ent.team_ids,
            )
        return await shared_object_role(
            db, org_id=org_id, object_id=object_id, user_id=user_id, team_ids=ent.team_ids
        )

    return await access.chat_standing_for(workspace=scope.workspace, user_id=user_id, rung=rung)


async def _shared_rung(
    db: AsyncSession,
    user_id: UUID,
    chat_id: str,
    *,
    org_id: UUID,
    ent: EntitlementSnapshot,
    rungs: SharedRungCache | None,
) -> str | None:
    """The rung the chat's node gives this person, through the connection's
    memory when it has one.

    It answers both halves of the subscribe for a caller who is not the
    publisher: whether they may read at all (any rung the ladder knows), and
    whether they are told they may write (``writer`` and above) — the same two
    the REST gates ask, in the same order. Being in the org is not being a
    reader, and being a reader is not being an editor: a reader is admitted
    and told ``can_write: false``; a member the chat was never shared with is
    refused as ``not_found``, exactly as a foreign org's member is.

    A chat id that is not a node id cannot have been granted a rung, so it
    answers none.
    """
    try:
        object_id = UUID(chat_id)
    except ValueError:
        return None
    if rungs is None:
        return await shared_object_role(
            db,
            org_id=org_id,
            object_id=object_id,
            user_id=user_id,
            team_ids=ent.team_ids,
        )
    return await rungs.rung(
        db,
        org_id=org_id,
        object_id=object_id,
        user_id=user_id,
        team_ids=ent.team_ids,
    )


__all__ = [
    "WORKSPACE_CHANNEL_RE",
    "WORKSPACE_CHANNEL_TYPE",
    "Channel",
    "ChannelError",
    "ChannelErrorCode",
    "ChannelGrant",
    "ChatStanding",
    "MachineChannel",
    "PresenceChannel",
    "PresenceGrant",
    "WorkspaceChannel",
    "WorkspaceGrant",
    "authorize",
    "authorize_machine",
    "authorize_machine_chat",
    "authorize_workspace",
    "chat_standing",
    "lookup_chat_doc",
    "parse_channel",
    "presence_grant_of_row",
    "team_of_scope",
]
