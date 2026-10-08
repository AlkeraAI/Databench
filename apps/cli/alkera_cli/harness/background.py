"""The per-`ChatSession` background-job registry.

Backgrounding a tool call (a long ``bash``, a heavy ``sql.query``, a
self-contained ``spawn_agent``) means starting the work, handing the model a job
id at once, keeping the turn moving, and notifying the model when the job
finishes. Both adapters share this supervisor.

- The registry knows nothing about the event bus, the chat, or what a job does.
  It supervises a coroutine, records its terminal outcome (the tool's native
  result object, or an error), and fires an injected ``on_terminal`` callback.
- :class:`ChatSession` wires ``on_terminal`` to emit the job's tool-result frame
  on the bus and inject the "Backgrounded Tool Finished" notification, so the
  result renders as a normal tool card and reaches the model.
- A backgrounded ``bash`` kills its own process group in a ``finally`` on
  cancellation; the registry does not manage processes.

The clock and the id factory are injectable so the supervisor is deterministic
under test.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from alkera_cli.host.limits import env_count

logger = structlog.get_logger(__name__)

#: Terminal job states (everything that is not ``"running"``).
JobState = Literal["running", "completed", "error", "cancelled"]

#: The three tool families that can be backgrounded today. Kept as a plain
#: ``str`` on the job (not an enum) so a future job kind needs no change here.
JobKind = str

#: Ceiling on CONCURRENTLY-running background jobs per chat, unset by default: a
#: count is not a property of the work, and a turn with twenty independent builds
#: to run has twenty. Refusing the ninth only makes the model queue them itself,
#: slower and with the same peak load. What bounds the fan-out is the box it runs
#: on and the shared per-family concurrency the tools already hold; an operator
#: who wants a chat-level ceiling sets one here, and 0 removes it again.
ENV_MAX_BACKGROUND_JOBS = "ALKERA_MAX_BACKGROUND_JOBS"
DEFAULT_MAX_RUNNING_JOBS = env_count(os.environ.get(ENV_MAX_BACKGROUND_JOBS), default=None)


class BackgroundJobLimitError(RuntimeError):
    """Raised by :meth:`BackgroundJobRegistry.submit` when the per-chat
    concurrency cap is already saturated. The caller surfaces it to the model as
    a clean tool error ("too many background jobs running; cancel one or wait")."""


@dataclass
class BackgroundJob:
    """One supervised background job + its observable state.

    ``result`` holds the tool's NATIVE result object on success (a
    ``SqlQueryResult`` / ``BashResult`` / ``SubagentRunResult`` …) so the
    completion notification can render it exactly like the synchronous result —
    never a flattened text dump. It is NOT serialized here; the consumer owns
    rendering.
    """

    job_id: str
    kind: JobKind
    title: str
    state: JobState = "running"
    started_at: float = 0.0
    completed_at: float | None = None
    result: Any = None
    error: str | None = None
    #: The originating tool-call args (the model's ``{command, …}`` / ``{sql, …}``),
    #: captured at submit so the terminal transcript card can show what was run.
    #: ``None`` for jobs that don't carry one (e.g. an agent — its card is the spawn).
    input: dict[str, Any] | None = None
    #: A short, live, human-readable progress line the status tool can show for a
    #: still-running job (e.g. a bash tail, a child's last tool call). The job's
    #: coroutine updates it in place; "" until set.
    preview: str = ""
    #: The supervising asyncio task. Internal; never part of the public snapshot.
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.state != "running"

    def snapshot(self) -> BackgroundJob:
        """A detached copy safe to hand outside the registry (no live task)."""
        return BackgroundJob(
            job_id=self.job_id,
            kind=self.kind,
            title=self.title,
            state=self.state,
            started_at=self.started_at,
            completed_at=self.completed_at,
            result=self.result,
            error=self.error,
            input=self.input,
            preview=self.preview,
            task=None,
        )


#: A factory the registry calls to obtain the job's coroutine. Taking a factory
#: (rather than a bare coroutine) lets the registry decide WHEN to start the work
#: and guarantees the coroutine is never created if the submit is refused.
CoroFactory = Callable[[], Awaitable[Any]]

#: Fired exactly once per job, when it reaches a terminal state. Receives a
#: SNAPSHOT (the live task is already done). Awaited; exceptions are swallowed +
#: logged so a bad consumer can't wedge the supervisor.
TerminalCallback = Callable[[BackgroundJob], Awaitable[None]]


class BackgroundJobRegistry:
    """Supervises a chat's background jobs. One instance per :class:`ChatSession`.

    Thread-affinity: all methods run on the chat's event loop; no locking needed
    beyond the loop's cooperative scheduling. ``submit`` forks a task; the task's
    completion handler records the outcome and fires ``on_terminal`` — so a job
    that finishes while the chat is mid-turn still lands a recorded result the
    turn-state pump can drain at the next idle edge.
    """

    def __init__(
        self,
        *,
        on_submit: TerminalCallback | None = None,
        on_terminal: TerminalCallback | None = None,
        max_running_jobs: int | None = DEFAULT_MAX_RUNNING_JOBS,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._jobs: dict[str, BackgroundJob] = {}
        #: Fired (as a task) the moment a job is submitted — the runtime publishes the
        #: job's RUNNING transcript card here, so a spinning card appears immediately.
        self._on_submit = on_submit
        #: Live on_submit notification tasks (fire-and-forget) — held so they aren't
        #: GC'd before they run; each removes itself on completion.
        self._submit_tasks: set[asyncio.Task[None]] = set()
        self._on_terminal = on_terminal
        #: Jobs that have ended and whose terminal callback is still running.
        self._delivering: set[str] = set()
        self._max_running = max_running_jobs
        self._clock = clock
        self._id_factory = id_factory or _default_job_id
        #: Set once draining/closed so a late ``submit`` is refused rather than
        #: leaking a task past chat teardown.
        self._closed = False

    # -- introspection ----------------------------------------------------

    def list(self) -> list[BackgroundJob]:
        """All jobs (running + terminal), oldest first — snapshots only."""
        return [j.snapshot() for j in sorted(self._jobs.values(), key=lambda j: j.started_at)]

    def get(self, job_id: str) -> BackgroundJob | None:
        job = self._jobs.get(job_id)
        return job.snapshot() if job is not None else None

    def set_preview(self, job_id: str, text: str) -> None:
        """Replace a running job's live progress text — what ``background_status``
        shows before the job finishes. A finished or unknown job is left alone."""
        job = self._jobs.get(job_id)
        if job is not None and job.state == "running":
            job.preview = text

    def running_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.state == "running")

    def can_accept(self) -> bool:
        """Whether another job could be submitted right now (under the cap and not
        closed). Lets a caller check BEFORE doing expensive setup (e.g. opening a
        child chat) so it never builds state it must immediately tear down."""
        return not self._closed and not self._at_capacity()

    def _at_capacity(self) -> bool:
        """Whether the running cap is set and reached. No cap is never reached."""
        return self._max_running is not None and self.running_count() >= self._max_running

    def has_running(self) -> bool:
        """Whether a job is still running, or has ended and its result is still
        being handed to the chat (its card and the model's wake): between the
        two the chat owes a turn it has not started, and a sleep or a drain
        that took it for idle would drop that turn."""
        return bool(self._delivering) or any(j.state == "running" for j in self._jobs.values())

    def has_jobs(self) -> bool:
        return bool(self._jobs)

    # -- mutation ---------------------------------------------------------

    def submit(
        self,
        coro_factory: CoroFactory,
        *,
        kind: JobKind,
        title: str,
        input: dict[str, Any] | None = None,
    ) -> BackgroundJob:
        """Start a background job; return its (running) snapshot immediately.

        Raises :class:`BackgroundJobLimitError` if the per-chat running cap is
        saturated, or :class:`RuntimeError` if the registry is already closed.
        The coroutine is created HERE (via the factory) and forked onto the
        running loop; its outcome is recorded by :meth:`_finish`. ``input`` is the
        originating tool-call args, carried onto the job so the terminal transcript
        card can show what ran.
        """
        if self._closed:
            raise RuntimeError("background registry is closed")
        if self._at_capacity():
            raise BackgroundJobLimitError(
                f"too many background jobs running ({self._max_running}); "
                "cancel one with background_cancel or wait for one to finish"
            )

        job = BackgroundJob(
            job_id=self._id_factory(),
            kind=kind,
            title=title,
            state="running",
            started_at=self._clock(),
            input=input,
        )
        self._jobs[job.job_id] = job
        if self._on_submit is not None:
            # Fire-and-forget (submit is sync): the runtime publishes the RUNNING
            # transcript card so a spinning "Background task" card shows at once. A
            # bad consumer can't wedge the submit — it runs on its own task. Tracked
            # so it isn't GC'd before it runs; self-removing on completion.
            #
            # Scheduled BEFORE the work task so the RUNNING frame is always published
            # before the job can finish + fire its terminal update — otherwise a job
            # that completes in one loop step would emit its result card BEFORE the
            # running card exists, and the late running frame would clobber the result.
            task = asyncio.ensure_future(self._fire_submit(job.snapshot()))
            self._submit_tasks.add(task)
            task.add_done_callback(self._submit_tasks.discard)
        job.task = asyncio.ensure_future(self._run(job, coro_factory))
        return job.snapshot()

    async def _fire_submit(self, job: BackgroundJob) -> None:
        if self._on_submit is None:
            return
        try:
            await self._on_submit(job)
        except Exception:
            logger.warning("background.on_submit_failed", job_id=job.job_id, exc_info=True)

    async def cancel(self, job_id: str) -> BackgroundJob | None:
        """Cancel a running job (no-op if missing or already terminal).

        Cancelling the supervising task raises ``CancelledError`` inside the
        job's coroutine, which is where process-group teardown for a
        backgrounded ``bash`` daemon happens. Returns the resulting snapshot.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.is_terminal:
            return job.snapshot()
        task = job.task
        if task is not None and not task.done():
            task.cancel()
            # Let the coroutine's cancellation cleanup run; _run records the
            # "cancelled" terminal state + fires on_terminal. gather(...) absorbs
            # both the CancelledError and any cleanup error — the outcome is
            # already recorded, so there's nothing to propagate here.
            await asyncio.gather(task, return_exceptions=True)
        await self._finalize_stranded([job])
        return self.get(job_id)

    async def drain(self) -> None:
        """Cancel + await every running job. Called by ``ChatSession.close()``
        BEFORE ``adapter.stop()`` so a chat never tears down with a live
        detached subprocess behind it. Idempotent; marks the registry closed."""
        self._closed = True
        running = [j for j in self._jobs.values() if j.state == "running" and j.task is not None]
        for job in running:
            assert job.task is not None
            if not job.task.done():
                job.task.cancel()
        # Await all cancellations together; gather absorbs the CancelledErrors
        # and any cleanup error (each outcome is recorded in _run).
        await asyncio.gather(
            *(job.task for job in running if job.task is not None),
            return_exceptions=True,
        )
        await self._finalize_stranded(running)

    async def _finalize_stranded(self, jobs: Iterable[BackgroundJob]) -> None:
        """Force a terminal state on any job still ``running`` after its task was
        cancelled. A task cancelled BEFORE the event loop ever ran its coroutine
        body never executes :meth:`_run` (the cancellation closes the coroutine at
        its first suspension, bypassing the ``except CancelledError`` finish), so it
        would otherwise stay ``running`` forever — a phantom that breaks
        ``cancel()``'s return, ``has_running``, and the resident-session reaper. Mark
        it ``cancelled`` and fire its terminal callback so it behaves exactly like a
        job that was cancelled mid-flight."""
        for job in jobs:
            if not job.is_terminal:
                self._finish(job, "cancelled", error="cancelled")
                await self._fire_terminal(job)

    # -- internals --------------------------------------------------------

    async def _run(self, job: BackgroundJob, coro_factory: CoroFactory) -> None:
        """Await the job's coroutine, record its terminal outcome, then fire the
        terminal callback. Never raises out of the supervised task — every exit
        path (success / error / cancellation) is recorded as a terminal state."""
        try:
            result = await coro_factory()
        except asyncio.CancelledError:
            self._finish(job, "cancelled", error="cancelled")
            await self._fire_terminal(job)
            # Re-raise so `await task` in cancel()/drain() observes the
            # cancellation and the task is reaped cleanly.
            raise
        except Exception as exc:  # a job failure is data, not a crash
            self._finish(job, "error", error=_error_text(exc))
            await self._fire_terminal(job)
            return
        self._finish(job, "completed", result=result)
        await self._fire_terminal(job)

    def _finish(
        self,
        job: BackgroundJob,
        state: JobState,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        # Guard against a double-finish (e.g. cancel racing natural completion):
        # the first terminal transition wins.
        if job.is_terminal:
            return
        job.state = state
        job.completed_at = self._clock()
        job.result = result
        job.error = error

    async def _fire_terminal(self, job: BackgroundJob) -> None:
        if self._on_terminal is None:
            return
        # Entered with no await since the job's terminal transition, so there is
        # no moment at which the job is neither running nor being delivered.
        self._delivering.add(job.job_id)
        try:
            await self._on_terminal(job.snapshot())
        except Exception:  # a bad consumer must not wedge the supervisor
            logger.warning("background.on_terminal_failed", job_id=job.job_id, exc_info=True)
        finally:
            self._delivering.discard(job.job_id)


def _default_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def _error_text(exc: BaseException) -> str:
    msg = str(exc)
    return msg if msg else exc.__class__.__name__


__all__ = [
    "DEFAULT_MAX_RUNNING_JOBS",
    "ENV_MAX_BACKGROUND_JOBS",
    "BackgroundJob",
    "BackgroundJobLimitError",
    "BackgroundJobRegistry",
    "CoroFactory",
    "JobKind",
    "JobState",
    "TerminalCallback",
]
