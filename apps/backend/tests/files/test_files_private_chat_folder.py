"""A private chat's folder answers an unshared org admin the way the chat does.

Every door onto a chat — the chat route, the object route, the listing, the
socket — refuses an org admin nobody shared the chat with unless the deployment
sets ``CHAT_ORG_ADMIN_READS_PRIVATE``. The folder behind the chat used to answer
from a different rule: the org-admin floor raised the admin to ``manager`` on
every node of the drive, chat folders included, so the same admin could list a
member's working tree, write into it, share it to themselves, download the
files inside it and — through ``…/duplicate``, which takes only ``COPY`` — walk
the transcript out row for row into a drive of their own.

These cases drive the real routes against real Postgres and pin that the one
setting decides the folder too: off, the admin gets the same opaque not-found
the chat gives them, on every Files verb; a rung the owner granted is exactly
the rung they get; on, the folder opens with the chat. Offboarding is the
control — ``DELETE /chats/{id}`` still trashes a member's chat without opening
it, because it never asks the caller's Files facts.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from _files_kit import NOT_FOUND, FilesOrgFixture, node_etag, refusal
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import ChatMessage, EventOutbox, OrgAuditEvent, User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import share_chat
from tests.conftest import app_client, login

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"
OPAQUE = NOT_FOUND


async def _drive_and_home(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["id"]), str(body["homeId"])


async def _new_chat(client: AsyncClient, title: str) -> str:
    response = await client.post(
        "/api/v1/chats", json={"title": title, "clientId": secrets.token_hex(8)}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _node_of(chat_id: str) -> FileNode:
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == uuid.UUID(chat_id),
                    FileNode.trashed_at.is_(None),
                    FileNode.subtype == "chat",
                )
            )
        ).scalar_one()
        session.expunge(node)
    return node


async def _say(session: AsyncSession, chat_id: str, org_id: uuid.UUID, *lines: str) -> None:
    for seq, line in enumerate(lines, start=1):
        session.add(
            ChatMessage(
                chat_id=uuid.UUID(chat_id),
                org_team_id=org_id,
                seq=seq,
                role="user" if seq % 2 else "assistant",
                kind="prompt" if seq % 2 else "message.completed",
                event_id=f"evt:{seq}",
                payload={"text": line},
            )
        )
    chat = await session.get(WorkspaceObject, uuid.UUID(chat_id))
    assert chat is not None
    chat.spec = {**chat.spec, "last_seq": len(lines)}
    await session.commit()


async def _transcript_rows(org_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.org_team_id == org_id)
                )
            ).scalar_one()
        )


async def _decisions_for(org_id: uuid.UUID, node_id: uuid.UUID) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "file_node",
                EventOutbox.entity_id == str(node_id),
            )
            .order_by(EventOutbox.id)
        )
        return [row.payload for row in rows.scalars().all()]


async def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


TITLE = "Quarterly review of the acquisition"
LINES = ("what did legal say", "they want the earn-out capped", "at how much", "twelve")


async def _members_private_chat(
    files_org: FilesOrgFixture, real_session: AsyncSession
) -> tuple[str, FileNode, AsyncClient]:
    """A chat a member started and never shared, with a transcript in it, and
    the member's client left signed in for the checks that need it."""
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    chat_id = await _new_chat(member, TITLE)
    await _say(real_session, chat_id, files_org.org.org_id, *LINES)
    return chat_id, await _node_of(chat_id), member


# -- the shipped default: the folder is as closed as the chat -----------------


