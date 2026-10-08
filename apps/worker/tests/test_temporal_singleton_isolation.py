"""A singleton run one test leaves open never reaches the next test.

Every test on an xdist worker shares that worker's dev server, and with it the
singleton ids (``drain_workflow_id``: the type name). A drain that hands work
to another singleton and never waits for it can end a test with that run still
open. The next test that nudged the singleton then signalled the stranger's run
instead of starting its own, and its worker ran that run for an extra pass.

The ``temporal_client`` fixture reaps those runs when each test ends. The first
two tests here run in file order on one worker (the module is one xdist group):
the first leaves a singleton open, the second sees it gone.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from alkera_core.temporal import WorkflowType, drain_workflow_id
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

SINGLETON = WorkflowType.FINISH_WORKSPACE_DELETIONS
DISPATCH_ID = drain_workflow_id(SINGLETON)
_LEFT_OPEN: dict[str, str] = {}


async def _status(client: Client, workflow_id: str) -> WorkflowExecutionStatus | None:
    try:
        return (await client.get_workflow_handle(workflow_id).describe()).status
    except RPCError as exc:
        if exc.status is RPCStatusCode.NOT_FOUND:
            return None
        raise


@pytest.mark.temporal
async def test_a_test_may_end_with_a_singleton_still_open(temporal_client: Client) -> None:
    """What a drain's hand-off does when no worker for it outlives the test."""
    handle = await temporal_client.start_workflow(
        SINGLETON.value,
        id=DISPATCH_ID,
        task_queue=f"nobody-serves-{uuid4().hex[:8]}",
    )
    assert await _status(temporal_client, DISPATCH_ID) is WorkflowExecutionStatus.RUNNING
    assert handle.result_run_id is not None
    _LEFT_OPEN["run_id"] = handle.result_run_id


@pytest.mark.temporal
async def test_the_next_test_finds_that_singleton_closed(temporal_client: Client) -> None:
    run_id = _LEFT_OPEN.get("run_id")
    if run_id is None:
        pytest.skip("runs after the test that leaves the singleton open")
    left = await temporal_client.get_workflow_handle(DISPATCH_ID, run_id=run_id).describe()
    assert left.status is WorkflowExecutionStatus.TERMINATED
    # Nothing else took the id since: a nudge now starts a run of its own.
    assert await _status(temporal_client, DISPATCH_ID) is WorkflowExecutionStatus.TERMINATED


@pytest.mark.temporal
async def test_the_reaper_closes_open_singletons_and_nothing_else(
    temporal_client: Client, open_singleton_reaper: Any
) -> None:
    """Only singleton ids are reaped: per-entity and ad-hoc runs are a test's own,
    under ids no other test can name, and a closed singleton is not an error."""
    queue = f"nobody-serves-{uuid4().hex[:8]}"
    await temporal_client.start_workflow(SINGLETON.value, id=DISPATCH_ID, task_queue=queue)
    own = f"{SINGLETON.value}-own-{uuid4().hex[:8]}"
    await temporal_client.start_workflow(SINGLETON.value, id=own, task_queue=queue)
    try:
        assert await open_singleton_reaper(temporal_client) == [DISPATCH_ID]
        assert await _status(temporal_client, DISPATCH_ID) is WorkflowExecutionStatus.TERMINATED
        assert await _status(temporal_client, own) is WorkflowExecutionStatus.RUNNING
        assert await open_singleton_reaper(temporal_client) == [], "nothing left open"
    finally:
        await temporal_client.get_workflow_handle(own).terminate()
