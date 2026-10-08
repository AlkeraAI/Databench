"""The Files copy job's production wiring: the queue, the policy, the core.

The copy is the second batched, cursor-resumable Files job, so it is wired
exactly like the batched move — and these pin that it really is, rather than
that someone remembered to add a workflow class.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.files.ids import OperationId
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from temporalio import activity, workflow
from worker.tasks import files as tasks
from worker.temporal import queues


def test_the_copy_is_served_on_the_default_queue() -> None:
    """A 202 from the route is only honoured if some queue serves the type."""
    assert QUEUE_FOR[WorkflowType.FILES_COPY] is TaskQueue.DEFAULT
    served = queues.WORKFLOWS_BY_QUEUE[TaskQueue.DEFAULT]
    assert WorkflowType.FILES_COPY.value in {
        workflow._Definition.must_from_class(one).name for one in served
    }
    activities = queues.ACTIVITIES_BY_QUEUE[TaskQueue.DEFAULT]
    assert WorkflowType.FILES_COPY.value in {
        activity._Definition.must_from_callable(one).name for one in activities
    }


def test_the_copy_is_retried_and_heartbeats_like_the_batched_move() -> None:
    """Its plan and cursor live on the operation row, so a crash is worth
    retrying — and a 40,000-node copy must beat while it runs."""
    from worker.temporal.retry import ACTIVITY_POLICIES, FILES_NON_RETRYABLE

    policy = ACTIVITY_POLICIES[WorkflowType.FILES_COPY.value]

    assert policy.retry.maximum_attempts > 1
    assert policy.retry.non_retryable_error_types == list(FILES_NON_RETRYABLE)
    assert policy.heartbeat_timeout is not None


@pytest.mark.asyncio
async def test_an_unwired_worker_refuses_a_copy_rather_than_guessing() -> None:
    """Which principal the worker acts as is a deployment decision, and a copy
    creates rows owned by somebody — so an unwired worker refuses."""
    tasks.reset_deps_factory()
    with pytest.raises(tasks.FilesJobsNotWiredError):
        await tasks.copy(OperationId(uuid.uuid4()), uuid.uuid4())
