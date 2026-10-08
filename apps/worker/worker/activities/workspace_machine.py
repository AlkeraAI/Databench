"""The activity behind a workspace move: one step per call, a thin wrapper
around the core in ``worker.tasks.workspace_machine``. Every step of a move is
the same activity type, so the move has one retry policy; the step it runs is
named in its request."""

from __future__ import annotations

from alkera_core.compute.workspace_move import StalledMoves, StepOutcome, StepRequest
from alkera_core.temporal import WorkflowType
from temporalio import activity

from worker.tasks import workspace_machine as tasks


@activity.defn(name=WorkflowType.WORKSPACE_MACHINE_MOVE.value)
async def workspace_machine_move(request: StepRequest) -> StepOutcome:
    """Run the step ``request`` names for its move."""
    return await tasks.run_step(request)


@activity.defn(name=WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER.value)
async def workspace_machine_move_recover() -> StalledMoves:
    """Find the moves the recovery sweep offers a runner; writes nothing."""
    return await tasks.stalled_moves()
