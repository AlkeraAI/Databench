"""Deleting a workspace that holds chats: one bounded request, then a drain.

The delete request makes the workspace and every chat in it unreachable at
once, rings each chat's doorbell with the deletion as the reason and ends
every lease under its folder, in a number of statements that does not grow
with the chats it holds. What is left of each chat's ending (asleep, its
folder trashed) and the workspace's own folder going to the trash is finished
by the background drain,
a committed batch at a time; ``GET /workspaces/{id}/deletion`` says how far it
has got, and a drain run again after it finished changes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal, engine
from alkera_core.models import EventOutbox, WorkspaceObject
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.tree import FileNode
from alkera_core.objects import workspace_end
from alkera_core.temporal import MORE_WORK_SIGNAL, WorkflowType
from httpx import AsyncClient
from sqlalchemy import event, func, select, text
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on", "multi_chat")]

WORKSPACES = "/api/v1/workspaces"
CHATS = "/api/v1/chats"


@pytest.fixture
def multi_chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    yield


async def _workspace_with_chats(
    client: AsyncClient, chats: int
) -> tuple[dict[str, Any], list[str]]:
    made = await client.post(WORKSPACES, json={"title": "Pricing study"})
    assert made.status_code == 201, made.text
    workspace: dict[str, Any] = made.json()
    ids = []
    for n in range(chats):
        chat = await client.post(
            CHATS, json={"title": f"Chat {n}", "workspace_id": workspace["id"]}
        )
        assert chat.status_code == 201, chat.text
        ids.append(chat.json()["id"])
    return workspace, ids


async def _copies_of(chat_id: str, count: int) -> None:
    """``count`` more chats in the same workspace, as rows: what a workspace a
    person used for months holds, without starting each through the route."""
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        await db.execute(
            text(
                "INSERT INTO workspace_objects (id, org_team_id, logical_id, namespace, type, "
                "title, version, status, spec, owner_user_id, team_id, visibility_scope, "
                "content_updated_at, deleted_at, created_at, updated_at) "
                "SELECT gen_random_uuid(), org_team_id, gen_random_uuid()::text, namespace, type, "
                "title, version, status, spec, owner_user_id, team_id, visibility_scope, "
                "content_updated_at, 0, created_at, updated_at "
                "FROM workspace_objects, generate_series(1, :count) WHERE id = :id"
            ),
            {"id": UUID(chat_id), "count": count},
        )
        await db.commit()


@contextmanager
def _counting_statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def record(*args: Any) -> None:
        seen.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def _row(object_id: str | UUID) -> WorkspaceObject:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        row = await db.get(WorkspaceObject, UUID(str(object_id)))
        assert row is not None
        return row


async def _node(node_id: str | UUID) -> FileNode:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        node = await db.get(FileNode, UUID(str(node_id)))
        assert node is not None
        return node


async def _deleted_doorbells(org_id: UUID, chat_ids: list[str]) -> int:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "chat.updated",
                EventOutbox.entity_id.in_(chat_ids),
            )
        )
        return sum(1 for row in rows.scalars() if row.payload.get("reason") == "deleted")


async def _finish(batch: int) -> workspace_end.Finished:
    async with AsyncSessionLocal() as db:
        done = await workspace_end.finish(db, batch=batch)
        await db.commit()
        return done


async def _finish_all() -> None:
    for _ in range(100):
        done = await _finish(workspace_end.BATCH)
        if not (done.chats or done.workspaces):
            return
    raise AssertionError("the drain never ran dry")


async def _deletion(client: AsyncClient, workspace_id: str) -> dict[str, Any]:
    resp = await client.get(f"{WORKSPACES}/{workspace_id}/deletion")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_the_delete_request_is_bounded_however_many_chats_the_workspace_holds(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The same statements end a workspace of two chats and one of two hundred
    and two: the request never walks the chats one by one."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    small, _small_chats = await _workspace_with_chats(client, 2)
    large, large_chats = await _workspace_with_chats(client, 2)
    await _copies_of(large_chats[0], 200)

    with _counting_statements() as for_small:
        assert (await client.delete(f"{WORKSPACES}/{small['id']}")).status_code == 204
    with _counting_statements() as for_large:
        assert (await client.delete(f"{WORKSPACES}/{large['id']}")).status_code == 204

    assert len(for_large) == len(for_small), (len(for_small), len(for_large))
    assert (await _deletion(client, large["id"]))["chats_remaining"] == 202
    assert await _deleted_doorbells(org_admin.org_id, large_chats) == 2


async def test_a_chat_of_a_deleted_workspace_is_unreachable_before_the_drain_runs(
    client: AsyncClient, org_admin: OrgWithAdmin, nudge_recorder: Any
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace, (chat_id, other_id) = await _workspace_with_chats(client, 2)

    assert (await client.delete(f"{WORKSPACES}/{workspace['id']}")).status_code == 204

    assert (await client.get(f"{CHATS}/{chat_id}")).status_code == 404
    listed = (await client.get(CHATS)).json()["items"]
    assert {chat_id, other_id}.isdisjoint({item["id"] for item in listed})
    assert (await client.get(f"{WORKSPACES}/{workspace['id']}")).status_code == 404
    assert (await client.get(f"{WORKSPACES}/{workspace['id']}/chats")).status_code == 404
    filed = await client.post(CHATS, json={"title": "Late", "workspace_id": workspace["id"]})
    assert filed.status_code in (404, 409), filed.text
    assert (await _row(chat_id)).deleted_at != 0
    # The box serving each chat is told it was deleted by the request itself.
    assert await _deleted_doorbells(org_admin.org_id, [chat_id, other_id]) == 2
    # The drain was asked for at once, as a signal-with-start on its singleton id.
    nudges = nudge_recorder.for_workflow(WorkflowType.FINISH_WORKSPACE_DELETIONS.value)
    assert [(n.workflow_id, n.signal) for n in nudges] == [
        (WorkflowType.FINISH_WORKSPACE_DELETIONS.value, MORE_WORK_SIGNAL)
    ]


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("native", id="a-chat-of-a-project-workspace"),
        pytest.param("of_one", id="the-chat-a-workspace-of-one-is"),
    ],
)
async def test_the_delete_ends_every_lease_under_the_workspace_folder(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """A box serving a chat of the workspace is fenced by the request itself:
    the lease on that chat's folder is released before the drain runs."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    if shape == "native":
        workspace, (chat_id,) = await _workspace_with_chats(client, 1)
        workspace_id = workspace["id"]
    else:
        monkeypatch.setattr(settings, "workspaces_multi_chat", False)
        alone = await client.post(CHATS, json={"title": "Alone"})
        assert alone.status_code == 201, alone.text
        chat_id, workspace_id = alone.json()["id"], alone.json()["workspace_id"]
    folder = UUID((await client.get(f"{CHATS}/{chat_id}")).json()["files_node_id"])
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        node = await db.get(FileNode, folder)
        assert node is not None
        db.add(
            FileLease(
                node_id=folder,
                org_team_id=node.org_team_id,
                epoch=1,
                holder_principal_kind="user",
                holder_kind="machine",
                holder_principal_id=uuid4(),
                holder_instance_id="box:chat",
                machine_id="box",
                purpose="chat",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await db.commit()

    assert (await client.delete(f"{WORKSPACES}/{workspace_id}")).status_code == 204

    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        live = await db.scalar(
            select(func.count())
            .select_from(FileLease)
            .where(
                FileLease.node_id == folder,
                FileLease.released_at.is_(None),
                FileLease.expires_at > func.now(),
            )
        )
    assert live == 0


async def test_the_drain_finishes_in_batches_and_reports_progress(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace, chat_ids = await _workspace_with_chats(client, 3)
    folders = [(await client.get(f"{CHATS}/{c}")).json()["files_node_id"] for c in chat_ids]
    # The drain is database-wide: whatever earlier tests left is finished
    # first, so the passes below see this workspace's chats alone.
    await _finish_all()
    assert (await client.delete(f"{WORKSPACES}/{workspace['id']}")).status_code == 204
    assert await _deletion(client, workspace["id"]) == {
        "id": workspace["id"],
        "state": "deleting",
        "chats_remaining": 3,
    }
    # Nothing of the chats' or the workspace's folders moved yet.
    assert (await _node(workspace["files_node_id"])).trashed_at is None

    first = await _finish(batch=2)

    assert (first.chats, first.workspaces) == (2, 0)
    assert (await _deletion(client, workspace["id"]))["chats_remaining"] == 1
    assert (await _node(workspace["files_node_id"])).trashed_at is None

    second = await _finish(batch=2)

    assert (second.chats, second.workspaces) == (1, 1)
    assert await _deletion(client, workspace["id"]) == {
        "id": workspace["id"],
        "state": "deleted",
        "chats_remaining": 0,
    }
    assert (await _node(workspace["files_node_id"])).trashed_at is not None
    for folder in folders:
        assert (await _node(folder)).trashed_at is not None
    for chat_id in chat_ids:
        row = await _row(chat_id)
        assert row.spec.get("mirror_state") == "asleep"
        assert workspace_end.ENDING_KEY not in row.spec
    assert await _deleted_doorbells(org_admin.org_id, chat_ids) == 3


async def test_a_drain_run_again_after_it_finished_changes_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace, chat_ids = await _workspace_with_chats(client, 2)
    assert (await client.delete(f"{WORKSPACES}/{workspace['id']}")).status_code == 204
    await _finish_all()
    rows_before = [(await _row(c)).version for c in chat_ids]
    rings_before = await _deleted_doorbells(org_admin.org_id, chat_ids)

    again = await _finish(batch=workspace_end.BATCH)

    assert (again.chats, again.workspaces, again.failed) == (0, 0, 0)
    assert [(await _row(c)).version for c in chat_ids] == rows_before
    assert await _deleted_doorbells(org_admin.org_id, chat_ids) == rings_before == 2


async def test_an_empty_workspace_is_finished_by_the_request(
    client: AsyncClient, org_admin: OrgWithAdmin, nudge_recorder: Any
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace, _ = await _workspace_with_chats(client, 0)

    assert (await client.delete(f"{WORKSPACES}/{workspace['id']}")).status_code == 204

    assert (await _node(workspace["files_node_id"])).trashed_at is not None
    assert (await _deletion(client, workspace["id"]))["state"] == "deleted"
    assert nudge_recorder.for_workflow(WorkflowType.FINISH_WORKSPACE_DELETIONS.value) == []


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("live", id="a-live-workspace-has-no-deletion"),
        pytest.param("unknown", id="an-unknown-id"),
    ],
)
async def test_the_deletion_read_is_not_found_for_a_workspace_never_deleted(
    client: AsyncClient, org_admin: OrgWithAdmin, path: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace, _ = await _workspace_with_chats(client, 0)
    target = workspace["id"] if path == "live" else "00000000-0000-4000-8000-000000000000"

    resp = await client.get(f"{WORKSPACES}/{target}/deletion")

    assert resp.status_code == 404, resp.text
