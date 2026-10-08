"""The pool of Loro sandbox workers one backend process keeps.

Each slot owns at most one worker process and serves one request at a time.
Workers are started warm: a worker pays its document types' one-time costs
(imports, parser tables; see ``core.warm_all``) before it answers the ping
that admits it, so no request's budget is ever spent on them. :meth:`start`
starts every slot when the process comes up, and a worker killed at its budget
is replaced in the background at once. A document always lands on the same
slot (a stable hash of its key), so the worker that judged its last update
usually still holds it in memory. The pool never trusts a worker past one
request: a reply that does not arrive within the request's timeout, a closed
pipe, or a malformed frame kills the process (``SIGKILL``) and the next
request on that slot starts a fresh one — after a short backoff, so a worker
that dies on start cannot spin. A caller that stops waiting (its socket
closed) does not end its request: once on the wire, the request runs to its
answer or its budget in a task the pool owns, and the answer is read and
dropped, so one caller's departure never costs every other document on the
slot its worker.

What the pool tells its caller:

* :class:`SandboxCrashedError` — the worker died or spoke garbage on THIS
  request. The caller decides whether to retry (a fresh worker holds no
  cache, so it will answer ``need`` first).
* :class:`SandboxTimeoutError` — the worker outlived the request's budget and
  was killed. It is deliberately NOT a crash: slow says the host is busy, not
  that the request is bad, so no caller can treat it as a verdict on the
  bytes (``except SandboxCrashedError`` never catches it), and the slot's
  breaker never counts it. The slot is started again at once, warm, so the
  retry the caller is told to make finds a worker ready.
* :class:`SandboxBusyError` — the slot's breaker is open (more than
  ``breaker_crashes`` crashes in ``breaker_window``), or its worker could not
  be started this time. Carries a jittered retry hint; nothing reached a
  worker.
* :class:`SandboxUnavailableError` — a worker cannot be started at all (the
  interpreter or the module is missing). The lane falls back; the backend
  keeps serving everything else.
* :class:`SandboxRefusedError` — the worker answered ``ok: false``: a request it
  would not do, with the code it named.

The pool is the only thing that speaks to a worker. It imports nothing from
Loro.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import ntpath
import os
import random
import sys
import time
import zlib
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from alkera_core.logging import get_logger
from alkera_core.process import SpawnSpec, kill_tree_now, spawn_async

from backend.services.crdt.sandbox.protocol import Frame, FrameError, encode_frame, read_frame

log = get_logger(__name__)

#: How long to wait before starting a worker again after the Nth consecutive
#: failure on a slot (the last figure repeats).
SPAWN_BACKOFF_SECONDS: tuple[float, ...] = (0.05, 0.2, 1.0)
#: How long a worker gets to answer its first ping. It warms before it
#: answers (a cold page cache on a loaded host measured ten seconds for the
#: notebook type's imports alone), and this is no request's budget: a worker
#: that has not started holds no request.
STARTUP_TIMEOUT_SECONDS = 60.0
#: A worker's stderr is logged at most this many lines per slot per window,
#: each cut to this many characters: what a worker prints (a Loro panic
#: message, say) is something a client can provoke at will.
STDERR_LINES_PER_WINDOW = 20
STDERR_WINDOW_SECONDS = 60.0
STDERR_LINE_CHARS = 500


class SandboxError(Exception):
    """Base for everything the pool raises."""


class SandboxCrashedError(SandboxError):
    """The worker died, hung or spoke garbage on this request; it is gone."""


class SandboxTimeoutError(SandboxError):
    """The worker outlived its budget on this request and was killed. Slow is
    not broken (a busy host makes honest requests slow), so a timeout is not a
    crash: it never counts toward the slot's breaker, and it is never a
    verdict on the request's bytes."""


class SandboxBusyError(SandboxError):
    def __init__(self, message: str, *, retry_after_ms: int) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class SandboxUnavailableError(SandboxError):
    """No worker can be started; the CRDT lane cannot serve."""


class SandboxRefusedError(SandboxError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


#: The only environment a worker is started with. It parses bytes a stranger
#: sent, so it must hold none of the backend's secrets (database URLs, signing
#: keys, cloud and provider credentials): what it needs is an interpreter, its
#: import path and a locale, and its configuration arrives on the command line.
WORKER_ENV_KEYS: Final = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONDONTWRITEBYTECODE",
        "VIRTUAL_ENV",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
    }
)


