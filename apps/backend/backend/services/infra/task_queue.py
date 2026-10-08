"""Nudge the worker through Temporal — best-effort, never in the request's way.

The backend starts and signals workflows; it never serves them. A nudge asks the
worker to look at new work NOW rather than at the next schedule tick: a route
nudges the work its write created the moment that write commits. A
distribution's own nudges call :func:`nudge` with its own workflow types.
Every nudge is best-effort (the schedules are the safety net), and the whole of
it, connecting included, is bounded by one small budget, so an unreachable
orchestrator delays a background task by at most that budget and never fails or
blocks the request that asked.

Two shapes, never combined. A drain or sweep is a singleton: its nudge is a
signal-with-start on the type-name id, so a running drain performs one more
pass and an idle one is started. Per-entity work (a check render, a probe) is
keyed by its entity and started with the use-existing policy, so a second nudge
for the same entity attaches to the run already in flight instead of
duplicating it.

A nudge the orchestrator did not take inside that budget answers ``False`` and
is not retried here. Work that must not wait for a schedule records its own
hand-off durably on its own row, and whoever next reads that row re-arms
delivery, from any replica. An in-process retrier could
not do that: a second instance behind the load balancer saw none of its memory,
so a dialog polling through the balancer was told a different story each time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from alkera_core.logging import get_logger
from alkera_core.temporal import (
    MORE_WORK_SIGNAL,
    QUEUE_FOR,
    FilesOperationInput,
    WorkflowType,
    drain_workflow_id,
    keyed_workflow_id,
    shared_client,
    start_workflow_best_effort,
)

if TYPE_CHECKING:
    from temporalio.client import Client

log = get_logger(__name__)

BACKEND_COMPONENT = "backend"
"""The identity prefix the backend's connection shows in the Temporal UI."""

NUDGE_TIMEOUT_S = 2.0
"""The whole nudge — connecting included — must finish inside this budget."""

TemporalClientProvider = Callable[[], Awaitable["Client"]]


async def backend_client() -> Client:
    """The process-wide client, connected on first use with the backend identity."""
    return await shared_client(component=BACKEND_COMPONENT)


temporal_client_provider: TemporalClientProvider = backend_client
"""How a nudge obtains its client — the module-level seam. Tests substitute a
provider that returns a recording client, the dev server's client, or one that
fails; production leaves the shared client in place."""


async def nudge(
    workflow: StrEnum,
    *,
    key: str | None,
    signal: str | None,
    fail_log_event: str,
    args: Sequence[Any] | None = None,
) -> bool:
    """Start or signal ``workflow`` inside the budget; report whether it took.

    Whatever goes wrong — the id cannot be built, the client cannot connect, the
    start is refused, the budget runs out — is logged once under
    ``fail_log_event`` and answered with ``False``. Only a cancellation of the
    caller propagates.

    A failure is not retried here. Work that must not be lost records its own
    hand-off on its own row and is re-armed by whoever next reads that row, from
    any replica — which an in-process retrier could not do, because a second
    instance behind the load balancer could not see it.

    ``args`` are the workflow's positional arguments when they are more than
    the key alone — a Files operation names its org beside its id. Left out,
    per-entity work takes its key and a drain takes nothing.
    """
    task_queue = QUEUE_FOR[workflow].value
    try:
        workflow_id = (
            drain_workflow_id(workflow) if key is None else keyed_workflow_id(workflow, key)
        )
    except Exception as exc:
        log.warning(
            fail_log_event,
            workflow=workflow.value,
            key=key,
            task_queue=task_queue,
            error=str(exc) or type(exc).__name__,
        )
        return False

    async def start() -> bool:
        client = await temporal_client_provider()
        # A refused start is already logged in there, once; it just answers False.
        # Per-entity work takes its entity id as the workflow's one argument; a
        # drain takes none and runs on its defaults.
        positional: Sequence[Any] = (() if key is None else (key,)) if args is None else args
        return await start_workflow_best_effort(
            client,
            workflow.value,
            id=workflow_id,
            task_queue=task_queue,
            args=positional,
            signal=signal,
            timeout_s=NUDGE_TIMEOUT_S,
            fail_log_event=fail_log_event,
        )

    async def attempt() -> tuple[bool, str]:
        """One bounded try: (whether it took, why not)."""
        try:
            took = await asyncio.wait_for(start(), NUDGE_TIMEOUT_S)
        except TimeoutError:
            error = f"no answer from the orchestrator within {NUDGE_TIMEOUT_S:g}s"
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        else:
            # A False here was already logged by the start primitive, once.
            return (True, "") if took else (False, "the orchestrator refused the start")
        log.warning(
            fail_log_event, workflow=workflow.value, key=key, task_queue=task_queue, error=error
        )
        return False, error

    took, _error = await attempt()
    return took


