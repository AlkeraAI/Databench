"""The deletion drain as a Temporal workflow: the real workflow and the real
activity through a real Worker, against the test database. A workspace
deleted with chats in it is finished by one run: every chat unstamped and
asleep, and the workspace itself unstamped."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team, User, WorkspaceObject
from alkera_core.objects import workspace_end
from alkera_core.schemas.temporal import DrainInput
from sqlalchemy import select
from temporalio.client import Client
from worker.activities.workspaces import finish_workspace_deletions
from worker.workflows.workspaces import FinishWorkspaceDeletions

pytestmark = pytest.mark.temporal


async def test_a_run_finishes_a_deleted_workspace(
    temporal_worker: Any, temporal_client: Client
) -> None:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"ws-end-wf-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"ws-end-wf-{secrets.token_hex(6)}@alkera.dev",
            first_name="Work",
            last_name="Flow",
        )
        s.add(user)
        await s.flush()
        workspace = WorkspaceObject(
            id=uuid4(),
            org_team_id=team.id,
            logical_id=uuid4().hex,
            type="workspace",
            title="Pricing",
            spec={"schema_version": "1.0.0", "kind": "project", "layout": "native"},
            owner_user_id=user.id,
            visibility_scope="private",
        )
        s.add(workspace)
        chat_ids = []
        for n in range(5):
            chat = WorkspaceObject(
                id=uuid4(),
                org_team_id=team.id,
                logical_id=uuid4().hex,
                type="chat",
                title=f"Chat {n}",
                spec={"workspace_id": str(workspace.id), "mirror_state": "awake"},
                owner_user_id=user.id,
                visibility_scope="private",
            )
            s.add(chat)
            chat_ids.append(chat.id)
        await s.flush()
        assert (
            await workspace_end.begin(s, workspace=workspace, now=datetime.now(UTC), actor=None)
            == 5
        )
        workspace.deleted_at = datetime.now(UTC).timestamp()
        await s.commit()

    async with temporal_worker(
        workflows=[FinishWorkspaceDeletions], activities=[finish_workspace_deletions]
    ) as running:
        # A page of two: the loop must go back for the rest on its own.
        report = await temporal_client.execute_workflow(
            FinishWorkspaceDeletions.run,
            DrainInput(limit=2, max_passes=200),
            id=f"finish-deletions-{uuid4()}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

    assert report.processed >= 5
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                select(WorkspaceObject).where(WorkspaceObject.id.in_([workspace.id, *chat_ids]))
            )
        ).scalars()
        rows_by_id = {row.id: row for row in rows}
    assert all(workspace_end.ENDING_KEY not in row.spec for row in rows_by_id.values())
    assert all(rows_by_id[chat_id].spec["mirror_state"] == "asleep" for chat_id in chat_ids)
