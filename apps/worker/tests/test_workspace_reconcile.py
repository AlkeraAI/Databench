"""The workspace reconcile pass: the async core against the real local Postgres,
the schedule pin, and the activity registry.

A chat a previous build created with no workspace is adopted into a workspace
of one keyed by its id, and a workspace of one whose chat is gone is retired;
a live chat's workspace and a native workspace are never touched, and a second
pass changes nothing.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, Team, User, WorkspaceObject
from alkera_core.objects import workspace_reconcile
from alkera_core.objects.workspaces import ADOPTED_NAMESPACE
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from sqlalchemy import select, text
from worker.activities.workspaces import reconcile_workspaces
from worker.schedules import SCHEDULES
from worker.tasks.workspaces import run_reconcile_workspaces
from worker.temporal.queues import activity_type_name, served_workflow_types
from worker.temporal.retry import policy_for


async def _org() -> tuple[UUID, UUID]:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"ws-org-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"ws-{secrets.token_hex(6)}@alkera.dev",
            first_name="Work",
            last_name="Space",
        )
        s.add(user)
        await s.commit()
        return team.id, user.id


async def _row(
    org: UUID,
    owner: UUID,
    *,
    type: str,
    spec: dict[str, Any],
    namespace: str = "workspace",
    logical_id: str | None = None,
    deleted_at: float = 0,
) -> UUID:
    async with AsyncSessionLocal() as s:
        row = WorkspaceObject(
            id=uuid4(),
            org_team_id=org,
            logical_id=logical_id or uuid4().hex,
            namespace=namespace,
            type=type,
            title="t",
            spec=spec,
            owner_user_id=owner,
            visibility_scope="private",
            deleted_at=deleted_at,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _get(object_id: UUID) -> WorkspaceObject:
    async with AsyncSessionLocal() as s:
        row = await s.get(WorkspaceObject, object_id)
        assert row is not None
        return row


async def _adopted_for(chat_id: UUID) -> list[WorkspaceObject]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.namespace == ADOPTED_NAMESPACE,
                WorkspaceObject.logical_id == str(chat_id),
            )
        )
        return list(rows.scalars())


async def test_a_pass_adopts_strays_and_retires_orphans_and_touches_nothing_else() -> None:
    org, owner = await _org()
    stray = await _row(org, owner, type="chat", spec={"schema_version": "1.9.0"})
    gone_chat = await _row(org, owner, type="chat", spec={}, deleted_at=1.0)
    orphan = await _row(
        org,
        owner,
        type="workspace",
        namespace=ADOPTED_NAMESPACE,
        logical_id=str(gone_chat),
        spec={"layout": "adopted", "adopted_chat_id": str(gone_chat)},
    )
    native = await _row(org, owner, type="workspace", spec={"kind": "project", "layout": "native"})
    held = await _row(org, owner, type="chat", spec={"workspace_id": str(native)})
    now = datetime.now(UTC)

    assert await run_reconcile_workspaces(now) >= 2

    (made,) = await _adopted_for(stray)
    assert made.spec["adopted_chat_id"] == str(stray) and made.deleted_at == 0
    assert (await _get(stray)).spec["workspace_id"] == str(made.id)
    assert (await _get(orphan)).deleted_at == now.timestamp()
    assert (await _get(native)).deleted_at == 0
    assert (await _get(held)).spec["workspace_id"] == str(native)
    assert [w.id for w in await _adopted_for(gone_chat)] == [orphan]

    await run_reconcile_workspaces(now + timedelta(minutes=15))
    assert [w.id for w in await _adopted_for(stray)] == [made.id], "a second pass adds nothing"


async def test_a_stray_that_cannot_be_adopted_never_holds_up_the_batch() -> None:
    """A chat whose workspace of one was deleted stays as it is, and is not
    selected again: a batch of one still reaches the stray behind it, even
    when the unadoptable chat sorts first."""
    org, owner = await _org()
    first, second = sorted([uuid4(), uuid4()])
    async with AsyncSessionLocal() as s:
        for chat_id in (first, second):
            s.add(
                WorkspaceObject(
                    id=chat_id,
                    org_team_id=org,
                    logical_id=uuid4().hex,
                    type="chat",
                    title="t",
                    spec={},
                    owner_user_id=owner,
                    visibility_scope="private",
                )
            )
        await s.commit()
    await _row(
        org,
        owner,
        type="workspace",
        namespace=ADOPTED_NAMESPACE,
        logical_id=str(first),
        spec={"layout": "adopted", "adopted_chat_id": str(first)},
        deleted_at=1.0,
    )
    now = datetime.now(UTC)

    async with AsyncSessionLocal() as s:
        await workspace_reconcile.reconcile(s, now=now, batch=1)
        await s.commit()

    assert "workspace_id" not in (await _get(first)).spec
    assert (await _get(second)).spec.get("workspace_id"), "the batch reached the next stray"


async def test_with_adoption_off_a_pass_adopts_nothing_and_still_retires_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a task of a build from before workspaces may still serve, no chat
    is put in a workspace; retiring an orphan makes no workspace row, so it
    goes on."""
    monkeypatch.setattr(settings, "workspaces_adoption_enabled", False)
    org, owner = await _org()
    stray = await _row(org, owner, type="chat", spec={"schema_version": "1.9.0"})
    gone_chat = await _row(org, owner, type="chat", spec={}, deleted_at=1.0)
    orphan = await _row(
        org,
        owner,
        type="workspace",
        namespace=ADOPTED_NAMESPACE,
        logical_id=str(gone_chat),
        spec={"layout": "adopted", "adopted_chat_id": str(gone_chat)},
    )
    now = datetime.now(UTC)

    await run_reconcile_workspaces(now)

    assert await _adopted_for(stray) == []
    assert "workspace_id" not in (await _get(stray)).spec
    assert (await _get(orphan)).deleted_at == now.timestamp()

    monkeypatch.setattr(settings, "workspaces_adoption_enabled", True)
    await run_reconcile_workspaces(now)
    assert len(await _adopted_for(stray)) == 1, "turning it on adopts what was left"


