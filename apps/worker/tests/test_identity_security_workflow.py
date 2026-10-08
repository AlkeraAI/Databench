"""The identity security log's retention prune as a Temporal workflow: the real
workflow and the real activity through a real Worker, against the test database.

The prune removes exactly the rows past the window and spares the rest; a
dropped connection is attempted again (the batches already committed stay
deleted, so the retry only finishes the job); and the catalog entry is created
by the schedule reconciler.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import IdentitySecurityEvent, Team, User
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import WorkflowType
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from temporalio.client import Client
from worker.activities import identity_security as activities
from worker.schedules import SCHEDULES, ScheduleSyncReport, fingerprint, sync_schedules
from worker.tasks import identity_security as tasks
from worker.temporal import queues
from worker.workflows.identity_security import PruneIdentitySecurityEvents

pytestmark = pytest.mark.temporal

NOW = datetime(2027, 5, 6, 7, 8, 9, tzinfo=UTC)


async def _seed() -> tuple[UUID, UUID]:
    """One row past the window and one inside it. Returns (expired, kept)."""
    window = timedelta(days=settings.identity_security_event_retention_days)
    async with AsyncSessionLocal() as s:
        team = Team(name=f"isec-wf-org-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"isec-wf-{secrets.token_hex(6)}@alkera.dev",
            first_name="Security",
            last_name="Workflow",
        )
        s.add(user)
        await s.flush()
        expired = IdentitySecurityEvent(
            id=uuid4(),
            user_id=user.id,
            event="auth.login_failed",
            created_at=NOW - window - timedelta(days=1),
        )
        kept = IdentitySecurityEvent(
            id=uuid4(), user_id=user.id, event="auth.login_failed", created_at=NOW
        )
        s.add_all([expired, kept])
        await s.commit()
        return expired.id, kept.id


async def _alive(ids: list[UUID]) -> set[UUID]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(IdentitySecurityEvent.id).where(IdentitySecurityEvent.id.in_(ids))
        )
        return set(rows.scalars())


async def _execute(temporal_worker: Any, temporal_client: Client) -> Any:
    async with temporal_worker(
        workflows=[PruneIdentitySecurityEvents],
        activities=[activities.prune_identity_security_events],
    ) as running:
        return await temporal_client.execute_workflow(
            PruneIdentitySecurityEvents.run,
            SweepInput(now=NOW),
            id=f"{WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value}-{uuid4().hex[:8]}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )


async def test_the_workflow_prunes_past_the_window_and_keeps_the_rest(
    temporal_worker: Any, temporal_client: Client
) -> None:
    expired, kept = await _seed()
    removed = await _execute(temporal_worker, temporal_client)
    # The count is the whole database's; only its shape travels back.
    assert isinstance(removed, int)
    assert await _alive([expired, kept]) == {kept}


async def test_a_dropped_connection_is_attempted_again(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy retries a transient failure: the first session drops, the
    second attempt runs the prune to completion."""
    expired, kept = await _seed()
    real = tasks.AsyncSessionLocal
    opened: list[int] = []

    def flaky_session() -> Any:
        opened.append(1)
        if len(opened) == 1:
            raise OperationalError("DELETE", {}, Exception("connection dropped"))
        return real()

    monkeypatch.setattr(tasks, "AsyncSessionLocal", flaky_session)
    await _execute(temporal_worker, temporal_client)
    assert len(opened) >= 2
    assert await _alive([expired, kept]) == {kept}


async def test_the_catalog_entry_is_created_by_the_reconciler(temporal_client: Client) -> None:
    assert WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value in queues.served_workflow_types()
    entries = [e for e in SCHEDULES if e.workflow is WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS]
    owner = f"test-{uuid4().hex[:10]}"
    try:
        report = await sync_schedules(temporal_client, entries, managed_by=owner)
        assert report == ScheduleSyncReport(created=tuple(e.id for e in entries))
        for entry in entries:
            described = await temporal_client.get_schedule_handle(entry.id).describe()
            assert fingerprint(described.schedule) == fingerprint(entry.to_schedule())
    finally:
        for entry in entries:
            await temporal_client.get_schedule_handle(entry.id).delete()
