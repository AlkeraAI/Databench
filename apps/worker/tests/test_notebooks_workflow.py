"""The notebook run sweep as a Temporal workflow: the real workflow and the
real activity through a real Worker, against the test database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.temporal import TaskQueue, WorkflowType
from temporalio.client import Client
from tests.test_notebooks import _org, _run, _status
from worker.activities.notebooks import sweep_runs
from worker.schedules import SCHEDULES, ScheduleSyncReport, fingerprint, sync_schedules
from worker.tasks._hardening import advisory_lock
from worker.tasks.notebooks import RUN_DEADLINE_AFTER, UNANSWERED_AFTER
from worker.temporal import queues
from worker.workflows.notebooks import SweepNotebookRuns

pytestmark = pytest.mark.temporal


async def _execute(temporal_worker: Any, temporal_client: Client) -> Any:
    async with temporal_worker(workflows=[SweepNotebookRuns], activities=[sweep_runs]) as w:
        return await temporal_client.execute_workflow(
            SweepNotebookRuns.run,
            id=f"notebooks.sweep_runs-{uuid.uuid4().hex[:8]}",
            task_queue=w.task_queue,
            execution_timeout=timedelta(seconds=60),
        )


async def test_one_run_ends_the_runs_nothing_holds_and_spares_the_fresh(
    temporal_worker: Any, temporal_client: Client
) -> None:
    now = datetime.now(UTC)
    org, item = await _org(), uuid.uuid4()
    silent = await _run(org, item, status="queued", created_at=now - UNANSWERED_AFTER * 2)
    fresh = await _run(org, item, status="queued", created_at=now)
    ended = await _execute(temporal_worker, temporal_client)
    assert isinstance(ended, int) and ended >= 1
    assert await _status(silent) == ("refused", "machine_silent")
    assert await _status(fresh) == ("queued", None)


async def test_a_sweep_that_loses_the_lock_ends_nothing(
    temporal_worker: Any, temporal_client: Client
) -> None:
    org, item = await _org(), uuid.uuid4()
    old = datetime.now(UTC) - RUN_DEADLINE_AFTER - timedelta(hours=1)
    run_id = await _run(org, item, status="running", created_at=old, started=True)
    async with advisory_lock("notebooks_sweep_runs") as held:
        assert held is True
        assert await _execute(temporal_worker, temporal_client) == 0
    assert await _status(run_id) == ("running", None)


def test_the_sweep_runs_on_the_default_queue() -> None:
    assert queues.workflow_type_name(SweepNotebookRuns) == "notebooks.sweep_runs"
    assert queues.activity_type_name(sweep_runs) == "notebooks.sweep_runs"
    for queue in TaskQueue:
        expect = queue is TaskQueue.DEFAULT
        assert (SweepNotebookRuns in queues.WORKFLOWS_BY_QUEUE[queue]) is expect, queue
        assert (sweep_runs in queues.ACTIVITIES_BY_QUEUE[queue]) is expect, queue


async def test_the_sweep_is_scheduled_every_minute(temporal_client: Client) -> None:
    assert WorkflowType.SWEEP_NOTEBOOK_RUNS.value in queues.served_workflow_types()
    [entry] = [e for e in SCHEDULES if e.workflow is WorkflowType.SWEEP_NOTEBOOK_RUNS]
    assert entry.every == timedelta(minutes=1)
    owner = f"test-{uuid.uuid4().hex[:10]}"
    try:
        report = await sync_schedules(temporal_client, [entry], managed_by=owner)
        assert report == ScheduleSyncReport(created=(entry.id,))
        described = await temporal_client.get_schedule_handle(entry.id).describe()
        assert fingerprint(described.schedule) == fingerprint(entry.to_schedule())
    finally:
        await temporal_client.get_schedule_handle(entry.id).delete()
