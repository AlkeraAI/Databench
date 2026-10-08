"""A copy of a folder may not carry out a conversation the copier cannot open.

The floor stops at a chat's folder for every direct verb, but a copy decided
on the PARENT walked the subtree with no further question: an org admin nobody
shared the chat with could duplicate a member's ``Chats`` folder, copy their
whole home, or batch-copy either, and the private chat came along — adopted
into the admin's own home with its transcript copied row for row. The archive
of the same parent is the same subtree walk with a different verb.

Every chat folder beneath the copied parent is now decided on its own, by the
decider the by-id door uses, and one the caller may not read refuses the whole
operation with the copy refusal, on record. The positive twins say what still
copies: the owner's own ``Chats`` folder with every conversation in it, the
same folder for an admin once the deployment opens chats to admins, and a
folder shared to a reader whose chats they read by inheritance.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from _files_kit import NOT_FOUND, FilesFixtures, FilesOrgFixture, node_etag, refusal
from alkera_core.authz.policies.files import COPY_REFUSED
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ChatMessage, EventOutbox, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"
OPAQUE = NOT_FOUND
TITLE = "Quarterly review of the acquisition"
LINES = ("what did legal say", "they want the earn-out capped", "at how much", "twelve")


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


async def _drive_and_home(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"]), str(response.json()["homeId"])


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


async def _parent_of(node_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as session:
        return str(
            (
                await session.execute(
                    text("SELECT parent_id FROM file_nodes WHERE id = :id"), {"id": node_id}
                )
            ).scalar_one()
        )


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


async def _chats_titled(org_id: uuid.UUID, title: str) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(WorkspaceObject)
                    .where(
                        WorkspaceObject.org_team_id == org_id,
                        WorkspaceObject.type == "chat",
                        WorkspaceObject.title == title,
                        WorkspaceObject.deleted_at == 0,
                    )
                )
            ).scalar_one()
        )


async def _chat_folders_under(node_id: str) -> int:
    """How many chat folders live anywhere under ``node_id``."""
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM file_nodes WHERE trashed_at IS NULL "
                        "AND drive_id = (SELECT drive_id FROM file_nodes WHERE id = :root) "
                        "AND name_display LIKE '%.alkerachat' AND path_ids <@ "
                        "(SELECT path_ids FROM file_nodes WHERE id = :root)"
                    ),
                    {"root": node_id},
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
        return [dict(row.payload) for row in rows.scalars().all()]


class Scene:
    """A member's private chat with a transcript, and the folders around it."""

    def __init__(self, chat_id: str, node: FileNode, chats_folder: str, home: str) -> None:
        self.chat_id = chat_id
        self.node = node
        self.chats_folder = chats_folder
        self.home = home
        self.drive = str(node.drive_id)


async def _members_scene(
    files_org: FilesOrgFixture, real_session: AsyncSession
) -> tuple[Scene, AsyncClient]:
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    chat_id = await _new_chat(member, TITLE)
    await _say(real_session, chat_id, files_org.org.org_id, *LINES)
    node = await _node_of(chat_id)
    chats_folder = await _parent_of(node.id)
    home = await _parent_of(uuid.UUID(chats_folder))
    return Scene(chat_id, node, chats_folder, home), member


async def _assert_nothing_walked_out(
    files_org: FilesOrgFixture, scene: Scene, admin_home: str, rows_before: int
) -> None:
    assert await _transcript_rows(files_org.org.org_id) == rows_before
    assert await _chats_titled(files_org.org.org_id, TITLE) == 1
    assert await _chat_folders_under(admin_home) == 0
    reasons = {
        (row["action"], row["effect"], row["reason"])
        for row in await _decisions_for(files_org.org.org_id, scene.node.id)
    }
    assert reasons, "the refusal is filed on the chat that caused it"
    assert all(effect == "deny" for _, effect, _ in reasons), reasons


# -- the unshared org admin, at the shipped default ---------------------------


async def test_duplicating_a_members_chats_folder_is_refused(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    scene, member = await _members_scene(files_org, real_session)
    _, admin_home = await _drive_and_home(files_client)
    before = await _transcript_rows(files_org.org.org_id)

    refused = await files_client.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.chats_folder}/duplicate",
        json={},
        headers=_idem(),
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == COPY_REFUSED
    await _assert_nothing_walked_out(files_org, scene, admin_home, before)
    assert ("copy", "deny", "action_not_allowed_unreadable") in {
        (row["action"], row["effect"], row["reason"])
        for row in await _decisions_for(files_org.org.org_id, scene.node.id)
    }
    await member.aclose()


