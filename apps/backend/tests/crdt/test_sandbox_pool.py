"""The sandbox pool against real worker processes.

The happy path runs the production worker (real Loro, real pipes). Every
failure the pool must survive — a worker that dies mid-request, hangs, answers
garbage, exits on start, or is killed between requests — is staged by a small
scripted worker the pool is pointed at through its ``command`` seam, so the
test controls exactly when and how the worker misbehaves.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from backend.services.crdt.sandbox.pool import (
    SPAWN_BACKOFF_SECONDS,
    STDERR_LINE_CHARS,
    STDERR_LINES_PER_WINDOW,
    STDERR_WINDOW_SECONDS,
    PoolConfig,
    SandboxBusyError,
    SandboxCrashedError,
    SandboxPool,
    SandboxRefusedError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    launch_spec,
    worker_command,
)
from backend.services.crdt.sandbox.protocol import MAX_HEADER_BYTES, decode_projection
from structlog.testing import capture_logs
from tests.crdt.crdt_world import alive, kill_hard

_FAKE = textwrap.dedent(
    """
    import os, sys, time
    from backend.services.crdt.sandbox.protocol import Frame, encode_frame
    from backend.services.crdt.sandbox.worker import read_request

    mode = sys.argv[1]
    if mode == "die-at-start":
        sys.exit(3)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        frame = read_request(stdin)
        if frame is None:
            sys.exit(0)
        op = frame.header.get("op")
        if op == "ping":
            if mode == "slow-start":
                time.sleep(0.5)
            ok = mode != "refuse-ping"
            reply = Frame({"ok": ok, "pid": os.getpid()})
        elif op == "crash":
            os._exit(1)
        elif op == "hang":
            time.sleep(3600)
        elif op == "slow":
            time.sleep(0.5)
            reply = Frame({"ok": True, "pid": os.getpid(), "echo": frame.header})
        elif op == "garbage":
            stdout.write(b"\\xff\\xff\\xff\\xff not a frame")
            stdout.flush()
            continue
        elif op == "spew":
            line = b"x" * frame.header.get("width", 10)
            end = b"\\n" if frame.header.get("newlines", True) else b""
            for _ in range(frame.header.get("lines", 1)):
                sys.stderr.buffer.write(line + end)
            sys.stderr.buffer.flush()
            reply = Frame({"ok": True, "pid": os.getpid(), "echo": frame.header})
        elif op == "refuse":
            reply = Frame({"ok": False, "code": "bad_request", "message": "no"})
        elif op == "stale":
            # Answers as if to some other request.
            reply = Frame({"ok": True, "id": -1, "echo": frame.header})
        else:
            reply = Frame({"ok": True, "pid": os.getpid(), "echo": frame.header}, frame.blobs)
        if op != "stale" and "id" in frame.header:
            reply.header["id"] = frame.header["id"]
        stdout.write(encode_frame(reply))
        stdout.flush()
    """
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))


def _fake_pool(
    tmp_path: Path, mode: str = "normal", *, clock: _Clock | None = None, **config: object
) -> SandboxPool:
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE)
    clock = clock or _Clock()
    return SandboxPool(
        PoolConfig(workers=1, command=[sys.executable, str(script), mode], **config),  # type: ignore[arg-type]
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda: 0.0,
    )


async def _pid(pool: SandboxPool, key: str = "doc") -> int:
    reply = await pool.request(key, {"op": "echo"}, budget_seconds=10)
    return int(reply.header["pid"])


# ---------------------------------------------------------------------------
# The production worker
# ---------------------------------------------------------------------------


async def test_a_file_far_larger_than_a_frame_header_seeds_and_reads_back_through_the_worker() -> (
    None
):
    """A seed's text and every projection travel as blobs: a file of 1 MiB
    (four times the header cap) is seeded, read back whole, and its
    projection is a digest and a size."""
    text = ("0123456789abcdef" * 64 + "\n") * 2000
    assert len(text.encode()) > 4 * MAX_HEADER_BYTES
    pool = SandboxPool(
        PoolConfig(
            workers=1,
            command=worker_command(memory_mb=1024, cache_docs=8, cache_bytes=64 * 1024 * 1024),
        )
    )
    try:
        key = "org:file:node-1"
        seeded = await pool.request(
            key,
            {"op": "seed", "key": key, "epoch": 1, "rules": "file", "peer": 4000},
            [text.encode()],
            budget_seconds=20,
        )
        _snapshot, _vv, projection, _base_vv = seeded.blobs
        assert decode_projection(projection) == {
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "bytes": len(text.encode()),
            "lines": 2001,
        }
        read = await pool.request(
            key,
            {"op": "content", "key": key, "epoch": 1, "log_seq": 0, "rules": "file"},
            [],
            budget_seconds=20,
        )
        assert read.blobs[0].decode() == text
    finally:
        await pool.close()


@pytest.mark.skipif(
    sys.platform == "win32", reason="a Windows process starts loader threads of its own"
)
async def test_a_warmed_worker_runs_on_one_thread_whatever_the_cores() -> None:
    """The warm-up parses a notebook's SQL cell. Through DuckDB that starts
    a thread per core, whose stacks and arenas exhaust the address-space cap
    on a many-core host; the worker parses it as its production venv does,
    with sqlglot, and stays on its one thread."""
    psutil = pytest.importorskip("psutil")
    pool = SandboxPool(
        PoolConfig(
            workers=1,
            command=worker_command(memory_mb=1024, cache_docs=8, cache_bytes=64 * 1024 * 1024),
        )
    )
    try:
        reply = await pool.request("doc", {"op": "stats"}, budget_seconds=20)
        assert reply.header["ok"] is True
        (pid,) = pool.pids()
        assert pid is not None
        assert psutil.Process(pid).num_threads() == 1
    finally:
        await pool.close()


async def test_the_real_worker_seeds_validates_and_advances_a_document() -> None:
    """End to end through the pipes: the pool, the frame codec, the worker's
    dispatch and Loro all agree on one keystroke."""
    from loro import ExportMode, LoroDoc

    pool = SandboxPool(
        PoolConfig(
            workers=1,
            command=worker_command(memory_mb=1024, cache_docs=8, cache_bytes=8 * 1024 * 1024),
        )
    )
    try:
        key = "org:chat_draft:sess-1"
        seeded = await pool.request(
            key,
            {
                "op": "seed",
                "key": key,
                "epoch": 1,
                "rules": "chat_draft",
                "peer": 4000,
            },
            [b"hi"],
            budget_seconds=10,
        )
        snapshot, _vv, projection, _base_vv = seeded.blobs
        assert decode_projection(projection)["text"] == "hi"

        client = LoroDoc()  # type: ignore[no-untyped-call]
        client.peer_id = 5000
        client.import_(snapshot)
        before = client.oplog_vv
        client.get_text("draft").insert(2, "!")
        client.commit()
        update = bytes(client.export(ExportMode.Updates(before)))

        header = {"key": key, "epoch": 1, "log_seq": 0, "rules": "chat_draft", "peers": [5000]}
        verdict = await pool.request(key, {"op": "validate", **header}, [update], budget_seconds=10)
        assert verdict.header["outcome"] == "ok"
        delta, after, projection = verdict.blobs
        assert decode_projection(projection)["text"] == "hi!"
        assert after == bytes(client.oplog_vv.encode())

        moved = await pool.request(
            key, {"op": "advance", "key": key, "epoch": 1, "log_seq": 1}, [delta], budget_seconds=10
        )
        assert moved.header["advanced"] is True

        # A position the worker does not hold is a request to load, not a guess.
        stale = await pool.request(key, {"op": "validate", **header}, [update], budget_seconds=10)
        assert {k: stale.header[k] for k in ("ok", "need", "at")} == {
            "ok": True,
            "need": True,
            "at": [1, 1],
        }

        with pytest.raises(SandboxRefusedError) as excinfo:
            await pool.request(
                key, {"op": "validate", **header, "rules": "nope"}, [update], budget_seconds=10
            )
        assert excinfo.value.code == "unknown_rules"
    finally:
        await pool.close()


async def test_a_document_always_lands_on_the_same_worker() -> None:
    pool = SandboxPool(PoolConfig(workers=4))
    keys = [f"org:chat_draft:{n}" for n in range(64)]
    assert [pool.slot_of(k) for k in keys] == [pool.slot_of(k) for k in keys]
    # Spread over every slot, and stable across processes (not Python's salted hash).
    assert {pool.slot_of(k) for k in keys} == {0, 1, 2, 3}
    assert pool.slot_of("org:chat_draft:0") == SandboxPool(PoolConfig(workers=4)).slot_of(
        "org:chat_draft:0"
    )


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["crash", "garbage"])
async def test_a_worker_that_dies_or_speaks_garbage_is_replaced_after_a_backoff(
    tmp_path: Path, op: str
) -> None:
    clock = _Clock()
    pool = _fake_pool(tmp_path, clock=clock)
    try:
        first = await _pid(pool)
        with pytest.raises(SandboxCrashedError):
            await pool.request("doc", {"op": op}, budget_seconds=10)
        assert pool.pids() == [None]
        second = await _pid(pool)
        assert second != first
        assert clock.slept == [SPAWN_BACKOFF_SECONDS[0]]
    finally:
        await pool.close()


@pytest.mark.parametrize(
    ("op", "raised", "crashed", "timed_out"),
    [
        pytest.param("crash", SandboxCrashedError, 1, 0, id="a-worker-that-dies"),
        pytest.param("garbage", SandboxCrashedError, 1, 0, id="a-worker-that-speaks-garbage"),
        pytest.param("refuse", SandboxRefusedError, 0, 0, id="a-refusal-is-not-a-crash"),
        pytest.param("hang", SandboxTimeoutError, 0, 1, id="a-hang-is-a-timeout-not-a-crash"),
    ],
)
async def test_a_crash_is_logged_once_and_a_refusal_never(
    tmp_path: Path, op: str, raised: type[Exception], crashed: int, timed_out: int
) -> None:
    """``crdt.sandbox.crashed`` is the figure the validator-crash alarm counts:
    one line per worker that died or broke its protocol on a request, none
    for a request the validator refused (the hostile update was caught, which
    is the sandbox working), and a hang is said as a timeout instead."""
    pool = _fake_pool(tmp_path)
    try:
        await _pid(pool)
        with capture_logs() as logs:
            with pytest.raises(raised):
                await pool.request("doc", {"op": op}, budget_seconds=0.5)
    finally:
        await pool.close()
    events = [e["event"] for e in logs]
    assert events.count("crdt.sandbox.crashed") == crashed
    assert events.count("crdt.sandbox.timed_out") == timed_out


async def test_a_worker_that_hangs_is_killed_at_its_budget(tmp_path: Path) -> None:
    pool = _fake_pool(tmp_path)
    try:
        hung = await _pid(pool)
        with pytest.raises(SandboxTimeoutError, match="timed out"):
            await pool.request("doc", {"op": "hang"}, budget_seconds=0.3)
        assert not alive(hung)
        assert await _pid(pool) != hung
    finally:
        await pool.close()


async def test_a_refusal_keeps_the_worker(tmp_path: Path) -> None:
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        with pytest.raises(SandboxRefusedError) as excinfo:
            await pool.request("doc", {"op": "refuse"}, budget_seconds=10)
        assert (excinfo.value.code, excinfo.value.message) == ("bad_request", "no")
        assert await _pid(pool) == pid
    finally:
        await pool.close()


async def test_a_worker_killed_between_requests_is_replaced_without_failing_the_next(
    tmp_path: Path,
) -> None:
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        kill_hard(pid)
        await asyncio.sleep(0.2)
        assert await _pid(pool) != pid
    finally:
        await pool.close()


async def test_consecutive_failures_back_off_further_and_a_success_resets_them(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    pool = _fake_pool(tmp_path, clock=clock, breaker_crashes=100)
    try:
        for _ in range(4):
            with pytest.raises(SandboxCrashedError):
                await pool.request("doc", {"op": "crash"}, budget_seconds=10)
        await _pid(pool)
        with pytest.raises(SandboxCrashedError):
            await pool.request("doc", {"op": "crash"}, budget_seconds=10)
        await _pid(pool)
        assert clock.slept == [
            *SPAWN_BACKOFF_SECONDS,
            SPAWN_BACKOFF_SECONDS[-1],
            SPAWN_BACKOFF_SECONDS[0],
        ]
    finally:
        await pool.close()


async def test_a_slot_that_keeps_crashing_opens_its_breaker_then_recovers(tmp_path: Path) -> None:
    clock = _Clock()
    pool = _fake_pool(
        tmp_path,
        clock=clock,
        breaker_crashes=2,
        breaker_window_seconds=60,
        breaker_cooldown_seconds=30,
    )
    try:
        for _ in range(3):
            with pytest.raises(SandboxCrashedError):
                await pool.request("doc", {"op": "crash"}, budget_seconds=10)
        with pytest.raises(SandboxBusyError) as excinfo:
            await pool.request("doc", {"op": "echo"}, budget_seconds=10)
        assert excinfo.value.retry_after_ms >= 30_000
        clock.now += 29
        with pytest.raises(SandboxBusyError):
            await pool.request("doc", {"op": "echo"}, budget_seconds=10)
        clock.now += 2
        assert await _pid(pool) > 0
    finally:
        await pool.close()


async def test_workers_that_hang_never_open_the_breaker(tmp_path: Path) -> None:
    """A slow host is not a broken worker: hangs are killed at their budget,
    and the slot keeps serving."""
    clock = _Clock()
    pool = _fake_pool(tmp_path, clock=clock, breaker_crashes=2, breaker_window_seconds=60)
    try:
        for _ in range(4):
            with pytest.raises(SandboxTimeoutError):
                await pool.request("doc", {"op": "hang"}, budget_seconds=0.3)
        reply = await pool.request("doc", {"op": "echo"}, budget_seconds=10)
        assert reply.header.get("ok") is True
    finally:
        await pool.close()


async def test_crashes_spread_over_more_than_the_window_do_not_open_the_breaker(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    pool = _fake_pool(tmp_path, clock=clock, breaker_crashes=2, breaker_window_seconds=60)
    try:
        for _ in range(5):
            with pytest.raises(SandboxCrashedError):
                await pool.request("doc", {"op": "crash"}, budget_seconds=10)
            clock.now += 31
        assert await _pid(pool) > 0
    finally:
        await pool.close()


async def test_requests_behind_a_busy_worker_wait_their_turn_and_are_all_served(
    tmp_path: Path,
) -> None:
    """Load makes the lane slow, never refusing: however many requests queue
    for one worker, each is answered in turn."""
    pool = _fake_pool(tmp_path)
    try:
        await _pid(pool)
        slow = asyncio.create_task(pool.request("doc", {"op": "slow"}, budget_seconds=5))
        await asyncio.sleep(0.05)
        queued = [
            asyncio.create_task(pool.request("doc", {"op": "echo", "n": n}, budget_seconds=5))
            for n in range(40)
        ]
        replies = await asyncio.gather(slow, *queued)
        assert all(reply.header.get("ok") is True for reply in replies)
    finally:
        await pool.close()


async def test_a_worker_that_cannot_start_reports_the_lane_unavailable(tmp_path: Path) -> None:
    missing = SandboxPool(PoolConfig(workers=1, command=[str(tmp_path / "no-such-python")]))
    with pytest.raises(SandboxUnavailableError):
        await missing.request("doc", {"op": "echo"}, budget_seconds=5)

    # A worker that dies before it is admitted never saw the request: the
    # caller is told to come back (busy), never that the request crashed it.
    dying = _fake_pool(tmp_path, "die-at-start", max_start_failures=2)
    with pytest.raises(SandboxBusyError, match="did not start"):
        await dying.request("doc", {"op": "echo"}, budget_seconds=5)
    with pytest.raises(SandboxUnavailableError):
        await dying.request("doc", {"op": "echo"}, budget_seconds=5)

    refusing = _fake_pool(tmp_path, "refuse-ping")
    with pytest.raises(SandboxUnavailableError):
        await refusing.request("doc", {"op": "echo"}, budget_seconds=5)


async def test_a_closed_pool_kills_its_workers_and_serves_nothing(tmp_path: Path) -> None:
    pool = _fake_pool(tmp_path)
    pid = await _pid(pool)
    await pool.close()
    assert not alive(pid)
    with pytest.raises(SandboxUnavailableError):
        await pool.request("doc", {"op": "echo"}, budget_seconds=5)


async def test_an_oversized_request_is_refused_before_it_is_sent(tmp_path: Path) -> None:
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        with pytest.raises(SandboxRefusedError) as excinfo:
            await pool.request("doc", {"op": "echo", "pad": "x" * 300_000}, budget_seconds=5)
        assert excinfo.value.code == "bad_request"
        assert await _pid(pool) == pid
    finally:
        await pool.close()


async def test_a_request_abandoned_mid_flight_never_answers_the_next_one(tmp_path: Path) -> None:
    """The caller of a slow request went away (its socket closed) while the
    worker was still working on it. The next request on that worker must get
    ITS answer, not the abandoned one still sitting in the pipe."""
    pool = _fake_pool(tmp_path)
    try:
        await _pid(pool)
        slow = asyncio.create_task(pool.request("doc", {"op": "slow", "n": 1}, budget_seconds=5))
        await asyncio.sleep(0.1)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        reply = await pool.request("doc", {"op": "echo", "n": 2}, budget_seconds=5)
        assert reply.header["echo"]["n"] == 2
    finally:
        await pool.close()


async def test_a_request_abandoned_while_its_worker_starts_leaves_the_next_one_whole(
    tmp_path: Path,
) -> None:
    """The caller went away while the worker was still answering its first
    ping. The next request gets its own answer from a worker it can trust —
    not the stray ping reply, which would read as a crash."""
    pool = _fake_pool(tmp_path, "slow-start")
    try:
        first = asyncio.create_task(pool.request("doc", {"op": "echo", "n": 1}, budget_seconds=5))
        await asyncio.sleep(0.1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        reply = await pool.request("doc", {"op": "echo", "n": 2}, budget_seconds=5)
        assert reply.header["echo"]["n"] == 2
    finally:
        await pool.close()


def _alive(pid: int) -> bool:
    return alive(pid)


async def test_a_caller_that_goes_away_mid_request_leaves_the_worker_serving(
    tmp_path: Path,
) -> None:
    """A socket closing while its request is on the wire is routine. The
    worker serving every other document on the slot must not pay for it: the
    request finishes, its answer is read and dropped, and the same worker
    answers the next request with that request's own reply."""
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        slow = asyncio.create_task(pool.request("doc", {"op": "slow", "n": 1}, budget_seconds=5))
        await asyncio.sleep(0.1)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        assert _alive(pid)
        reply = await pool.request("doc", {"op": "echo", "n": 2}, budget_seconds=5)
        assert (reply.header["pid"], reply.header["echo"]["n"]) == (pid, 2)
    finally:
        await pool.close()


