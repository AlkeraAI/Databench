"""The Files plane's workflows: one thin workflow per core.

Each executes the identically named activity under the policy the retry table
declares for it — the transient budget minus the refusals a second attempt
cannot change for the upload commit and the ACL rewrite, and no retry at all
for the janitor, whose five-minute schedule is a better recovery than a backoff
that would still be running when the next tick fires. A janitor run started
with no input — every scheduled run — uses the wall clock; ``SweepInput.now``
pins it for a test or a replay. The janitor is the one workflow with a loop:
its activity sweeps a bounded page of orgs and returns a cursor, and the
workflow drives the pages.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from alkera_core.files.ops import RUNNER_NUDGED, RUNNER_PRESENT
    from alkera_core.schemas.temporal import SweepInput
    from alkera_core.temporal import (
        QUEUE_FOR,
        RECOVER_SETTLE_ACTIVITY,
        FilesOperationInput,
        WorkflowType,
        keyed_workflow_id,
    )
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.workflow import ParentClosePolicy

    from worker.activities import files as activities
    from worker.workflows._policy import execute_under_policy


@workflow.defn(name=WorkflowType.FILES_PROMOTE.value)
class FilesPromote:
    """Turn the upload an operation was queued for into a version. Takes
    ``(op_id, org)`` like every queued kind; the session is on the row.
    Returns the version id."""

    @workflow.run
    async def run(self, op_id: str, org_team_id: str) -> str:
        return await execute_under_policy(
            WorkflowType.FILES_PROMOTE,
            activities.files_promote,
            op_id,
            org_team_id,
        )


@workflow.defn(name=WorkflowType.FILES_JANITOR.value)
class FilesJanitor:
    """Every five minutes: run every sweeper once, in order, for every org that
    uses Files. Returns how many rows and objects the pass swept.

    The fleet is swept a page at a time: each activity covers at most the
    deployment's org budget and answers with the org it stopped at, and this
    loop hands that cursor to the next activity until a page runs short. The
    bound belongs here rather than in the activity because an activity long
    enough to hold every tenant is one a timeout kills mid-fleet, losing the
    whole pass; a page that dies is retried from its own cursor and the pages
    before it stay done.
    """

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        swept = 0
        after: str | None = None
        while True:
            page = await execute_under_policy(
                WorkflowType.FILES_JANITOR, activities.files_janitor, input, after
            )
            swept += page.swept
            # A cursor that did not move would loop forever; stopping is the
            # safe reading, since the next scheduled tick sweeps again anyway.
            if page.cursor is None or page.cursor == after:
                return swept
            after = page.cursor


@workflow.defn(name=WorkflowType.FILES_GC.value)
class FilesGc:
    """Every day: reclaim the bytes of dedup domains no drive row names.

    The janitor sweeps a tenant's objects against that tenant's rows, so a
    domain whose rows are gone -- a tenant deleted in full, a test run against
    a shared bucket whose database has since been dropped -- is swept by
    nobody and keeps its bytes forever. This is the pass that sees them.

    Bounded the same way the janitor is: the activity visits a page of domains
    and answers with the one it stopped at, and this loop feeds that cursor
    back until a page runs short.

    A page that declines to collect -- the pass is switched off, the database
    cannot vouch for its own rows, too much of the bucket reads as orphaned --
    ends the run there, and the run COMPLETES with that verdict on its result:
    a refusal retried is a refusal repeated, and an operator has to be able to
    read why from the workflow. ``allow_mass_collect`` is the operator's
    per-run override of the last of those, and of nothing else.
    """

    @workflow.run
    async def run(
        self,
        input: SweepInput | None = None,
        dry_run: bool = False,
        allow_mass_collect: bool = False,
    ) -> activities.GcRun:
        parked = 0
        erased = 0
        pages = 0
        after: str | None = None
        while True:
            page = await execute_under_policy(
                WorkflowType.FILES_GC,
                activities.files_gc,
                input,
                after,
                dry_run,
                allow_mass_collect,
            )
            pages += 1
            parked += page.parked
            erased += page.erased
            if page.verdict != "ok":
                return activities.GcRun(
                    verdict=page.verdict, parked=parked, erased=erased, pages=pages
                )
            # A cursor that did not move would loop forever; stopping is the
            # safe reading, since tomorrow's tick collects again anyway.
            if page.cursor is None or page.cursor == after:
                return activities.GcRun(parked=parked, erased=erased, pages=pages)
            after = page.cursor


@workflow.defn(name=WorkflowType.FILES_ACL_REWRITE.value)
class FilesAclRewrite:
    """Repair one batch of a subtree's ACL caches. Returns how many it fixed."""

    @workflow.run
    async def run(self, op_id: str, org_team_id: str) -> int:
        return await execute_under_policy(
            WorkflowType.FILES_ACL_REWRITE, activities.files_acl_rewrite, op_id, org_team_id
        )


