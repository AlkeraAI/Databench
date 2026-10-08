"""Nudge a singleton workflow from inside another workflow.

A client nudges a singleton with one call, a signal-with-start. Workflow code
has no such call: it can start a child, which the server refuses when a
workflow with that id is already running, and it can signal a running
workflow, which fails when none is. Put together they are the same nudge —
start the singleton as an abandoned child, and if one is already running,
signal it instead. The one race, the running one completing between the
refused start and the signal, is retried a bounded number of times.

The helper takes the two operations as callables so the decision is a plain
coroutine a unit test exhausts with fakes; the workflow wires in
``start_child_workflow`` and ``get_external_workflow_handle(...).signal``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

EXTERNAL_WORKFLOW_NOT_FOUND = "ExternalWorkflowExecutionNotFound"
"""The ``ApplicationError.type`` the server answers a signal to a workflow that
is not running with."""

HandoffOutcome = Literal["started", "signalled", "lost_race"]


async def start_or_signal(
    *,
    start: Callable[[], Awaitable[object]],
    signal: Callable[[], Awaitable[None]],
    attempts: int = 3,
) -> HandoffOutcome:
    """Start the singleton, or signal the one already running.

    ``start`` is awaited first; ``WorkflowAlreadyStartedError`` means a run
    exists, so ``signal`` is awaited. A signal refused as not found means that
    run finished in between, and the round starts over — ``attempts`` times.
    Any other failure of either operation propagates: it is not the race, and
    the caller decides. ``lost_race`` is returned, never raised, when every
    round raced; the caller's next nudge is the recovery.
    """
    for _ in range(attempts):
        try:
            await start()
            return "started"
        except WorkflowAlreadyStartedError:
            pass
        try:
            await signal()
            return "signalled"
        except ApplicationError as exc:
            if exc.type != EXTERNAL_WORKFLOW_NOT_FOUND:
                raise
    return "lost_race"
