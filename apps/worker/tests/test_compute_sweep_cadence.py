"""The reachability sweep is what tells a browser the box went quiet.

A workspace machine's status is DERIVED from its last heartbeat against the
ready window, but a browser only re-reads it when a ``compute_machine.changed``
frame lands — and the box that STOPPED heartbeating can only be announced by
the sweep (``sweep_reachability``: a heartbeat that never arrives emits nothing).
So the time from "last heartbeat" to "banner says unreachable" is the ready
window PLUS up to one sweep period. The window is a full minute of silence on
purpose (the banner is not a verdict on the turn, so waiting costs only how
soon a reader learns), and the announce has to follow it closely rather than
add a second wait of its own — which it does only if the sweep runs at least
once inside the window.

Pure catalog check, no Temporal needed.
"""

from __future__ import annotations

from datetime import timedelta

from alkera_core.config import settings
from alkera_core.temporal import WorkflowType
from worker.schedules import SCHEDULES

#: The window plus one sweep, rounded up: how long after a box dies its reader
#: may still be told it is fine.
BANNER_BUDGET_SECONDS = 90.0


def _sweep_period() -> timedelta:
    entries = [e for e in SCHEDULES if e.workflow is WorkflowType.COMPUTE_SWEEP]
    assert len(entries) == 1, "exactly one reachability sweep is scheduled"
    period = entries[0].every
    assert period is not None, "the sweep is an interval, not a cron"
    return period


def test_the_sweep_runs_inside_the_heartbeat_window() -> None:
    """A box that dies is announced within the window, not one sweep later."""
    window = timedelta(seconds=settings.compute_heartbeat_ready_seconds)
    assert _sweep_period() <= window, (
        f"the sweep runs every {_sweep_period().total_seconds():g}s but a machine reads "
        f"unreachable after {window.total_seconds():g}s: the banner can lag a dead box by "
        f"up to {_sweep_period().total_seconds():g}s"
    )


def test_a_dead_box_is_announced_within_the_drill_budget() -> None:
    """Worst case = the heartbeat window + one full sweep period."""
    worst = settings.compute_heartbeat_ready_seconds + _sweep_period().total_seconds()
    assert worst <= BANNER_BUDGET_SECONDS, (
        f"worst-case banner delay is {worst:g}s; the kill drill budgets "
        f"{BANNER_BUDGET_SECONDS:g}s for 'unreachable'"
    )
