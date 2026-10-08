"""The box running a chat holds a rung of its own on the chat's folder.

The org-admin floor stops at a chat's folder, and a box's reach used to arrive
through it: the org's box is operated by an admin, so a member's chat folder
was the box's to lease, pull and write only because it was the admin's to
open. With the floor closed for people, the box's rung comes from the one fact
that says which box runs a chat — the chat row's machine binding — and from
nothing its operator holds. Every case below is a member's own chat, made
through ``POST /chats`` and filed in their home.

What each caller gets:

* the box the chat is bound to leases, reads and writes the folder;
* a chat bound to no machine is nobody's: the binding is written when a box
  takes the chat, before it touches the folder, and a deleted chat's restored
  folder can never be bound;
* a box a plain member registered proves nothing and runs no chat;
* a box of the same org that the chat is NOT bound to gets the opaque 404;
* the operator's own session naming the box — an assertion nobody verified —
  gets the opaque 404;
* the unshared org admin gets the opaque 404, and the owner keeps everything.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, FilesOrgFixture, refusal
from _live_holder import MockHolder
from alkera_core.authz.headers import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from alkera_core.models.files.tree import FileNode
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client, login, make_member
from tests.files._boxes import registered_box

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"
OPAQUE = NOT_FOUND


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


@pytest_asyncio.fixture
async def box(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> AsyncIterator[tuple[AsyncClient, str]]:
    """The org's box, operated by the org admin, on its own box credential."""
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as client:
        yield client, machine_id


class MemberChat:
    def __init__(self, chat_id: str, node: FileNode, member: AsyncClient) -> None:
        self.chat_id = chat_id
        self.node = node
        self.drive = node.drive_id
        self.member = member


@pytest_asyncio.fixture
async def members_chat(files_org: FilesOrgFixture) -> AsyncIterator[MemberChat]:
    """A member's private chat, made through the route and filed where the
    product files it: the member's home."""
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    made = await member.post(
        "/api/v1/chats", json={"title": "Pricing review", "clientId": secrets.token_hex(8)}
    )
    assert made.status_code == 201, made.text
    chat_id = str(made.json()["id"])
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == uuid.UUID(chat_id),
                    FileNode.subtype == "chat",
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalar_one()
        session.expunge(node)
    yield MemberChat(chat_id, node, member)
    await member.aclose()


async def _bind(chat_id: str, machine: str | None) -> None:
    """Set the chat row's machine binding, as placement and rebinding do."""
    async with AsyncSessionLocal() as session:
        if machine is None:
            await session.execute(
                text("UPDATE workspace_objects SET spec = spec - 'machine_id' WHERE id = :id"),
                {"id": uuid.UUID(chat_id)},
            )
        else:
            await session.execute(
                text(
                    "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                    "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
                ),
                {"id": uuid.UUID(chat_id), "machine": machine},
            )
        await session.commit()


async def _decisions(org_id: uuid.UUID, node_id: uuid.UUID) -> list[dict[str, Any]]:
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
        return [dict(row.payload) for row in rows.scalars().all()]


def _item(chat: MemberChat) -> str:
    return f"{BASE}/drives/{chat.drive}/items/{chat.node.id}"


