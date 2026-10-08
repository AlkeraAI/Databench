"""The drain that finishes a deleted workspace's chats: the async core against
the real local Postgres, and where the job is registered.

A workspace deleted with chats in it leaves every chat tombstoned, stamped
with ``ending_workspace_id`` and announced with the deletion as the reason.
Each pass finishes at most its page of chats (asleep, unstamped) and commits;
a workspace whose chats are all finished is unstamped too. Rows of a
workspace nobody deleted are never touched, and a pass run again after the
drain finished changes nothing.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, Team, User, WorkspaceObject
from alkera_core.objects import workspace_end
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from sqlalchemy import select, text
from worker.activities.workspaces import finish_workspace_deletions
from worker.schedules import SCHEDULES
from worker.tasks.workspaces import run_finish_workspace_deletions
from worker.temporal.queues import activity_type_name, served_workflow_types
from worker.temporal.retry import TRANSIENT_RETRY, policy_for


async def _org() -> tuple[UUID, UUID]:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"ws-end-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"ws-end-{secrets.token_hex(6)}@alkera.dev",
            first_name="Work",
            last_name="Space",
        )
        s.add(user)
        await s.commit()
        return team.id, user.id


async def _workspace_with_chats(org: UUID, owner: UUID, chats: int) -> tuple[UUID, list[UUID]]:
    async with AsyncSessionLocal() as s:
        workspace = WorkspaceObject(
            id=uuid4(),
            org_team_id=org,
            logical_id=uuid4().hex,
            type="workspace",
            title="Pricing",
            spec={"schema_version": "1.0.0", "kind": "project", "layout": "native"},
            owner_user_id=owner,
            visibility_scope="private",
        )
        s.add(workspace)
        ids = []
        for n in range(chats):
            chat = WorkspaceObject(
                id=uuid4(),
                org_team_id=org,
                logical_id=uuid4().hex,
                type="chat",
                title=f"Chat {n}",
                spec={"workspace_id": str(workspace.id), "mirror_state": "awake"},
                owner_user_id=owner,
                visibility_scope="private",
            )
            s.add(chat)
            ids.append(chat.id)
        await s.commit()
        return workspace.id, ids


async def _delete(workspace_id: UUID) -> int:
    """What the delete request does to the rows: the chats tombstoned and
    stamped, then the workspace tombstoned."""
    async with AsyncSessionLocal() as s:
        workspace = await s.get(WorkspaceObject, workspace_id)
        assert workspace is not None
        ended = await workspace_end.begin(s, workspace=workspace, now=datetime.now(UTC), actor=None)
        workspace.deleted_at = datetime.now(UTC).timestamp()
        await s.commit()
        return ended


async def _rows(ids: list[UUID]) -> list[WorkspaceObject]:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(select(WorkspaceObject).where(WorkspaceObject.id.in_(ids)))
        ).scalars()
        return sorted(rows, key=lambda row: ids.index(row.id))


async def _doorbells(org: UUID) -> int:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(EventOutbox).where(EventOutbox.org_id == org, EventOutbox.type == "chat.updated")
        )
        return sum(1 for row in rows.scalars() if row.payload.get("reason") == "deleted")


async def _drain_everything() -> None:
    for _ in range(200):
        done = await run_finish_workspace_deletions(limit=workspace_end.BATCH)
        if not (done.chats or done.workspaces):
            return
    raise AssertionError("the drain never ran dry")


async def test_begin_tombstones_and_stamps_only_the_deleted_workspaces_chats() -> None:
    org, owner = await _org()
    doomed, doomed_chats = await _workspace_with_chats(org, owner, 3)
    kept, kept_chats = await _workspace_with_chats(org, owner, 2)

    assert await _delete(doomed) == 3

    assert await _doorbells(org) == 3
    for row in await _rows(doomed_chats):
        assert row.deleted_at != 0
        assert row.spec[workspace_end.ENDING_KEY] == str(doomed)
    for row in await _rows(kept_chats):
        assert row.deleted_at == 0
        assert workspace_end.ENDING_KEY not in row.spec
    (workspace,) = await _rows([doomed])
    assert workspace_end.is_ending(workspace)
    async with AsyncSessionLocal() as s:
        assert await workspace_end.chats_remaining(s, doomed) == 3
        assert await workspace_end.chats_remaining(s, kept) == 0
    await _drain_everything()


async def test_a_pass_finishes_one_page_and_commits_it() -> None:
    await _drain_everything()
    org, owner = await _org()
    workspace, chats = await _workspace_with_chats(org, owner, 5)
    await _delete(workspace)

    first = await run_finish_workspace_deletions(limit=2)

    assert (first.chats, first.workspaces, first.failed) == (2, 0, 0)
    async with AsyncSessionLocal() as s:
        assert await workspace_end.chats_remaining(s, workspace) == 3
    finished = [row for row in await _rows(chats) if workspace_end.ENDING_KEY not in row.spec]
    assert len(finished) == 2
    assert all(row.spec["mirror_state"] == "asleep" for row in finished)
    # Each chat's doorbell rang once, in the delete; finishing it rings none.
    assert await _doorbells(org) == 5
    (still,) = await _rows([workspace])
    assert workspace_end.is_ending(still)


async def test_the_drain_finishes_the_workspace_and_a_rerun_changes_nothing() -> None:
    org, owner = await _org()
    workspace, chats = await _workspace_with_chats(org, owner, 4)
    await _delete(workspace)

    await _drain_everything()

    rows = await _rows([workspace, *chats])
    assert all(workspace_end.ENDING_KEY not in row.spec for row in rows)
    versions = [row.version for row in rows]

    again = await run_finish_workspace_deletions(limit=workspace_end.BATCH)

    assert (again.chats, again.workspaces, again.failed) == (0, 0, 0)
    assert [row.version for row in await _rows([workspace, *chats])] == versions


async def test_a_chat_held_by_another_pass_is_skipped_not_waited_on() -> None:
    await _drain_everything()
    org, owner = await _org()
    workspace, (held, free) = await _workspace_with_chats(org, owner, 2)
    await _delete(workspace)

    async with AsyncSessionLocal() as holder:
        await holder.execute(
            text("SELECT 1 FROM workspace_objects WHERE id = :id FOR UPDATE"), {"id": held}
        )
        done = await run_finish_workspace_deletions(limit=workspace_end.BATCH)
        await holder.rollback()

    assert (done.chats, done.workspaces) == (1, 0)
    held_row, free_row = await _rows([held, free])
    assert workspace_end.ENDING_KEY in held_row.spec
    assert workspace_end.ENDING_KEY not in free_row.spec
    await _drain_everything()


async def test_the_pending_rows_are_read_off_their_partial_index() -> None:
    """On a table of thousands of finished chats a pass reads the few rows it
    has to finish off ``ix_workspace_objects_ending``, never the table."""
    await _drain_everything()
    org, owner = await _org()
    await _seed_finished_chats(org, owner, 3000)
    workspace, _chats = await _workspace_with_chats(org, owner, 2)
    await _delete(workspace)
    async with AsyncSessionLocal() as s:
        await s.execute(text("ANALYZE workspace_objects"))
        plan = await _plan(s, workspace_end._CHATS_TO_FINISH.text, {"batch": 100})
        await s.commit()
    assert "ix_workspace_objects_ending" in plan, plan
    await _drain_everything()


async def _seed_finished_chats(org: UUID, owner: UUID, count: int) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO workspace_objects (id, org_team_id, logical_id, namespace, type, "
                "title, version, status, spec, owner_user_id, visibility_scope, deleted_at, "
                "created_at, updated_at) "
                "SELECT gen_random_uuid(), :org, gen_random_uuid()::text, 'workspace', 'chat', "
                "'old', 1, 'ready', jsonb_build_object('workspace_id', gen_random_uuid()::text), "
                ":owner, 'private', 0, now(), now() FROM generate_series(1, :count)"
            ),
            {"org": org, "owner": owner, "count": count},
        )
        await s.commit()


async def _plan(s: Any, sql: str, params: dict[str, object]) -> str:
    rows = await s.execute(text(f"EXPLAIN {sql}"), params)
    return "\n".join(row[0] for row in rows)


def test_the_drain_is_registered_on_the_default_queue_with_a_transient_retry() -> None:
    entry = next(e for e in SCHEDULES if e.workflow is WorkflowType.FINISH_WORKSPACE_DELETIONS)
    assert entry.every == timedelta(minutes=2)
    assert QUEUE_FOR[WorkflowType.FINISH_WORKSPACE_DELETIONS] is TaskQueue.DEFAULT
    assert policy_for(WorkflowType.FINISH_WORKSPACE_DELETIONS.value).retry is TRANSIENT_RETRY
    assert WorkflowType.FINISH_WORKSPACE_DELETIONS.value in served_workflow_types()
    assert (
        activity_type_name(finish_workspace_deletions)
        == WorkflowType.FINISH_WORKSPACE_DELETIONS.value
    )