async def test_a_hung_request_whose_caller_went_away_is_still_killed_at_its_budget(
    tmp_path: Path,
) -> None:
    """The caller's departure does not end the request, its budget does. The
    next request on the slot queues behind the hung one, so its answer is the
    signal that the budget kill finished: no guessed sleep stands in for it."""
    budget = 0.6
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        started = time.monotonic()
        hung = asyncio.create_task(pool.request("doc", {"op": "hang"}, budget_seconds=budget))
        await asyncio.sleep(0.1)
        hung.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hung
        assert _alive(pid)
        replacement = await _pid(pool)
        assert time.monotonic() - started >= budget
        assert not _alive(pid)
        assert replacement != pid
    finally:
        await pool.close()


async def test_the_process_the_pool_holds_is_the_worker_itself(tmp_path: Path) -> None:
    """The pid the pool waits on is the pid that answers. A venv interpreter on
    Windows is a launcher with the worker as its child; held through it, a kill
    the pool awaited would leave the worker still running."""
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        assert pool.pids() == [pid]
    finally:
        await pool.close()


_VENV = r"C:\work\.venv\Scripts\python.exe"
_BASE = r"C:\Python313\python.exe"


@pytest.mark.parametrize(
    ("command", "windows", "executable", "expected_argv", "launcher"),
    [
        pytest.param([_VENV, "-m", "w"], True, _VENV, [_BASE, "-m", "w"], _VENV, id="windows-venv"),
        pytest.param(
            [_VENV.lower().replace("\\", "/"), "w"],
            True,
            _VENV,
            [_BASE, "w"],
            _VENV,
            id="windows-venv-spelled-differently",
        ),
        pytest.param([_BASE, "w"], True, _BASE, [_BASE, "w"], None, id="windows-no-venv"),
        pytest.param(
            [r"C:\other\python.exe", "w"],
            True,
            _VENV,
            [r"C:\other\python.exe", "w"],
            None,
            id="windows-other-interpreter",
        ),
        pytest.param([_VENV, "w"], False, _VENV, [_VENV, "w"], None, id="posix"),
        pytest.param([], True, _VENV, [], None, id="empty-command"),
    ],
)
def test_a_venv_interpreter_on_windows_is_started_without_its_launcher(
    command: list[str],
    windows: bool,
    executable: str,
    expected_argv: list[str],
    launcher: str | None,
) -> None:
    argv, env = launch_spec(
        command, {"PATH": "p"}, executable=executable, base_executable=_BASE, windows=windows
    )
    assert argv == expected_argv
    assert env.get("__PYVENV_LAUNCHER__") == launcher
    assert env["PATH"] == "p"