def _end_worker(process: asyncio.subprocess.Process) -> None:
    """Kill a worker and everything it started, at once. On POSIX it leads
    its own process group; on Windows the whole tree is ended. The worker is
    started as itself (:func:`launch_spec`), so the exit the caller then
    awaits is the worker's own."""
    kill_tree_now(process.pid)


def worker_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a worker starts with: ``source`` (the backend's own)
    narrowed to :data:`WORKER_ENV_KEYS`."""
    env = os.environ if source is None else source
    return {key: value for key, value in env.items() if key in WORKER_ENV_KEYS}


def launch_spec(
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    executable: str | None = None,
    base_executable: str | None = None,
    windows: bool | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment that start ``command`` as the worker itself.

    A venv's ``python.exe`` on Windows is a launcher that runs the base
    interpreter as its child. Started through it, the process the pool holds
    is the launcher: its pid is not the worker's, and waiting for it to exit
    says nothing about whether the worker it started has. So a command naming
    this venv's interpreter runs the base interpreter directly, told which venv
    it belongs to the way ``multiprocessing`` does it on Windows. Any other
    command, and every command on POSIX, is started as given."""
    executable = sys.executable if executable is None else executable
    if base_executable is None:
        base_executable = getattr(sys, "_base_executable", executable)
    windows = os.name == "nt" if windows is None else windows
    argv, environment = list(command), dict(env)
    in_venv = ntpath.normcase(executable) != ntpath.normcase(base_executable)
    if windows and in_venv and argv and ntpath.normcase(argv[0]) == ntpath.normcase(executable):
        argv[0] = base_executable
        environment["__PYVENV_LAUNCHER__"] = executable
    return argv, environment


def worker_command(
    *, memory_mb: int, cache_docs: int, cache_bytes: int, python: str = ""
) -> list[str]:
    """The argv that starts one worker with ``python`` (this interpreter when
    empty)."""
    return [
        python or sys.executable,
        "-m",
        "backend.services.crdt.sandbox.worker",
        "--memory-mb",
        str(memory_mb),
        "--cache-docs",
        str(cache_docs),
        "--cache-bytes",
        str(cache_bytes),
    ]


@dataclass(frozen=True, slots=True)
class PoolConfig:
    workers: int = 2
    command: Sequence[str] = field(
        default_factory=lambda: worker_command(
            memory_mb=512, cache_docs=256, cache_bytes=64 * 1024 * 1024
        )
    )
    breaker_crashes: int = 5
    breaker_window_seconds: float = 60.0
    breaker_cooldown_seconds: float = 30.0
    #: Consecutive start failures on one slot before the pool reports it
    #: cannot run workers at all.
    max_start_failures: int = 3
    #: How long a starting worker (warming included) has to answer its ping.
    startup_seconds: float = STARTUP_TIMEOUT_SECONDS

    @classmethod
    def from_settings(cls) -> PoolConfig:
        from alkera_core.config import settings

        return cls(
            workers=settings.realtime_crdt_workers,
            command=worker_command(
                memory_mb=settings.realtime_crdt_worker_memory_mb,
                cache_docs=settings.realtime_crdt_cache_docs,
                cache_bytes=settings.realtime_crdt_cache_bytes,
                python=settings.realtime_crdt_worker_python,
            ),
        )


@dataclass
class _Slot:
    index: int
    process: asyncio.subprocess.Process | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0
    failures: int = 0
    start_failures: int = 0
    not_before: float = 0.0
    crashes: deque[float] = field(default_factory=deque)
    open_until: float = 0.0
    stderr_task: asyncio.Task[None] | None = None
    pid: int | None = None
    #: The stderr budget, kept on the slot rather than the worker so a client
    #: cannot reset it by crashing the worker.
    stderr_window_start: float = float("-inf")
    stderr_logged: int = 0
    stderr_suppressed: int = 0


class SandboxPool:
    """See the module docstring. ``clock`` and ``sleep`` are seams for tests."""

    def __init__(
        self,
        config: PoolConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.config = config or PoolConfig()
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._slots = [_Slot(index) for index in range(max(1, self.config.workers))]
        self._closed = False
        self._next_id = 0
        self._reapers: set[asyncio.Task[int]] = set()
        self._inflight: set[asyncio.Task[Frame]] = set()
        self._starters: set[asyncio.Task[None]] = set()

    # -- public --------------------------------------------------------------

    def slot_of(self, key: str) -> int:
        """The slot ``key`` always lands on (stable across processes)."""
        return zlib.crc32(key.encode("utf-8")) % len(self._slots)

    async def request(
        self,
        key: str,
        header: dict[str, Any],
        blobs: Sequence[bytes] = (),
        *,
        budget_seconds: float,
    ) -> Frame:
        """Send one request for the document ``key`` and return the reply. The
        worker has ``budget_seconds`` to answer, after which it is killed."""
        if self._closed:
            raise SandboxUnavailableError("the sandbox pool is closed")
        slot = self._slots[self.slot_of(key)]
        now = self._clock()
        if slot.open_until > now:
            raise SandboxBusyError(
                "the sandbox worker is cooling down after repeated crashes",
                retry_after_ms=self._retry_hint(slot.open_until - now),
            )
        slot.waiting += 1
        sent = asyncio.Event()
        frame = Frame(header=dict(header), blobs=tuple(blobs))
        task = asyncio.create_task(self._serve(slot, frame, budget_seconds, sent))
        self._inflight.add(task)
        task.add_done_callback(functools.partial(self._settled, slot))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The caller went away (its socket closed). A request still
            # queued is dropped; one the worker already holds runs to its
            # end, within its budget, so the worker every other document on
            # this slot shares is never killed for a caller's departure.
            if not sent.is_set():
                task.cancel()
            raise

    def start(self) -> asyncio.Task[None]:
        """Start (and so warm) every slot's worker now, in the background, so
        the first request on each finds one ready. Returns the task, which
        ends once every slot has started or failed to; a slot that fails is
        started again by its next request. Closing the pool cancels it."""
        task = asyncio.create_task(self._start_all(), name="crdt-sandbox-start")
        self._starters.add(task)
        task.add_done_callback(self._starters.discard)
        return task

    async def _start_all(self) -> None:
        await asyncio.gather(*(self._start_slot(slot) for slot in self._slots))

    async def _start_slot(self, slot: _Slot) -> None:
        """Start ``slot``'s worker unless it has one, taking the slot's turn
        like a request does. A failure is the next request's to meet."""
        if self._closed:
            return
        async with slot.lock:
            if self._closed:
                return
            with contextlib.suppress(SandboxError):
                await self._ensure_started(slot)

    def _restart_soon(self, slot: _Slot) -> None:
        """Replace ``slot``'s worker in the background, so the warm-up runs
        while the caller waits out its retry hint rather than after it."""
        if self._closed:
            return
        task = asyncio.get_running_loop().create_task(self._start_slot(slot))
        self._starters.add(task)
        task.add_done_callback(self._starters.discard)

    async def close(self) -> None:
        self._closed = True
        for starter in list(self._starters):
            starter.cancel()
        inflight = list(self._inflight)
        for task in inflight:
            task.cancel()
        for slot in self._slots:
            await self._kill(slot)
        await asyncio.gather(*inflight, return_exceptions=True)
        # A starter that had just spawned its worker as it was cancelled.
        await asyncio.gather(*list(self._starters), return_exceptions=True)
        for slot in self._slots:
            await self._kill(slot)
        await asyncio.gather(*self._reapers, return_exceptions=True)

    def pids(self) -> list[int | None]:
        """Each slot's live worker pid (tests and diagnostics)."""
        return [
            slot.pid if slot.process is not None and slot.process.returncode is None else None
            for slot in self._slots
        ]

    # -- internals -----------------------------------------------------------

    def _retry_hint(self, base_seconds: float) -> int:
        return int((base_seconds + 0.05 + 0.2 * self._jitter()) * 1000)

    async def _serve(
        self, slot: _Slot, frame: Frame, budget_seconds: float, sent: asyncio.Event
    ) -> Frame:
        async with slot.lock:
            sent.set()
            return await self._call(slot, frame, budget_seconds)

    def _settled(self, slot: _Slot, task: asyncio.Task[Frame]) -> None:
        """A request ended, whether or not anyone still waits on it: it leaves
        the queue, and an outcome nobody will read is consumed here."""
        slot.waiting -= 1
        self._inflight.discard(task)
        if not task.cancelled():
            task.exception()

    async def _call(self, slot: _Slot, frame: Frame, budget_seconds: float) -> Frame:
        await self._ensure_started(slot)
        process = slot.process
        assert process is not None and process.stdin is not None and process.stdout is not None
        self._next_id += 1
        request_id = self._next_id
        try:
            encoded = encode_frame(
                Frame(header={**frame.header, "id": request_id}, blobs=frame.blobs)
            )
        except FrameError as exc:
            raise SandboxRefusedError("bad_request", str(exc)) from exc
        try:
            process.stdin.write(encoded)
            async with asyncio.timeout(budget_seconds):
                await process.stdin.drain()
                reply = await read_frame(process.stdout)
        except TimeoutError as exc:
            await self._crashed(slot, "timed out", counts=False)
            self._restart_soon(slot)
            raise SandboxTimeoutError("the sandbox worker timed out") from exc
        except (asyncio.IncompleteReadError, ConnectionError, FrameError) as exc:
            reason = type(exc).__name__
            await self._crashed(slot, reason)
            raise SandboxCrashedError(f"the sandbox worker {reason}") from exc
        except BaseException:
            # Cancelled with the request on the wire (the pool is closing):
            # its answer would be read by the next request, so the worker's
            # stream cannot be trusted past here and it goes.
            self._abandon(slot)
            raise
        if reply.header.get("id") != request_id:
            await self._crashed(slot, "a reply to another request")
            raise SandboxCrashedError("the sandbox worker answered another request")
        slot.failures = 0
        if reply.header.get("ok") is not True:
            raise SandboxRefusedError(
                str(reply.header.get("code") or "refused"), str(reply.header.get("message") or "")
            )
        return reply

    def _abandon(self, slot: _Slot) -> None:
        """Kill the slot's worker without waiting (its request is being
        cancelled); it is reaped in the background and the next request starts
        a fresh one."""
        process, slot.process = slot.process, None
        if process is None or process.returncode is not None:
            return
        _end_worker(process)
        task, slot.stderr_task = slot.stderr_task, None
        if task is not None:
            task.cancel()
        reaper = asyncio.get_running_loop().create_task(process.wait())
        self._reapers.add(reaper)
        reaper.add_done_callback(self._reapers.discard)

    async def _ensure_started(self, slot: _Slot) -> None:
        if slot.process is not None and slot.process.returncode is None:
            return
        if slot.process is not None:
            # It died between requests (killed from outside, or out of memory).
            await self._crashed(slot, f"exited with {slot.process.returncode}")
        wait = slot.not_before - self._clock()
        if wait > 0:
            await self._sleep(wait)
        try:
            argv, env = launch_spec(self.config.command, worker_environment())
            process = await spawn_async(
                SpawnSpec(
                    argv=argv,
                    env=env,
                    stdin="pipe",
                    stdout="pipe",
                    stderr="pipe",
                    read_limit=4 * 1024 * 1024,
                )
            )
        except OSError as exc:
            self._start_failed(slot)
            raise SandboxUnavailableError(f"cannot start a sandbox worker: {exc}") from exc
        slot.process = process
        slot.pid = process.pid
        slot.stderr_task = asyncio.create_task(self._drain_stderr(slot, process))
        try:
            async with asyncio.timeout(self.config.startup_seconds):
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(encode_frame(Frame({"op": "ping"})))
                await process.stdin.drain()
                hello = await read_frame(process.stdout)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, FrameError) as exc:
            # A worker that does not come up is never the request's doing (it
            # has not been sent): the caller is told to come back, as busy.
            await self._kill(slot)
            self._start_failed(slot)
            if slot.start_failures >= self.config.max_start_failures:
                raise SandboxUnavailableError("sandbox workers do not start") from exc
            raise SandboxBusyError(
                "the sandbox worker did not start",
                retry_after_ms=self._retry_hint(max(0.0, slot.not_before - self._clock())),
            ) from exc
        except BaseException:
            # Cancelled with the ping unanswered: its reply would be read by
            # the next request. The worker goes, as in ``_call``.
            self._abandon(slot)
            raise
        if hello.header.get("ok") is not True:
            await self._kill(slot)
            self._start_failed(slot)
            raise SandboxUnavailableError("a sandbox worker refused its first ping")
        slot.start_failures = 0
        log.info(
            "crdt.sandbox.started",
            slot=slot.index,
            pid=process.pid,
            loro=hello.header.get("loro"),
            warm=hello.header.get("warm"),
        )

    def _start_failed(self, slot: _Slot) -> None:
        slot.start_failures += 1
        slot.failures += 1
        slot.not_before = self._clock() + self._backoff(slot.failures)

    def _backoff(self, failures: int) -> float:
        return SPAWN_BACKOFF_SECONDS[min(failures, len(SPAWN_BACKOFF_SECONDS)) - 1]

    async def _crashed(self, slot: _Slot, reason: str, *, counts: bool = True) -> None:
        pid = slot.pid
        await self._kill(slot)
        now = self._clock()
        if not counts:
            # Killed for taking too long: replaced at once, and not a crash.
            log.warning("crdt.sandbox.timed_out", slot=slot.index, pid=pid)
            return
        slot.failures += 1
        slot.not_before = now + self._backoff(slot.failures)
        slot.crashes.append(now)
        while slot.crashes and slot.crashes[0] <= now - self.config.breaker_window_seconds:
            slot.crashes.popleft()
        log.warning("crdt.sandbox.crashed", slot=slot.index, pid=pid, reason=reason)
        if len(slot.crashes) > self.config.breaker_crashes:
            slot.open_until = now + self.config.breaker_cooldown_seconds
            slot.crashes.clear()
            log.error(
                "crdt.sandbox.breaker_open",
                slot=slot.index,
                cooldown_seconds=self.config.breaker_cooldown_seconds,
            )

    async def _kill(self, slot: _Slot) -> None:
        process, slot.process = slot.process, None
        if process is not None and process.returncode is None:
            _end_worker(process)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5):
                    await process.wait()
        task, slot.stderr_task = slot.stderr_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _drain_stderr(self, slot: _Slot, process: asyncio.subprocess.Process) -> None:
        """Read the worker's stderr to its end, whatever it holds, so the
        worker never blocks writing to it, and log it within the slot's
        budget. Read in chunks rather than lines: a line longer than the
        reader's limit would end ``readline`` and, with it, the draining."""
        if process.stderr is None:
            return
        pending = b""
        try:
            while True:
                chunk = await process.stderr.read(64 * 1024)
                if not chunk:
                    if pending:
                        self._stderr_line(slot, process.pid, pending)
                    return
                *lines, pending = (pending + chunk).split(b"\n")
                if len(pending) > STDERR_LINE_CHARS * 4:
                    # The rest of an overlong line is not worth keeping.
                    lines.append(pending)
                    pending = b""
                for line in lines:
                    self._stderr_line(slot, process.pid, line)
        finally:
            self._stderr_summary(slot, process.pid)

    def _stderr_line(self, slot: _Slot, pid: int, line: bytes) -> None:
        now = self._clock()
        if now - slot.stderr_window_start >= STDERR_WINDOW_SECONDS:
            self._stderr_summary(slot, pid)
            slot.stderr_window_start = now
            slot.stderr_logged = 0
        if slot.stderr_logged >= STDERR_LINES_PER_WINDOW:
            slot.stderr_suppressed += 1
            return
        slot.stderr_logged += 1
        log.warning(
            "crdt.sandbox.stderr",
            slot=slot.index,
            pid=pid,
            line=line[: STDERR_LINE_CHARS * 4]
            .decode("utf-8", "replace")
            .rstrip()[:STDERR_LINE_CHARS],
        )

    def _stderr_summary(self, slot: _Slot, pid: int) -> None:
        if slot.stderr_suppressed:
            log.warning(
                "crdt.sandbox.stderr_suppressed",
                slot=slot.index,
                pid=pid,
                lines=slot.stderr_suppressed,
            )
            slot.stderr_suppressed = 0


__all__ = [
    "SPAWN_BACKOFF_SECONDS",
    "STDERR_LINES_PER_WINDOW",
    "STDERR_LINE_CHARS",
    "STDERR_WINDOW_SECONDS",
    "PoolConfig",
    "SandboxBusyError",
    "SandboxCrashedError",
    "SandboxError",
    "SandboxPool",
    "SandboxRefusedError",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "worker_command",
    "worker_environment",
]
