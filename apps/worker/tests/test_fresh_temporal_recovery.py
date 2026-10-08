"""A fresh Temporal loses nothing the database still owes.

A Temporal server can start on an empty database (a new environment, or one
moved without its history): no workflow history, no running executions. That is
only safe if every workflow the backend starts on its own (a one-shot "nudge"
for a row it just wrote) is either a scheduled pass itself or is re-driven from
its rows by a schedule, so the work a dropped execution owed is picked up by
the first tick after the workers boot and re-sync the catalog.

The backend's starts are found in its source, not listed here: a new nudge for
a workflow with no recovery schedule fails this test instead of silently
depending on the Temporal history surviving.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alkera_core.temporal.contract import WorkflowType
from worker.schedules import SCHEDULES

BACKEND = Path(__file__).resolve().parents[2] / "backend" / "backend"

#: A one-shot workflow the backend starts -> the schedule that re-drives its
#: unfinished rows. Only types that are not themselves on a schedule belong here.
RECOVERED_BY: dict[WorkflowType, str] = {
    WorkflowType.FILES_PROMOTE: "files-recover-queued",
    WorkflowType.FILES_COPY: "files-recover-queued",
    WorkflowType.FILES_LARGE_MOVE: "files-recover-queued",
    WorkflowType.FILES_BULK: "files-recover-queued",
    WorkflowType.WORKSPACE_MACHINE_MOVE: "recover-machine-moves",
}


def _backend_started() -> set[WorkflowType]:
    names: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r"(?<![A-Za-z])WorkflowType\.([A-Z_]+)", text))
    return {WorkflowType[name] for name in names}


def test_the_backend_starts_workflows_and_the_scan_finds_them() -> None:
    started = _backend_started()
    # The scan is what the other tests stand on; an empty scan would pass them all.
    assert WorkflowType.FINISH_WORKSPACE_DELETIONS in started
    assert WorkflowType.FILES_PROMOTE in started


@pytest.mark.parametrize("workflow", sorted(_backend_started(), key=lambda w: w.value), ids=str)
def test_every_workflow_the_backend_starts_is_recovered_by_a_schedule(
    workflow: WorkflowType,
) -> None:
    scheduled_types = {entry.workflow for entry in SCHEDULES}
    schedule_ids = {entry.id for entry in SCHEDULES}
    if workflow in scheduled_types:
        return
    assert workflow in RECOVERED_BY, (
        f"the backend starts {workflow.value}, which is neither scheduled nor re-driven by a "
        "schedule: an execution dropped with Temporal's history (a fresh server, a lost "
        "namespace) would leave its rows unfinished forever. Give it a recovery schedule and "
        "name it in RECOVERED_BY."
    )
    assert RECOVERED_BY[workflow] in schedule_ids, (
        f"{workflow.value}'s recovery schedule {RECOVERED_BY[workflow]!r} is not in the catalog"
    )


def test_the_recovery_table_names_only_unscheduled_backend_starts() -> None:
    scheduled_types = {entry.workflow for entry in SCHEDULES}
    started = _backend_started()
    stale = {w for w in RECOVERED_BY if w not in started or w in scheduled_types}
    assert not stale, f"RECOVERED_BY entries that no longer describe a nudge: {stale}"