async def test_an_unshared_org_admin_gets_the_opaque_not_found_on_a_members_chat_folder(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    _chat_id, node, member = await _members_private_chat(files_org, real_session)
    drive = str(node.drive_id)
    before = await _transcript_rows(files_org.org.org_id)

    item = await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")
    assert item.status_code == 404, item.text
    assert refusal(item) == OPAQUE

    children = await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}/children")
    assert children.status_code == 404, children.text
    assert refusal(children) == OPAQUE

    duplicated = await files_client.post(
        f"{BASE}/drives/{drive}/items/{node.id}/duplicate", json={}, headers=await _idem()
    )
    assert duplicated.status_code == 404, duplicated.text
    assert refusal(duplicated) == OPAQUE

    # The refusals are on record as denials, and nothing walked out: the org
    # holds exactly the rows the member's chat had, and one chat of that title
    # (counted in this org: other modules on the same worker seed the same title).
    rows = await _decisions_for(files_org.org.org_id, node.id)
    assert rows, "every refusal writes a decision row"
    assert {(row["effect"], row["reason"]) for row in rows} == {
        ("deny", "action_not_allowed_unreadable")
    }
    assert await _transcript_rows(files_org.org.org_id) == before
    async with AsyncSessionLocal() as session:
        titled = (
            await session.execute(
                select(func.count())
                .select_from(WorkspaceObject)
                .where(
                    WorkspaceObject.org_team_id == files_org.org.org_id,
                    WorkspaceObject.type == "chat",
                    WorkspaceObject.title == TITLE,
                    WorkspaceObject.deleted_at == 0,
                )
            )
        ).scalar_one()
    assert titled == 1
    await member.aclose()


