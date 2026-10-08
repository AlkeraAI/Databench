"""Ending a runtime: the file watcher it restarts and the beat it stops.

What a runtime's teardown had wrong, all of it invisible until a loop was
shared for a whole test session. Two watcher restarts in flight at once each
found no watcher to stop, started one each, and kept only the last — the other
watched on with nobody holding it, one worker thread short for the rest of the
process; a close that read the field beside a restart still in flight left that
restart's watch the same way. And a caller cancelled while ``close_all`` waited
on the beat had its cancellation swallowed as the beat's own, so a shutdown
deadline never reached past a beat that had not ended — and a beat that never
ended was waited on for as long as it ran, with nothing said about it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness import runtime as runtime_module
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

# Counting borrowed worker threads needs pools no earlier test has touched.
_OWN_LOOP = pytest.mark.asyncio(loop_scope="function")


def _runtime(tmp_path: Path) -> HarnessRuntime:
    workspace = tmp_path / "work"
    workspace.mkdir()
    return HarnessRuntime(
        ProjectDirectory(workspace / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )


def _borrowed_threads() -> int:
    """How many worker threads the running loop's watches hold right now."""
    import anyio.to_thread

    return int(anyio.to_thread.current_default_thread_limiter().borrowed_tokens)


async def _until(predicate: Callable[[], bool], *, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@_OWN_LOOP
async def test_two_watcher_restarts_in_flight_at_once_leave_one_watch_and_close_all_ends_it(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    before = _borrowed_threads()
    try:
        # Both start before either has a registry to build its watcher from, so
        # both reach the "nothing to stop" branch: the shape the beat's first
        # pass and a connection mutation land in together.
        await asyncio.gather(runtime.restart_file_watcher(), runtime.restart_file_watcher())
        assert await _until(lambda: _borrowed_threads() == before + 1), (
            f"{_borrowed_threads() - before} watches hold a thread; one restart's watch was "
            "left running with nobody holding it"
        )
    finally:
        await runtime.close_all()
    assert await _until(lambda: _borrowed_threads() == before), (
        "closing the runtime left a watch holding a worker thread"
    )


async def test_a_close_the_caller_gives_up_on_is_the_callers_to_hear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    reached = asyncio.Event()
    obey = asyncio.Event()

    async def stubborn_tick() -> Any:
        """A beat wedged in a wait its cancellation cannot reach, until told otherwise."""
        reached.set()
        while not obey.is_set():
            try:
                await obey.wait()
            except asyncio.CancelledError:
                continue
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime, "beat_tick", stubborn_tick)
    runtime.start_scheduler_beat(interval_seconds=0.01)
    await reached.wait()
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await runtime.close_all()
    finally:
        obey.set()
        await runtime.close_all()


@_OWN_LOOP
async def test_close_all_waits_for_a_restart_in_flight_so_its_watch_is_not_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close that reads the watcher field beside a restart still building one
    finds nothing to stop, and the watch that lands a moment later runs on with
    nobody holding it — one worker thread short for the rest of the process."""
    runtime = _runtime(tmp_path)
    before = _borrowed_threads()
    await runtime.restart_file_watcher()
    assert await _until(lambda: _borrowed_threads() == before + 1), (
        "this workspace watches nothing, so there is no watch for the close to lose"
    )
    building = asyncio.Event()
    finish = asyncio.Event()
    real_registry = runtime.plugin_registry

    async def slow_registry() -> Any:
        building.set()
        await finish.wait()
        return await real_registry()

    # The second restart stops the first watch, then parks while it builds the next one:
    # the close now meets a runtime whose watcher field is empty and whose watch is a
    # moment away.
    monkeypatch.setattr(runtime, "plugin_registry", slow_registry)
    restart = asyncio.create_task(runtime.restart_file_watcher())
    await building.wait()
    closing = asyncio.create_task(runtime.close_all())
    await asyncio.sleep(0)  # the close reaches the watcher field, if nothing holds it back
    finish.set()
    await asyncio.gather(restart, closing)
    assert await _until(lambda: _borrowed_threads() == before), (
        "the restart's watch outlived the close and still holds a worker thread"
    )


async def test_a_beat_that_will_not_end_is_abandoned_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """And a beat that never obeys is left behind rather than held on to: the
    close returns within its bound and says which step it gave up on. Unbounded,
    a daemon shutdown or a chat's exit waits on that beat for as long as it runs,
    with nothing in the log naming it."""
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(runtime_module, "BEAT_STOP_TIMEOUT_S", 0.2)
    reached = asyncio.Event()
    obey = asyncio.Event()

    async def stubborn_tick() -> Any:
        reached.set()
        while not obey.is_set():
            try:
                await obey.wait()
            except asyncio.CancelledError:
                continue
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime, "beat_tick", stubborn_tick)
    runtime.start_scheduler_beat(interval_seconds=0.01)
    await reached.wait()
    try:
        with caplog.at_level(logging.WARNING, logger="alkera_cli.harness.runtime"):
            async with asyncio.timeout(5.0):  # the test's own guard, far past the bound
                await runtime.close_all()
        said = [r.getMessage() for r in caplog.records if "abandoning it" in r.getMessage()]
        assert said and "scheduler beat" in said[0], said
    finally:
        obey.set()
        await runtime.close_all()
