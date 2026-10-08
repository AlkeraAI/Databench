"""Unit tests for the per-chat background-job registry.

The registry is a generic asyncio task supervisor (no event bus, no chat) — so
these tests drive it with plain coroutines + a fake clock and assert the
observable contract: state transitions, the NATIVE result carried through to the
terminal callback, the cancellation cleanup path (the seam a backgrounded `bash`
daemon uses to kill its process group), the concurrency cap, and drain.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable

import pytest
from alkera_cli.harness.background import (
    BackgroundJob,
    BackgroundJobLimitError,
    BackgroundJobRegistry,
)


class _Clock:
    """A deterministic, manually-advanced clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"job_{next(counter):03d}"


def _registry(**kw: object) -> tuple[BackgroundJobRegistry, list[BackgroundJob]]:
    """A registry whose terminal callback records every fired snapshot."""
    fired: list[BackgroundJob] = []

    async def on_terminal(job: BackgroundJob) -> None:
        fired.append(job)

    reg = BackgroundJobRegistry(on_terminal=on_terminal, id_factory=_ids(), **kw)  # type: ignore[arg-type]
    return reg, fired


async def _settle() -> None:
    """Yield to the loop enough for forked tasks to run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Submit + completion: the native result rides through to the callback.
# --------------------------------------------------------------------------- #


async def test_submit_returns_running_snapshot_immediately() -> None:
    reg, _ = _registry()
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "done"

    snap = reg.submit(work, kind="sql", title="a query")
    assert snap.job_id == "job_001"
    assert snap.state == "running"
    assert snap.kind == "sql"
    assert snap.title == "a query"
    assert reg.running_count() == 1
    assert reg.has_running() is True
    # The snapshot never leaks the live task.
    assert snap.task is None
    gate.set()
    await _settle()


async def test_completion_records_native_result_object() -> None:
    reg, fired = _registry()
    sentinel = {"columns": ["a"], "rows": [[1]]}  # a stand-in "native result"

    async def work() -> object:
        return sentinel

    job = reg.submit(work, kind="sql", title="q")
    await _settle()

    got = reg.get(job.job_id)
    assert got is not None
    assert got.state == "completed"
    assert got.result is sentinel  # the SAME object, not a flattened copy
    assert got.error is None
    assert reg.running_count() == 0
    # Terminal callback fired exactly once with the result-bearing snapshot.
    assert [f.job_id for f in fired] == [job.job_id]
    assert fired[0].state == "completed"
    assert fired[0].result is sentinel


async def test_error_is_recorded_not_raised() -> None:
    reg, fired = _registry()

    async def boom() -> None:
        raise ValueError("kaboom")

    job = reg.submit(boom, kind="bash", title="bad")
    await _settle()

    got = reg.get(job.job_id)
    assert got is not None
    assert got.state == "error"
    assert got.error == "kaboom"
    assert got.result is None
    assert [f.state for f in fired] == ["error"]
    assert fired[0].error == "kaboom"


async def test_error_with_empty_message_falls_back_to_class_name() -> None:
    reg, _ = _registry()

    async def boom() -> None:
        raise RuntimeError()

    job = reg.submit(boom, kind="bash", title="x")
    await _settle()
    got = reg.get(job.job_id)
    assert got is not None and got.error == "RuntimeError"


# --------------------------------------------------------------------------- #
# Cancellation: the coroutine's cleanup runs (the bash process-group seam).
# --------------------------------------------------------------------------- #


async def test_cancel_runs_coroutine_cleanup_and_records_cancelled() -> None:
    reg, fired = _registry()
    cleaned = asyncio.Event()
    started = asyncio.Event()

    async def daemon() -> None:
        started.set()
        try:
            await asyncio.Event().wait()  # never completes on its own
        finally:
            # This is where a real backgrounded bash kills its process group.
            cleaned.set()

    job = reg.submit(daemon, kind="bash", title="dev server")
    await started.wait()
    assert reg.has_running() is True

    snap = await reg.cancel(job.job_id)
    assert snap is not None
    assert snap.state == "cancelled"
    assert cleaned.is_set()  # the finally ran
    assert reg.running_count() == 0
    assert [f.state for f in fired] == ["cancelled"]


async def test_cancel_missing_job_is_none() -> None:
    reg, _ = _registry()
    assert await reg.cancel("nope") is None


async def test_cancel_terminal_job_is_noop_returns_snapshot() -> None:
    reg, fired = _registry()

    async def work() -> str:
        return "ok"

    job = reg.submit(work, kind="sql", title="q")
    await _settle()
    # Already completed → cancel is a no-op, returns the completed snapshot,
    # does NOT fire a second terminal callback.
    snap = await reg.cancel(job.job_id)
    assert snap is not None and snap.state == "completed"
    assert len(fired) == 1


# --------------------------------------------------------------------------- #
# Concurrency cap.
# --------------------------------------------------------------------------- #


async def test_a_registry_built_the_shipped_way_keeps_starting_jobs() -> None:
    """A count is not a property of the work: a turn that has twenty independent
    builds to run has twenty, and refusing the ninth only makes the model queue
    them itself. What bounds the fan-out is the box, and an operator who wants a
    ceiling sets one."""
    reg, _ = _registry()
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    for i in range(20):
        reg.submit(work, kind="bash", title=str(i))

    assert reg.running_count() == 20
    assert reg.can_accept() is True
    gate.set()
    await asyncio.sleep(0)


async def test_the_cap_can_be_removed_so_a_long_turn_fans_out() -> None:
    """An explicit "no cap" is still honoured for a caller that passes one."""
    reg, _ = _registry(max_running_jobs=None)
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    for i in range(20):
        reg.submit(work, kind="bash", title=str(i))

    assert reg.running_count() == 20
    assert reg.can_accept() is True
    gate.set()
    await asyncio.sleep(0)


async def test_concurrency_cap_refuses_over_limit_then_frees_up() -> None:
    reg, _ = _registry(max_running_jobs=2)
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    reg.submit(work, kind="bash", title="1")
    reg.submit(work, kind="bash", title="2")
    assert reg.running_count() == 2

    with pytest.raises(BackgroundJobLimitError):
        reg.submit(work, kind="bash", title="3")

    # Let the two running jobs finish → a slot frees up.
    gate.set()
    await _settle()
    assert reg.running_count() == 0
    # Now a submit succeeds again.
    fresh_gate = asyncio.Event()

    async def work2() -> None:
        await fresh_gate.wait()

    ok = reg.submit(work2, kind="bash", title="4")
    assert ok.state == "running"
    fresh_gate.set()
    await _settle()


# --------------------------------------------------------------------------- #
# Drain (close path) + closed-registry refusal.
# --------------------------------------------------------------------------- #


async def test_drain_cancels_all_running_and_runs_cleanup() -> None:
    reg, fired = _registry()
    cleaned: list[str] = []

    def make(name: str):
        async def daemon() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(name)

        return daemon

    reg.submit(make("a"), kind="bash", title="a")
    reg.submit(make("b"), kind="bash", title="b")
    await _settle()
    assert reg.running_count() == 2

    await reg.drain()
    assert reg.running_count() == 0
    assert sorted(cleaned) == ["a", "b"]
    assert sorted(f.state for f in fired) == ["cancelled", "cancelled"]


async def test_on_submit_fires_once_with_the_running_snapshot() -> None:
    """``on_submit`` fires the instant a job is submitted (so the runtime can publish
    the RUNNING transcript card), with the job's running snapshot + its captured
    input — exactly once, BEFORE the terminal callback."""
    order: list[str] = []
    submitted: list[BackgroundJob] = []

    async def on_submit(job: BackgroundJob) -> None:
        submitted.append(job)
        order.append("submit")

    async def on_terminal(job: BackgroundJob) -> None:
        order.append("terminal")

    reg = BackgroundJobRegistry(on_submit=on_submit, on_terminal=on_terminal, id_factory=_ids())
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "done"

    reg.submit(work, kind="bash", title="x", input={"command": "ls"})
    await _settle()
    assert len(submitted) == 1
    assert submitted[0].state == "running"
    assert submitted[0].input == {"command": "ls"}

    gate.set()
    await reg.drain()
    assert order == ["submit", "terminal"]  # submit always precedes terminal


async def test_drain_finalizes_a_task_cancelled_before_it_started() -> None:
    """A job whose supervised task is cancelled BEFORE the event loop ever ran its
    coroutine body never executes ``_run`` — so without the stranded-finalize it
    would be left ``running`` forever. Submitting then draining with NO yield in
    between exercises exactly that race."""
    reg, fired = _registry()
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "done"

    reg.submit(work, kind="bash", title="never-ran")
    gate.set()  # the task is ready to complete — but it hasn't been scheduled yet
    await reg.drain()  # cancels it before it ever ran a single line

    assert reg.running_count() == 0  # not a phantom 'running'
    assert [j.state for j in reg.list()] == ["cancelled"]
    assert [f.state for f in fired] == ["cancelled"]  # terminal callback still fired


async def test_cancel_finalizes_a_task_cancelled_before_it_started() -> None:
    """The same stranded-task race via ``cancel()``: the returned snapshot — what
    ``background_cancel`` reports — must be terminal, never a phantom ``running``."""
    reg, fired = _registry()
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "done"

    job = reg.submit(work, kind="sql", title="never-ran")
    result = await reg.cancel(job.job_id)  # cancel before the loop scheduled the task

    assert result is not None
    assert result.state == "cancelled"
    assert reg.running_count() == 0
    assert [f.state for f in fired] == ["cancelled"]


async def test_submit_after_drain_is_refused() -> None:
    reg, _ = _registry()
    await reg.drain()  # marks closed even with nothing running

    async def work() -> None: ...

    with pytest.raises(RuntimeError):
        reg.submit(work, kind="sql", title="late")


# --------------------------------------------------------------------------- #
# Clock injection + list ordering + snapshot isolation.
# --------------------------------------------------------------------------- #


async def test_started_and_completed_at_use_injected_clock() -> None:
    clock = _Clock()
    reg, _ = _registry(clock=clock)
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    job = reg.submit(work, kind="sql", title="q")
    assert reg.get(job.job_id).started_at == 1000.0  # type: ignore[union-attr]

    clock.advance(42.0)
    release.set()
    await _settle()
    done = reg.get(job.job_id)
    assert done is not None
    assert done.completed_at == 1042.0


async def test_list_is_oldest_first() -> None:
    clock = _Clock()
    reg, _ = _registry(clock=clock)
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    reg.submit(work, kind="sql", title="first")
    clock.advance(1.0)
    reg.submit(work, kind="bash", title="second")
    titles = [j.title for j in reg.list()]
    assert titles == ["first", "second"]
    gate.set()
    await _settle()


async def test_snapshot_is_isolated_from_registry_state() -> None:
    reg, _ = _registry()
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    job = reg.submit(work, kind="sql", title="q")
    snap = reg.get(job.job_id)
    assert snap is not None
    snap.title = "mutated"  # mutate the copy
    assert reg.get(job.job_id).title == "q"  # type: ignore[union-attr]
    gate.set()
    await _settle()


@pytest.mark.parametrize("outcome", ["completed", "error", "cancelled"])
async def test_a_job_counts_as_running_until_its_result_is_delivered(outcome: str) -> None:
    """A job that has ended still owes the chat its wake: until the terminal
    callback (the finish card, then the model's turn) has run, the chat is not
    idle. A box draining in that instant once took it for idle and handed the
    chat back, and the model never answered the job's result."""
    release = asyncio.Event()
    seen_during: list[bool] = []
    reg: BackgroundJobRegistry

    async def on_terminal(job: BackgroundJob) -> None:
        seen_during.append(reg.has_running())
        await release.wait()

    reg = BackgroundJobRegistry(on_terminal=on_terminal, id_factory=_ids())
    gate = asyncio.Event()

    async def work() -> object:
        await gate.wait()
        if outcome == "error":
            raise RuntimeError("boom")
        return "ok"

    job = reg.submit(work, kind="bash", title="sleep")
    await _settle()
    assert reg.has_running()
    if outcome == "cancelled":
        cancelling = asyncio.ensure_future(reg.cancel(job.job_id))
    else:
        gate.set()
    await _settle()

    snapshot = reg.get(job.job_id)
    assert snapshot is not None and snapshot.state == outcome
    assert reg.running_count() == 0
    assert seen_during == [True] and reg.has_running()

    release.set()
    await _settle()
    if outcome == "cancelled":
        await cancelling
    assert not reg.has_running()


async def test_a_failing_delivery_still_lets_the_chat_go_idle() -> None:
    async def on_terminal(job: BackgroundJob) -> None:
        raise RuntimeError("the card could not be published")

    reg = BackgroundJobRegistry(on_terminal=on_terminal, id_factory=_ids())

    async def work() -> object:
        return "ok"

    reg.submit(work, kind="bash", title="echo")
    await _settle()
    assert not reg.has_running()
