"""``POST …/items/{id}/duplicate`` — a copy into the caller's own drive.

One route copies everything the tree holds: a file, a folder with all beneath
it, a chat, a saved query. Every case here drives the real route against real
Postgres and then reads the tree back the way a client does, because "where the
copy landed and what it is" is what a person sees, not what the plan said.

The copy is a NEW thing: new ids over the same bytes, the copier as owner, the
destination's sharing and none of the source's, a chat that opens under a new
id with the source's transcript and no machine. The refusals are the policy's
— a trashed or a sealed source, a node in another org — and each leaves the
decision row the audit lane reads.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT, SEAL_SELF_ONLY_BIT
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.files.drives import CHATS_NAME
from alkera_core.models import ChatMessage, EventOutbox, WorkspaceObject
from alkera_core.models.files.history import FileDirStatsDelta
from alkera_core.models.files.tree import FileNode
from alkera_core.models.org_audit_event import OrgAuditEvent
from alkera_core.models.user import User
from alkera_core.models.workspace_object import ObjectPayloadRow
from backend.services.objects import object_service
from backend.services.org import storage_limits as storage_limit_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import app_client, login
from tests.test_chat_machine_binding_seam import _heartbeat, _register

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


# ---------------------------------------------------------------------------
# reading and writing the tree the way a client does
# ---------------------------------------------------------------------------


async def _drive_and_home(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["homeId"]
    return str(body["id"]), str(body["homeId"])


async def _item(client: AsyncClient, drive_id: str, item_id: str) -> dict[str, Any]:
    response = await client.get(f"{BASE}/drives/{drive_id}/items/{item_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _children(client: AsyncClient, drive_id: str, item_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"{BASE}/drives/{drive_id}/items/{item_id}/children")
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()["value"]]


async def _duplicate(
    client: AsyncClient, drive_id: str, item_id: str, body: dict[str, Any] | None = None
) -> Any:
    return await client.post(
        f"{BASE}/drives/{drive_id}/items/{item_id}/duplicate",
        json=body or {},
        headers={"Idempotency-Key": secrets.token_hex(8)},
    )


async def _new_chat(client: AsyncClient, title: str) -> str:
    response = await client.post(
        "/api/v1/chats", json={"title": title, "clientId": secrets.token_hex(8)}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _node_id_of(object_id: uuid.UUID) -> uuid.UUID | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM file_nodes WHERE target_object_id = :object "
                    "AND trashed_at IS NULL AND subtype NOT LIKE '%:%'"
                ),
                {"object": object_id},
            )
        ).scalar_one_or_none()
    return uuid.UUID(str(row)) if row is not None else None


async def _say(session: AsyncSession, chat_id: str, org_id: uuid.UUID, *lines: str) -> None:
    """A transcript, written the way the service leaves one: dense seqs."""
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


async def _grant_reader(
    fx: FilesFixtures, files_org: FilesOrgFixture, node_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    node = await fx.folder(node_id)
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, node, Principal(kind="user", id=user_id), ROLE_READER)
    await fx.repo.session.commit()


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


async def _member_client(client: AsyncClient, files_org: FilesOrgFixture) -> AsyncClient:
    member = app_client()
    return await login(member, files_org.member.email, files_org.member_password)


# ---------------------------------------------------------------------------
# files and folders
# ---------------------------------------------------------------------------


async def test_a_file_copies_into_the_callers_home_with_the_same_bytes_and_a_new_id(
    files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    source = await fx.node(b"notes.txt", parent=await fx.shared())
    await fx.version(source, size_bytes=11, content_hash="cd" * 32)
    drive_id, home_id = await _drive_and_home(files_client)

    response = await _duplicate(files_client, drive_id, str(source.id))
    assert response.status_code == 201, response.text
    body = response.json()
    made = body["item"]
    assert body["chatId"] is None and body["objectId"] is None
    assert made["id"] != str(source.id)
    assert made["parentId"] == home_id
    assert made["name"] == "notes.txt"
    # The same bytes: the copy's head names the source's content, and the
    # store was never asked for a second object.
    assert made["file"]["content_hash"] == "cd" * 32
    assert [row["id"] for row in await _children(files_client, drive_id, home_id)] == [made["id"]]
    # The source is where it was, untouched.
    still = await _item(files_client, drive_id, str(source.id))
    assert still["parentId"] == str(source.parent_id)

    decided = [
        (p["effect"], p["action"]) for p in await _decisions_for(files_org.org.org_id, source.id)
    ]
    assert ("allow", "copy") in decided, decided


async def test_a_folder_copies_recursively_and_a_second_copy_is_renamed(
    files_client: AsyncClient, fx: FilesFixtures
) -> None:
    root = await fx.node(b"Project", kind="folder", parent=await fx.shared())
    empty = await fx.node(b"empty", kind="folder", parent=root)
    full = await fx.node(b"data", kind="folder", parent=root)
    leaf = await fx.node(b"rows.csv", parent=full)
    await fx.version(leaf, size_bytes=7, content_hash="ef" * 32)
    drive_id, home_id = await _drive_and_home(files_client)

    first = await _duplicate(files_client, drive_id, str(root.id))
    assert first.status_code == 201, first.text
    copy = first.json()["item"]
    assert copy["kind"] == "folder" and copy["parentId"] == home_id
    top = {row["name"]: row for row in await _children(files_client, drive_id, copy["id"])}
    assert set(top) == {"empty", "data"}, "the empty folder is copied too"
    assert top["empty"]["id"] != str(empty.id) and top["data"]["id"] != str(full.id)
    inside = await _children(files_client, drive_id, top["data"]["id"])
    assert [row["name"] for row in inside] == ["rows.csv"]
    assert inside[0]["id"] != str(leaf.id)
    assert inside[0]["file"]["content_hash"] == "ef" * 32

    second = await _duplicate(files_client, drive_id, str(root.id))
    assert second.status_code == 201, second.text
    twice = second.json()["item"]
    names = sorted(row["name"] for row in await _children(files_client, drive_id, home_id))
    assert len(names) == 2 and names[0] != names[1]
    assert twice["name"] != copy["name"] and twice["name"].startswith("Project")


async def test_a_named_destination_and_a_new_name_are_honoured(
    files_client: AsyncClient, fx: FilesFixtures
) -> None:
    source = await fx.node(b"memo.txt", parent=await fx.shared())
    await fx.version(source)
    target = await fx.node(b"Inbox", kind="folder", parent=await fx.shared())
    drive_id, _ = await _drive_and_home(files_client)

    response = await _duplicate(
        files_client,
        drive_id,
        str(source.id),
        {"destinationId": str(target.id), "name": "kept.txt"},
    )
    assert response.status_code == 201, response.text
    made = response.json()["item"]
    assert made["parentId"] == str(target.id)
    assert made["name"] == "kept.txt"


# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------


async def test_the_owner_duplicates_a_chat_beside_it_under_a_new_id_with_the_transcript(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    await fx.drive()
    drive_id, _ = await _drive_and_home(files_client)
    chat_id = await _new_chat(files_client, "Kickoff")
    await _say(real_session, chat_id, files_org.org.org_id, "hello", "hi there", "more")
    node_id = await _node_id_of(uuid.UUID(chat_id))
    assert node_id is not None
    source = await _item(files_client, drive_id, str(node_id))
    # The source's links as a chat from before the rows carries them: the array
    # still names a file that was detached since (its row is gone), and the
    # rows are what the chat actually holds.
    kept, detached = uuid.uuid4(), uuid.uuid4()
    await real_session.execute(
        text("INSERT INTO chat_attachments (chat_id, node_id, position) VALUES (:chat, :node, 4)"),
        {"chat": uuid.UUID(chat_id), "node": kept},
    )
    await real_session.execute(
        text(
            "UPDATE workspace_objects SET spec = jsonb_set(spec, '{attachments}', "
            "CAST(:links AS jsonb)) WHERE id = :chat"
        ),
        {"chat": uuid.UUID(chat_id), "links": f'["{kept}", "{detached}"]'},
    )
    # ...and the detach this release made since: the row goes, the array entry
    # stays, exactly as the new writer leaves it.
    await real_session.execute(
        text("DELETE FROM chat_attachments WHERE chat_id = :chat AND node_id = :node"),
        {"chat": uuid.UUID(chat_id), "node": detached},
    )
    await real_session.commit()

    response = await _duplicate(files_client, drive_id, str(node_id))
    assert response.status_code == 201, response.text
    body = response.json()
    new_chat = body["chatId"]
    # The copy links what the source holds — the rows, not the stale array —
    # and its own spec carries no array for anything to read links from.
    async with AsyncSessionLocal() as session:
        copied = (
            await session.execute(
                text(
                    "SELECT node_id, position FROM chat_attachments WHERE chat_id = :chat "
                    "ORDER BY position"
                ),
                {"chat": uuid.UUID(new_chat)},
            )
        ).all()
        copy_spec = (
            await session.execute(
                text("SELECT spec FROM workspace_objects WHERE id = :chat"),
                {"chat": uuid.UUID(new_chat)},
            )
        ).scalar_one()
    assert [(row[0], row[1]) for row in copied] == [(kept, 4)]
    assert copy_spec["attachments"] == []
    assert new_chat and new_chat != chat_id
    made = body["item"]
    assert made["id"] != str(node_id)
    assert made["parentId"] == source["parentId"], "the owner's duplicate lands beside the source"
    assert made["name"] == "Kickoff (copy).alkerachat"
    # The seal travels: the conversation still never leaves as a file.
    assert made["capabilities"]["can_download"] is False
    assert {row["name"] for row in await _children(files_client, drive_id, made["id"])} >= {
        "scratch"
    }

    opened = await files_client.get(f"/api/v1/chats/{new_chat}")
    assert opened.status_code == 200, opened.text
    chat = opened.json()
    assert chat["title"] == "Kickoff (copy)"
    assert chat["files_node_id"] == made["id"]
    assert chat["machine_id"] is None and chat["machine_status"] == "none"
    assert chat["last_seq"] == 3
    # A copy is a chat of its own, in a workspace of its own that adopts the
    # copy's folder; it is never put in the source's workspace.
    source_chat = (await files_client.get(f"/api/v1/chats/{chat_id}")).json()
    assert chat["workspace_id"] is not None
    assert chat["workspace_id"] != source_chat["workspace_id"]
    copy_workspace = (await files_client.get(f"/api/v1/workspaces/{chat['workspace_id']}")).json()
    assert copy_workspace["adopted_chat_id"] == new_chat
    assert copy_workspace["files_node_id"] == made["id"]
    transcript = await files_client.get(f"/api/v1/chats/{new_chat}/messages")
    assert transcript.status_code == 200, transcript.text
    assert [m["payload"]["text"] for m in transcript.json()["items"]] == [
        "hello",
        "hi there",
        "more",
    ]
    # The source's own transcript is not the copy's rows.
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChatMessage.id, ChatMessage.chat_id).where(
                    ChatMessage.chat_id.in_([uuid.UUID(chat_id), uuid.UUID(new_chat)])
                )
            )
        ).all()
    assert len(rows) == 6 and len({row[0] for row in rows}) == 6

    async with AsyncSessionLocal() as session:
        node = await session.get(FileNode, uuid.UUID(made["id"]))
        assert node is not None
        assert node.target_object_id == uuid.UUID(new_chat)
        assert node.flags & NO_DOWNLOAD_BIT and node.flags & SEAL_SELF_ONLY_BIT


async def test_a_member_copies_a_shared_chat_into_their_own_chats_folder(
    client: AsyncClient,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    await fx.drive()
    admin_drive, _ = await _drive_and_home(files_client)
    chat_id = await _new_chat(files_client, "Warehouse spike")
    await _say(real_session, chat_id, files_org.org.org_id, "what happened", "revenue fell")
    node_id = await _node_id_of(uuid.UUID(chat_id))
    assert node_id is not None
    await _grant_reader(fx, files_org, node_id, files_org.member.id)

    member = await _member_client(client, files_org)
    drive_id, home_id = await _drive_and_home(member)
    assert drive_id == admin_drive
    assert [row["name"] for row in await _children(member, drive_id, home_id)] == []

    response = await _duplicate(member, drive_id, str(node_id))
    assert response.status_code == 201, response.text
    body = response.json()
    made = body["item"]
    chats = await _item(member, drive_id, made["parentId"])
    assert chats["name"] == CHATS_NAME.decode() and chats["parentId"] == home_id
    assert made["name"] == "Warehouse spike.alkerachat", "a non-owner's copy keeps the title"

    opened = await member.get(f"/api/v1/chats/{body['chatId']}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["owner_user_id"] == str(files_org.member.id)
    assert opened.json()["title"] == "Warehouse spike"
    transcript = await member.get(f"/api/v1/chats/{body['chatId']}/messages")
    assert [m["payload"]["text"] for m in transcript.json()["items"]] == [
        "what happened",
        "revenue fell",
    ]
    # The source is still the admin's: the copy inherited nothing of its audience
    # and the member gained nothing on the original.
    assert (await files_client.get(f"/api/v1/chats/{chat_id}")).json()["owner_user_id"] == str(
        files_org.org.admin_id
    )

    # The Chats folder is recreated when it is gone.
    trashed = await member.delete(
        f"{BASE}/drives/{drive_id}/items/{chats['id']}",
        headers={"If-Match": str(chats["etag"]), "Idempotency-Key": secrets.token_hex(8)},
    )
    assert trashed.status_code in {200, 204}, trashed.text
    again = await _duplicate(member, drive_id, str(node_id))
    assert again.status_code == 201, again.text
    fresh = await _item(member, drive_id, again.json()["item"]["parentId"])
    assert fresh["name"] == CHATS_NAME.decode() and fresh["id"] != chats["id"]


# ---------------------------------------------------------------------------
# saved objects
# ---------------------------------------------------------------------------


async def test_a_saved_query_copies_as_an_independent_object(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    owner = (
        await real_session.execute(select(User).where(User.id == files_org.org.admin_id))
    ).scalar_one()
    query, created = await object_service.create_object(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        type="query",
        title="Top customers",
        spec={"sql": "select 1", "params": []},
    )
    assert created
    real_session.add(
        ObjectPayloadRow(object_id=query.id, page=0, sha256="ab" * 32, size=3, page_rows=[[1]])
    )
    await real_session.commit()
    node_id = await _node_id_of(query.id)
    assert node_id is not None
    target = await fx.node(b"Saved", kind="folder", parent=await fx.folder(uuid.UUID(home_id)))

    response = await _duplicate(
        files_client, drive_id, str(node_id), {"destinationId": str(target.id)}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    new_object = body["objectId"]
    assert new_object and new_object != str(query.id) and body["chatId"] is None
    made = body["item"]
    assert made["parentId"] == str(target.id)
    assert made["name"] == "Top customers (copy).alkeraquery"

    async with AsyncSessionLocal() as session:
        copy = await session.get(WorkspaceObject, uuid.UUID(new_object))
        assert copy is not None
        assert copy.type == "query" and copy.spec == query.spec
        assert copy.owner_user_id == files_org.org.admin_id
        assert copy.logical_id != query.logical_id
        pages = (
            await session.execute(
                select(ObjectPayloadRow.object_id, ObjectPayloadRow.page_rows).where(
                    ObjectPayloadRow.object_id.in_([query.id, copy.id])
                )
            )
        ).all()
        assert sorted(str(row[0]) for row in pages) == sorted([str(query.id), str(copy.id)])
        node = await session.get(FileNode, uuid.UUID(made["id"]))
        assert node is not None and node.target_object_id == copy.id


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


async def test_a_trashed_source_is_refused_and_the_refusal_is_on_record(
    files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    source = await fx.node(b"gone.txt", parent=await fx.shared())
    await fx.version(source)
    drive_id, _ = await _drive_and_home(files_client)
    trashed = await files_client.delete(
        f"{BASE}/drives/{drive_id}/items/{source.id}",
        headers={"If-Match": str(source.etag), "Idempotency-Key": secrets.token_hex(8)},
    )
    assert trashed.status_code in {200, 204}, trashed.text

    response = await _duplicate(files_client, drive_id, str(source.id))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "files.forbidden"
    rows = await _decisions_for(files_org.org.org_id, source.id)
    assert rows[-1]["effect"] == "deny"
    assert rows[-1]["action"] == "copy"
    assert rows[-1]["reason"] == "trashed"
    assert rows[-1]["attrs"]["trashed"] is True


async def test_a_sealed_folder_is_refused_but_a_chats_self_only_seal_is_not(
    files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    sealed = await fx.node(b"Held", kind="folder", parent=await fx.shared(), flags=NO_DOWNLOAD_BIT)
    drive_id, _ = await _drive_and_home(files_client)

    response = await _duplicate(files_client, drive_id, str(sealed.id))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "files.forbidden"
    rows = await _decisions_for(files_org.org.org_id, sealed.id)
    assert (rows[-1]["effect"], rows[-1]["reason"]) == ("deny", "sealed")
    assert rows[-1]["attrs"]["seal_self_only"] is False

    self_only = await fx.node(
        b"Sealed only here",
        kind="folder",
        parent=await fx.shared(),
        flags=NO_DOWNLOAD_BIT | SEAL_SELF_ONLY_BIT,
    )
    allowed = await _duplicate(files_client, drive_id, str(self_only.id))
    assert allowed.status_code == 201, allowed.text
    rows = await _decisions_for(files_org.org.org_id, self_only.id)
    assert rows[-1]["effect"] == "allow" and rows[-1]["attrs"]["seal_self_only"] is True


async def test_a_node_in_another_org_is_an_opaque_not_found(
    client: AsyncClient, files_client: AsyncClient, fx: FilesFixtures
) -> None:
    source = await fx.node(b"acquisition-memo.txt", parent=await fx.shared())
    await fx.version(source)
    drive_id, _ = await _drive_and_home(files_client)
    async with AsyncSessionLocal() as session:
        _org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other {secrets.token_hex(3)}",
            admin_email=f"other-{secrets.token_hex(4)}@example.com",
            admin_first_name="Outside",
            admin_last_name="Admin",
            admin_password="Passw0rd!Passw0rd!",
        )
        await session.commit()
    outsider = app_client()
    outsider = await login(outsider, admin.email, "Passw0rd!Passw0rd!")
    other_drive, _ = await _drive_and_home(outsider)

    for drive in (drive_id, other_drive):
        response = await _duplicate(outsider, drive, str(source.id))
        assert response.status_code == 404, response.text


async def test_a_reader_may_copy_what_they_can_read_but_not_into_a_folder_they_cannot_write(
    client: AsyncClient, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    source = await fx.node(b"shared.txt", parent=await fx.shared())
    await fx.version(source)
    await _grant_reader(fx, files_org, source.id, files_org.member.id)
    admins_only = await fx.node(b"Admin", kind="folder", parent=await fx.shared())
    member = await _member_client(client, files_org)
    drive_id, home_id = await _drive_and_home(member)

    into_home = await _duplicate(member, drive_id, str(source.id))
    assert into_home.status_code == 201, into_home.text
    assert into_home.json()["item"]["parentId"] == home_id

    # ``/Shared`` lets every member read, so the folder is visible and the
    # refusal is the policy's visible one: readable, not writable.
    refused = await _duplicate(
        member, drive_id, str(source.id), {"destinationId": str(admins_only.id)}
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.forbidden"
    rows = await _decisions_for(files_org.org.org_id, admins_only.id)
    assert (rows[-1]["effect"], rows[-1]["action"]) == ("deny", "write")


# ---------------------------------------------------------------------------
# what a copy costs the copier
# ---------------------------------------------------------------------------


async def test_the_copy_is_charged_to_the_copier_and_refused_past_their_limit(
    client: AsyncClient, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """The bytes are shared, the accounting is not: the copy's logical size
    counts against the org drive and against the copier's own limit, so a
    member with less room than the file is refused with the caller-limit code
    while the admin, who has room, is not."""
    source = await fx.node(b"big.bin", parent=await fx.shared(), size=64)
    await fx.version(source, size_bytes=64)
    await _grant_reader(fx, files_org, source.id, files_org.member.id)
    async with AsyncSessionLocal() as session:
        await storage_limit_service.set_user_limit(
            session,
            org_id=files_org.org.org_id,
            team_id=None,
            user_id=files_org.member.id,
            limit_bytes=40,
            by=None,
        )
        await session.commit()
    member = await _member_client(client, files_org)
    drive_id, _ = await _drive_and_home(member)

    refused = await _duplicate(member, drive_id, str(source.id))
    assert refused.status_code == 507, refused.text
    assert refused.json()["code"] == "files.user_quota_bytes"

    fits = await _duplicate(files_client, drive_id, str(source.id))
    assert fits.status_code == 201, fits.text
    # The admin's copy is on the org's bill: the drive's usage grew by the
    # file's logical size, though no second object was stored.
    async with AsyncSessionLocal() as session:
        charged = (
            await session.execute(
                select(FileDirStatsDelta.bytes_delta).where(
                    FileDirStatsDelta.node_id == uuid.UUID(fits.json()["item"]["parentId"])
                )
            )
        ).scalars()
        assert sum(charged) == 64

    async with AsyncSessionLocal() as session:
        await storage_limit_service.set_org_override(
            session, files_org.org.org_id, limit_bytes=100, by=None
        )
        await session.commit()
    # 64 held by the admin's copy already: a second 64 crosses the org ceiling.
    over = await _duplicate(files_client, drive_id, str(source.id))
    assert over.status_code == 507, over.text
    assert over.json()["code"] == "files.quota_bytes"


async def test_a_copy_is_placed_on_the_orgs_live_box_and_named_by_the_caller(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The copy is opened the moment it exists, so it has to be able to take a
    message: it is placed on whatever machine the org is running, exactly as a
    chat started from the composer is. And the name the caller asked for is the
    name the copy carries — the client asks before the copy is made, because a
    name decided afterwards would mean renaming a chat already on screen."""
    await fx.drive()
    drive_id, _ = await _drive_and_home(files_client)
    chat_id = await _new_chat(files_client, "Kickoff")
    node_id = await _node_id_of(uuid.UUID(chat_id))
    assert node_id is not None

    machine_type = await make_machine_type(real_session)
    await make_grant(
        real_session, org_team_id=files_org.org.org_id, machine_type_id=machine_type.id
    )
    machine_id = await _register(files_org.org, machine_type.provider_type_id, "pod-duplicate-1")
    await _heartbeat(files_org.org, machine_id)

    made = await _duplicate(files_client, drive_id, str(node_id), {"name": "Kickoff, take two"})
    assert made.status_code == 201, made.text
    copy = (await files_client.get(f"/api/v1/chats/{made.json()['chatId']}")).json()
    assert copy["machine_id"] == machine_id
    assert copy["machine_status"] == "ready"
    assert copy["title"] == "Kickoff, take two"
    assert made.json()["item"]["name"] == "Kickoff, take two.alkerachat"


