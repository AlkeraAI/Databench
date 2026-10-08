"""The box asks once more, before it starts a turn, whether the author may send.

A message is admitted when it is sent; the box picks it up later. A share
revoked in between used to leave that message to run once. The box now asks
``GET /chats/{id}/send-admission`` with the author, and the answer is the send
rule's own over the author's facts at that moment.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz import ActingContext
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.files import acl as files_acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member
from tests.test_chat_send_rung import _decisions
from tests.test_chats_api import _registered_machine

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _grant(db: Any, chat: WorkspaceObject, owner: User, member: User, role: str) -> Any:
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(db, ctx) as repo:
        node = (
            await repo.session.execute(select(FileNode).where(FileNode.target_object_id == chat.id))
        ).scalar_one()
        share = await files_acl.grant(repo, ctx, node, Principal(kind="user", id=member.id), role)
        share_id = share.id
    await db.commit()
    return share_id


async def _revoke(db: Any, chat: WorkspaceObject, owner: User, share_id: Any) -> None:
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(db, ctx) as repo:
        node = (
            await repo.session.execute(select(FileNode).where(FileNode.target_object_id == chat.id))
        ).scalar_one()
        await files_acl.revoke(repo, ctx, node, share_id)
    await db.commit()


async def test_a_share_revoked_between_the_send_and_the_pickup_stops_the_turn(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-adm-1")
    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=box)
    assert beat.status_code in (200, 204), beat.text
    async with app_client() as person:
        await login(person, org_admin.admin_email, org_admin.admin_password)
        created = await person.post("/api/v1/chats", json={"title": "Shared"})
    assert created.status_code == 201, created.text
    chat_id = created.json()["id"]
    owner = await real_session.get(User, org_admin.admin_id)
    chat = await real_session.get(WorkspaceObject, UUID(chat_id))
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    share_id = await _grant(real_session, chat, owner, member, ROLE_WRITER)
    route = f"/api/v1/chats/{chat_id}/send-admission"

    # While the member may edit, the box is told the turn may run.
    before = await client.get(route, params={"user_id": str(member.id)}, headers=box)
    assert before.status_code == 200, before.text
    assert before.json() == {"allowed": True, "code": None, "message": None}
    # An allow files nothing: the send was filed when it was made.
    assert await _decisions(org_admin.org_id, chat.id) == []

    await _revoke(real_session, chat, owner, share_id)
    after = await client.get(route, params={"user_id": str(member.id)}, headers=box)
    assert after.status_code == 200, after.text
    assert after.json()["allowed"] is False
    assert after.json()["message"]
    # The refusal is on record like a refused send.
    assert [(a, e) for a, e, _ in await _decisions(org_admin.org_id, chat.id)] == [("send", "deny")]
    # Narrowed to view is the same answer: viewing never drives the agent.
    await _grant(real_session, chat, owner, member, ROLE_READER)
    viewer = (await client.get(route, params={"user_id": str(member.id)}, headers=box)).json()
    assert viewer["allowed"] is False
    assert viewer["code"] == chat_policy.SEND_DENIED_CODE
    # The owner is still admitted, and a stranger to the org is not an author.
    mine = await client.get(route, params={"user_id": str(org_admin.admin_id)}, headers=box)
    assert mine.json()["allowed"] is True


async def test_only_the_chats_machine_may_ask(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await client.post("/api/v1/chats", json={"title": "Private"})
    chat_id = created.json()["id"]
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        asked = await theirs.get(
            f"/api/v1/chats/{chat_id}/send-admission", params={"user_id": str(member.id)}
        )
    assert asked.status_code in (403, 404), asked.text
    owner_asks = await client.get(
        f"/api/v1/chats/{chat_id}/send-admission", params={"user_id": str(org_admin.admin_id)}
    )
    assert owner_asks.status_code in (403, 404), "a person is not the chat's machine"