async def test_the_box_the_chat_is_bound_to_leases_reads_and_writes_its_folder(
    box: tuple[AsyncClient, str],
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    client, machine_id = box
    await _bind(members_chat.chat_id, machine_id)

    read = await client.get(_item(members_chat))
    assert read.status_code == 200, read.text
    # Running the chat, and nothing a person decides about it: the rung is
    # the box's own, not the admin floor its operator holds elsewhere.
    capabilities = read.json()["capabilities"]
    assert capabilities["can_lease"] is True
    assert capabilities["can_share"] is False
    assert capabilities["can_lease_force"] is False
    assert capabilities["can_purge"] is False

    holder = MockHolder(client, members_chat.drive, members_chat.node.id, machine=machine_id)
    taken = await holder.take(real_session, lambda: _idem(), purpose="chat")
    assert taken.status_code == 200, taken.text

    written = await client.post(
        f"{_item(members_chat)}/children",
        json={"name": "outputs", "kind": "folder"},
        headers={**_idem(), **holder.fence},
    )
    assert written.status_code == 201, written.text

    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert ("lease", "allow") in {(row["action"], row["effect"]) for row in rows}
    assert not [row for row in rows if row["effect"] == "deny"], rows


async def test_a_box_the_chat_is_not_bound_to_is_told_it_does_not_exist(
    box: tuple[AsyncClient, str],
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """Same org, same operator, proven machine — and another box's chat."""
    client, machine_id = box
    await _bind(members_chat.chat_id, str(uuid.uuid4()))

    read = await client.get(_item(members_chat))
    assert read.status_code == 404, read.text
    assert refusal(read) == OPAQUE
    taken = await MockHolder(
        client, members_chat.drive, members_chat.node.id, machine=machine_id
    ).take(real_session, lambda: _idem(), purpose="chat")
    assert taken.status_code == 404, taken.text
    assert refusal(taken) == OPAQUE

    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert {row["effect"] for row in rows} == {"deny"}
    assert {"read", "lease"} <= {row["action"] for row in rows}


async def test_a_box_holds_no_rung_on_a_chat_no_box_has_been_bound_to(
    box: tuple[AsyncClient, str],
    members_chat: MemberChat,
    real_session: AsyncSession,
) -> None:
    """The binding is written when a box takes the chat, before the box
    touches the folder, so a folder with no binding is nobody's to lease. The
    same shape covers a restored folder of a deleted chat, which can never be
    bound and used to read as "no machine yet"."""
    client, machine_id = box
    await _bind(members_chat.chat_id, None)

    read = await client.get(_item(members_chat))
    assert read.status_code == 404, read.text
    assert refusal(read) == OPAQUE
    taken = await MockHolder(
        client, members_chat.drive, members_chat.node.id, machine=machine_id
    ).take(real_session, lambda: _idem(), purpose="chat")
    assert taken.status_code == 404, taken.text
    assert refusal(taken) == OPAQUE


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound-to-it"])
async def test_a_box_no_admin_stands_behind_runs_no_chat(
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    bound: bool,
) -> None:
    """A box a plain member registered is a registration, not a machine the
    org's chats are put on: it proves nothing, so neither a chat bound to no
    box nor one bound to it by name opens to it."""
    operator, _ = await make_member(real_session, org_id=files_org.org.org_id)
    token, machine_id = await registered_box(
        real_session, user_id=operator.id, email=operator.email, org_id=files_org.org.org_id
    )
    await _bind(members_chat.chat_id, machine_id if bound else None)
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as client:
        read = await client.get(_item(members_chat))

    assert read.status_code == 404, read.text
    assert refusal(read) == OPAQUE


async def test_the_operators_own_session_naming_the_box_is_not_the_box(
    box: tuple[AsyncClient, str],
    members_chat: MemberChat,
    files_client: AsyncClient,
) -> None:
    """The assertion is two headers anybody can send; the box's id is public.
    On the admin's cookie session nobody verified it, so it earns no rung."""
    _, machine_id = box
    await _bind(members_chat.chat_id, machine_id)

    read = await files_client.get(_item(members_chat), headers=agent_headers(machine_id))

    assert read.status_code == 404, read.text
    assert refusal(read) == OPAQUE


async def test_the_unshared_admin_and_the_owner_are_unchanged(
    box: tuple[AsyncClient, str],
    members_chat: MemberChat,
    files_client: AsyncClient,
) -> None:
    """The box's rung is the box's: binding the chat to the admin's box opens
    nothing to the admin in person, and takes nothing from the owner."""
    _, machine_id = box
    await _bind(members_chat.chat_id, machine_id)

    admin = await files_client.get(_item(members_chat))
    assert admin.status_code == 404, admin.text
    assert refusal(admin) == OPAQUE

    owner = await members_chat.member.get(_item(members_chat))
    assert owner.status_code == 200, owner.text
    assert owner.json()["capabilities"]["can_purge"] is True