# ---------------------------------------------------------------------------
# folders that hold conversations and templates
# ---------------------------------------------------------------------------


async def _new_template(
    session: AsyncSession, files_org: FilesOrgFixture, title: str
) -> tuple[WorkspaceObject, uuid.UUID]:
    """A chat template and its Files node, made the way the object service makes one."""
    owner = (
        await session.execute(select(User).where(User.id == files_org.org.admin_id))
    ).scalar_one()
    template, created = await object_service.create_object(
        session,
        owner=owner,
        org_id=owner.home_org_team_id,
        type="chat_template",
        title=title,
        spec={"brief": "Open the warehouse and start here", "permission_mode": "read_only"},
    )
    assert created
    await session.commit()
    node_id = await _node_id_of(template.id)
    assert node_id is not None
    return template, node_id


async def _move(client: AsyncClient, drive_id: str, item_id: str, parent_id: str) -> None:
    item = await _item(client, drive_id, item_id)
    response = await client.patch(
        f"{BASE}/drives/{drive_id}/items/{item_id}",
        json={"parentId": parent_id},
        headers={"If-Match": str(item["etag"]), "Idempotency-Key": secrets.token_hex(8)},
    )
    assert response.status_code == 200, response.text


async def test_a_template_duplicates_into_the_copiers_templates_folder_unsealed(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """A template is material a member is meant to take away: its copy is a new
    template object filed where templates live, carrying the starting files and
    none of the seal a conversation wears."""
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    template, node_id = await _new_template(real_session, files_org, "Warehouse starter")
    working = {row["name"]: row for row in await _children(files_client, drive_id, str(node_id))}[
        "scratch"
    ]
    seed = await fx.node(b"queries.sql", parent=await fx.folder(uuid.UUID(working["id"])))
    await fx.version(seed, size_bytes=9, content_hash="ab" * 32)

    response = await _duplicate(files_client, drive_id, str(node_id))
    assert response.status_code == 201, response.text
    body = response.json()
    made = body["item"]
    assert body["objectId"] and body["objectId"] != str(template.id)
    place = await _item(files_client, drive_id, made["parentId"])
    assert place["name"] == "Chat Templates" and place["parentId"] == home_id
    # Nothing a conversation wears: a template's bytes are meant to leave.
    assert made["capabilities"]["can_download"] is True
    copied = await _children(files_client, drive_id, made["id"])
    assert {row["name"] for row in copied} == {"scratch", "README.md"}
    scratch = next(row for row in copied if row["name"] == "scratch")
    assert [row["name"] for row in await _children(files_client, drive_id, scratch["id"])] == [
        "queries.sql"
    ]

    async with AsyncSessionLocal() as session:
        copy = await session.get(WorkspaceObject, uuid.UUID(body["objectId"]))
        assert copy is not None
        assert copy.type == "chat_template"
        assert copy.spec["brief"] == "Open the warehouse and start here"
        assert copy.logical_id != template.logical_id
        node = await session.get(FileNode, uuid.UUID(made["id"]))
        assert node is not None and node.target_object_id == copy.id
        assert node.flags & (NO_DOWNLOAD_BIT | SEAL_SELF_ONLY_BIT) == 0


async def test_a_copied_main_workspace_is_a_project_of_the_copiers(
    files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """A member has one main workspace. Copying its folder makes a new
    workspace, and that copy is a project: never a second main, and never
    the source's binding."""
    await fx.drive()
    drive_id, _home_id = await _drive_and_home(files_client)
    main = (await files_client.get("/api/v1/workspaces/main")).json()

    response = await _duplicate(files_client, drive_id, main["files_node_id"])

    assert response.status_code == 201, response.text
    copy_id = response.json()["objectId"]
    assert copy_id and copy_id != main["id"]
    copied = (await files_client.get(f"/api/v1/workspaces/{copy_id}")).json()
    assert copied["kind"] == "project"
    assert copied["layout"] == "native"
    assert copied["can_delete"] is True
    again = (await files_client.get("/api/v1/workspaces/main")).json()
    assert again["id"] == main["id"]


async def test_a_copied_folder_holding_a_chat_and_a_template_adopts_each(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """Copying the folder around them copies what they are: each copied folder
    is a NEW conversation and a NEW template of its own, never a second door
    onto the source's rows."""
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    shelf = await fx.node(b"Shelf", kind="folder", parent=await fx.folder(uuid.UUID(home_id)))
    chat_id = await _new_chat(files_client, "Kickoff")
    await _say(real_session, chat_id, files_org.org.org_id, "hello", "hi there")
    chat_node = await _node_id_of(uuid.UUID(chat_id))
    assert chat_node is not None
    template, template_node = await _new_template(real_session, files_org, "Warehouse starter")
    await _move(files_client, drive_id, str(chat_node), str(shelf.id))
    await _move(files_client, drive_id, str(template_node), str(shelf.id))

    response = await _duplicate(files_client, drive_id, str(shelf.id))
    assert response.status_code == 201, response.text
    copy = response.json()["item"]
    assert response.json()["chatId"] is None and response.json()["objectId"] is None
    rows = {row["name"]: row for row in await _children(files_client, drive_id, copy["id"])}
    assert set(rows) == {
        "Kickoff.alkerachat",
        "Warehouse starter.alkerachat.template",
    }, sorted(rows)

    async with AsyncSessionLocal() as session:
        copied_chat = await session.get(FileNode, uuid.UUID(rows["Kickoff.alkerachat"]["id"]))
        copied_template = await session.get(
            FileNode, uuid.UUID(rows["Warehouse starter.alkerachat.template"]["id"])
        )
        assert copied_chat is not None and copied_template is not None
        assert copied_chat.target_object_id not in (None, uuid.UUID(chat_id))
        assert copied_template.target_object_id not in (None, template.id)
        assert copied_chat.flags & NO_DOWNLOAD_BIT, "the copied conversation is sealed like one"
        assert copied_template.flags & NO_DOWNLOAD_BIT == 0
        new_chat = await session.get(WorkspaceObject, copied_chat.target_object_id)
        new_template = await session.get(WorkspaceObject, copied_template.target_object_id)
        assert new_chat is not None and new_chat.type == "chat"
        assert new_template is not None and new_template.type == "chat_template"
        assert new_chat.owner_user_id == files_org.org.admin_id
        new_chat_id = str(new_chat.id)

    # The copy is a conversation a person can open, with the source's words.
    opened = await files_client.get(f"/api/v1/chats/{new_chat_id}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["files_node_id"] == rows["Kickoff.alkerachat"]["id"]
    transcript = await files_client.get(f"/api/v1/chats/{new_chat_id}/messages")
    assert [m["payload"]["text"] for m in transcript.json()["items"]] == ["hello", "hi there"]
    # Each copied folder still opens its own working directory.
    for row in await _children(files_client, drive_id, copy["id"]):
        assert (row.get("object") or {}).get("metadata", {}).get("files_node_id")


async def test_a_folder_holding_more_objects_than_the_copy_cap_is_refused(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """A copy adopts every conversation and template it carries, so the work is
    bounded before a row is written: the folder past the cap is refused and
    nothing of it is copied."""
    drive_id, home_id = await _drive_and_home(files_client)
    home = await fx.folder(uuid.UUID(home_id))
    chat_id = await _new_chat(files_client, "Kickoff")
    holder = await fx.node(b"Too many", kind="folder", parent=home)
    for index in range(201):
        await fx.node(
            f"chat-{index}.alkerachat".encode(),
            kind="folder",
            parent=holder,
            subtype="chat",
            target_object_id=uuid.UUID(chat_id),
        )

    refused = await _duplicate(files_client, drive_id, str(holder.id))
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.too_many_objects_to_copy"
    names = [row["name"] for row in await _children(files_client, drive_id, home_id)]
    assert names.count("Too many") == 1, "nothing of the refused copy was written"


async def test_the_duplicate_records_the_org_audit_row_from_inside_the_files_role(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The copy's foreign statements run as the platform, not as the tenant.

    ``@idempotent_route`` runs the whole handler inside the Files claim's
    transaction, stamped ``alkera_files_app`` — a role that can neither read
    ``workspace_objects`` (the source object a chat's copy is made from) nor
    write ``org_audit_events``. Without the windows that step out of the role
    for exactly those statements the duplicate is a 500, so this pins both: the
    source read that decides what kind of object to mint, and the org's audit
    row naming the copy.
    """
    await fx.drive()
    drive_id, _ = await _drive_and_home(files_client)
    chat_id = await _new_chat(files_client, "Kickoff")
    node_id = await _node_id_of(uuid.UUID(chat_id))
    assert node_id is not None

    # The premise: the role the handler's transaction runs as cannot touch
    # either table. A GRANT that widened it would make this test stop proving
    # that the windows are what makes the copy work.
    async with AsyncSessionLocal() as probe:
        for table in ("workspace_objects", "org_audit_events"):
            await probe.execute(text("SET LOCAL ROLE alkera_files_app"))
            with pytest.raises(ProgrammingError):
                await probe.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
            await probe.rollback()

    response = await _duplicate(files_client, drive_id, str(node_id))

    assert response.status_code == 201, response.text
    made = response.json()["item"]["id"]
    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == files_org.org.org_id,
                        OrgAuditEvent.action == "files.duplicate",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [row.target for row in rows] == [made], (
        "the copy's audit row is written as the platform; under the Files role the "
        "INSERT into org_audit_events is refused and the whole request 500s"
    )
    assert rows[0].detail["source_node_id"] == str(node_id)
