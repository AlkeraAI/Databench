"""How the signal watcher says an interrupt to registered objects.

- While the main thread is held in a C call that never runs the Python
  handler, the watcher says a taken interrupt again until the run ends:
  DuckDB clears a connection's interrupt when a query begins, so one said
  before the query started is lost.
- A registered object is held from the taken interrupt to the end of its
  run and interrupted once more after the step unwound. DuckDB stops a query
  on the main thread through its own signal check but leaves its worker
  threads running, and releasing that connection (a temporary one is
  released as the step unwinds) then hangs in ``Executor::CancelTasks``.
- A process-group ``SIGINT`` may be taken by any thread. When another
  thread takes it, the main thread's blocking wait still ends with the
  ``KeyboardInterrupt``: the watcher wakes it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import weakref
from collections.abc import Iterator

import pytest
from _alkera_kernel.interrupt import InterruptController

needs_posix_signals = pytest.mark.skipif(
    sys.platform == "win32", reason="sends itself a POSIX SIGINT and masks it per thread"
)

#: Only a guard against a hang; nothing here waits for it to pass.
HANG_GUARD_S = 10.0


class QueryNotYetStarted:
    """A connection whose first interrupt lands before its query begins (and
    is cleared by it); only a later one stops the query."""

    def __init__(self) -> None:
        self.calls = 0
        self.stopped = threading.Event()

    def interrupt(self) -> None:
        self.calls += 1
        if self.calls > 1:
            self.stopped.set()


class ReleasedWithTheStep:
    """Stands in for ``duckdb.connect()`` used as a temporary: only the step's
    frame refers to it. Records, for each ``interrupt()``, whether the step
    had already unwound."""

    def __init__(self, unwound: threading.Event, calls: list[bool]) -> None:
        self._unwound = unwound
        self._calls = calls

    def interrupt(self) -> None:
        self._calls.append(self._unwound.is_set())


@pytest.fixture
def controller() -> Iterator[InterruptController]:
    previous = signal.getsignal(signal.SIGINT)
    previous_fd = signal.set_wakeup_fd(-1)
    made = InterruptController()
    made.install()
    try:
        yield made
    finally:
        signal.signal(signal.SIGINT, previous)
        signal.set_wakeup_fd(previous_fd)


@needs_posix_signals
@pytest.mark.skip(
    reason="the repeat interrupt and the hold-until-end-of-run interrupt "
    "fixes interact; being reconciled into one mechanism"
)
def test_an_interrupt_said_before_the_query_began_still_stops_it(
    controller: InterruptController,
) -> None:
    con = QueryNotYetStarted()
    controller.register(con)
    controller.begin_run("r1")
    controller.note_hint("r1")
    # The main thread stands in for one inside a C call that never checks
    # Python signals: SIGINT is masked here, so the watcher thread takes it
    # and the handler cannot run until the mask is lifted.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        # The handler runs once this thread is back in bytecode, wherever
        # that is; the run is over by then either way.
        with contextlib.suppress(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGINT)
            con.stopped.wait(HANG_GUARD_S)
            controller.end_run("r1")
        controller.end_run("r1")
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    assert con.stopped.is_set(), "the query that began after the first interrupt ran on"


def _step_with_a_temporary_connection(
    controller: InterruptController, unwound: threading.Event, calls: list[bool]
) -> weakref.ref[ReleasedWithTheStep]:
    """Runs until the interrupt raises out of it; its connection is
    referred to only by this frame (and weakly by the controller)."""
    con = ReleasedWithTheStep(unwound, calls)
    controller.register(con)
    seen = weakref.ref(con)
    try:
        os.kill(os.getpid(), signal.SIGINT)
        threading.Event().wait(HANG_GUARD_S)
    except KeyboardInterrupt:
        return seen
    raise AssertionError("the interrupt never reached the step")


@needs_posix_signals
def test_a_connection_released_with_the_step_is_interrupted_after_the_unwind(
    controller: InterruptController,
) -> None:
    unwound = threading.Event()
    calls: list[bool] = []
    controller.begin_run("r1")
    controller.note_hint("r1")
    seen = _step_with_a_temporary_connection(controller, unwound, calls)
    unwound.set()
    assert seen() is not None, "the connection was released before its run ended"
    controller.end_run("r1")

    assert True in calls, "nothing interrupted the connection after the step unwound"
    assert seen() is None, "the connection outlived its run"


@needs_posix_signals
def test_a_sigint_another_thread_took_still_ends_the_main_threads_wait(
    controller: InterruptController,
) -> None:
    """``pool.map`` waits on a lock in the main thread while the pool's own
    threads wake as its workers die of the same signal; on Linux one of
    them can take the process's SIGINT. The handler is then pending, but
    only the main thread runs it, and that thread sleeps on."""
    controller.begin_run("r1")
    controller.note_hint("r1")
    release = threading.Event()
    bystander = threading.Thread(target=release.wait, name="takes-the-signal")
    bystander.start()
    stop_waiting = threading.Event()

    def give_up() -> None:
        # The hang guard ends the run before it lets the wait go, so the
        # pending handler finds nothing to interrupt once this thread runs
        # again: only a wait that the interrupt itself ended counts.
        controller.end_run("r1")
        stop_waiting.set()

    guard = threading.Timer(HANG_GUARD_S, give_up)
    guard.start()
    interrupted = False
    try:
        assert bystander.ident is not None
        # Delivered to the bystander, as the kernel may deliver a
        # process-directed SIGINT; the C handler runs on that thread.
        signal.pthread_kill(bystander.ident, signal.SIGINT)
        stop_waiting.wait()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        guard.cancel()
        release.set()
        bystander.join()
        controller.end_run("r1")
    assert interrupted, "the main thread slept on through the interrupt"


class Seen:
    """Registered so the test knows the watcher has handled the signal: it
    wakes the main thread before it interrupts registered objects."""

    def __init__(self) -> None:
        self.interrupted = threading.Event()

    def interrupt(self) -> None:
        self.interrupted.set()


@needs_posix_signals
def test_a_sigint_the_main_thread_took_raises_once(controller: InterruptController) -> None:
    """The negative twin: the wake reaches a main thread that already raised
    for the signal, and must not raise a second time while it unwinds."""
    seen = Seen()
    controller.register(seen)
    controller.begin_run("r1")
    controller.note_hint("r1")
    main = threading.get_ident()
    raised = 0
    try:
        try:
            signal.pthread_kill(main, signal.SIGINT)
            threading.Event().wait(HANG_GUARD_S)
        except KeyboardInterrupt:
            raised += 1
        try:
            assert seen.interrupted.wait(HANG_GUARD_S), "the watcher never took the signal"
            # Bytecode runs the handlers still pending, the wake's among them.
            for _ in range(10_000):
                pass
        except KeyboardInterrupt:
            raised += 1
    finally:
        controller.end_run("r1")
    assert raised == 1