async def test_copying_a_members_home_is_refused(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    scene, member = await _members_scene(files_org, real_session)
    _, admin_home = await _drive_and_home(files_client)
    before = await _transcript_rows(files_org.org.org_id)

    refused = await files_client.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.home}/copy",
        json={"parentId": admin_home},
        headers=_idem(),
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == COPY_REFUSED
    await _assert_nothing_walked_out(files_org, scene, admin_home, before)
    await member.aclose()


async def test_a_batch_copy_of_a_members_chats_folder_is_refused_per_row(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    scene, member = await _members_scene(files_org, real_session)
    _, admin_home = await _drive_and_home(files_client)
    before = await _transcript_rows(files_org.org.org_id)

    response = await files_client.post(
        f"{BASE}/drives/{scene.drive}/bulk",
        json={
            "items": [
                {"id": "c", "op": "copy", "itemId": scene.chats_folder, "parentId": admin_home}
            ]
        },
        headers=_idem(),
    )

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["responses"]}
    assert rows["c"]["status"] == 403, rows
    assert rows["c"]["body"]["code"] == COPY_REFUSED
    await _assert_nothing_walked_out(files_org, scene, admin_home, before)
    await member.aclose()


@pytest.mark.parametrize("which", ["chats_folder", "home"])
async def test_an_archive_of_a_parent_holding_a_members_chat_is_refused(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    which: str,
) -> None:
    """The archive is the same subtree walk with ``EXPORT`` for a verb. Its
    walk prunes what it may not read, but the pruned entry would still name
    the chat's id in the archive's own manifest — so the archive of a parent
    holding a conversation the caller cannot open is refused before it is
    minted, like the copy."""
    scene, member = await _members_scene(files_org, real_session)
    _, admin_home = await _drive_and_home(files_client)
    parent = getattr(scene, which)

    refused = await files_client.post(
        f"{BASE}/drives/{scene.drive}/items/{parent}/download",
        headers={**_idem(), "If-Match": await node_etag(real_session, uuid.UUID(parent))},
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == COPY_REFUSED
    reasons = {
        (row["action"], row["effect"])
        for row in await _decisions_for(files_org.org.org_id, scene.node.id)
    }
    assert ("export", "deny") in reasons
    assert await _chat_folders_under(admin_home) == 0
    await member.aclose()


async def test_a_tree_planted_into_a_members_chat_folder_is_the_opaque_not_found(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """``POST …/tree`` reads nothing — it creates a folder skeleton under a
    parent decided as a write — and a chat folder the admin may not open is
    not a parent they may write into either."""
    scene, member = await _members_scene(files_org, real_session)

    planted = await files_client.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.node.id}/tree",
        json={"paths": ["notes/drafts"]},
        headers=_idem(),
    )

    assert planted.status_code == 404, planted.text
    assert refusal(planted) == OPAQUE
    await member.aclose()


# -- the positive twins: what still copies whole --------------------------------


async def test_the_owner_duplicates_their_own_chats_folder_with_every_conversation(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    scene, member = await _members_scene(files_org, real_session)
    before = await _transcript_rows(files_org.org.org_id)

    duplicated = await member.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.chats_folder}/duplicate",
        json={},
        headers=_idem(),
    )

    assert duplicated.status_code == 201, duplicated.text
    assert await _transcript_rows(files_org.org.org_id) == before + len(LINES)
    assert await _chats_titled(files_org.org.org_id, TITLE) == 2
    await member.aclose()


async def test_the_setting_that_opens_the_chat_lets_the_admin_copy_the_folder_above_it(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "chat_org_admin_reads_private", True)
    scene, member = await _members_scene(files_org, real_session)
    _, admin_home = await _drive_and_home(files_client)
    before = await _transcript_rows(files_org.org.org_id)

    duplicated = await files_client.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.chats_folder}/duplicate",
        json={},
        headers=_idem(),
    )

    assert duplicated.status_code == 201, duplicated.text
    assert await _transcript_rows(files_org.org.org_id) == before + len(LINES)
    assert await _chat_folders_under(admin_home) >= 1
    await member.aclose()


