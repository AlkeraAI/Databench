"""``/api/v1/workspaces`` and the workspace every chat is created in.

What a person and the box can observe: the status contract of every route, the
``authz.decision`` row each decision leaves, where a chat's folder lands, and
what a workspace reads back as. The flag ``workspaces_multi_chat`` is driven
both ways, because what a new chat does differs: off, it gets a workspace of
one that adopts its folder (the box still runs one sandbox per chat); on, it
lands in its owner's main workspace.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz.policies import workspace as workspace_policy
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import EventOutbox, OrgAuditEvent, User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.objects import chat_spares
from backend.services import workspaces
from backend.services.chats import chat_service
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

WORKSPACES = "/api/v1/workspaces"
CHATS = "/api/v1/chats"


@contextmanager
def projects_allowed() -> Iterator[None]:
    """Project workspaces are refused while a workspace holds one chat; a test
    that needs one as a fixture makes it with the flag on for that request."""
    previous = settings.workspaces_multi_chat
    settings.workspaces_multi_chat = True
    try:
        yield
    finally:
        settings.workspaces_multi_chat = previous


@pytest.fixture
def multi_chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    yield


async def _chat(client: AsyncClient, title: str = "Quarterly review") -> dict[str, Any]:
    resp = await client.post(CHATS, json={"title": title})
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _workspace(client: AsyncClient, title: str = "Pricing study") -> dict[str, Any]:
    with projects_allowed():
        resp = await client.post(WORKSPACES, json={"title": title})
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _node(node_id: str | UUID) -> FileNode:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        node = await db.get(FileNode, UUID(str(node_id)))
        assert node is not None
        return node


async def _children(node_id: str | UUID) -> set[bytes]:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        rows = await db.execute(
            select(FileNode.name).where(
                FileNode.parent_id == UUID(str(node_id)), FileNode.trashed_at.is_(None)
            )
        )
        return {bytes(name) for name in rows.scalars()}


async def _row(object_id: str | UUID) -> WorkspaceObject:
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, UUID(str(object_id)))
        assert row is not None
        return row


async def _decisions(org_id: UUID, entity_id: str) -> list[tuple[str, str, str]]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity_id == entity_id,
            )
            .order_by(EventOutbox.id)
        )
        return [
            (r.payload["action"], r.payload["effect"], r.payload["reason"])
            for r in rows.scalars().all()
        ]


async def _grant(client: AsyncClient, workspace: dict[str, Any], member: User, role: str) -> None:
    """Share the workspace's folder through the Files permissions route, the
    way the share dialog does."""
    etag = (await _node(workspace["files_node_id"])).etag
    granted = await client.post(
        f"/api/v1/files/drives/{workspace['files_drive_id']}/items/"
        f"{workspace['files_node_id']}/permissions",
        json={"principal": {"kind": "user", "id": str(member.id)}, "role": role},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(etag)},
    )
    assert granted.status_code == 201, granted.text


async def _member(real_session: AsyncSession, org_admin: OrgWithAdmin) -> tuple[User, str]:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    return member, password


# ---------------------------------------------------------------------------
# A new chat, with the flag off: a workspace of one that adopts its folder
# ---------------------------------------------------------------------------


async def test_a_new_chat_gets_a_workspace_of_one_that_adopts_its_folder(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)

    assert chat["workspace_id"] is not None
    resp = await client.get(f"{WORKSPACES}/{chat['workspace_id']}")
    assert resp.status_code == 200, resp.text
    workspace = resp.json()
    assert workspace["layout"] == "adopted"
    assert workspace["kind"] == "project"
    assert workspace["adopted_chat_id"] == chat["id"]
    assert workspace["chat_count"] == 1
    # Its folder IS the chat's: sharing one shares the other, and nothing moved.
    assert workspace["files_node_id"] == chat["files_node_id"] is not None
    working = await _node(workspace["working_node_id"])
    assert bytes(working.name) == b"scratch"
    assert str(working.parent_id) == chat["files_node_id"]
    # No folder of its own was made for it.
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        own = await db.execute(
            select(FileNode.id).where(FileNode.target_object_id == UUID(workspace["id"]))
        )
        assert own.first() is None
    listed = await client.get(f"{WORKSPACES}/{workspace['id']}/chats")
    assert [row["id"] for row in listed.json()["items"]] == [chat["id"]]
    single = (await client.get(f"{CHATS}/{chat['id']}")).json()
    assert single["workspace_id"] == workspace["id"]


async def test_a_workspace_of_one_answers_to_its_chats_name(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, "Before")
    renamed = await client.put(
        f"/api/v1/objects/{chat['id']}", json={"title": "After", "expected_version": 1}
    )
    assert renamed.status_code == 200, renamed.text

    workspace = (await client.get(f"{WORKSPACES}/{chat['workspace_id']}")).json()

    assert workspace["title"] == "After"


async def test_renaming_a_workspace_of_one_renames_its_chat(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, "Old name")
    workspace = (await client.get(f"{WORKSPACES}/{chat['workspace_id']}")).json()

    resp = await client.patch(
        f"{WORKSPACES}/{workspace['id']}",
        json={"title": "New name", "expected_version": workspace["version"]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "New name"
    assert (await client.get(f"{CHATS}/{chat['id']}")).json()["title"] == "New name"
    stale = await client.patch(
        f"{WORKSPACES}/{workspace['id']}",
        json={"title": "Lost race", "expected_version": workspace["version"]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "version_conflict"


async def test_deleting_a_chat_ends_its_workspace_of_one(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)

    assert (await client.delete(f"{CHATS}/{chat['id']}")).status_code == 204

    assert (await client.get(f"{WORKSPACES}/{chat['workspace_id']}")).status_code == 404
    assert (await _row(chat["workspace_id"])).deleted_at != 0


async def test_deleting_a_workspace_of_one_ends_its_chat(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)

    resp = await client.delete(f"{WORKSPACES}/{chat['workspace_id']}")

    assert resp.status_code == 204, resp.text
    assert (await client.get(f"{CHATS}/{chat['id']}")).status_code == 404
    assert (await _row(chat["id"])).deleted_at != 0
    assert _decisions_of(await _decisions(org_admin.org_id, chat["workspace_id"])) == [
        ("delete", "allow", "owner_deletes")
    ]


def _decisions_of(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return [row for row in rows if row[0] != "read"]


async def _strip_workspace(chat_id: str, *, drop_workspace_rows: bool) -> None:
    """Leave the chat the way a task on the previous build writes one: a spec
    with no workspace, and (for a chat that build created) no workspace row."""
    async with AsyncSessionLocal() as db:
        if drop_workspace_rows:
            await db.execute(
                text(
                    "DELETE FROM workspace_objects WHERE type = 'workspace' AND logical_id = :chat"
                ),
                {"chat": chat_id},
            )
        await db.execute(
            text("UPDATE workspace_objects SET spec = spec - 'workspace_id' WHERE id = :id"),
            {"id": UUID(chat_id)},
        )
        await db.commit()


async def _chat_updates(chat_id: str) -> int:
    async with AsyncSessionLocal() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(EventOutbox)
            .where(EventOutbox.type == "chat.updated", EventOutbox.entity_id == chat_id)
        )
    return int(count or 0)


async def test_a_chat_that_lost_its_workspace_reference_is_relinked_on_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _strip_workspace(chat["id"], drop_workspace_rows=False)

    read = (await client.get(f"{CHATS}/{chat['id']}")).json()

    # The workspace already minted for it, never a second one.
    assert read["workspace_id"] == chat["workspace_id"]
    async with AsyncSessionLocal() as db:
        count = await db.scalar(
            text("SELECT count(*) FROM workspace_objects WHERE logical_id = :c"),
            {"c": chat["id"]},
        )
    assert count == 1


async def test_a_chat_an_older_build_created_is_adopted_when_it_is_listed(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _strip_workspace(chat["id"], drop_workspace_rows=True)
    before = await _chat_updates(chat["id"])

    listed = (await client.get(CHATS)).json()["items"]

    # Anyone holding the chat is told its row changed.
    assert await _chat_updates(chat["id"]) == before + 1

    row = next(item for item in listed if item["id"] == chat["id"])
    assert row["workspace_id"] is not None and row["workspace_id"] != chat["workspace_id"]
    workspace = (await client.get(f"{WORKSPACES}/{row['workspace_id']}")).json()
    assert workspace["adopted_chat_id"] == chat["id"]
    assert workspace["files_node_id"] == chat["files_node_id"]


async def test_with_adoption_off_chats_are_served_and_none_is_put_in_a_workspace(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a task of a build from before workspaces may still be serving, no
    chat is put in a workspace: a new chat gets none, and a chat with none is
    listed and opened as it is. Turning adoption on heals it on the next read."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    older = await _chat(client, "From the previous build")
    await _strip_workspace(older["id"], drop_workspace_rows=True)
    monkeypatch.setattr(settings, "workspaces_adoption_enabled", False)

    created = await _chat(client, "Started with adoption off")
    listed = await client.get(CHATS)
    opened = await client.get(f"{CHATS}/{older['id']}")

    assert created["workspace_id"] is None
    assert listed.status_code == 200, listed.text
    by_id = {item["id"]: item for item in listed.json()["items"]}
    assert by_id[older["id"]]["workspace_id"] is None
    assert by_id[created["id"]]["workspace_id"] is None
    assert opened.status_code == 200, opened.text
    assert opened.json()["workspace_id"] is None
    async with AsyncSessionLocal() as db:
        made = await db.scalar(
            text(
                "SELECT count(*) FROM workspace_objects "
                "WHERE type = 'workspace' AND logical_id IN (:a, :b)"
            ),
            {"a": older["id"], "b": created["id"]},
        )
    assert made == 0

    monkeypatch.setattr(settings, "workspaces_adoption_enabled", True)
    healed = (await client.get(f"{CHATS}/{created['id']}")).json()
    assert healed["workspace_id"] is not None


async def _waiting_on_a_lock(deadline_s: float = 10.0) -> None:
    """Return once some backend is blocked on a lock (the second healer's
    insert, queued behind the first's uncommitted row)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    while loop.time() < end:
        async with AsyncSessionLocal() as db:
            waiting = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    pytest.fail("the second healer never queued behind the first")


async def test_two_reads_healing_the_same_stray_at_once_make_one_workspace(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Two requests read the same stray before either commits. The second's
    insert waits on the first's row and is refused by the unique logical id
    when the first commits; it lands on the first's workspace instead of
    failing the request."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _strip_workspace(chat["id"], drop_workspace_rows=True)

    async with AsyncSessionLocal() as first, AsyncSessionLocal() as second:
        first_chat = await first.get(WorkspaceObject, UUID(chat["id"]))
        second_chat = await second.get(WorkspaceObject, UUID(chat["id"]))
        assert first_chat is not None and second_chat is not None
        owner = await first.get(User, first_chat.owner_user_id)
        assert owner is not None
        await workspaces.adopt_chat(first, chat=first_chat, owner=owner)

        racing = asyncio.create_task(workspaces.adopt_strays(second, [second_chat]))
        await _waiting_on_a_lock()
        await first.commit()
        healed = await asyncio.wait_for(racing, 10.0)
        await second.commit()

    assert healed == [second_chat]
    async with AsyncSessionLocal() as db:
        minted = (
            (
                await db.execute(
                    text(
                        "SELECT id FROM workspace_objects "
                        "WHERE type = 'workspace' AND logical_id = :c"
                    ),
                    {"c": chat["id"]},
                )
            )
            .scalars()
            .all()
        )
    assert len(minted) == 1
    assert (await _row(chat["id"])).spec["workspace_id"] == str(minted[0])


async def test_a_heal_that_would_wait_on_a_lock_leaves_the_read_unblocked(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Another transaction holds the stray chat's row: healing it would wait,
    so the read skips the heal (the next read or the reconcile pass does it)
    and returns at once rather than queueing behind that transaction."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _strip_workspace(chat["id"], drop_workspace_rows=True)

    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as reader:
        await holder.execute(
            text("SELECT 1 FROM workspace_objects WHERE id = :id FOR UPDATE"),
            {"id": UUID(chat["id"])},
        )
        stray = await reader.get(WorkspaceObject, UUID(chat["id"]))
        assert stray is not None
        healed = await asyncio.wait_for(workspaces.adopt_strays(reader, [stray]), 5.0)
        await reader.commit()
        await holder.rollback()

    assert healed == []
    assert "workspace_id" not in (await _row(chat["id"])).spec


async def test_a_heal_that_gives_up_on_a_lock_leaves_the_listing_whole(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The first list after a migration heals strays; one whose row another
    transaction holds gives up after the lock wait, and the list still answers
    200 with every chat, each read from rows the give-up never touched."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    healthy = await _chat(client, "Healthy")
    stray = await _chat(client, "Stray")
    await _strip_workspace(stray["id"], drop_workspace_rows=True)

    async with AsyncSessionLocal() as holder:
        await holder.execute(
            text("SELECT 1 FROM workspace_objects WHERE id = :id FOR UPDATE"),
            {"id": UUID(stray["id"])},
        )
        answer = await client.get(CHATS)
        await holder.rollback()

    assert answer.status_code == 200, answer.text
    listed = {item["id"]: item for item in answer.json()["items"]}
    assert {healthy["id"], stray["id"]} <= set(listed)
    assert listed[healthy["id"]]["workspace_id"] == healthy["workspace_id"]
    assert listed[stray["id"]]["workspace_id"] is None


async def test_a_heal_never_expires_or_writes_the_callers_rows(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Healing runs in a session of its own: whatever it rolls back, the rows
    the caller loaded stay loaded (reading them needs no IO) and the caller's
    session holds nothing the heal wrote; only the chats it healed are
    re-read, carrying their new workspace."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    healthy = await _chat(client, "Healthy")
    held = await _chat(client, "Held")
    healable = await _chat(client, "Healable")
    await _strip_workspace(held["id"], drop_workspace_rows=True)
    await _strip_workspace(healable["id"], drop_workspace_rows=True)

    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as reader:
        await holder.execute(
            text("SELECT 1 FROM workspace_objects WHERE id = :id FOR UPDATE"),
            {"id": UUID(held["id"])},
        )
        rows = [
            await reader.get(WorkspaceObject, UUID(chat["id"]))
            for chat in (healthy, held, healable)
        ]
        assert all(row is not None for row in rows)
        loaded = [row for row in rows if row is not None]

        healed = await workspaces.adopt_strays(reader, loaded)

        assert [row.id for row in healed] == [UUID(healable["id"])]
        assert all(not sa_inspect(row).expired_attributes for row in loaded)
        assert loaded[0].spec["workspace_id"] == healthy["workspace_id"]
        assert "workspace_id" not in loaded[1].spec
        assert loaded[2].spec["workspace_id"]
        assert not reader.new and not reader.dirty
        await holder.rollback()


async def test_two_first_requests_for_the_main_workspace_make_one(
    org_admin: OrgWithAdmin,
) -> None:
    """A browser chat and a Slack mention can both be a member's first: the
    second insert waits on the first's row, is refused by the unique logical
    id when the first commits, and answers with the first's workspace."""
    async with AsyncSessionLocal() as first, AsyncSessionLocal() as second:
        first_owner = await first.get(User, org_admin.admin_id)
        second_owner = await second.get(User, org_admin.admin_id)
        assert first_owner is not None and second_owner is not None
        made, made_now = await workspaces.ensure_main(
            first, owner=first_owner, org_id=org_admin.org_id
        )
        assert made_now is True

        racing = asyncio.create_task(
            workspaces.ensure_main(second, owner=second_owner, org_id=org_admin.org_id)
        )
        await _waiting_on_a_lock()
        await first.commit()
        raced, raced_made = await asyncio.wait_for(racing, 10.0)
        await second.commit()

    assert (raced.id, raced_made) == (made.id, False)


async def test_linking_a_chat_keeps_a_field_another_writer_committed_since_the_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Healing writes only the chat's workspace reference: a spec field another
    writer committed after this request read the chat survives it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _strip_workspace(chat["id"], drop_workspace_rows=False)

    async with AsyncSessionLocal() as healer:
        stale = await healer.get(WorkspaceObject, UUID(chat["id"]))
        assert stale is not None
        async with AsyncSessionLocal() as other:
            await other.execute(
                text(
                    "UPDATE workspace_objects SET spec = spec || "
                    '\'{"permission_mode": "plan"}\'::jsonb WHERE id = :id'
                ),
                {"id": UUID(chat["id"])},
            )
            await other.commit()
        assert await workspaces.adopt_strays(healer, [stale]) == [stale]
        assert stale.spec["permission_mode"] == "plan"
        await healer.commit()

    spec = (await _row(chat["id"])).spec
    assert spec["permission_mode"] == "plan"
    assert spec["workspace_id"] == chat["workspace_id"]


async def test_a_spare_chats_workspace_is_hidden_and_goes_with_the_spare(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    spare, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title=None,
        client_id=None,
        machine_id=None,
        machine_status="none",
        spare=True,
    )
    await real_session.commit()
    workspace_id = chat_service.chat_spec_of(spare).workspace_id
    assert workspace_id is not None
    await login(client, org_admin.admin_email, org_admin.admin_password)

    listed = await client.get(WORKSPACES)
    assert workspace_id not in {row["id"] for row in listed.json()["items"]}

    await chat_spares.erase_row(real_session, spare)
    await real_session.commit()
    async with AsyncSessionLocal() as db:
        assert await db.get(WorkspaceObject, UUID(workspace_id)) is None


# ---------------------------------------------------------------------------
# Project workspaces and the main workspace
# ---------------------------------------------------------------------------


async def test_a_project_workspace_is_born_with_a_shared_tree_and_a_records_folder(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client)

    assert workspace["layout"] == "native"
    assert workspace["kind"] == "project"
    assert workspace["chat_count"] == 0
    folder = await _node(workspace["files_node_id"])
    assert bytes(folder.name) == b"Pricing study.alkeraworkspace"
    assert folder.subtype == "workspace"
    assert await _children(folder.id) == {b"files", b".chats"}
    assert bytes((await _node(workspace["working_node_id"])).name) == b"files"
    assert await _decisions(org_admin.org_id, workspace["id"]) == [
        ("create", "allow", "org_member_creates")
    ]


async def test_a_workspace_row_in_files_opens_its_page_and_names_its_shared_tree(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The workspace's folder, read through Files, says where it opens and
    where its files are. A double-click follows ``web_url`` to the workspace's
    page in Chats, and "Browse files" opens the node the facet names, which is
    the shared ``files/`` tree and never the ``.chats/`` records beside it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client)

    resp = await client.get(
        f"/api/v1/files/drives/{workspace['files_drive_id']}/items/{workspace['files_node_id']}"
    )
    assert resp.status_code == 200, resp.text
    facet = resp.json()["object"]
    assert facet["type"] == "workspace"
    assert facet["web_url"] == f"/workspaces/{workspace['id']}"
    assert facet["metadata"]["files_node_id"] == workspace["working_node_id"]
    assert bytes((await _node(facet["metadata"]["files_node_id"])).name) == b"files"


async def test_a_retried_create_lands_on_the_workspace_it_made(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await client.post(CHATS, json={"title": "Taken", "client_id": "ws-retry-2"})
    with projects_allowed():
        first = await client.post(WORKSPACES, json={"title": "Once", "client_id": "ws-retry-1"})
        again = await client.post(WORKSPACES, json={"title": "Once", "client_id": "ws-retry-1"})
        taken = await client.post(WORKSPACES, json={"title": "Clash", "client_id": "ws-retry-2"})

    assert first.status_code == 201 and again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert chat.status_code == 201
    assert taken.status_code == 409, taken.text
    assert taken.json()["error"]["code"] == "client_id_in_use"


async def test_the_main_workspace_is_made_once_and_never_deleted(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    first = await client.get(f"{WORKSPACES}/main")
    second = await client.get(f"{WORKSPACES}/main")

    assert first.status_code == 200, first.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["kind"] == "main"
    assert first.json()["can_delete"] is False
    async with AsyncSessionLocal() as db:
        mains = await db.execute(
            select(WorkspaceObject.id).where(
                WorkspaceObject.type == "workspace",
                WorkspaceObject.owner_user_id == org_admin.admin_id,
                WorkspaceObject.spec["kind"].astext == "main",
            )
        )
        assert len(mains.all()) == 1

    refused = await client.delete(f"{WORKSPACES}/{first.json()['id']}")

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == workspace_policy.MAIN_DELETE_DENIED_CODE
    assert ("delete", "deny", "main_not_deletable") in await _decisions(
        org_admin.org_id, first.json()["id"]
    )


@pytest.mark.usefixtures("multi_chat")
async def test_with_several_chats_allowed_a_new_chat_lands_in_the_main_workspace(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    one = await _chat(client, "First")
    two = await _chat(client, "Second")
    main = (await client.get(f"{WORKSPACES}/main")).json()

    assert one["workspace_id"] == two["workspace_id"] == main["id"]
    assert main["chat_count"] == 2
    records = {
        str((await _node(one["files_node_id"])).parent_id),
        str((await _node(two["files_node_id"])).parent_id),
    }
    (records_id,) = records
    records_folder = await _node(records_id)
    assert bytes(records_folder.name) == b".chats"
    assert str(records_folder.parent_id) == main["files_node_id"]
    # A box finds the chat's working directory exactly where it always has.
    assert await _children(one["files_node_id"]) == {b"scratch"}


async def test_without_the_flag_a_chat_cannot_be_started_in_an_existing_workspace(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client)

    resp = await client.post(CHATS, json={"title": "Two", "workspace_id": workspace["id"]})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "workspaces_multi_chat_disabled"
    assert (await client.get(f"{WORKSPACES}/{workspace['id']}")).json()["can_add_chat"] is False


@pytest.mark.usefixtures("multi_chat")
async def test_with_the_flag_a_chat_is_started_in_a_native_workspace_only(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client)

    started = await client.post(CHATS, json={"title": "Two", "workspace_id": workspace["id"]})

    assert started.status_code == 201, started.text
    assert started.json()["workspace_id"] == workspace["id"]
    chat_folder = await _node(started.json()["files_node_id"])
    records = await _node(chat_folder.parent_id)
    assert bytes(records.name) == b".chats"
    assert str(records.parent_id) == workspace["files_node_id"]
    refreshed = (await client.get(f"{WORKSPACES}/{workspace['id']}")).json()
    assert refreshed["chat_count"] == 1
    assert ("write", "allow", "writer_may_add_chat") in await _decisions(
        org_admin.org_id, workspace["id"]
    )


async def test_a_workspace_of_one_holds_only_its_chat(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "workspaces_multi_chat", False)
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    chat, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="A thread",
        client_id=None,
        machine_id=None,
        machine_status="none",
    )
    await real_session.commit()
    workspace_id = chat_service.chat_spec_of(chat).workspace_id
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    resp = await client.post(CHATS, json={"title": "Two", "workspace_id": workspace_id})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "workspace_holds_one_chat"


# ---------------------------------------------------------------------------
# Who may do what: status contract and the decision row
# ---------------------------------------------------------------------------


async def test_an_unshared_colleague_is_told_the_workspace_does_not_exist(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client)
    member, password = await _member(real_session, org_admin)

    async with app_client() as other:
        await login(other, member.email, password)
        read = await other.get(f"{WORKSPACES}/{workspace['id']}")
        renamed = await other.patch(
            f"{WORKSPACES}/{workspace['id']}", json={"title": "Mine", "expected_version": 1}
        )
        deleted = await other.delete(f"{WORKSPACES}/{workspace['id']}")
        listed = await other.get(WORKSPACES)

    for resp in (read, renamed, deleted):
        assert resp.status_code == 404, resp.text
    assert workspace["id"] not in {row["id"] for row in listed.json()["items"]}
    assert [
        row for row in await _decisions(org_admin.org_id, workspace["id"]) if row[1] == "deny"
    ] == [
        ("read", "deny", "not_in_audience"),
        ("rename", "deny", "not_in_audience"),
        ("delete", "deny", "not_in_audience"),
    ]


@pytest.mark.usefixtures("multi_chat")
async def test_sharing_the_workspace_folder_shares_the_workspace_and_its_chats(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client)
    chat = (
        await client.post(CHATS, json={"title": "In it", "workspace_id": workspace["id"]})
    ).json()
    member, password = await _member(real_session, org_admin)

    async with app_client() as other:
        await login(other, member.email, password)
        assert (await other.get(f"{CHATS}/{chat['id']}")).status_code == 404
        await _grant(client, workspace, member, ROLE_READER)
        read = await other.get(f"{WORKSPACES}/{workspace['id']}")
        chat_read = await other.get(f"{CHATS}/{chat['id']}")
        in_it = await other.get(f"{WORKSPACES}/{workspace['id']}/chats")
        listed = await other.get(WORKSPACES)
        renamed = await other.patch(
            f"{WORKSPACES}/{workspace['id']}", json={"title": "Mine", "expected_version": 1}
        )
        started = await other.post(CHATS, json={"title": "x", "workspace_id": workspace["id"]})
        deleted = await other.delete(f"{WORKSPACES}/{workspace['id']}")

    assert read.status_code == 200, read.text
    assert read.json()["can_rename"] is False and read.json()["can_add_chat"] is False
    assert chat_read.status_code == 200, chat_read.text
    assert [row["id"] for row in in_it.json()["items"]] == [chat["id"]]
    assert workspace["id"] in {row["id"] for row in listed.json()["items"]}
    # A reader sees it; changing it takes more, and they are told so plainly.
    assert renamed.status_code == 403
    assert renamed.json()["error"]["code"] == workspace_policy.RENAME_DENIED_CODE
    assert started.status_code == 403
    assert started.json()["error"]["code"] == workspace_policy.WRITE_DENIED_CODE
    assert deleted.status_code == 403
    assert deleted.json()["error"]["message"] == workspace_policy.DELETE_DENIED_MESSAGE
    assert [
        row for row in await _decisions(org_admin.org_id, workspace["id"]) if row[1] == "deny"
    ] == [
        ("rename", "deny", "rename_rung_required"),
        ("write", "deny", "write_rung_required"),
        ("delete", "deny", "owner_or_org_admin_required"),
    ]


@pytest.mark.usefixtures("multi_chat")
async def test_a_writer_on_the_workspace_may_start_a_chat_in_it(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client)
    member, password = await _member(real_session, org_admin)
    await _grant(client, workspace, member, ROLE_WRITER)

    async with app_client() as other:
        await login(other, member.email, password)
        started = await other.post(CHATS, json={"title": "Theirs", "workspace_id": workspace["id"]})

    assert started.status_code == 201, started.text
    assert started.json()["workspace_id"] == workspace["id"]
    assert started.json()["owner_user_id"] == str(member.id)


async def test_sharing_a_chat_in_a_workspace_of_one_shares_the_workspace(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    member, password = await _member(real_session, org_admin)
    owner = await real_session.get(User, org_admin.admin_id)
    row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    assert owner is not None and row is not None

    async with app_client() as other:
        await login(other, member.email, password)
        before = await other.get(f"{WORKSPACES}/{chat['workspace_id']}")
        await share_chat(real_session, chat=row, owner=owner, user=member, role=ROLE_READER)
        after = await other.get(f"{WORKSPACES}/{chat['workspace_id']}")

    assert before.status_code == 404
    assert after.status_code == 200, after.text
    assert ("read", "allow", "shared_reads") in await _decisions(
        org_admin.org_id, chat["workspace_id"]
    )


@pytest.mark.parametrize(
    ("reads_private", "status"),
    [
        pytest.param(False, 404, id="one-they-cannot-read-is-not-found"),
        pytest.param(True, 204, id="one-the-deployment-lets-them-read-is-deleted"),
    ],
)
async def test_an_org_admin_deletes_a_members_workspace_only_if_they_may_read_it(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reads_private: bool,
    status: int,
) -> None:
    monkeypatch.setattr(settings, "chat_org_admin_reads_private", reads_private)
    member, password = await _member(real_session, org_admin)
    async with app_client() as other:
        await login(other, member.email, password)
        workspace = await _workspace(other)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    resp = await client.delete(f"{WORKSPACES}/{workspace['id']}")

    assert resp.status_code == status, resp.text
    if status == 404:
        assert (await _row(workspace["id"])).deleted_at == 0
        return

    assert resp.status_code == 204, resp.text
    assert (await _row(workspace["id"])).deleted_at != 0
    folder = await _node(workspace["files_node_id"])
    assert folder.trashed_at is not None


async def test_offboarding_ends_a_departed_members_workspaces(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """An org admin ends a deactivated member's private workspace, main one
    included, without reading it; while the member is active it stays the
    same not-found."""
    member, password = await _member(real_session, org_admin)
    async with app_client() as other:
        await login(other, member.email, password)
        workspace = await _workspace(other)
        main = (await other.get(f"{WORKSPACES}/main")).json()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.delete(f"{WORKSPACES}/{workspace['id']}")).status_code == 404
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE users SET is_active = false WHERE id = :id"), {"id": member.id}
        )
        await db.commit()

    ended = await client.delete(f"{WORKSPACES}/{workspace['id']}")
    ended_main = await client.delete(f"{WORKSPACES}/{main['id']}")

    assert (ended.status_code, ended_main.status_code) == (204, 204)
    assert (await _row(workspace["id"])).deleted_at != 0
    assert ("delete", "allow", "org_admin_offboards") in await _decisions(
        org_admin.org_id, workspace["id"]
    )
    async with AsyncSessionLocal() as db:
        audited = await db.execute(
            select(OrgAuditEvent.target).where(
                OrgAuditEvent.org_team_id == org_admin.org_id,
                OrgAuditEvent.action == "workspace.offboarded",
            )
        )
        assert set(audited.scalars()) == {workspace["id"], main["id"]}


@pytest.mark.usefixtures("multi_chat")
async def test_a_member_whose_main_workspace_was_offboarded_gets_a_new_one_back(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Offboarding ended a departed member's main workspace. Reactivated, the
    member has a main workspace again on first ask, and a chat started with no
    place named lands in it; the ended one stays ended."""
    member, password = await _member(real_session, org_admin)
    async with app_client() as other:
        await login(other, member.email, password)
        ended_main = (await other.get(f"{WORKSPACES}/main")).json()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    for active in (False, True):
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE users SET is_active = :on WHERE id = :id"),
                {"on": active, "id": member.id},
            )
            await db.commit()
        if not active:
            assert (await client.delete(f"{WORKSPACES}/{ended_main['id']}")).status_code == 204

    async with app_client() as back:
        await login(back, member.email, password)
        main = await back.get(f"{WORKSPACES}/main")
        started = await back.post(CHATS, json={"title": "First day back"})
        again = await back.get(f"{WORKSPACES}/main")

    assert main.status_code == 200, main.text
    assert main.json()["id"] != ended_main["id"]
    assert main.json()["kind"] == "main"
    assert started.status_code == 201, started.text
    assert started.json()["workspace_id"] == main.json()["id"]
    assert again.json()["id"] == main.json()["id"], "one main workspace, made once"
    assert (await _row(ended_main["id"])).deleted_at != 0


async def test_without_the_flag_a_project_workspace_is_refused_and_main_still_made(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_multi_chat", False)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    refused = await client.post(WORKSPACES, json={"title": "Pricing"})
    main = await client.get(f"{WORKSPACES}/main")

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "workspaces_multi_chat_disabled"
    assert main.status_code == 200, main.text


async def _outsider() -> tuple[UUID, AsyncClient]:
    """A member of another org, logged in."""
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Elsewhere {uuid.uuid4().hex[:8]}",
            admin_email=f"elsewhere-{uuid.uuid4().hex[:12]}@alkera.dev",
            admin_first_name="Else",
            admin_last_name="Where",
            admin_password="pw-1234567890",
        )
        await session.commit()
        member, password = await make_member(session, org_id=org.id, verified=True)
    outsider = app_client()
    await login(outsider, member.email, password or "")
    return org.id, outsider


async def test_a_workspace_in_another_org_is_not_found_and_the_probe_is_on_record(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Another org's workspace is the same not-found as one that does not
    exist, through the workspace route and through starting a chat in it, and
    each probe leaves a cross-org refusal on record in the org it reached."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(client, "Ours")
    _org, outsider = await _outsider()
    async with outsider:
        read = await outsider.get(f"{WORKSPACES}/{workspace['id']}")
        with projects_allowed():
            started = await outsider.post(
                CHATS, json={"title": "In theirs", "workspace_id": workspace["id"]}
            )

    assert (read.status_code, started.status_code) == (404, 404)
    refusals = [
        row for row in await _decisions(org_admin.org_id, workspace["id"]) if row[1] == "deny"
    ]
    assert [(action, reason) for action, _effect, reason in refusals] == [
        ("read", "cross_org"),
        ("write", "cross_org"),
    ]


@pytest.mark.usefixtures("multi_chat")
async def test_a_client_id_another_member_used_is_never_their_workspace_handed_back(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A ``client_id`` is a retry key for the caller's own request. The same
    key from a colleague who can read the workspace it made is a different
    request, refused, not a replay that hands them someone else's workspace
    as the one they just created."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"ws-{uuid.uuid4()}"
    made = await client.post(WORKSPACES, json={"title": "Pricing", "client_id": client_id})
    assert made.status_code == 201, made.text
    member, password = await _member(real_session, org_admin)
    await _grant(client, made.json(), member, ROLE_WRITER)

    async with app_client() as other:
        await login(other, member.email, password)
        assert (await other.get(f"{WORKSPACES}/{made.json()['id']}")).status_code == 200
        replayed = await other.post(WORKSPACES, json={"title": "Pricing", "client_id": client_id})

    assert replayed.status_code == 409, replayed.text
    assert replayed.json()["error"]["code"] == "client_id_in_use"