async def test_the_members_chats_folder_lists_no_private_chat_to_the_admin(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """The admin still browses the member's home — the floor is unchanged
    outside a chat — but the listing is cut by the same decision the by-id
    door makes, so neither the chat folder's id nor its title is in the page."""
    _chat_id, node, member = await _members_private_chat(files_org, real_session)
    async with AsyncSessionLocal() as session:
        chats_folder = (
            await session.execute(
                text("SELECT parent_id FROM file_nodes WHERE id = :id"), {"id": node.id}
            )
        ).scalar_one()

    listed = await files_client.get(f"{BASE}/drives/{node.drive_id}/items/{chats_folder}/children")
    assert listed.status_code == 200, listed.text
    assert str(node.id) not in listed.text
    assert TITLE not in listed.text
    await member.aclose()


async def test_the_admin_cannot_share_a_members_private_chat_to_themselves(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The floor used to hand the admin ``SHARE`` on the folder, and a grant to
    themselves at ``writer`` is a rung on the chat's node — which is exactly
    what ``chat_read_reason`` and the send rung admit. The grant door is the
    same opaque not-found as every other, and no share row appears."""
    _chat_id, node, member = await _members_private_chat(files_org, real_session)
    refused = await files_client.post(
        f"{BASE}/drives/{node.drive_id}/items/{node.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.org.admin_id)}, "role": "writer"},
        headers={**await _idem(), "If-Match": await node_etag(real_session, node.id)},
    )
    assert refused.status_code == 404, refused.text
    assert refusal(refused) == OPAQUE
    async with AsyncSessionLocal() as session:
        shares = (
            await session.execute(
                text(
                    "SELECT count(*) FROM file_shares WHERE node_id = :node AND revoked_at IS NULL"
                ),
                {"node": node.id},
            )
        ).scalar_one()
    assert shares == 0
    await member.aclose()


async def test_a_rung_the_owner_granted_is_exactly_the_rung_the_admin_gets(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """Shared at "Can view", the admin reads the folder and may copy it — the
    same as any colleague at that rung — and is NOT raised to ``manager`` on
    it: creating a file inside is the coded refusal a reader gets, not a 201."""
    chat_id, node, member = await _members_private_chat(files_org, real_session)
    chat = await real_session.get(WorkspaceObject, uuid.UUID(chat_id))
    assert chat is not None
    owner = await real_session.get(User, files_org.member.id)
    admin = await real_session.get(User, files_org.org.admin_id)
    assert owner is not None and admin is not None
    await share_chat(real_session, chat=chat, owner=owner, user=admin, role=ROLE_READER)
    drive = str(node.drive_id)

    item = await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")
    assert item.status_code == 200, item.text

    write = await files_client.post(
        f"{BASE}/drives/{drive}/items/{node.id}/children",
        json={"name": "admin-wrote-here", "kind": "folder"},
        headers=await _idem(),
    )
    assert write.status_code == 403, write.text
    assert write.json()["code"] == "files.forbidden"

    duplicated = await files_client.post(
        f"{BASE}/drives/{drive}/items/{node.id}/duplicate", json={}, headers=await _idem()
    )
    assert duplicated.status_code == 201, duplicated.text
    await member.aclose()


# -- the deployment that opens the chat opens its folder too ------------------


async def test_the_setting_that_opens_the_chat_opens_its_folder(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One input. With ``CHAT_ORG_ADMIN_READS_PRIVATE`` on, the admin reads the
    chat through every door, and the folder follows: the floor applies and
    the duplicate lands, transcript and all."""
    monkeypatch.setattr(settings, "chat_org_admin_reads_private", True)
    _chat_id, node, member = await _members_private_chat(files_org, real_session)
    before = await _transcript_rows(files_org.org.org_id)

    duplicated = await files_client.post(
        f"{BASE}/drives/{node.drive_id}/items/{node.id}/duplicate", json={}, headers=await _idem()
    )
    assert duplicated.status_code == 201, duplicated.text
    assert duplicated.json()["chatId"]
    assert await _transcript_rows(files_org.org.org_id) == before + len(LINES)
    await member.aclose()


# -- the controls -------------------------------------------------------------


async def _audit(org_id: Any, action: str) -> list[tuple[str | None, str | None]]:
    """``(target, owner)`` of each org-chain event of ``action``."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.action == action
            )
        )
        return [(row.target, (row.detail or {}).get("owner_user_id")) for row in rows.scalars()]


async def _delete_decisions(org_id: Any, chat_id: str) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity_id == chat_id,
            )
            .order_by(EventOutbox.id)
        )
        return [
            (row.payload["effect"], row.payload["reason"])
            for row in rows.scalars()
            if row.payload["action"] == "delete"
        ]


async def test_offboarding_still_deletes_a_members_private_chat(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """While the member is active their private chat is the same not-found to
    an org admin on DELETE as everywhere else. Once they are offboarded
    (deactivated), the admin deletes it without reading it: the folder is
    trashed under the object's own context, not the caller's Files facts, so
    closing the folder to the admin does not close offboarding."""
    chat_id, node, member = await _members_private_chat(files_org, real_session)
    await member.aclose()

    refused = await files_client.delete(f"/api/v1/chats/{chat_id}")
    assert refused.status_code == 404, refused.text
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE users SET is_active = false WHERE id = :id"),
            {"id": files_org.member.id},
        )
        await session.commit()

    deleted = await files_client.delete(f"/api/v1/chats/{chat_id}")

    assert deleted.status_code == 204, deleted.text
    assert await _delete_decisions(files_org.org.org_id, chat_id) == [
        ("deny", "not_in_audience"),
        ("allow", "org_admin_offboards"),
    ]
    assert await _audit(files_org.org.org_id, "chat.offboarded") == [
        (chat_id, str(files_org.member.id))
    ], "the offboarding is on the org's audit chain, naming whose chat it was"
    async with AsyncSessionLocal() as session:
        trashed_at = (
            await session.execute(
                text("SELECT trashed_at FROM file_nodes WHERE id = :id"), {"id": node.id}
            )
        ).scalar_one()
    assert trashed_at is not None


async def test_the_admins_own_chat_folder_is_theirs_as_before(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    chat_id = await _new_chat(files_client, "Board prep")
    await _say(real_session, chat_id, files_org.org.org_id, *LINES)
    node = await _node_of(chat_id)

    item = await files_client.get(f"{BASE}/drives/{node.drive_id}/items/{node.id}")
    assert item.status_code == 200, item.text
    duplicated = await files_client.post(
        f"{BASE}/drives/{node.drive_id}/items/{node.id}/duplicate", json={}, headers=await _idem()
    )
    assert duplicated.status_code == 201, duplicated.text


async def test_a_plain_folder_in_a_members_home_still_answers_the_admin(
    files_client: AsyncClient, files_org: FilesOrgFixture
) -> None:
    """The floor is narrowed to chat folders and nothing else: a member's
    ordinary folder is still the admin's to browse, as the drive has always
    promised an org admin."""
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    drive, home = await _drive_and_home(member)
    made = await member.post(
        f"{BASE}/drives/{drive}/items/{home}/children",
        json={"name": "expenses", "kind": "folder"},
        headers=await _idem(),
    )
    assert made.status_code == 201, made.text
    await member.aclose()

    seen = await files_client.get(f"{BASE}/drives/{drive}/items/{made.json()['id']}")
    assert seen.status_code == 200, seen.text
