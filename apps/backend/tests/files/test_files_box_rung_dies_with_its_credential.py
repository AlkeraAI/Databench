"""The box's own rung on the chat it runs dies with its machine credential.

Composes the two rules: a proven machine holds a rung of its own on the
folder of a chat bound to it, and a platform box whose credential was
revoked is no machine at all. A revoked box must not keep the rung.
"""

from __future__ import annotations

import secrets
import uuid

import pytest
from _files_kit import FilesOrgFixture
from _live_holder import MockHolder
from alkera_core.authz.headers import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.files.tree import FileNode
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client, login
from tests.files._boxes import registered_box

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


async def test_a_revoked_platform_box_loses_the_rung_on_its_own_chat(
    files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
        platform=True,
    )
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    made = await member.post(
        "/api/v1/chats", json={"title": "Pricing review", "clientId": secrets.token_hex(8)}
    )
    assert made.status_code == 201, made.text
    chat_id = uuid.UUID(str(made.json()["id"]))
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == chat_id, FileNode.subtype == "chat"
                )
            )
        ).scalar_one()
        await session.execute(
            text(
                "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                "to_jsonb(CAST(:m AS text))) WHERE id = :id"
            ),
            {"id": chat_id, "m": machine_id},
        )
        await session.commit()
    item = f"/api/v1/files/drives/{node.drive_id}/items/{node.id}"
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as box:
        standing = await box.get(item)
        assert standing.status_code == 200, standing.text

        await real_session.execute(
            text("UPDATE machine_credentials SET revoked_at = now() WHERE machine_id = :m"),
            {"m": uuid.UUID(machine_id)},
        )
        await real_session.commit()

        after = await box.get(item)
        assert after.status_code in {401, 404}, after.text
        taken = await MockHolder(box, node.drive_id, node.id, machine=machine_id).take(
            real_session, lambda: _idem(), purpose="chat"
        )
        assert taken.status_code in {401, 404}, taken.text
    await member.aclose()
