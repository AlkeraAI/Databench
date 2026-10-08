"""The drain loop the inbox and sweep workflows share.

A drain runs its activity in passes until the work is gone. The loop owns the
three behaviours every drain needs and none should reimplement:

- **Another pass when more arrived.** A nudge during a running pass sets the
  workflow's ``more_work`` flag (the signal is recorded in history, so one that
  lands while the last pass is completing is replayed into view before the
  loop exits); a pass that filled its page with applied rows is followed by
  another one too, because a full page means more may be waiting. Rows that
  failed do not fill a page.
- **A short retry when the advisory lock was lost.** A nudged drain that races
  a scheduled one used to be dropped until the next 30 s tick; now it waits
  ``locked_retry_seconds`` and tries again, a bounded number of times.
- **A hard bound.** ``max_passes`` caps the run; the schedule is the net past it.

The loop is a plain coroutine over injected callables so it can be exhausted
by a unit test with fakes and exercised for real through a stub workflow on the
dev server.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from temporalio import workflow

# Workflow code runs in the SDK's sandbox, which re-imports a workflow module in
# isolation; app modules are passed through so the sandbox does not try to
# re-execute them (they touch the clock and the environment at import).
with workflow.unsafe.imports_passed_through():
    from alkera_core.schemas.temporal import DrainInput, DrainOutcome, DrainReport


async def run_drain_loop(
    *,
    run_pass: Callable[[], Awaitable[DrainOutcome]],
    more_work: Callable[[], bool],
    clear_more_work: Callable[[], None],
    input: DrainInput,
    after_pass: Callable[[DrainOutcome], Awaitable[int]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = workflow.sleep,
) -> DrainReport:
    """Run passes until the drain is idle, bounded by ``input``.

    ``clear_more_work`` runs before each pass so a nudge that arrives *during*
    the pass is seen afterwards; ``after_pass`` (the Stripe drain's email
    dispatch) runs after every completed pass and its count is summed into
    ``report.emails``. A pass that lost the lock is retried without counting as
    a pass; a pass that found the integration unconfigured ends the run.
    """
    report = DrainReport()
    while report.passes < input.max_passes:
        clear_more_work()
        outcome = await run_pass()
        if outcome.skipped_locked:
            if report.locked_retries >= input.locked_retries:
                break
            report.locked_retries += 1
            await sleep(input.locked_retry_seconds)
            continue
        report.passes += 1
        report.processed += outcome.processed
        if after_pass is not None:
            report.emails += await after_pass(outcome)
        if outcome.skipped_unconfigured:
            break
        # Only rows that were applied fill a page: a page of failures is not
        # progress, and going straight back at it would only hammer whatever
        # made them fail. A nudge is new work regardless.
        if not (more_work() or outcome.applied >= input.limit):
            break
    return report