async def nudge_workspace_deletions() -> bool:
    """Ask the worker to finish the deleted workspaces' chats NOW (the delete
    route, after its commit). The schedule is the net for a nudge it missed."""
    return await nudge(
        WorkflowType.FINISH_WORKSPACE_DELETIONS,
        key=None,
        signal=MORE_WORK_SIGNAL,
        fail_log_event="workspace.deletion.nudge_failed",
    )


async def nudge_account_lifecycle_sweep() -> bool:
    """Ask the worker for a lifecycle pass NOW (support ending a deletion's
    grace window). The schedule is the net."""
    return await nudge(
        WorkflowType.ACCOUNT_LIFECYCLE_SWEEP,
        key=None,
        signal=None,
        fail_log_event="account.sweep.nudge_failed",
    )


async def nudge_files_operation(workflow: WorkflowType, op_id: UUID, org_team_id: UUID) -> bool:
    """Ask the default-queue worker to run one queued Files operation NOW.

    A Files route that queues work — an oversized move, a subtree copy, a batch
    over the inline threshold, an upload's commit — answers with a ``queued``
    operation on the shape that leaves the running to the worker. Nothing
    sweeps those rows, so the request says so the moment the row is durable;
    without the nudge the row waits for a pickup that never comes. Keyed by the
    operation, started use-existing: a second request for the same row attaches
    to the run in flight. Every runner's claim is a compare-and-swap, so a
    nudge that arrives for an operation already done is answered by the row,
    never by a second rewrite.

    Every kind, an upload's promote included, runs ``(op_id, org_team_id)``: a
    promote reads its session from the row ``complete`` bound it to.
    """
    return await nudge(
        workflow,
        key=str(op_id),
        signal=None,
        args=tuple(FilesOperationInput(op_id=str(op_id), org_team_id=str(org_team_id)).args()),
        fail_log_event=f"{workflow.value}.nudge_failed",
    )


async def nudge_org_machine_reconcile() -> bool:
    """Ask the money worker to converge org machines NOW (after the commit of
    a purchase, a power change or a move). The 30-second schedule is the net."""
    return await nudge(
        WorkflowType.ORG_MACHINE_RECONCILE,
        key=None,
        signal=MORE_WORK_SIGNAL,
        fail_log_event="compute.org_machine.nudge_failed",
    )


async def start_workspace_machine_move(move_id: UUID, args: Sequence[str]) -> bool:
    """Ask the worker to run one workspace machine move NOW. Keyed by the
    move, started use-existing, so a second start attaches to the run in
    flight. ``args`` are the workflow's positional arguments, built by the
    move's own input type. A start the orchestrator does not take leaves the
    move where it is, and the next read of the workspace's machine starts it
    again."""
    return await nudge(
        WorkflowType.WORKSPACE_MACHINE_MOVE,
        key=str(move_id),
        signal=None,
        args=tuple(args),
        fail_log_event="workspace.machine_move.start_failed",
    )