async def test_a_request_cancelled_while_queued_never_reaches_the_worker(tmp_path: Path) -> None:
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        slow = asyncio.create_task(pool.request("doc", {"op": "slow"}, budget_seconds=5))
        await asyncio.sleep(0.1)
        queued = asyncio.create_task(pool.request("doc", {"op": "crash"}, budget_seconds=5))
        await asyncio.sleep(0.05)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        await slow
        assert await _pid(pool) == pid
    finally:
        await pool.close()


async def test_an_abandoned_request_holds_its_place_in_the_queue_until_it_ends(
    tmp_path: Path,
) -> None:
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        slow = asyncio.create_task(pool.request("doc", {"op": "slow"}, budget_seconds=5))
        await asyncio.sleep(0.1)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        # The next request waits for the abandoned one to finish on the same
        # worker (its answer is read and dropped), then is served by it.
        loop = asyncio.get_running_loop()
        started = loop.time()
        reply = await pool.request("doc", {"op": "echo"}, budget_seconds=5)
        assert reply.header.get("ok") is True
        assert loop.time() - started > 0.1, "it waited for the abandoned request"
        assert await _pid(pool) == pid
    finally:
        await pool.close()


async def test_closing_the_pool_kills_a_worker_still_serving_an_abandoned_request(
    tmp_path: Path,
) -> None:
    pool = _fake_pool(tmp_path)
    pid = await _pid(pool)
    hung = asyncio.create_task(pool.request("doc", {"op": "hang"}, budget_seconds=30))
    await asyncio.sleep(0.1)
    hung.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hung
    async with asyncio.timeout(5):
        await pool.close()
    await asyncio.sleep(0.1)
    assert not _alive(pid)