@workflow.defn(name=WorkflowType.FILES_LARGE_MOVE.value)
class FilesLargeMove:
    """Drive an oversized subtree's queued move to its end. Returns the
    operation's terminal state."""

    @workflow.run
    async def run(self, op_id: str, org_team_id: str) -> str:
        return await execute_under_policy(
            WorkflowType.FILES_LARGE_MOVE, activities.files_large_move, op_id, org_team_id
        )


@workflow.defn(name=WorkflowType.FILES_COPY.value)
class FilesCopy:
    """Drive a queued subtree copy to its end. Returns the operation's terminal
    state."""

    @workflow.run
    async def run(self, op_id: str, org_team_id: str) -> str:
        return await execute_under_policy(
            WorkflowType.FILES_COPY, activities.files_copy, op_id, org_team_id
        )


@workflow.defn(name=WorkflowType.FILES_BULK.value)
class FilesBulk:
    """Drive a queued batch of tree changes to its end. Returns the operation's
    terminal state."""

    @workflow.run
    async def run(self, op_id: str, org_team_id: str) -> str:
        return await execute_under_policy(
            WorkflowType.FILES_BULK, activities.files_bulk, op_id, org_team_id
        )


@workflow.defn(name=WorkflowType.FILES_RECOVER_QUEUED.value)
class FilesRecoverQueued:
    """Every tick: hand every abandoned ``queued`` operation back to a runner.

    The net under every Files hand-off. A route nudges the workflow that runs
    its queued operation the moment the row is durable, but that nudge is
    best-effort inside a two-second budget — a briefly unreachable orchestrator,
    or a process killed after the 202, leaves a durable row nobody was ever told
    about. Nothing else looks at ``queued`` rows: the watchdog only judges
    ``running`` ones by their heartbeat, so without this pass such a row is
    never run, never failed, and polled forever.

    Two halves, because neither side can do the other's job. The first activity
    FINDS the rows and writes nothing: from the database a row whose runner is
    waiting for a worker slot is indistinguishable from one nobody was ever
    told about. This then offers each row a runner, keyed by the operation —
    the same workflow id the route's own nudge uses — and what the server
    answers is the evidence: a start that succeeded proves the runner was
    absent, and ``WorkflowAlreadyStartedError`` proves one exists. The second
    activity records that, counting an attempt only where a runner really had
    to be taken and clearing the count where one was already there, so a busy
    queue can never fail an operation that is about to run.

    Returns how many operations had no runner and were given one.
    """

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        page = await execute_under_policy(
            WorkflowType.FILES_RECOVER_QUEUED, activities.files_recover_queued, input
        )
        by_org: dict[str, dict[str, str]] = {}
        started = 0
        for handoff in page.handoffs:
            runner = WorkflowType(handoff.workflow)
            try:
                await workflow.start_child_workflow(
                    runner.value,
                    args=FilesOperationInput(
                        op_id=handoff.op_id, org_team_id=handoff.org_team_id
                    ).args(),
                    id=keyed_workflow_id(runner, handoff.op_id),
                    task_queue=QUEUE_FOR[runner].value,
                    parent_close_policy=ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                # A runner exists for this row: it was told after all, and is
                # waiting its turn. Not an abandonment, and not an attempt.
                by_org.setdefault(handoff.org_team_id, {})[handoff.op_id] = RUNNER_PRESENT
                continue
            by_org.setdefault(handoff.org_team_id, {})[handoff.op_id] = RUNNER_NUDGED
            started += 1
        for org_team_id, outcomes in by_org.items():
            await execute_under_policy(
                RECOVER_SETTLE_ACTIVITY,
                activities.files_recover_settle,
                activities.RecoverySettlement(org_team_id=org_team_id, outcomes=outcomes),
            )
        return started
