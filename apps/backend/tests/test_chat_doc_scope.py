"""A chat is declared before it is spoken to, and both surfaces agree on who
may read it.

Two things are pinned here.

**Explicit creation.** A chat id used to be its own capability: the first
``hello`` for an unknown id created the document and made its caller the owner,
so anyone in the org who guessed or replayed an id owned a document nobody had
created. The socket now resolves a chat's owner and audience through
``lookup_chat_doc``, and an id it does not know is ``not_found`` — the same
answer a chat the caller may not read gets, so the refusal is not an oracle
either.

**Agreement between the two surfaces.** The REST policy (``chat.access``) and
the channel rules are two callers of one predicate, and this file drives both
over the same matrix of readers and scopes: a chat readable over REST is
readable over the socket, and one that is not, is not. That is the obligation
the split in the design carries — the socket keeps its own rules only as long
as they cannot diverge from the policy's.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import (
    ActingContext,
    Action,
    Resource,
    ResourceType,
    Role,
    authorize,
    scope_for_team,
)
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import TeamMembership, TeamRole, User, WorkspaceObject
from alkera_core.schemas.objects import ChatSpec
from backend.services.chats import chat_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from backend.services.realtime import channels
from backend.services.realtime.channels import Channel, ChannelError
from backend.services.realtime.chat_lookup import ChatDocScope, lookup_chat_doc
from backend.services.realtime.filters import EntitlementSnapshot, load_entitlements
from backend.services.sharing import access
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio

#: A registered workspace machine's id, as the compute plane mints them.
MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
OTHER_MACHINE = "7b3d2c5e-4f20-4c4d-9f7e-93c5e1b2d333"


async def declare_chat(
    db: AsyncSession,
    *,
    org_id: UUID,
    owner_user_id: UUID,
    team_id: UUID | None = None,
    visibility_scope: str | None = None,
    machine_id: str | None = None,
) -> Channel:
    """Declare the chat explicitly — the workspace object ``POST /api/v1/chats``
    writes, and what a socket can no longer do by asking. The document row is
    still created lazily by the first ``hello``. ``machine_id`` is the binding
    the placement resolver writes into the spec."""
    chat = WorkspaceObject(
        id=uuid4(),
        org_team_id=org_id,
        logical_id=f"test-{uuid4().hex[:10]}",
        namespace="workspace",
        type="chat",
        title="",
        version=1,
        status="ready",
        spec=ChatSpec(
            machine_id=machine_id, machine_status="ready" if machine_id else "none"
        ).model_dump(mode="json"),
        owner_user_id=owner_user_id,
        team_id=team_id,
        visibility_scope=visibility_scope or scope_for_team(team_id),
    )
    db.add(chat)
    await db.commit()
    return Channel("chat", str(chat.id))


async def shared_chat(
    db: AsyncSession,
    *,
    owner: User,
    machine_id: str | None = None,
    viewers: tuple[User, ...] = (),
    writers: tuple[User, ...] = (),
    teams: tuple[UUID, ...] = (),
) -> tuple[WorkspaceObject, Channel]:
    """A chat created the way ``POST /api/v1/chats`` creates it — so the object
    bridge mints the Files node a grant hangs from — shared with ``viewers``
    and ``teams`` at "Can view" and ``writers`` at "Can edit".

    A chat is private until its node is shared, so every case here about a
    SECOND person reading one has to make the grant the real way. The caller
    needs the ``files_on`` fixture: with no configured store there is no node
    and nothing to grant a rung on.
    """
    chat, _ = await chat_service.create_chat(
        db,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="Ops",
        client_id=None,
        machine_id=machine_id,
        machine_status="ready" if machine_id else "none",
    )
    await db.commit()
    granted: list[tuple[Principal, str]] = [
        (Principal(kind="user", id=viewer.id), ROLE_READER) for viewer in viewers
    ]
    granted += [(Principal(kind="user", id=writer.id), ROLE_WRITER) for writer in writers]
    granted += [(Principal(kind="team", id=team_id), ROLE_READER) for team_id in teams]
    for principal, role in granted:
        await share_chat_with(db, chat=chat, owner=owner, principal=principal, role=role)
    return chat, Channel("chat", str(chat.id))


async def _chat_row(db: AsyncSession, channel: Channel) -> WorkspaceObject:
    """The workspace object behind a channel, so the REST side of an agreement
    case resolves its facts from the very row the socket read."""
    found = await db.get(WorkspaceObject, UUID(channel.doc_id))
    assert found is not None
    return found


async def _refused(
    db: AsyncSession, user: User, channel: Channel, *, agent_id: str | None = None
) -> None:
    """``channel`` is not this caller's to see: the socket's opaque
    ``not_found``, the same answer another org's member gets."""
    with pytest.raises(ChannelError) as info:
        await channels.authorize(
            db,
            user,
            channel,
            ent=await load_entitlements(db, user, org_id=user.home_org_team_id),
            agent_id=agent_id,
        )
    assert info.value.code == "not_found"