async def test_a_reply_to_some_other_request_is_never_trusted(tmp_path: Path) -> None:
    pool = _fake_pool(tmp_path)
    try:
        first = await _pid(pool)
        with pytest.raises(SandboxCrashedError, match="answered another request"):
            await pool.request("doc", {"op": "stale"}, budget_seconds=5)
        assert await _pid(pool) != first
    finally:
        await pool.close()


async def _until(check: Callable[[], bool], seconds: float = 5.0) -> None:
    """Poll ``check`` until it holds; the drain logs on its own schedule."""
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the condition never held")


async def test_a_worker_spewing_stderr_is_logged_at_a_bounded_rate(tmp_path: Path) -> None:
    """A Loro panic the core refuses still prints to stderr, and a client can
    trigger one at will; the log takes at most a fixed number of lines per
    slot per window, each cut short, then one count of what it dropped."""
    clock = _Clock()
    pool = _fake_pool(tmp_path, clock=clock)
    spewed = STDERR_LINES_PER_WINDOW * 10
    try:
        with capture_logs() as logs:

            def lines() -> list[dict[str, object]]:
                return [e for e in logs if e["event"] == "crdt.sandbox.stderr"]

            def dropped() -> list[dict[str, object]]:
                return [e for e in logs if e["event"] == "crdt.sandbox.stderr_suppressed"]

            pid = await _pid(pool)
            await pool.request(
                "doc",
                {"op": "spew", "lines": spewed, "width": STDERR_LINE_CHARS * 4},
                budget_seconds=5,
            )
            await _until(lambda: len(lines()) == STDERR_LINES_PER_WINDOW)
            await asyncio.sleep(0.3)
            assert len(lines()) == STDERR_LINES_PER_WINDOW
            assert all(len(str(e["line"])) <= STDERR_LINE_CHARS for e in lines())
            assert dropped() == []

            # A new window: the count of what was dropped, then lines again.
            clock.now += STDERR_WINDOW_SECONDS
            await pool.request("doc", {"op": "spew", "lines": 1}, budget_seconds=5)
            await _until(lambda: len(lines()) == STDERR_LINES_PER_WINDOW + 1)
            assert [e["lines"] for e in dropped()] == [spewed - STDERR_LINES_PER_WINDOW]
            assert await _pid(pool) == pid
    finally:
        await pool.close()


