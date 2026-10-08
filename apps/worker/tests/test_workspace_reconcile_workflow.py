"""The workspace reconcile pass as a Temporal workflow: the real workflow and
the real activity through a real Worker, against the test database. A stray
chat seeded before the run names its workspace of one after it."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team, User, WorkspaceObject
from temporalio.client import Client
from worker.activities.workspaces import reconcile_workspaces
from worker.workflows.workspaces import ReconcileWorkspaces

pytestmark = pytest.mark.temporal


async def test_a_run_adopts_a_stray_chat(temporal_worker: Any, temporal_client: Client) -> None:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"ws-wf-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"ws-wf-{secrets.token_hex(6)}@alkera.dev",
            first_name="Work",
            last_name="Flow",
        )
        s.add(user)
        await s.flush()
        chat = WorkspaceObject(
            id=uuid4(),
            org_team_id=team.id,
            logical_id=uuid4().hex,
            type="chat",
            title="stray",
            spec={},
            owner_user_id=user.id,
            visibility_scope="private",
        )
        s.add(chat)
        await s.commit()

    async with temporal_worker(
        workflows=[ReconcileWorkspaces], activities=[reconcile_workspaces]
    ) as running:
        changed = await temporal_client.execute_workflow(
            ReconcileWorkspaces.run,
            id=f"reconcile-{uuid4()}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

    assert changed >= 1
    async with AsyncSessionLocal() as s:
        row = await s.get(WorkspaceObject, chat.id)
        assert row is not None and row.spec.get("workspace_id")
