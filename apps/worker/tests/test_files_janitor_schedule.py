"""The Files reconciliation pass has exactly one schedule, and it is the one
that runs the whole ledger.

The single-schedule claim is the point: every sweeper in ``JANITOR_ORDER``
rides this entry — the dir-stats aggregation and the lease reaper included — so
a sweeper added later needs no schedule of its own, and there stays one place
to look when something lingers. Another per-tenant Files entry would silently
split that.

The collection (``files-gc``) is the one entry that is deliberately not a
sweeper: this pass is driven from the drive rows, one org at a time, so a dedup
domain whose rows are gone is invisible to it no matter which sweeper is added.
That job walks the bucket instead, which is why it is a schedule of its own and
why it is pinned here beside the claim it is the exception to.
"""

from __future__ import annotations

from datetime import timedelta

from alkera_core.files.sweepers import JANITOR_ORDER, LIVE_VERSION_HOT
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from temporalio.client import ScheduleOverlapPolicy
from worker.schedules import SCHEDULES

ENTRY_ID = "files-janitor"
BY_ID = {entry.id: entry for entry in SCHEDULES}


def test_the_catalog_carries_the_janitor_at_five_minutes() -> None:
    entry = BY_ID[ENTRY_ID]
    assert entry.workflow is WorkflowType.FILES_JANITOR
    assert entry.every == timedelta(minutes=5)
    assert entry.cron is None
    assert entry.catchup_window == timedelta(minutes=1)


def test_the_janitor_is_the_only_per_tenant_files_schedule() -> None:
    """Every sweeper rides this one pass; a further Files entry would mean one
    of them had quietly been given a cadence of its own.

    Two other entries exist, and neither is a sweeper. The collection walks the
    bucket rather than the tenants, which is the one thing the janitor
    structurally cannot do — it is driven from the drive rows, so a domain
    whose rows are gone is invisible to it. The recovery of abandoned queued
    operations is not a sweep either: it re-hands work a lost nudge stranded,
    and it needs a cadence a person waiting on an operation can live with,
    which five minutes is not. Both are pinned here so a sweeper cannot be
    promoted to a schedule of its own under cover of them."""
    files_entries = {e.id for e in SCHEDULES if e.workflow.value.startswith("files.")}
    assert files_entries == {ENTRY_ID, "files-gc", "files-recover-queued"}
    sweeps = {kind.name for kind in JANITOR_ORDER}
    assert "files.gc" not in sweeps
    assert "files.recover_queued" not in sweeps


def test_the_pass_carries_the_aggregation_and_the_reaper_that_have_no_schedule() -> None:
    names = {kind.name for kind in JANITOR_ORDER}
    assert {"dir_stats_aggregate", "lease_reaper", "live_version_collapse"} <= names


def test_the_pass_runs_often_enough_to_bound_what_a_live_write_leaves() -> None:
    """The collapse's own deadline has to be reachable from this cadence: a
    generation that goes cold between two ticks is swept on the next one, so
    the rows an agent's autosaves leave can never outlive the window by more
    than a single pass."""
    assert LIVE_VERSION_HOT >= BY_ID[ENTRY_ID].every


def test_a_tick_that_fires_mid_pass_is_dropped_not_stacked() -> None:
    """SKIP, like every entry: two janitors over one org would each hand the
    other's cursor back a page it had already swept."""
    schedule = BY_ID[ENTRY_ID].to_schedule()
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    assert schedule.action.task_queue == QUEUE_FOR[WorkflowType.FILES_JANITOR].value
    assert QUEUE_FOR[WorkflowType.FILES_JANITOR] is TaskQueue.DEFAULT