async def test_a_stderr_line_longer_than_the_pipe_buffer_never_stalls_the_worker(
    tmp_path: Path,
) -> None:
    pool = _fake_pool(tmp_path)
    try:
        pid = await _pid(pool)
        reply = await pool.request(
            "doc",
            {"op": "spew", "lines": 1, "width": 6 * 1024 * 1024, "newlines": False},
            budget_seconds=5,
        )
        assert reply.header["pid"] == pid
        await pool.request(
            "doc", {"op": "spew", "lines": 200, "width": 64 * 1024}, budget_seconds=5
        )
        assert await _pid(pool) == pid
    finally:
        await pool.close()


def test_a_timeout_is_not_a_crash_so_no_crash_handler_can_take_it_for_one() -> None:
    """Every caller that answers a dead worker (retry once, then poison the
    bytes) does it with ``except SandboxCrashedError``. A timeout caught
    there would be a verdict on the bytes; it must never be one."""
    assert not issubclass(SandboxTimeoutError, SandboxCrashedError)


async def test_a_worker_too_slow_to_start_is_busy_not_a_crash(tmp_path: Path) -> None:
    """Warming is start-up, not the request: a worker that does not answer
    its first ping in time costs the caller a retry, never its request."""
    pool = _fake_pool(tmp_path, "slow-start", startup_seconds=0.1)
    try:
        with pytest.raises(SandboxBusyError) as busy:
            await pool.request("doc", {"op": "echo"}, budget_seconds=5)
        assert busy.value.retry_after_ms > 0
    finally:
        await pool.close()