async def test_a_drain_commits_pass_after_pass_and_stops_at_its_bound() -> None:
    """The backlog the schema revision leaves is walked a committed batch at a
    time: a run stops after its last pass, and the next run picks up the rest."""
    org, owner = await _org()
    strays = [await _row(org, owner, type="chat", spec={}) for _ in range(3)]
    now = datetime.now(UTC)

    bounded = await workspace_reconcile.drain(
        AsyncSessionLocal, now=now, batch=1, max_passes=2, adopt=True
    )

    assert bounded.adopted == 2
    rest = await workspace_reconcile.drain(
        AsyncSessionLocal, now=now, batch=1, max_passes=10_000, adopt=True
    )
    assert rest.adopted >= 1
    for chat_id in strays:
        (made,) = await _adopted_for(chat_id)
        assert (await _get(chat_id)).spec["workspace_id"] == str(made.id)


async def test_an_adopted_chat_is_announced_so_a_holder_re_reads_it() -> None:
    org, owner = await _org()
    stray = await _row(org, owner, type="chat", spec={"machine_id": "box-1"})
    async with AsyncSessionLocal() as s:
        await workspace_reconcile.reconcile(s, now=datetime.now(UTC), adopt=True)
        await s.commit()
        rows = (
            await s.execute(
                select(EventOutbox).where(
                    EventOutbox.type == "chat.updated", EventOutbox.entity_id == str(stray)
                )
            )
        ).scalars()
        announced = [(row.org_id, row.payload["machine_id"]) for row in rows]
    assert len(await _adopted_for(stray)) == 1
    assert announced == [(org, "box-1")]


async def test_an_orphan_whose_logical_id_is_no_chat_id_is_retired_not_a_failed_pass() -> None:
    org, owner = await _org()
    mangled = await _row(
        org,
        owner,
        type="workspace",
        namespace=ADOPTED_NAMESPACE,
        logical_id="not-a-chat-id",
        spec={"layout": "adopted"},
    )
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as s:
        await workspace_reconcile.reconcile(s, now=now, adopt=True)
        await s.commit()
    assert (await _get(mangled)).deleted_at == now.timestamp()


def test_the_pass_is_a_quarter_hour_schedule_on_the_default_queue_with_no_retry() -> None:
    entry = next(e for e in SCHEDULES if e.workflow is WorkflowType.RECONCILE_WORKSPACES)
    assert entry.every == timedelta(minutes=15)
    assert QUEUE_FOR[WorkflowType.RECONCILE_WORKSPACES] is TaskQueue.DEFAULT
    assert policy_for(WorkflowType.RECONCILE_WORKSPACES.value).retry.maximum_attempts == 1
    assert WorkflowType.RECONCILE_WORKSPACES.value in served_workflow_types()
    assert activity_type_name(reconcile_workspaces) == WorkflowType.RECONCILE_WORKSPACES.value


async def test_the_stray_scan_reads_its_partial_index_on_a_table_of_adopted_chats() -> None:
    """Every fifteen minutes the pass looks for live chats in no workspace. On
    a table where thousands of chats are adopted and a few are strays, it
    reads them off ``ix_workspace_objects_chat_unadopted`` rather than walking
    the table (or the primary key) to find them."""
    org, owner = await _org()
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO workspace_objects (id, org_team_id, logical_id, namespace, type, "
                "title, version, status, spec, owner_user_id, visibility_scope, deleted_at, "
                "created_at, updated_at) "
                "SELECT gen_random_uuid(), :org, gen_random_uuid()::text, 'workspace', 'chat', "
                "'adopted', 1, 'ready', "
                "jsonb_build_object('workspace_id', gen_random_uuid()::text), "
                ":owner, 'private', 0, now(), now() FROM generate_series(1, 3000)"
            ),
            {"org": org, "owner": owner},
        )
        await s.commit()
    for _ in range(3):
        await _row(org, owner, type="chat", spec={})
    async with AsyncSessionLocal() as s:
        await s.execute(text("ANALYZE workspace_objects"))
        rows = await s.execute(
            text(f"EXPLAIN {workspace_reconcile._ADOPT_STRAYS.text}"), {"batch": 500}
        )
        plan = "\n".join(row[0] for row in rows)
        await s.commit()

    assert "ix_workspace_objects_chat_unadopted" in plan, plan
