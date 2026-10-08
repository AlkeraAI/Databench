"""``can_delete`` on a chat read is the delete route's own answer, ahead of time.

Deleting a chat is its owner's or an org admin's, never a rung a share grants,
so a colleague holding the chat at any rung must not be offered the control —
and the flag is asserted against what the DELETE actually does for the same
caller, so it cannot drift into a second opinion.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.files.authz.ladder import ROLE_MANAGER, ROLE_READER, ROLE_WRITER
from alkera_core.models import User, WorkspaceObject
from httpx import AsyncClient
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _create_chat(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json={"title": "Quarterly review"})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize(
    "role",
    [
        pytest.param(ROLE_READER, id="shared-can-view"),
        pytest.param(ROLE_WRITER, id="shared-can-edit"),
        pytest.param(ROLE_MANAGER, id="shared-full-access"),
    ],
)
async def test_a_shared_reader_is_told_they_may_not_delete_and_the_delete_agrees(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any, role: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=role)

    async with app_client() as other:
        await login(other, member.email, password)
        single = await other.get(f"/api/v1/chats/{chat['id']}")
        listed = await other.get("/api/v1/chats")
        deleted = await other.delete(f"/api/v1/chats/{chat['id']}")

    assert single.json()["can_delete"] is False
    rows = {row["id"]: row for row in listed.json()["items"]}
    assert rows[chat["id"]]["can_delete"] is False
    assert deleted.status_code == 403, deleted.text
    # The refusal is a sentence a person can read, not a bare "Not allowed".
    assert deleted.json()["error"]["message"] == chat_policy.DELETE_DENIED_MESSAGE
    # The chat is still there for its owner.
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 200


async def test_the_owner_is_told_they_may_delete_and_the_delete_agrees(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    single = await client.get(f"/api/v1/chats/{chat['id']}")
    listed = await client.get("/api/v1/chats")

    assert chat["can_delete"] is True
    assert single.json()["can_delete"] is True
    rows = {row["id"]: row for row in listed.json()["items"]}
    assert rows[chat["id"]]["can_delete"] is True
    deleted = await client.delete(f"/api/v1/chats/{chat['id']}")
    assert deleted.status_code in (200, 202, 204), deleted.text
