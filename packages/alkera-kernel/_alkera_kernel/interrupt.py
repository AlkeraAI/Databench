"""Interrupts, the way Jupyter does them: a real ``SIGINT`` from outside.

The engine sends ``run.interrupt {run_id}`` (a hint) and then signals the
kernel's process group. Two things happen on the signal:

- Python's C-level handler writes to the wakeup descriptor at once, even
  while the main thread is inside a C call that holds the GIL or releases it
  (a DuckDB query). The watcher thread reads it and calls ``.interrupt()`` on
  every registered interruptible and cancels the step's asyncio tasks.
- When the main thread next runs bytecode, the Python-level handler raises
  ``KeyboardInterrupt`` there, which also ends ``time.sleep``, lock waits and
  socket reads (they return to the interpreter on ``EINTR``).

A ``SIGINT`` sent to the process group is the process's, not the main
thread's: whichever thread next enters the kernel may take it. When another
thread does (a pool's worker handler waking as the workers die, the reader
taking the hint), the C handler runs there and the main thread sleeps on in
its lock or ``select``, so the Python handler never runs. The watcher
therefore also wakes the main thread with :data:`WAKE_SIGNAL`, whose handler
does nothing: the wait ends with ``EINTR`` and the interpreter runs the
``SIGINT`` handler that is pending, on the main thread. If the main thread
took the ``SIGINT`` itself, nothing is pending by then and the wake is a
no-op, so one signal never raises twice.

Both act only when the signal targets the run that is executing: the hint
names it. When no hint has arrived (the reader thread was starved) the
handler waits briefly for one and, if none comes, interrupts whatever runs.
A hint naming a run that already finished makes the signal a no-op, so an
interrupt racing a run boundary never interrupts the next run.

Once an interrupt is taken, every registered object is held until its run
ends and is interrupted once more as the run ends, after the step unwound.
DuckDB stops a query on the main thread through its own signal check, but
its worker threads run on: a connection released then waits in
``Executor::CancelTasks`` forever (a temporary connection, as in
``duckdb.connect().execute(q)``, is released while the step unwinds). An
``interrupt()`` after the unwind stops those workers, so the release returns.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any, Final

#: How long the handler waits for a hint that the reader has not delivered yet.
HINT_WAIT_S = 0.15
#: A hint older than this no longer explains a signal.
HINT_TTL_S = 30.0
#: How often a taken interrupt is said again to the registered objects until
#: its run ends. DuckDB clears a connection's interrupt when a query begins,
#: so one said between ``execute()`` being called and the query starting is
#: lost; saying it again reaches the query that started.
REPEAT_S = 0.1
#: Sent by the watcher to the main thread to end a blocking wait there. Its
#: default action is to ignore it, so a cell that resets the handler can make
#: a wake a no-op but never kill the kernel with one.
WAKE_SIGNAL: Final = getattr(signal, "SIGURG", None)


class InterruptController:
    def __init__(self) -> None:
        # Reentrant: the handler takes it on the main thread, which may be
        # inside begin_run or end_run when the signal lands.
        self._lock = threading.RLock()
        self._hint_event = threading.Event()
        self._hint: tuple[str, float] | None = None
        self.current_run: str | None = None
        self._pending_for: str | None = None
        self._interruptibles: weakref.WeakSet[Any] = weakref.WeakSet()
        self._strong: list[Any] = []
        #: Registered objects kept alive from a taken interrupt to the end
        #: of its run, to be interrupted once more then.
        self._held: list[Any] = []
        self.on_cancel_tasks: Callable[[], None] | None = None
        self._wake_r: int | None = None
        self._main_ident: int | None = None

    # ------------------------------------------------------------------ state

    def begin_run(self, run_id: str) -> None:
        with self._lock:
            self.current_run = run_id
            self._pending_for = None

    def end_run(self, run_id: str) -> None:
        with self._lock:
            if self.current_run == run_id:
                self.current_run = None
            self._pending_for = None
            if self._hint is not None and self._hint[0] == run_id:
                self._hint = None
            held, self._held = self._held, []
        # After the step unwound and before anything is released: a query
        # stopped by DuckDB's own signal check leaves its workers running.
        for obj in held:
            with contextlib.suppress(Exception):
                obj.interrupt()

    def pending(self, run_id: str) -> bool:
        """Whether an interrupt for ``run_id`` was taken (the step that was
        running ends ``interrupted`` whatever it raised)."""
        return self._pending_for == run_id

    def note_hint(self, run_id: str) -> None:
        """Called by the reader thread on ``run.interrupt``."""
        with self._lock:
            self._hint = (run_id, time.monotonic())
        self._hint_event.set()

    def register(self, obj: Any) -> None:
        try:
            self._interruptibles.add(obj)
        except TypeError:
            self._strong.append(obj)

    def unregister(self, obj: Any) -> None:
        self._interruptibles.discard(obj)
        if obj in self._strong:
            self._strong.remove(obj)

    # ------------------------------------------------------------------ deciding

    def _target(self) -> str | None:
        """The run this signal interrupts, or None to ignore it."""
        run = self.current_run
        if run is None:
            return None
        hint = self._hint
        if hint is None or time.monotonic() - hint[1] > HINT_TTL_S:
            self._hint_event.clear()
            self._hint_event.wait(HINT_WAIT_S)
            hint = self._hint
        if hint is None or time.monotonic() - hint[1] > HINT_TTL_S:
            return run  # no hint: the reader was starved, interrupt what runs
        return run if hint[0] == run else None

    def _take(self) -> str | None:
        target = self._target()
        if target is None:
            return None
        with self._lock:
            if self.current_run != target:
                return None
            self._pending_for = target
        self._hold_registered()
        return target

    def _hold_registered(self) -> list[Any]:
        """Hold every registered object until the run ends; all of them."""
        objs = [*self._interruptibles, *self._strong]
        with self._lock:
            if self._pending_for is None or self.current_run != self._pending_for:
                return objs  # the run ended: nothing is held into the next
            held = {id(obj) for obj in self._held}
            self._held.extend(obj for obj in objs if id(obj) not in held)
        return objs

    def _interrupt_registered(self) -> None:
        # A broken interrupt() of one object must not keep the others from
        # being interrupted.
        for obj in self._hold_registered():
            with contextlib.suppress(Exception):
                obj.interrupt()
        if self.on_cancel_tasks is not None:
            with contextlib.suppress(Exception):
                self.on_cancel_tasks()

    # ------------------------------------------------------------------ wiring

    def install(self) -> None:
        """Install the handler (main thread) and the wakeup watcher."""
        r, w = os.pipe()
        os.set_blocking(w, False)
        signal.set_wakeup_fd(w, warn_on_full_buffer=False)
        self._wake_r = r
        self._main_ident = threading.get_ident()
        signal.signal(signal.SIGINT, self._handler)
        if WAKE_SIGNAL is not None:
            signal.signal(WAKE_SIGNAL, _ends_a_wait)
        threading.Thread(target=self._watch, name="alkera-interrupt", daemon=True).start()

    def _handler(self, signum: int, frame: object) -> None:
        run = self.current_run
        if run is not None and self._pending_for == run:
            raise KeyboardInterrupt
        if self._take() is not None:
            raise KeyboardInterrupt

    def _watch(self) -> None:
        assert self._wake_r is not None
        while True:
            try:
                data = os.read(self._wake_r, 64)
            except InterruptedError:
                continue
            except OSError:
                return
            if not data:
                return
            if signal.SIGINT not in data:
                continue
            run = self.current_run
            if run is None:
                continue
            target = self._take() if self._pending_for != run else run
            if target is not None:
                self._wake_main()
                self._interrupt_registered()
                threading.Thread(
                    target=self._keep_interrupting,
                    args=(target,),
                    name="alkera-interrupt-repeat",
                    daemon=True,
                ).start()

    def _wake_main(self) -> None:
        """End the main thread's blocking wait, so the ``SIGINT`` handler
        runs there even when another thread took the signal."""
        if WAKE_SIGNAL is None or self._main_ident is None:
            return
        with contextlib.suppress(OSError, ValueError):
            signal.pthread_kill(self._main_ident, WAKE_SIGNAL)

    def _keep_interrupting(self, run_id: str) -> None:
        """Say a taken interrupt again to the registered objects until its
        run ends, so one that landed before a query began still stops it."""
        while True:
            time.sleep(REPEAT_S)
            if self.current_run != run_id or self._pending_for != run_id:
                return
            for obj in self._hold_registered():
                with contextlib.suppress(Exception):
                    obj.interrupt()

    def reset_in_child(self) -> None:
        """After ``fork``: a child does not own the parent's interrupt state;
        ``SIGINT`` raises ``KeyboardInterrupt`` there as in any Python
        process, so a pool worker stops with the run."""
        try:
            signal.set_wakeup_fd(-1)
        except (ValueError, OSError):
            pass
        try:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        except (ValueError, OSError):
            pass
        if WAKE_SIGNAL is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(WAKE_SIGNAL, signal.SIG_DFL)
        self._main_ident = None
        self._interruptibles = weakref.WeakSet()
        self._strong = []
        self._held = []
        self.current_run = None


def _ends_a_wait(signum: int, frame: object) -> None:
    """The wake's handler. Having one is what makes the main thread's
    wait return with ``EINTR``; the ``SIGINT`` handler does the work."""
