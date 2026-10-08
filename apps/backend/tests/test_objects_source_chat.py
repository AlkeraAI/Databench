"""A result created through ``POST /objects`` names its source chat only when
its creator may read that chat.

The source is not decoration. The box that runs the named chat is admitted to
the result and asked for its payload, and the result reads as that chat's
answer. So naming a chat is a read of it, decided by the chat policy: a chat
the creator cannot read, one in another org and one that does not exist all
get the policy's opaque not-found, nothing is saved, and the decision is on
record under the chat.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import EventOutbox, User, WorkspaceObject
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _member(real_session: AsyncSession, org_id: UUID) -> tuple[User, str]:
    member, password = await make_member(real_session, org_id=org_id, verified=True)
    assert password is not None
    return member, password


async def _chat_of(client: AsyncClient, email: str, password: str) -> dict[str, Any]:
    await login(client, email, password)
    created = await client.post("/api/v1/chats", json={"title": "Quarterly review"})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


async def _create_result(client: AsyncClient, source_chat_id: str, client_id: str) -> Any:
    return await client.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "Rows",
            "spec": {"columns": [{"name": "n"}], "source_chat_id": source_chat_id},
            "client_id": client_id,
        },
    )


async def _saved(client_id: str) -> list[WorkspaceObject]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(WorkspaceObject).where(WorkspaceObject.logical_id == client_id)
        )
        return list(rows.scalars().all())


async def _chat_decisions(chat_id: str) -> list[tuple[str, str]]:
    """Every ``authz.decision`` filed under the chat, as (action, effect)."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.type == "authz.decision", EventOutbox.entity_id == chat_id)
            .order_by(EventOutbox.id)
        )
        return [(row.payload["action"], row.payload["effect"]) for row in rows.scalars().all()]


async def _foreign_org_chat(client: AsyncClient) -> str:
    async with AsyncSessionLocal() as session:
        _org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other Org {secrets.token_hex(4)}",
            admin_email=f"other-{secrets.token_hex(4)}@example.com",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="other-pass-12345",
        )
        admin.email_verified_at = datetime.now(UTC)
        email = admin.email
        await session.commit()
    chat = await _chat_of(client, email, "other-pass-12345")
    return str(chat["id"])


async def test_the_creators_own_chat_is_named_and_the_read_is_on_record(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await _member(real_session, org_admin.org_id)
    chat = await _chat_of(client, member.email, password)
    client_id = f"o-{uuid4().hex[:8]}"

    response = await _create_result(client, chat["id"], client_id)

    assert response.status_code == 201, response.text
    assert response.json()["spec"]["source_chat_id"] == chat["id"]
    assert ("read", "allow") in await _chat_decisions(chat["id"])


async def test_a_chat_shared_with_the_creator_can_be_named(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    owner, owner_password = await _member(real_session, org_admin.org_id)
    reader, reader_password = await _member(real_session, org_admin.org_id)
    chat = await _chat_of(client, owner.email, owner_password)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    assert chat_row is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=reader, role=ROLE_READER)
    await login(client, reader.email, reader_password)

    response = await _create_result(client, chat["id"], f"o-{uuid4().hex[:8]}")

    assert response.status_code == 201, response.text
    assert response.json()["spec"]["source_chat_id"] == chat["id"]


async def test_another_members_private_chat_is_not_found_and_nothing_is_saved(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    owner, owner_password = await _member(real_session, org_admin.org_id)
    stranger, stranger_password = await _member(real_session, org_admin.org_id)
    chat = await _chat_of(client, owner.email, owner_password)
    await login(client, stranger.email, stranger_password)
    client_id = f"o-{uuid4().hex[:8]}"

    response = await _create_result(client, chat["id"], client_id)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Not found"
    assert await _saved(client_id) == []
    assert ("read", "deny") in await _chat_decisions(chat["id"])


async def test_another_orgs_chat_is_the_same_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    foreign_chat_id = await _foreign_org_chat(client)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"o-{uuid4().hex[:8]}"

    response = await _create_result(client, foreign_chat_id, client_id)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Not found"
    assert await _saved(client_id) == []
    assert ("read", "deny") in await _chat_decisions(foreign_chat_id)


@pytest.mark.parametrize(
    ("source", "status"),
    [
        pytest.param(lambda: str(uuid4()), 404, id="no-such-chat"),
        pytest.param(lambda: "not-a-chat-id", 422, id="malformed"),
    ],
)
async def test_a_chat_that_does_not_exist_cannot_be_named(
    client: AsyncClient, org_admin: OrgWithAdmin, source: Any, status: int
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"o-{uuid4().hex[:8]}"

    response = await _create_result(client, source(), client_id)

    assert response.status_code == status, response.text
    assert await _saved(client_id) == []


async def test_a_result_with_no_source_chat_is_still_created(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "Rows",
            "spec": {"columns": [{"name": "n"}]},
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["spec"]["source_chat_id"] is None
