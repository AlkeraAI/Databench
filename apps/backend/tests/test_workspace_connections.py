"""A chat uses its WORKSPACE owner's connections, never the person who started it.

Every chat in a workspace runs on the workspace owner's connections. So a box
holding a chat a collaborator started in somebody else's workspace is answered
the OWNER's set on ``GET /chats/{id}/connections`` and leases the OWNER's
credential, while the collaborator's own personal connection never reaches it.
For a workspace of one the owner is the chat's owner, so nothing changes for
any chat that existed before workspaces.

Each case is built so that resolving the chat's own owner would answer
differently: the owner and the collaborator hold different personal
connections, and the assertion names which one came back.
"""

from __future__ import annotations

import secrets
from uuid import UUID

import pytest
from alkera_core.authz import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl as files_acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_WRITER
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from backend.services.chats import chat_service
from backend.services.org import teams as team_service
from backend.services.workspaces import workspace_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member
from tests.test_chat_connections_machine import _audit_rows, _decisions, _effects
from tests.test_machine_principal_routes import _box
from tests.test_personal_connections import _save_personal
from tests.test_workspace_connections_machine import _per_user_row, _sign_in

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

ROUTE = "/api/v1/chats/{chat_id}/connections"
LEASE = "/api/v1/chats/{chat_id}/connections/{connection_id}/credential-lease"


class _People:
    def __init__(self, org_id: UUID, owner: User, collaborator: User) -> None:
        self.org_id = org_id
        self.owner = owner
        self.collaborator = collaborator
        self.owner_connection = ""
        self.collaborator_connection = ""
        self.workspace_id = UUID(int=0)
        self.share_id = UUID(int=0)


async def _people() -> _People:
    """An org with two verified members, each holding a personal connection of
    their own."""
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Workspace Org {secrets.token_hex(4)}",
            admin_email=f"ws-admin-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="Ws",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
        owner, owner_pw = await make_member(session, org_id=org.id, verified=True)
        collaborator, collab_pw = await make_member(session, org_id=org.id, verified=True)
    people = _People(org.id, owner, collaborator)
    for user, password, attr in (
        (owner, owner_pw, "owner_connection"),
        (collaborator, collab_pw, "collaborator_connection"),
    ):
        async with app_client() as client:
            await login(client, user.email, password or "")
            saved = await _save_personal(client, f"pg-{secrets.token_hex(3)}")
            assert saved.status_code in (200, 201), saved.text
            setattr(people, attr, saved.json()["id"])
    return people


async def _workspace_folder(session: AsyncSession, workspace_id: UUID) -> FileNode:
    return (
        await session.execute(
            select(FileNode).where(
                FileNode.target_object_id == workspace_id, FileNode.trashed_at.is_(None)
            )
        )
    ).scalar_one()


async def _grant_on_workspace(
    session: AsyncSession, workspace: WorkspaceObject, owner: User, user_id: UUID, role: str
) -> UUID:
    """Share the workspace's folder with ``user_id`` the way the dialog does."""
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(session, ctx) as repo:
        node = await _workspace_folder(repo.session, workspace.id)
        share = await files_acl.grant(repo, ctx, node, Principal(kind="user", id=user_id), role)
        share_id = share.id
    await session.commit()
    return share_id


async def _revoke_on_workspace(people: _People) -> None:
    async with AsyncSessionLocal() as session:
        owner = await session.get(User, people.owner.id)
        assert owner is not None
        ctx = ActingContext.for_user(
            user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email
        )
        async with team_service.files_transaction(session, ctx) as repo:
            node = await _workspace_folder(repo.session, people.workspace_id)
            await files_acl.revoke(repo, ctx, node, people.share_id)
        await session.commit()


async def _chat_in_owners_workspace(people: _People, *, machine_id: UUID) -> str:
    """A chat the collaborator started in a project workspace the owner owns."""
    async with AsyncSessionLocal() as session:
        owner = await session.get(User, people.owner.id)
        collaborator = await session.get(User, people.collaborator.id)
        assert owner is not None and collaborator is not None
        workspace, _ = await workspace_service.create_project(
            session,
            owner=owner,
            org_id=owner.home_org_team_id,
            title="Shared analysis",
            client_id=None,
        )
        await session.commit()
        people.workspace_id = workspace.id
        people.share_id = await _grant_on_workspace(
            session, workspace, owner, people.collaborator.id, ROLE_WRITER
        )
        chat, _ = await chat_service.create_chat(
            session,
            owner=collaborator,
            org_id=collaborator.home_org_team_id,
            title="Started by the collaborator",
            client_id=None,
            machine_id=str(machine_id),
            machine_status="ready",
            workspace=workspace,
        )
        await session.commit()
        assert chat.owner_user_id == people.collaborator.id
        return str(chat.id)


