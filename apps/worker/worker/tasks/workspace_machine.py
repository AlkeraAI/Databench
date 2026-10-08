"""Moving a workspace to another machine: one step of the move per call.

Every step's logic is :mod:`alkera_core.compute.workspace_move`; this core opens
the transaction, hands the step the deployment's bounds, and commits. A step is
keyed by the move id and idempotent, so a retried activity repeats nothing.
"""

from __future__ import annotations

from datetime import datetime

from alkera_core.compute import workspace_move
from alkera_core.config import Settings, settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger

log = get_logger(__name__)


async def run_step(
    request: workspace_move.StepRequest,
    *,
    config: Settings = settings,
    flusher: workspace_move.Flusher | None = None,
    now: datetime | None = None,
) -> workspace_move.StepOutcome:
    """Run one step of a move in its own transaction. The flush asks the
    leaving boxes over the machine channel and holds no transaction while it
    waits for them."""
    if request.step == workspace_move.STEP_FLUSH:
        outcome = await workspace_move.flush_departing(
            request,
            # Read at call time: the live flusher is the module's seam.
            flusher=flusher or workspace_move.promoter_flusher,
            session_factory=AsyncSessionLocal,
            now=now,
        )
        log.info(
            "workspace.machine_move.step",
            move_id=request.move_id,
            step=request.step,
            state=outcome.state,
            ready=outcome.ready,
        )
        return outcome
    async with AsyncSessionLocal() as session:
        outcome = await workspace_move.run_step(
            session,
            request,
            grace_seconds=config.move_turn_grace_seconds,
            wake_timeout_seconds=config.move_wake_timeout_seconds,
            now=now,
        )
        await session.commit()
    log.info(
        "workspace.machine_move.step",
        move_id=request.move_id,
        step=request.step,
        state=outcome.state,
        ready=outcome.ready,
    )
    return outcome


async def stalled_moves(*, now: datetime | None = None) -> workspace_move.StalledMoves:
    """End every move past its registered bound, then find the moves gone
    quiet long enough that their workflow may be gone, for the recovery sweep
    to offer a runner. A move that is overdue is failed here rather than given
    another runner, whose deadlines would start over."""
    async with AsyncSessionLocal() as session:
        ended = await workspace_move.end_overdue_moves(session, now=now)
    async with AsyncSessionLocal() as session:
        found = await workspace_move.stalled_moves(session, now=now)
    log.info("workspace.machine_move.recover.found", stalled=len(found.moves), ended=ended)
    return found
