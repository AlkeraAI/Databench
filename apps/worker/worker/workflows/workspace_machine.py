"""A workspace moving to another machine: sleep it where it runs, wake it
where it goes.

The workflow drives one ``workspace_machine_moves`` row through its states; the
row is the truth, and every step reads it first, so a person canceling the
move between two steps is seen at the next one, and a run started again for a
move already past a step (a re-armed start, a worker that died) resumes from
the row instead of repeating the step. The waits are durable timers: a worker
restart in the middle of the drain picks up at the next poll with nothing
done twice.

- **draining**: poll until no turn runs in the workspace on a machine that
  still answers. Past the grace the move was given, stop what is left, then
  give the stops a short while to land.
- **flushing** (still ``draining`` on the row): every box leaving the
  workspace is asked to push what it holds of it, and the move waits for each
  to say it did. ``busy`` or no answer fails the move with nothing moved.
- **switching**: one step, one transaction.
- **waking**: poll until the target took the workspace, bounded by the wake
  timeout, which fails the move and puts the chats back.

A step whose activity fails past its retries fails the move with the code of
the phase it was in, so nothing is left half-moved without a word on the row.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from alkera_core.compute.workspace_move import (
        DRAIN_TIMEOUT,
        STEP_BEGIN,
        STEP_DRAIN_POLL,
        STEP_FAIL,
        STEP_FLUSH,
        STEP_STOP_BUSY,
        STEP_SWITCH,
        STEP_WAKE_BEGIN,
        STEP_WAKE_POLL,
        WAKE_TIMEOUT,
        MoveInput,
        StepOutcome,
        StepRequest,
    )
    from alkera_core.models.org_machines import (
        MOVE_DRAINING,
        MOVE_REQUESTED,
        MOVE_SWITCHING,
        MOVE_WAKING,
    )
    from alkera_core.temporal import QUEUE_FOR, WorkflowType, keyed_workflow_id

    from worker.activities import workspace_machine as activities
    from worker.workflows._policy import execute_under_policy

#: How often a wait reads the rows again.
POLL = timedelta(seconds=2)
#: How long stopped turns get to end before the chats move regardless. The
#: box pushes a chat's folder when it lets the chat go, so a turn that ignored
#: its stop loses nothing it wrote.
STOP_SETTLE = timedelta(seconds=30)


@workflow.defn(name=WorkflowType.WORKSPACE_MACHINE_MOVE.value)
class WorkspaceMachineMove:
    """Move one workspace. Takes ``(move_id, org_team_id, default_machine_id)``
    (``""`` when the move is to an org machine); returns the state the move
    ended in."""

    def __init__(self) -> None:
        self._move_id = ""
        self._org = ""
        self._default: str | None = None
        self._origin: dict[str, str | None] = {}

    async def _step(self, step: str, *, error_code: str = "") -> StepOutcome:
        return await execute_under_policy(
            WorkflowType.WORKSPACE_MACHINE_MOVE,
            activities.workspace_machine_move,
            StepRequest(
                move_id=self._move_id,
                org_team_id=self._org,
                step=step,
                default_machine_id=self._default,
                origin=self._origin,
                error_code=error_code,
            ),
        )

    @workflow.run
    async def run(self, move_id: str, org_team_id: str, default_machine_id: str = "") -> str:
        self._move_id = move_id
        self._org = org_team_id
        self._default = default_machine_id or None
        first = await self._step(STEP_BEGIN)
        self._origin = dict(first.origin)
        state = first.state
        try:
            if state in (MOVE_REQUESTED, MOVE_DRAINING):
                state = await self._drain(first.grace_seconds)
            if state == MOVE_DRAINING:
                # Every leaving box pushes what it holds before any chat moves;
                # a box that cannot fails the move with nothing moved.
                state = (await self._step(STEP_FLUSH)).state
            if state == MOVE_DRAINING:
                state = (await self._step(STEP_SWITCH)).state
        except ActivityError:
            return (await self._step(STEP_FAIL, error_code=DRAIN_TIMEOUT)).state
        try:
            if state == MOVE_SWITCHING:
                state = (await self._step(STEP_WAKE_BEGIN)).state
            if state == MOVE_WAKING:
                state = await self._wake(first.wake_timeout_seconds)
        except ActivityError:
            return (await self._step(STEP_FAIL, error_code=WAKE_TIMEOUT)).state
        return state

    async def _drain(self, grace_seconds: int) -> str:
        """Poll until the chats are at rest; the state the move is in then."""
        deadline = workflow.now() + timedelta(seconds=grace_seconds)
        stopped_at = None
        while True:
            polled = await self._step(STEP_DRAIN_POLL)
            if polled.state != MOVE_DRAINING or polled.ready:
                return polled.state
            moment = workflow.now()
            if stopped_at is None and moment >= deadline:
                await self._step(STEP_STOP_BUSY)
                stopped_at = moment
            elif stopped_at is not None and moment >= stopped_at + STOP_SETTLE:
                return MOVE_DRAINING
            await workflow.sleep(POLL)

    async def _wake(self, timeout_seconds: int) -> str:
        """Poll until the target took the workspace, or fail it at the timeout."""
        deadline = workflow.now() + timedelta(seconds=timeout_seconds)
        while True:
            polled = await self._step(STEP_WAKE_POLL)
            if polled.state != MOVE_WAKING:
                return polled.state
            if workflow.now() >= deadline:
                return (await self._step(STEP_FAIL, error_code=WAKE_TIMEOUT)).state
            await workflow.sleep(POLL)


@workflow.defn(name=WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER.value)
class WorkspaceMachineMoveRecover:
    """Every tick: offer each move that has gone quiet a runner again.

    The request that asks for a move starts its workflow best-effort, and a
    read of the workspace's machine re-arms a quiet one; neither helps a move
    nobody reads after its workflow went with Temporal's history. This offers
    each such move its runner under the same workflow id the request uses: a
    start that succeeds resumes the move from the state on its row (each step
    is keyed by the move and repeats nothing), and ``WorkflowAlreadyStartedError``
    means a runner is alive and waiting, so nothing is done. A move to the
    org's default is resumed without the default its request resolved, so it
    fails cleanly rather than guessing where the chats go.

    Returns how many moves had no runner and were given one.
    """

    @workflow.run
    async def run(self) -> int:
        found = await execute_under_policy(
            WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER, activities.workspace_machine_move_recover
        )
        runner = WorkflowType.WORKSPACE_MACHINE_MOVE
        started = 0
        for move in found.moves:
            try:
                await workflow.start_child_workflow(
                    runner.value,
                    args=MoveInput(move_id=move.move_id, org_team_id=move.org_team_id).args(),
                    id=keyed_workflow_id(runner, move.move_id),
                    task_queue=QUEUE_FOR[runner].value,
                    parent_close_policy=ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                continue
            started += 1
        return started
