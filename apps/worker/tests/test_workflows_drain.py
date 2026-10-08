"""The shared drain loop: exhausted with fakes, then proven through a stub
workflow on the dev server (signals, sleeps and replay are real there).

Every drain workflow (Stripe events, GitHub deliveries, enrollment sweep) runs
this loop, so its branches are pinned once here: another pass on a nudge or a
full page, a bounded short retry when the advisory lock was lost, a hard cap on
passes, and an early stop when the integration is unconfigured.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from alkera_core.schemas.temporal import DrainInput, DrainOutcome, DrainReport
from alkera_core.temporal import MORE_WORK_SIGNAL
from temporalio import activity, workflow
from temporalio.client import Client
from worker.workflows._drain import run_drain_loop

# --- the loop with fakes -------------------------------------------------------


@dataclass
class _Fakes:
    outcomes: list[DrainOutcome]
    more_work_after: list[bool] = field(default_factory=list)
    after_pass_counts: list[int] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    seen_outcomes: list[DrainOutcome] = field(default_factory=list)

    async def run_pass(self) -> DrainOutcome:
        self.log.append("pass")
        return self.outcomes.pop(0)

    def more_work(self) -> bool:
        self.log.append("more_work?")
        return self.more_work_after.pop(0) if self.more_work_after else False

    def clear_more_work(self) -> None:
        self.log.append("clear")

    async def after_pass(self, outcome: DrainOutcome) -> int:
        self.log.append("after")
        self.seen_outcomes.append(outcome)
        return self.after_pass_counts.pop(0) if self.after_pass_counts else 0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


async def _loop(
    fakes: _Fakes, input: DrainInput | None = None, *, with_after: bool = False
) -> DrainReport:
    return await run_drain_loop(
        run_pass=fakes.run_pass,
        more_work=fakes.more_work,
        clear_more_work=fakes.clear_more_work,
        after_pass=fakes.after_pass if with_after else None,
        input=input or DrainInput(),
        sleep=fakes.sleep,
    )


async def test_one_quiet_pass_ends_the_run() -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(processed=3)])
    report = await _loop(fakes)
    assert report == DrainReport(passes=1, processed=3, emails=0, locked_retries=0)
    assert fakes.log == ["clear", "pass", "more_work?"]


async def test_a_nudge_during_the_pass_adds_exactly_one_more_pass() -> None:
    fakes = _Fakes(
        outcomes=[DrainOutcome(processed=2), DrainOutcome(processed=1)],
        more_work_after=[True, False],
    )
    report = await _loop(fakes)
    assert (report.passes, report.processed) == (2, 3)
    # The flag is cleared BEFORE each pass so a nudge that lands mid-pass survives.
    assert fakes.log == ["clear", "pass", "more_work?", "clear", "pass", "more_work?"]


async def test_a_full_page_earns_another_pass_without_a_nudge() -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(processed=10), DrainOutcome(processed=4)])
    report = await _loop(fakes, DrainInput(limit=10))
    assert (report.passes, report.processed) == (2, 14)


async def test_a_page_one_short_of_full_does_not() -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(processed=9)])
    report = await _loop(fakes, DrainInput(limit=10))
    assert report.passes == 1


async def test_a_page_of_failed_rows_earns_no_further_pass() -> None:
    """A pass whose whole page failed (a Stripe incident, a poison batch) is not
    progress: the rows are still in the inbox, and going straight back at them
    only hammers the dependency that is down. The outcome carries the total AND
    the split, so the total alone cannot earn the pass."""
    fakes = _Fakes(outcomes=[DrainOutcome(processed=10, applied=0, failed=10)] * 3)
    report = await _loop(fakes, DrainInput(limit=10))
    assert report == DrainReport(passes=1, processed=10)
    assert len(fakes.outcomes) == 2, "no second pass was attempted"


@pytest.mark.parametrize(
    ("applied", "failed", "passes"),
    [
        pytest.param(10, 0, 2, id="full-page-applied"),
        pytest.param(10, 3, 2, id="full-page-applied-with-failures-beside"),
        pytest.param(7, 3, 1, id="full-only-when-failures-are-counted"),
        pytest.param(9, 0, 1, id="one-short"),
        pytest.param(0, 10, 1, id="all-failed"),
    ],
)
async def test_only_applied_rows_fill_a_page(applied: int, failed: int, passes: int) -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(applied=applied, failed=failed), DrainOutcome()])
    report = await _loop(fakes, DrainInput(limit=10))
    assert report.passes == passes
    assert report.processed == applied + failed, "the report still counts every row"


async def test_a_nudge_still_earns_a_pass_after_a_failed_page() -> None:
    """A nudge is new work, whatever the last page did with the old."""
    fakes = _Fakes(
        outcomes=[DrainOutcome(failed=10), DrainOutcome(applied=1)], more_work_after=[True, False]
    )
    report = await _loop(fakes, DrainInput(limit=10))
    assert (report.passes, report.processed) == (2, 11)


async def test_max_passes_caps_a_run_that_never_goes_quiet() -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(processed=1)] * 10, more_work_after=[True] * 10)
    report = await _loop(fakes, DrainInput(max_passes=3))
    assert (report.passes, report.processed) == (3, 3)
    assert fakes.outcomes, "no pass beyond the cap was attempted"


async def test_a_lost_lock_is_retried_after_the_configured_pause() -> None:
    fakes = _Fakes(
        outcomes=[
            DrainOutcome(skipped_locked=True),
            DrainOutcome(skipped_locked=True),
            DrainOutcome(processed=5),
        ]
    )
    report = await _loop(fakes, DrainInput(locked_retries=5, locked_retry_seconds=0.25))
    assert report == DrainReport(passes=1, processed=5, emails=0, locked_retries=2)
    assert fakes.sleeps == [0.25, 0.25]


async def test_locked_retries_are_bounded_and_a_locked_pass_is_not_a_pass() -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(skipped_locked=True)] * 10)
    report = await _loop(fakes, DrainInput(locked_retries=3, locked_retry_seconds=0.1))
    assert report == DrainReport(passes=0, processed=0, emails=0, locked_retries=3)
    assert fakes.sleeps == [0.1, 0.1, 0.1]
    assert len(fakes.outcomes) == 6, "three retries: four locked attempts in total"


async def test_zero_locked_retries_gives_up_on_the_first_lost_lock() -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(skipped_locked=True), DrainOutcome(processed=1)])
    report = await _loop(fakes, DrainInput(locked_retries=0))
    assert report.passes == 0
    assert fakes.sleeps == []


async def test_an_unconfigured_integration_ends_the_run_even_when_nudged() -> None:
    fakes = _Fakes(outcomes=[DrainOutcome(skipped_unconfigured=True)], more_work_after=[True])
    report = await _loop(fakes)
    assert report.passes == 1
    assert fakes.log == ["clear", "pass"], "more_work is not even consulted"


async def test_after_pass_runs_once_per_completed_pass_and_its_counts_are_summed() -> None:
    fakes = _Fakes(
        outcomes=[
            DrainOutcome(skipped_locked=True),
            DrainOutcome(processed=2),
            DrainOutcome(processed=1),
        ],
        more_work_after=[True, False],
        after_pass_counts=[3, 4],
    )
    report = await _loop(fakes, DrainInput(locked_retry_seconds=0), with_after=True)
    assert report == DrainReport(passes=2, processed=3, emails=7, locked_retries=1)
    assert [o.processed for o in fakes.seen_outcomes] == [2, 1]
    assert fakes.log.count("after") == 2


async def test_after_pass_runs_even_for_an_unconfigured_pass() -> None:
    """The dispatch step drains what earlier passes left behind; an unconfigured
    pass still gets it before the run stops."""
    fakes = _Fakes(outcomes=[DrainOutcome(skipped_unconfigured=True)], after_pass_counts=[1])
    report = await _loop(fakes, with_after=True)
    assert report.emails == 1


# --- through a real workflow on the dev server -----------------------------------

_SCRIPTS: dict[str, list[DrainOutcome]] = {}
_GATES: dict[str, asyncio.Event] = {}
_STARTED: dict[str, asyncio.Event] = {}
_PASSES: dict[str, int] = {}


@activity.defn(name="stub.drain_pass")
async def drain_pass(key: str) -> DrainOutcome:
    _PASSES[key] = _PASSES.get(key, 0) + 1
    started = _STARTED.get(key)
    if started is not None:
        started.set()
    gate = _GATES.get(key)
    if gate is not None and _PASSES[key] == 1:
        await gate.wait()
    script = _SCRIPTS.get(key)
    return script.pop(0) if script else DrainOutcome()


# Unsandboxed like every test stub: the test module is not sandbox-importable.
@workflow.defn(name="stub.drain", sandboxed=False)
class StubDrain:
    def __init__(self) -> None:
        self._more_work = False

    @workflow.signal(name=MORE_WORK_SIGNAL)
    def more_work(self) -> None:
        self._more_work = True

    @workflow.run
    async def run(self, input: DrainInput, key: str) -> DrainReport:
        async def run_pass() -> DrainOutcome:
            return await workflow.execute_activity(
                drain_pass,
                key,
                start_to_close_timeout=timedelta(seconds=30),
                result_type=DrainOutcome,
            )

        def clear() -> None:
            self._more_work = False

        return await run_drain_loop(
            run_pass=run_pass,
            more_work=lambda: self._more_work,
            clear_more_work=clear,
            input=input,
        )


def _script(request: pytest.FixtureRequest, outcomes: list[DrainOutcome]) -> str:
    key = f"{request.node.name}-{id(request)}"
    _SCRIPTS[key] = list(outcomes)
    _PASSES.pop(key, None)
    _GATES.pop(key, None)
    _STARTED.pop(key, None)
    return key


@pytest.mark.temporal
async def test_a_quiet_drain_makes_one_pass_on_the_server(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    key = _script(request, [DrainOutcome(processed=1)])
    async with temporal_worker(workflows=[StubDrain], activities=[drain_pass]) as running:
        report = await temporal_client.execute_workflow(
            StubDrain.run,
            args=[DrainInput(), key],
            id=f"drain-{key}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
    assert report == DrainReport(passes=1, processed=1)
    assert _PASSES[key] == 1


@pytest.mark.temporal
async def test_a_signal_during_the_first_pass_produces_a_second_pass(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    key = _script(request, [DrainOutcome(processed=2), DrainOutcome(processed=1)])
    gate = _GATES[key] = asyncio.Event()
    started = _STARTED[key] = asyncio.Event()
    async with temporal_worker(workflows=[StubDrain], activities=[drain_pass]) as running:
        handle = await temporal_client.start_workflow(
            StubDrain.run,
            args=[DrainInput(), key],
            id=f"drain-{key}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        # The first pass is held open until the nudge has been recorded.
        await asyncio.wait_for(started.wait(), timeout=30)
        await handle.signal(StubDrain.more_work)
        gate.set()
        report = await handle.result()
    assert report == DrainReport(passes=2, processed=3)
    assert _PASSES[key] == 2


@pytest.mark.temporal
async def test_a_signal_with_start_does_not_add_a_pass_of_its_own(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    """A nudge that starts the drain is satisfied by the first pass; the flag is
    cleared before the pass, not consumed after it."""
    key = _script(request, [DrainOutcome(processed=1)])
    async with temporal_worker(workflows=[StubDrain], activities=[drain_pass]) as running:
        handle = await temporal_client.start_workflow(
            StubDrain.run,
            args=[DrainInput(), key],
            id=f"drain-{key}",
            task_queue=running.task_queue,
            start_signal=MORE_WORK_SIGNAL,
            execution_timeout=timedelta(seconds=30),
        )
        report = await handle.result()
    assert report.passes == 1


@pytest.mark.temporal
async def test_a_lost_lock_sleeps_with_the_workflow_timer_and_retries(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    key = _script(request, [DrainOutcome(skipped_locked=True), DrainOutcome(processed=4)])
    async with temporal_worker(workflows=[StubDrain], activities=[drain_pass]) as running:
        report = await temporal_client.execute_workflow(
            StubDrain.run,
            args=[DrainInput(locked_retry_seconds=0.05), key],
            id=f"drain-{key}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
    assert report == DrainReport(passes=1, processed=4, locked_retries=1)
    assert _PASSES[key] == 2


@pytest.mark.temporal
async def test_a_page_of_failures_on_the_server_ends_the_run_after_one_pass(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    """The split crosses history through the pydantic converter: a full page
    that all failed is one pass on the real server too, and the report still
    counts the rows."""
    key = _script(request, [DrainOutcome(processed=5, applied=0, failed=5)] * 3)
    async with temporal_worker(workflows=[StubDrain], activities=[drain_pass]) as running:
        report = await temporal_client.execute_workflow(
            StubDrain.run,
            args=[DrainInput(limit=5), key],
            id=f"drain-{key}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
    assert report == DrainReport(passes=1, processed=5)
    assert _PASSES[key] == 1