async def test_a_reader_copies_a_shared_folder_and_the_chat_they_read_by_inheritance(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """Sharing a folder shares what is inside it: a chat the owner moved into
    a folder shared at "Can view" is readable to the reader through the
    folder's grant, so the reader's copy of the folder carries it. The rule
    refuses only what the copier cannot open."""
    drive, admin_home = await _drive_and_home(files_client)
    shelf = await files_client.post(
        f"{BASE}/drives/{drive}/items/{admin_home}/children",
        json={"name": "shelf", "kind": "folder"},
        headers=_idem(),
    )
    assert shelf.status_code == 201, shelf.text
    shelf_id = str(shelf.json()["id"])
    chat_id = await _new_chat(files_client, "Board prep")
    await _say(real_session, chat_id, files_org.org.org_id, *LINES)
    node = await _node_of(chat_id)
    moved = await files_client.patch(
        f"{BASE}/drives/{drive}/items/{node.id}",
        json={"parentId": shelf_id},
        headers={**_idem(), "If-Match": await node_etag(real_session, node.id)},
    )
    assert moved.status_code == 200, moved.text
    shared = await files_client.post(
        f"{BASE}/drives/{drive}/items/{shelf_id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers={**_idem(), "If-Match": await node_etag(real_session, uuid.UUID(shelf_id))},
    )
    assert shared.status_code == 201, shared.text

    member = await login(app_client(), files_org.member.email, files_org.member_password)
    _, member_home = await _drive_and_home(member)
    copied = await member.post(
        f"{BASE}/drives/{drive}/items/{shelf_id}/copy",
        json={"parentId": member_home},
        headers=_idem(),
    )

    assert copied.status_code == 202, copied.text
    assert await _chat_folders_under(member_home) == 1
    await member.aclose()


# -- every door, for the two callers the chat admits ------------------------------

#: The four subtree doors, the status each answers when it goes ahead, and the
#: verb its decision row records on the folder it was aimed at.
DOORS = [
    pytest.param("duplicate", 201, "copy", id="duplicate"),
    pytest.param("copy", 202, "copy", id="copy"),
    pytest.param("batch", 202, "copy", id="batch"),
    pytest.param("archive", 202, "export", id="archive"),
]


async def _through(
    door: str, client: AsyncClient, drive: str, source: str, into: str, session: AsyncSession
) -> int:
    """Send one subtree door at ``source`` and return the status it answered
    (for the batch, the row's own)."""
    if door == "duplicate":
        response = await client.post(
            f"{BASE}/drives/{drive}/items/{source}/duplicate",
            json={"destinationId": into},
            headers=_idem(),
        )
    elif door == "copy":
        response = await client.post(
            f"{BASE}/drives/{drive}/items/{source}/copy",
            json={"parentId": into},
            headers=_idem(),
        )
    elif door == "batch":
        response = await client.post(
            f"{BASE}/drives/{drive}/bulk",
            json={"items": [{"id": "c", "op": "copy", "itemId": source, "parentId": into}]},
            headers=_idem(),
        )
        assert response.status_code == 200, response.text
        return int({row["id"]: row for row in response.json()["responses"]}["c"]["status"])
    else:
        response = await client.post(
            f"{BASE}/drives/{drive}/items/{source}/download",
            headers={**_idem(), "If-Match": await node_etag(session, uuid.UUID(source))},
        )
    assert response.status_code < 500, response.text
    return response.status_code


async def _folder(client: AsyncClient, drive: str, parent: str, name: str) -> str:
    made = await client.post(
        f"{BASE}/drives/{drive}/items/{parent}/children",
        json={"name": name, "kind": "folder"},
        headers=_idem(),
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


async def _allowed(org_id: uuid.UUID, node_id: str, action: str) -> bool:
    return any(
        row["action"] == action and row["effect"] == "allow"
        for row in await _decisions_for(org_id, uuid.UUID(node_id))
    )


@pytest.mark.parametrize(("door", "status", "action"), DOORS)
async def test_the_owner_takes_their_own_chats_folder_through_every_door(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    door: str,
    status: int,
    action: str,
) -> None:
    scene, member = await _members_scene(files_org, real_session)
    backup = await _folder(member, scene.drive, scene.home, "backup")

    answered = await _through(door, member, scene.drive, scene.chats_folder, backup, real_session)

    assert answered == status
    assert await _allowed(files_org.org.org_id, scene.chats_folder, action)
    assert not any(
        row["effect"] == "deny" for row in await _decisions_for(files_org.org.org_id, scene.node.id)
    ), "nothing inside the owner's own folder was refused"
    await member.aclose()


async def _share_the_chat_with_the_admin(
    files_org: FilesOrgFixture, member: AsyncClient, scene: Scene, session: AsyncSession
) -> None:
    shared = await member.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.node.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.org.admin_id)}, "role": "reader"},
        headers={**_idem(), "If-Match": await node_etag(session, scene.node.id)},
    )
    assert shared.status_code == 201, shared.text