async def test_the_box_is_answered_the_workspace_owners_connections(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    people = await _people()
    box = await _box(org_admin, tenancy="dedicated", served_org=people.org_id)
    chat_id = await _chat_in_owners_workspace(people, machine_id=box.machine_id)

    resp = await client.get(ROUTE.format(chat_id=chat_id), headers=box.headers)

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()["connections"]}
    assert people.owner_connection in ids
    assert people.collaborator_connection not in ids
    assert _effects(await _decisions(people.org_id, chat_id)) == [
        ("list_connections", "allow", "machine_holds_chat")
    ]


async def test_the_box_leases_the_owners_credential_and_never_the_collaborators(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    people = await _people()
    box = await _box(org_admin, tenancy="dedicated", served_org=people.org_id)
    chat_id = await _chat_in_owners_workspace(people, machine_id=box.machine_id)

    owners = await client.post(
        LEASE.format(chat_id=chat_id, connection_id=people.owner_connection),
        headers=box.headers,
        json={},
    )
    collaborators = await client.post(
        LEASE.format(chat_id=chat_id, connection_id=people.collaborator_connection),
        headers=box.headers,
        json={},
    )

    assert owners.status_code == 200, owners.text
    # The collaborator's row is not the workspace owner's to use: the
    # connector policy, deciding on the owner's entitlement, refuses it.
    assert collaborators.status_code in (403, 404), collaborators.text
    leased = await _audit_rows(people.org_id, "team_connection.credential_leased")
    assert [row.actor_id for row in leased] == [people.owner.id]


async def test_once_the_share_is_revoked_the_chat_falls_back_to_its_own_owner(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The rule is decided on every list and every lease, never trusted off the
    chat: a collaborator who may no longer write the workspace keeps their
    chat, and it runs on their own connections again."""
    people = await _people()
    box = await _box(org_admin, tenancy="dedicated", served_org=people.org_id)
    chat_id = await _chat_in_owners_workspace(people, machine_id=box.machine_id)
    before = await client.get(ROUTE.format(chat_id=chat_id), headers=box.headers)
    assert people.owner_connection in {row["id"] for row in before.json()["connections"]}

    await _revoke_on_workspace(people)

    after = await client.get(ROUTE.format(chat_id=chat_id), headers=box.headers)
    assert after.status_code == 200, after.text
    ids = {row["id"] for row in after.json()["connections"]}
    assert people.owner_connection not in ids
    assert people.collaborator_connection in ids
    leased = await client.post(
        LEASE.format(chat_id=chat_id, connection_id=people.owner_connection),
        headers=box.headers,
        json={},
    )
    assert leased.status_code in (403, 404), leased.text


async def test_a_workspace_of_one_still_uses_the_chats_own_owner(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The unchanged case: a chat in its own workspace of one is answered its
    owner's set, exactly as before workspaces existed."""
    people = await _people()
    box = await _box(org_admin, tenancy="dedicated", served_org=people.org_id)
    async with AsyncSessionLocal() as session:
        collaborator = await session.get(User, people.collaborator.id)
        assert collaborator is not None
        chat, _ = await chat_service.create_chat(
            session,
            owner=collaborator,
            org_id=collaborator.home_org_team_id,
            title="Their own",
            client_id=None,
            machine_id=str(box.machine_id),
            machine_status="ready",
        )
        await session.commit()
        chat_id = str(chat.id)

    resp = await client.get(ROUTE.format(chat_id=chat_id), headers=box.headers)

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()["connections"]}
    assert people.collaborator_connection in ids
    assert people.owner_connection not in ids


async def test_a_shared_workspace_chat_uses_the_owners_per_user_grant_until_the_share_ends(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Sharing a workspace shares the owner's per-user connections too: the
    collaborator's chat leases the OWNER's grant. Revoking the share ends that
    on the very next lease, with no wait on any cache."""
    people = await _people()
    box = await _box(org_admin, tenancy="dedicated", served_org=people.org_id)
    per_user = await _per_user_row(people.org_id, "bq_shared_ws")
    await _sign_in(people.owner, per_user, "owners-grant")
    chat_id = await _chat_in_owners_workspace(people, machine_id=box.machine_id)

    listed = await client.get(ROUTE.format(chat_id=chat_id), headers=box.headers)
    assert per_user in {row["id"] for row in listed.json()["connections"]}
    leased = await client.post(
        LEASE.format(chat_id=chat_id, connection_id=per_user), headers=box.headers, json={}
    )
    assert leased.status_code == 200, leased.text
    assert leased.json()["secret"] == "owners-grant"

    await _revoke_on_workspace(people)

    after = await client.post(
        LEASE.format(chat_id=chat_id, connection_id=per_user), headers=box.headers, json={}
    )
    # The chat now runs on the collaborator's own connections, and the
    # collaborator never signed in to this one: nothing, and never the owner's.
    assert after.status_code == 409, after.text
    assert "owners-grant" not in after.text