async def test_a_worker_killed_at_its_budget_is_replaced_before_the_next_request(
    tmp_path: Path,
) -> None:
    """The replacement starts (and warms) while the caller waits out its
    retry hint, not when it comes back."""
    pool = _fake_pool(tmp_path)
    try:
        hung = await _pid(pool)
        with pytest.raises(SandboxTimeoutError):
            await pool.request("doc", {"op": "hang"}, budget_seconds=0.3)
        async with asyncio.timeout(10):
            while pool.pids() == [None]:  # noqa: ASYNC110 - no event says a slot started; its pid does
                await asyncio.sleep(0.02)
        (replacement,) = pool.pids()
        assert replacement is not None and replacement != hung and alive(replacement)
    finally:
        await pool.close()


async def test_start_brings_up_every_slot_before_any_request(tmp_path: Path) -> None:
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE)
    pool = SandboxPool(PoolConfig(workers=3, command=[sys.executable, str(script), "normal"]))
    try:
        assert pool.pids() == [None, None, None]
        await pool.start()
        pids = pool.pids()
        assert None not in pids and len(set(pids)) == 3
        # The request is served by the worker already running on its slot.
        reply = await pool.request("doc", {"op": "echo"}, budget_seconds=5)
        assert reply.header["pid"] == pids[pool.slot_of("doc")]
    finally:
        await pool.close()
