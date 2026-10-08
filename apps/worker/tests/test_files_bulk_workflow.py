"""The Files batch job's production wiring: the queue, the policy, the core.

A batch over the inline threshold answers 202 and is only ever finished by a
worker, so "some queue serves this type" is the whole difference between a
client that eventually sees ``done`` and one that polls a ``queued`` row
forever. These pin the wiring, not that somebody remembered a class name.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.files.ids import OperationId
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from temporalio import activity, workflow
from worker.tasks import files as tasks
from worker.temporal import queues


def test_the_batch_is_served_on_the_default_queue() -> None:
    """A 202 from the bulk route is only honoured if some queue serves it."""
    assert QUEUE_FOR[WorkflowType.FILES_BULK] is TaskQueue.DEFAULT
    served = queues.WORKFLOWS_BY_QUEUE[TaskQueue.DEFAULT]
    assert WorkflowType.FILES_BULK.value in {
        workflow._Definition.must_from_class(one).name for one in served
    }
    activities = queues.ACTIVITIES_BY_QUEUE[TaskQueue.DEFAULT]
    assert WorkflowType.FILES_BULK.value in {
        activity._Definition.must_from_callable(one).name for one in activities
    }


def test_the_batch_is_retried_and_heartbeats_like_the_copy() -> None:
    """Each window of items commits its own cursor, so a crash is worth
    retrying — and a thousand-item batch must beat while it runs."""
    from worker.temporal.retry import ACTIVITY_POLICIES, FILES_NON_RETRYABLE

    policy = ACTIVITY_POLICIES[WorkflowType.FILES_BULK.value]

    assert policy.retry.maximum_attempts > 1
    assert policy.retry.non_retryable_error_types == list(FILES_NON_RETRYABLE)
    assert policy.heartbeat_timeout is not None


@pytest.mark.asyncio
async def test_an_unwired_worker_refuses_a_batch_rather_than_guessing() -> None:
    """A batch creates, moves and trashes rows owned by somebody, so which
    principal the worker acts as is a deployment decision, never a default."""
    tasks.reset_deps_factory()
    with pytest.raises(tasks.FilesJobsNotWiredError):
        await tasks.bulk(OperationId(uuid.uuid4()), uuid.uuid4())