async def _user(db: AsyncSession, user_id: UUID) -> User:
    found = await db.get(User, user_id)
    assert found is not None
    return found


# ---------------------------------------------------------------------------
# The lookup seam
# ---------------------------------------------------------------------------


async def test_lookup_answers_none_for_an_id_the_org_never_declared(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    assert (
        await lookup_chat_doc(real_session, org_id=org_admin.org_id, chat_id="sess-nobody-made")
        is None
    )


async def test_lookup_reads_the_declared_owner_and_team(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    channel = await declare_chat(
        real_session, org_id=org_admin.org_id, owner_user_id=org_admin.admin_id, team_id=team.id
    )
    assert await lookup_chat_doc(
        real_session, org_id=org_admin.org_id, chat_id=channel.doc_id
    ) == ChatDocScope(
        owner_user_id=org_admin.admin_id,
        team_id=team.id,
        visibility_scope=f"team:{team.id}",
        machine_id=None,
    )


async def test_lookup_reads_the_machine_the_chat_is_bound_to(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The binding the placement resolver wrote into the spec is part of the
    declaration: it names the machine that may publish the chat."""
    channel = await declare_chat(
        real_session, org_id=org_admin.org_id, owner_user_id=org_admin.admin_id, machine_id=MACHINE
    )
    scope = await lookup_chat_doc(real_session, org_id=org_admin.org_id, chat_id=channel.doc_id)
    assert scope is not None and scope.machine_id == MACHINE


async def test_a_chat_declared_by_one_org_is_invisible_to_another(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_support: OrgWithAdmin
) -> None:
    """The org is half the key: the same id in another tenant is another
    document, and this org has none."""
    channel = await declare_chat(
        real_session, org_id=org_admin.org_id, owner_user_id=org_admin.admin_id
    )
    assert (
        await lookup_chat_doc(real_session, org_id=platform_support.org_id, chat_id=channel.doc_id)
        is None
    )


# ---------------------------------------------------------------------------
# The socket refuses an undeclared chat
# ---------------------------------------------------------------------------


async def test_an_undeclared_chat_is_not_found_rather_than_owned_by_its_first_caller(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The regression this file exists for: subscribing to an id nobody created
    used to hand the caller a write grant over a document that did not exist."""
    admin = await _user(real_session, org_admin.admin_id)
    ent = await load_entitlements(real_session, admin, org_id=admin.home_org_team_id)
    with pytest.raises(ChannelError) as info:
        await channels.authorize(
            real_session, admin, Channel("chat", f"sess-{uuid4().hex[:8]}"), ent=ent
        )
    assert info.value.code == "not_found"
    async with AsyncSessionLocal() as db:
        rows = await lookup_chat_doc(db, org_id=org_admin.org_id, chat_id="sess-nobody-made")
    assert rows is None, "a refused subscribe creates nothing"


async def test_a_declared_chat_grants_write_to_its_owner_and_nothing_to_anyone_else(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat is private until its node is shared: the member who declared it
    publishes it, and nobody else in the org — org admin included — is told it
    exists at all."""
    member, _pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    other, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    channel = await declare_chat(real_session, org_id=org_admin.org_id, owner_user_id=member.id)
    admin = await _user(real_session, org_admin.admin_id)

    owner_grant = await channels.authorize(
        real_session,
        member,
        channel,
        ent=await load_entitlements(real_session, member, org_id=member.home_org_team_id),
    )
    assert owner_grant.can_write is True
    assert owner_grant.owner_user_id == member.id

    await _refused(real_session, admin, channel)
    await _refused(real_session, other, channel)


@pytest.mark.usefixtures("files_on")
async def test_a_shared_chat_is_read_at_the_rung_it_was_granted(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The other half of the rule: what admits a colleague is a grant on the
    chat's node, and the grant's rung is what they are told about writing."""
    owner, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    viewer, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    editor, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    unshared, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    _chat, channel = await shared_chat(
        real_session, owner=owner, viewers=(viewer,), writers=(editor,)
    )

    viewer_grant = await channels.authorize(
        real_session,
        viewer,
        channel,
        ent=await load_entitlements(real_session, viewer, org_id=viewer.home_org_team_id),
    )
    assert viewer_grant.can_write is False
    assert viewer_grant.owner_user_id == owner.id
    editor_grant = await channels.authorize(
        real_session,
        editor,
        channel,
        ent=await load_entitlements(real_session, editor, org_id=editor.home_org_team_id),
    )
    assert editor_grant.can_write is True, "a Can-edit rung drives the chat"
    await _refused(real_session, unshared, channel)


async def test_a_team_scoped_chat_admits_its_owner_and_refuses_the_team_and_the_org(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    insider, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await membership_service.add_member(
        real_session, team_id=team.id, user_id=insider.id, role=TeamRole.MEMBER
    )
    await real_session.commit()
    outsider, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    channel = await declare_chat(
        real_session, org_id=org_admin.org_id, owner_user_id=insider.id, team_id=team.id
    )

    grant = await channels.authorize(
        real_session,
        insider,
        channel,
        ent=await load_entitlements(real_session, insider, org_id=insider.home_org_team_id),
    )
    assert grant.can_write is True and grant.team_id == team.id
    await _refused(real_session, outsider, channel)

    # The team column says what audience the chat was addressed to; it no
    # longer admits anybody. The insider above reads because they OWN it, and
    # a teammate who does not would be refused just like the outsider.
    admin = await _user(real_session, org_admin.admin_id)
    await _refused(real_session, admin, channel)


# ---------------------------------------------------------------------------
# REST and the socket agree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reader:
    label: str
    user: User
    ent: EntitlementSnapshot


def _rest_reader(reader: _Reader, org: OrgWithAdmin) -> access.Reader:
    """``reader`` as the route resolves them: roles at the ORG ROOT, the
    memberships the socket read, a verified address."""
    roles = {Role.MEMBER}
    if reader.ent.org_admin:
        roles.add(Role.ADMIN)
    return access.Reader(
        ctx=ActingContext.for_user(
            user_id=reader.user.id, org_id=org.org_id, email=reader.user.email
        ),
        in_org=True,
        roles=frozenset(roles),
        is_org_admin=reader.ent.org_admin,
        team_ids=reader.ent.team_ids,
        email_verified=True,
    )


async def _rest_attrs(
    db: AsyncSession, *, reader: _Reader, org: OrgWithAdmin, chat: WorkspaceObject
) -> dict[str, object]:
    """The facts a chat route resolves for the policy, through the route's own
    resolver: the rung on the chat's node and the machine it is bound to are
    read by production code, so a test can no longer agree with the socket by
    hand-feeding the policy a fact the route would have resolved differently."""
    return await access.chat_attrs(db, chat, _rest_reader(reader, org))


async def _readers(db: AsyncSession, org: OrgWithAdmin, team_id: UUID) -> list[_Reader]:
    admin = await _user(db, org.admin_id)
    insider, _ = await make_member(db, org_id=org.org_id, verified=True)
    await membership_service.add_member(
        db, team_id=team_id, user_id=insider.id, role=TeamRole.MEMBER
    )
    await db.commit()
    outsider, _ = await make_member(db, org_id=org.org_id, verified=True)
    stranger, _ = await make_member(db, org_id=org.org_id, verified=True)
    # A user with no membership row at all: in the org, on no team.
    await db.execute(TeamMembership.__table__.delete().where(TeamMembership.user_id == stranger.id))
    await db.commit()
    return [
        _Reader(
            "org-admin", admin, await load_entitlements(db, admin, org_id=admin.home_org_team_id)
        ),
        _Reader(
            "team-insider",
            insider,
            await load_entitlements(db, insider, org_id=insider.home_org_team_id),
        ),
        _Reader(
            "team-outsider",
            outsider,
            await load_entitlements(db, outsider, org_id=outsider.home_org_team_id),
        ),
        _Reader(
            "no-memberships",
            stranger,
            await load_entitlements(db, stranger, org_id=stranger.home_org_team_id),
        ),
    ]


@pytest.mark.usefixtures("files_on")
async def test_rest_and_the_socket_admit_exactly_the_same_readers(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The agreement obligation, over a matrix of readers and grants: for every
    pair, the REST policy allows ``chat.read`` if and only if the socket issues
    a grant. A change to either side that drifts fails here."""
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    readers = await _readers(real_session, org_admin, team.id)
    insider = readers[1].user
    owner, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chats = {
        "unshared": await shared_chat(real_session, owner=owner),
        "shared-with-the-insider": await shared_chat(real_session, owner=owner, viewers=(insider,)),
        "shared-with-the-team": await shared_chat(real_session, owner=owner, teams=(team.id,)),
    }
    readers = [
        _Reader(
            "owner",
            owner,
            await load_entitlements(real_session, owner, org_id=owner.home_org_team_id),
        ),
        *readers,
    ]
    seen: dict[tuple[str, str], bool] = {}
    for chat_label, (chat, channel) in chats.items():
        for reader in readers:
            try:
                await channels.authorize(real_session, reader.user, channel, ent=reader.ent)
                socket_ok = True
            except ChannelError as exc:
                assert exc.code == "not_found"
                socket_ok = False
            decision = authorize(
                ActingContext.for_user(
                    user_id=reader.user.id,
                    org_id=org_admin.org_id,
                    email=reader.user.email,
                ),
                Action.READ,
                Resource(ResourceType.CHAT, id=channel.doc_id, org_id=org_admin.org_id),
                await _rest_attrs(real_session, reader=reader, org=org_admin, chat=chat),
            )
            assert decision.allowed is socket_ok, (
                f"{chat_label} / {reader.label}: REST said {decision.allowed} "
                f"({decision.reason}), the socket said {socket_ok}"
            )
            seen[(chat_label, reader.label)] = socket_ok
    # The matrix is only worth something if it contains both answers.
    assert all(seen[(label, "owner")] for label in chats), "the owner reads their own chat"
    # A chat nobody shared is nobody else's, whatever standing they have.
    for label in ("org-admin", "team-insider", "team-outsider", "no-memberships"):
        assert seen[("unshared", label)] is False
    # A grant is the only thing that admits a second person — to the person it
    # names, or to a team they are on, and to nobody beside them.
    assert seen[("shared-with-the-insider", "team-insider")] is True
    assert seen[("shared-with-the-insider", "team-outsider")] is False
    assert seen[("shared-with-the-insider", "org-admin")] is False
    assert seen[("shared-with-the-team", "team-insider")] is True
    assert seen[("shared-with-the-team", "team-outsider")] is False
    assert seen[("shared-with-the-team", "no-memberships")] is False


async def test_rest_and_the_socket_agree_that_an_undeclared_chat_does_not_exist(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Neither surface invents a chat: the socket refuses the id, and a route
    that cannot resolve a row has no facts to enforce with."""
    admin = await _user(real_session, org_admin.admin_id)
    channel = Channel("chat", f"sess-{uuid4().hex[:8]}")
    with pytest.raises(ChannelError):
        await channels.authorize(
            real_session,
            admin,
            channel,
            ent=await load_entitlements(real_session, admin, org_id=admin.home_org_team_id),
        )
    assert (
        await lookup_chat_doc(real_session, org_id=org_admin.org_id, chat_id=channel.doc_id) is None
    )


# ---------------------------------------------------------------------------
# The declaration is the chat's workspace object
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_a_chat_created_over_rest_is_subscribable_and_owned_by_its_creator(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The seam end to end: ``POST /api/v1/chats`` writes the declaration the
    socket resolves — through the real lookup, not a test fixture — so the
    creator holds the write grant, a member of the org is told the chat does
    not exist, and a share is what changes that."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await client.post("/api/v1/chats", json={"title": "Ops"})
    assert created.status_code == 201, created.text
    channel = Channel("chat", created.json()["id"])

    assert await lookup_chat_doc(
        real_session, org_id=org_admin.org_id, chat_id=channel.doc_id
    ) == ChatDocScope(owner_user_id=org_admin.admin_id, team_id=None, visibility_scope="private")

    admin = await _user(real_session, org_admin.admin_id)
    owner_grant = await channels.authorize(
        real_session,
        admin,
        channel,
        ent=await load_entitlements(real_session, admin, org_id=admin.home_org_team_id),
    )
    assert owner_grant.can_write is True
    assert owner_grant.owner_user_id == org_admin.admin_id

    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await _refused(real_session, member, channel)

    await share_chat_with(
        real_session,
        chat=await _chat_row(real_session, channel),
        owner=admin,
        principal=Principal(kind="user", id=member.id),
        role=ROLE_READER,
    )
    reader_grant = await channels.authorize(
        real_session,
        member,
        channel,
        ent=await load_entitlements(real_session, member, org_id=member.home_org_team_id),
    )
    assert reader_grant.can_write is False


@pytest.mark.parametrize(
    "chat_id",
    [
        pytest.param("sess-not-a-row-id", id="an-id-that-is-not-a-row-id"),
        pytest.param(str(uuid4()), id="a-row-id-nobody-made"),
    ],
)
async def test_an_id_that_names_no_chat_is_not_found_on_both_surfaces(
    real_session: AsyncSession, org_admin: OrgWithAdmin, chat_id: str
) -> None:
    admin = await _user(real_session, org_admin.admin_id)
    assert await lookup_chat_doc(real_session, org_id=org_admin.org_id, chat_id=chat_id) is None
    with pytest.raises(ChannelError) as info:
        await channels.authorize(
            real_session,
            admin,
            Channel("chat", chat_id),
            ent=await load_entitlements(real_session, admin, org_id=admin.home_org_team_id),
        )
    assert info.value.code == "not_found"


async def test_a_workspace_object_that_is_not_a_chat_names_no_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A promoted result's id is a workspace object's row id, and still names
    no chat: the type discriminator is part of the declaration."""
    from backend.services.objects import object_service

    admin = await _user(real_session, org_admin.admin_id)
    saved, _ = await object_service.create_object(
        real_session,
        owner=admin,
        org_id=admin.home_org_team_id,
        type="result",
        title="Daily prompts",
        spec={"columns": [{"name": "day"}]},
        logical_id=f"r-{uuid4().hex[:8]}",
    )
    await real_session.commit()
    object_id = str(saved.id)
    assert await lookup_chat_doc(real_session, org_id=org_admin.org_id, chat_id=object_id) is None
    with pytest.raises(ChannelError) as info:
        await channels.authorize(
            real_session,
            admin,
            Channel("chat", object_id),
            ent=await load_entitlements(real_session, admin, org_id=admin.home_org_team_id),
        )
    assert info.value.code == "not_found"


async def test_a_private_chat_admits_its_owner_and_nobody_else(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The socket's private rule, on its own: the owner publishes, and everyone
    else — the org's admin included — is told the chat does not exist."""
    owner, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    other, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    channel = await declare_chat(
        real_session, org_id=org_admin.org_id, owner_user_id=owner.id, visibility_scope="private"
    )
    owner_grant = await channels.authorize(
        real_session,
        owner,
        channel,
        ent=await load_entitlements(real_session, owner, org_id=owner.home_org_team_id),
    )
    assert owner_grant.can_write is True
    await _refused(real_session, await _user(real_session, org_admin.admin_id), channel)
    await _refused(real_session, other, channel)


async def test_a_row_whose_scope_and_team_disagree_is_refused_by_both_surfaces(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The scope string and the team column are two spellings of one row; a
    writer that lets them disagree gets nobody admitted, on either path — not
    even the org admin who owns the row."""
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    channel = await declare_chat(
        real_session,
        org_id=org_admin.org_id,
        owner_user_id=org_admin.admin_id,
        team_id=team.id,
        visibility_scope="org",
    )
    admin = await _user(real_session, org_admin.admin_id)
    ent = await load_entitlements(real_session, admin, org_id=admin.home_org_team_id)
    with pytest.raises(ChannelError) as info:
        await channels.authorize(real_session, admin, channel, ent=ent)
    assert info.value.code == "not_found"
    scope = await lookup_chat_doc(real_session, org_id=org_admin.org_id, chat_id=channel.doc_id)
    assert scope is not None
    decision = authorize(
        ActingContext.for_user(user_id=admin.id, org_id=org_admin.org_id, email=admin.email),
        Action.READ,
        Resource(ResourceType.CHAT, id=channel.doc_id, org_id=org_admin.org_id),
        await _rest_attrs(
            real_session,
            reader=_Reader("org-admin", admin, ent),
            org=org_admin,
            chat=await _chat_row(real_session, channel),
        ),
    )
    assert (decision.allowed, decision.reason) == (False, "scope_team_mismatch")


# ---------------------------------------------------------------------------
# The machine a chat is bound to publishes it
# ---------------------------------------------------------------------------


async def test_the_bound_machine_publishes_a_chat_another_member_owns(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The org's box signs in as its operator and speaks as the machine it
    registered; a chat any member created is bound to that machine, so the
    operator's socket — opened as that agent — writes it. The same socket as a
    person, or as any other agent, is a reader like everyone else."""
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    operator, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    channel = await declare_chat(
        real_session, org_id=org_admin.org_id, owner_user_id=member.id, machine_id=MACHINE
    )
    ent = await load_entitlements(real_session, operator, org_id=operator.home_org_team_id)

    as_machine = await channels.authorize(
        real_session, operator, channel, ent=ent, agent_id=MACHINE
    )
    assert as_machine.can_write is True
    assert as_machine.owner_user_id == member.id, "the owner is unchanged: the machine publishes"

    # The binding is the whole of the box's standing: the same operator as a
    # person, or as any other agent, holds nothing on a colleague's chat and
    # is told it does not exist.
    await _refused(real_session, operator, channel)
    await _refused(real_session, operator, channel, agent_id=OTHER_MACHINE)
    await _refused(real_session, operator, channel, agent_id=channel.doc_id)

    owner_grant = await channels.authorize(
        real_session,
        member,
        channel,
        ent=await load_entitlements(real_session, member, org_id=member.home_org_team_id),
    )
    assert owner_grant.can_write is True, "the owner keeps write"


@pytest.mark.usefixtures("files_on")
async def test_a_chat_bound_to_no_machine_has_no_machine_publisher(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Asserting a machine id adds nothing to a chat that is bound to none: the
    socket answers exactly what the operator's own standing answers — nothing
    at all unshared, and a reader's grant once the chat is shared with them."""
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    operator, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    _chat, channel = await shared_chat(real_session, owner=member)
    await _refused(real_session, operator, channel, agent_id=MACHINE)

    await share_chat_with(
        real_session,
        chat=_chat,
        owner=member,
        principal=Principal(kind="user", id=operator.id),
        role=ROLE_READER,
    )
    grant = await channels.authorize(
        real_session,
        operator,
        channel,
        ent=await load_entitlements(real_session, operator, org_id=operator.home_org_team_id),
        agent_id=MACHINE,
    )
    assert grant.can_write is False


async def test_the_binding_is_the_boxs_whole_standing_and_the_wrong_machine_holds_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box publishes the chat it is bound to whoever signed it in — that is
    the point of the binding, and why the machine branch does not wait on its
    operator's own rights. What it is NOT is a way into a chat bound to some
    other machine: an id that is not this chat's binding is told the chat does
    not exist, whichever member's socket asserts it."""
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    insider, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await membership_service.add_member(
        real_session, team_id=team.id, user_id=insider.id, role=TeamRole.MEMBER
    )
    await real_session.commit()
    outsider, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    channel = await declare_chat(
        real_session,
        org_id=org_admin.org_id,
        owner_user_id=insider.id,
        team_id=team.id,
        machine_id=MACHINE,
    )
    # The bound machine, on any operator's socket: the publisher.
    for operator in (outsider, await _user(real_session, org_admin.admin_id)):
        as_machine = await channels.authorize(
            real_session,
            operator,
            channel,
            ent=await load_entitlements(real_session, operator, org_id=operator.home_org_team_id),
            agent_id=MACHINE,
        )
        assert as_machine.can_write is True
        assert as_machine.owner_user_id == insider.id, "the owner is unchanged"
    # Any other machine id, and the operator's own person: nothing.
    await _refused(real_session, outsider, channel, agent_id=OTHER_MACHINE)
    await _refused(real_session, outsider, channel)


async def test_another_orgs_machine_id_on_a_chat_in_this_org_publishes_nothing_across_orgs(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_support: OrgWithAdmin
) -> None:
    """The org is half the key: a socket of org B asserting the very machine
    id org A's chat is bound to finds no chat in B at all."""
    channel = await declare_chat(
        real_session, org_id=org_admin.org_id, owner_user_id=org_admin.admin_id, machine_id=MACHINE
    )
    outsider = await _user(real_session, platform_support.admin_id)
    with pytest.raises(ChannelError) as info:
        await channels.authorize(
            real_session,
            outsider,
            channel,
            ent=await load_entitlements(real_session, outsider, org_id=outsider.home_org_team_id),
            agent_id=MACHINE,
        )
    assert info.value.code == "not_found"
