"""Temporal: the shared contract (queues, workflow types, ids) and the client factory.

The backend imports only this package — it starts and signals workflows but
never serves them, so the worker's registries, retry policies and runtime live
in the worker package and are never needed here.
"""

from alkera_core.temporal.client import (
    DEFAULT_COMPONENT,
    ClientOptions,
    client_options,
    close_shared_client,
    connect_client,
    default_identity,
    reset_shared_client,
    shared_client,
    start_workflow_best_effort,
)
from alkera_core.temporal.contract import (
    COMPANION_ACTIVITIES,
    JOB_QUEUES,
    MORE_WORK_SIGNAL,
    QUEUE_FOR,
    RECOVER_SETTLE_ACTIVITY,
    FilesOperationInput,
    JobQueueTable,
    TaskQueue,
    WorkflowType,
    drain_workflow_id,
    keyed_workflow_id,
)

__all__ = [
    "COMPANION_ACTIVITIES",
    "DEFAULT_COMPONENT",
    "JOB_QUEUES",
    "MORE_WORK_SIGNAL",
    "QUEUE_FOR",
    "RECOVER_SETTLE_ACTIVITY",
    "ClientOptions",
    "FilesOperationInput",
    "JobQueueTable",
    "TaskQueue",
    "WorkflowType",
    "client_options",
    "close_shared_client",
    "connect_client",
    "default_identity",
    "drain_workflow_id",
    "keyed_workflow_id",
    "reset_shared_client",
    "shared_client",
    "start_workflow_best_effort",
]