@pytest.mark.parametrize(("door", "status", "action"), DOORS)
async def test_an_admin_the_chat_was_shared_with_at_can_view_copies_the_folder_above_it(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    door: str,
    status: int,
    action: str,
) -> None:
    """The refusal is about the chat, not about the admin: once the member
    shares the conversation at "Can view", it is one the admin may read, and
    every door over the folder above it goes ahead — copy and export being on
    the reader rung."""
    scene, member = await _members_scene(files_org, real_session)
    await _share_the_chat_with_the_admin(files_org, member, scene, real_session)
    drive, admin_home = await _drive_and_home(files_client)

    answered = await _through(
        door, files_client, drive, scene.chats_folder, admin_home, real_session
    )

    assert answered == status
    assert await _allowed(files_org.org.org_id, scene.chats_folder, action)
    await member.aclose()


# -- the folder-skeleton door, walking into a folder that is already there --------


async def _children_named(parent: uuid.UUID, name: str) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM file_nodes WHERE parent_id = :parent "
                        "AND name_display = :name AND trashed_at IS NULL"
                    ),
                    {"parent": parent, "name": name},
                )
            ).scalar_one()
        )


def _through_the_chat(scene: Scene) -> dict[str, list[str]]:
    """A skeleton posted at the member's ``Chats`` folder whose path enters the
    private chat's folder by its name and makes a folder inside it."""
    return {"paths": [f"{scene.node.name.decode()}/planted"]}


async def test_a_tree_posted_at_the_parent_does_not_walk_into_a_members_chat(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """The walk enters a folder that already exists by name, and the WRITE it
    decided on the ``Chats`` folder used to cover everything beneath — the
    private chat's folder included. The folder it enters is decided on its
    own now, as the by-id door decides it."""
    scene, member = await _members_scene(files_org, real_session)

    planted = await files_client.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.chats_folder}/tree",
        json=_through_the_chat(scene),
        headers=_idem(),
    )

    assert planted.status_code == 404, planted.text
    assert refusal(planted) == OPAQUE
    assert await _children_named(scene.node.id, "planted") == 0
    assert ("read", "deny", "action_not_allowed_unreadable") in {
        (row["action"], row["effect"], row["reason"])
        for row in await _decisions_for(files_org.org.org_id, scene.node.id)
    }
    await member.aclose()


async def test_a_tree_through_a_chat_shared_at_can_view_stops_at_the_viewers_rung(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """A reader of the chat may enter its folder but not write in it, and the
    admin's WRITE on the ``Chats`` folder above says nothing about a chat."""
    scene, member = await _members_scene(files_org, real_session)
    await _share_the_chat_with_the_admin(files_org, member, scene, real_session)

    planted = await files_client.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.chats_folder}/tree",
        json=_through_the_chat(scene),
        headers=_idem(),
    )

    assert planted.status_code == 403, planted.text
    assert planted.json()["code"] == "files.forbidden"
    assert await _children_named(scene.node.id, "planted") == 0
    assert ("write", "deny") in {
        (row["action"], row["effect"])
        for row in await _decisions_for(files_org.org.org_id, scene.node.id)
    }
    await member.aclose()


async def test_the_owner_plants_a_tree_through_their_own_chat(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    scene, member = await _members_scene(files_org, real_session)

    planted = await member.post(
        f"{BASE}/drives/{scene.drive}/items/{scene.chats_folder}/tree",
        json=_through_the_chat(scene),
        headers=_idem(),
    )

    assert planted.status_code == 201, planted.text
    assert [item["name"] for item in planted.json()] == ["planted"]
    assert await _children_named(scene.node.id, "planted") == 1
    assert await _allowed(files_org.org.org_id, str(scene.node.id), "write")
    await member.aclose()
