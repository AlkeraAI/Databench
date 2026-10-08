"""A share to the item's owner is refused with a sentence, and adds no row.

The owner typed their own email into a chat's Share dialog, the POST answered
201, and the dialog listed them twice — as Owner and as Can view. Owning is
above every rung a share hands out, so the route refuses it; a share to someone
who already holds a rung moves that rung rather than adding a second row.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from _files_kit import FilesOrgFixture, node_etag
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.files.tree import FileNode
from backend.api.routes.files.sharing import ALREADY_OWNER_OTHER, ALREADY_OWNER_SELF
from httpx import AsyncClient
from sqlalchemy import select, text
from tests.conftest import app_client, login

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


async def _chat_node(client: AsyncClient) -> FileNode:
    response = await client.post(
        "/api/v1/chats", json={"title": "Quarterly review", "clientId": secrets.token_hex(8)}
    )
    assert response.status_code == 201, response.text
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == uuid.UUID(response.json()["id"]),
                    FileNode.subtype == "chat",
                )
            )
        ).scalar_one()
        session.expunge(node)
    return node


async def _share(
    client: AsyncClient, real_session: Any, node: FileNode, user_id: uuid.UUID, role: str
) -> Any:
    return await client.post(
        f"{BASE}/drives/{node.drive_id}/items/{node.id}/permissions",
        json={"principal": {"kind": "user", "id": str(user_id)}, "role": role},
        headers={
            "Idempotency-Key": secrets.token_hex(8),
            "If-Match": await node_etag(real_session, node.id),
        },
    )


async def _rows(node: FileNode, user_id: uuid.UUID) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT role FROM file_shares WHERE node_id = :node AND principal_id = :who"
                " AND revoked_at IS NULL"
            ),
            {"node": node.id, "who": user_id},
        )
        return [str(row[0]) for row in result]


async def test_the_owner_sharing_with_themselves_is_refused_and_adds_no_row(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: Any
) -> None:
    node = await _chat_node(files_client)
    owner = files_org.org.admin_id
    before = await _rows(node, owner)

    refused = await _share(files_client, real_session, node, owner, "reader")

    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "files.already_owner"
    assert body["message"] == ALREADY_OWNER_SELF
    assert await _rows(node, owner) == before


async def test_a_manager_sharing_with_the_owner_is_told_they_already_own_it(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: Any
) -> None:
    node = await _chat_node(files_client)
    member = files_org.member
    granted = await _share(files_client, real_session, node, member.id, "manager")
    assert granted.status_code == 201, granted.text

    async with app_client() as theirs:
        await login(theirs, member.email, files_org.member_password)
        refused = await _share(theirs, real_session, node, files_org.org.admin_id, "writer")

    assert refused.status_code == 409, refused.text
    assert refused.json()["message"] == ALREADY_OWNER_OTHER


async def test_sharing_again_with_someone_who_has_access_moves_their_rung(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: Any
) -> None:
    node = await _chat_node(files_client)
    member = files_org.member
    first = await _share(files_client, real_session, node, member.id, "reader")
    again = await _share(files_client, real_session, node, member.id, "writer")

    assert first.status_code == 201, first.text
    assert again.status_code == 201, again.text
    assert await _rows(node, member.id) == ["writer"]
