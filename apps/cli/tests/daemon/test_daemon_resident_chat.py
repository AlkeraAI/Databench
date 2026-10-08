"""Daemon resident-session tests: keep a chat ALIVE while background jobs run.

When the UI navigates away from a chat that still has running background jobs, the
daemon must NOT tear the session down: the jobs have to keep running (and keep
notifying + persisting). These tests drive the daemon's ``harness.close_chat`` /
``harness.open_chat`` / ``harness.delete_chat`` handlers DIRECTLY against a real
``HarnessRuntime`` (FakeAdapter-backed, no subprocess) and a minimal fake server,
pinning the resident-session contract:

* close-with-jobs keeps the session RESIDENT + addressable + spawns a reaper;
* the reaper closes the session once the LAST job finishes;
* a re-attach (``open_chat``) CANCELS the reaper and keeps the session alive;
* close-without-jobs still tears down (the original behavior, unchanged);
* delete is a FORCE teardown that kills resident jobs and cancels the reaper.

The reaper poll interval is monkeypatched tiny so the idle-reap is observed in
milliseconds, not the production 2s.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.daemon import methods as _register_methods  # noqa: F401
from alkera_cli.daemon.methods import harness as harness_mod
from alkera_cli.daemon.methods.harness import (
    HarnessCloseChatRequest,
    HarnessDeleteChatRequest,
    HarnessOpenChatRequest,
    _event_forwarders,
    _resident_reapers,
    _session_to_runtime,
    harness_close_chat,
    harness_delete_chat,
    harness_open_chat,
)
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.runtime import ChatSession, HarnessRuntime
from alkera_core.project.directory import ProjectDirectory


def _factory() -> FakeAdapterFactory:
    """Auto-replying FakeAdapters (no subprocess)."""
    return FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"), available=True)


class _FakeServer:
    """The slice of ``JsonRpcServer`` the resident-session handlers touch: per-session/runtime/
    reaper/forwarder dicts are attached lazily via ``getattr``/``setattr`` by the
    helpers, so this only needs the two async wire methods. ``notify`` is exercised
    by the re-attach forward loop; ``request`` only fires on a permission prompt
    (never reached here)."""

    async def notify(self, method: str, params: Any) -> None:
        return None

    async def request(self, method: str, params: Any, *, timeout_seconds: Any = None) -> Any:
        return {}


async def _wait_for(predicate: Any, *, tries: int = 200) -> bool:
    """Poll (~4s total) until ``predicate()`` is true or the tries run out."""
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _submit_hanging_job(session: ChatSession, gate: asyncio.Event) -> str:
    """Put a real running job in the session's registry that blocks on ``gate``
    until the test releases it — the minimal way to make ``has_running_background``
    true without a subprocess."""

    async def _hang() -> str:
        await gate.wait()
        return "released"

    return session._background.submit(_hang, kind="bash", title="dev server").job_id


def _abs(path: Path) -> str:
    """Resolve to an absolute string SYNCHRONOUSLY — keeps ``Path.resolve()`` (a
    blocking fs call ruff's ASYNC240 forbids inline) out of the async test bodies."""
    return str(path.resolve())


async def _open_resident_chat(
    server: _FakeServer, tmp_path: Path
) -> tuple[HarnessRuntime, ChatSession, str, str]:
    """Open a chat through a runtime registered the way ``_runtime_for`` would cache
    it (so ``open_chat`` re-attach resolves the SAME runtime), and register the
    session in the daemon addressing map the way ``open_chat`` would. Returns the
    resolved project path so re-attach/delete don't re-resolve inside an async body."""
    abs_path = _abs(tmp_path)
    rt = HarnessRuntime(ProjectDirectory(Path(abs_path) / ".alkera"), adapter_factory=_factory())  # type: ignore[arg-type]
    server.harness_runtimes = {abs_path: rt}  # type: ignore[attr-defined]
    session = await rt.open_chat(create=True, harness_type="agent")
    _session_to_runtime(server)[session.session_id] = rt  # type: ignore[arg-type]
    return rt, session, session.session_id, abs_path


@pytest.fixture(autouse=True)
def _fast_reaper_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness_mod, "_RESIDENT_REAP_POLL_SECONDS", 0.02)


# --------------------------------------------------------------------------- #
# close_chat: resident-while-running, reaps when idle.
# --------------------------------------------------------------------------- #


async def test_close_chat_with_running_jobs_keeps_resident_then_reaps(tmp_path: Path) -> None:
    server = _FakeServer()
    rt, session, sid, _ = await _open_resident_chat(server, tmp_path)
    gate = asyncio.Event()
    _submit_hanging_job(session, gate)
    assert session.has_running_background

    # Navigate away while a job runs → session stays RESIDENT + addressable, NOT
    # torn down, and a reaper is now watching it.
    await harness_close_chat(server, HarnessCloseChatRequest(session_id=sid))  # type: ignore[arg-type]
    assert sid in _session_to_runtime(server)  # still addressable
    assert rt.open_session(sid) is session  # the live session survives
    assert sid in _resident_reapers(server)  # reaper is watching
    assert sid not in _event_forwarders(server)  # UI stream detached

    # Finish the last job → the reaper reaps the (still-detached) session.
    gate.set()
    assert await _wait_for(lambda: sid not in _session_to_runtime(server)), "session was not reaped"
    assert rt.open_session(sid) is None  # fully closed
    assert sid not in _resident_reapers(server)  # reaper cleaned itself up


async def test_close_chat_without_jobs_tears_down(tmp_path: Path) -> None:
    server = _FakeServer()
    rt, session, sid, _ = await _open_resident_chat(server, tmp_path)
    assert not session.has_running_background

    await harness_close_chat(server, HarnessCloseChatRequest(session_id=sid))  # type: ignore[arg-type]
    # No running jobs → the original full-teardown path: popped + closed, no reaper.
    assert sid not in _session_to_runtime(server)
    assert rt.open_session(sid) is None
    assert sid not in _resident_reapers(server)


async def test_concurrent_close_and_open_same_sid_no_lock_conflict(tmp_path: Path) -> None:
    """The full-teardown branch pops the addressing map then drains the session while
    still holding its on-disk ``.lock``. A re-open of the SAME chat that interleaves the
    drain must WAIT it out (the per-session op-lock) and re-open cleanly — never race the
    ``.lock`` into a ``LockHeldError``. Without serialization the open falls through to a
    full re-open (the map was already popped, so the resident fast path is skipped) and
    hits the still-held lock. This pins the op-lock that closes that window."""
    server = _FakeServer()
    rt, session, sid, abs_path = await _open_resident_chat(server, tmp_path)
    assert not session.has_running_background  # routes through the full-teardown branch

    # Make the original session's teardown observably slow, so its .lock is provably
    # STILL HELD when the concurrent open runs (it's released only at the end of close()).
    gate = asyncio.Event()
    in_teardown = asyncio.Event()
    orig_close = session.close

    async def _slow_close() -> None:
        in_teardown.set()
        await gate.wait()
        await orig_close()

    session.close = _slow_close  # type: ignore[method-assign]

    # Start the close; let it reach the slow teardown — op-lock + .lock held, map popped.
    close_task = asyncio.create_task(
        harness_close_chat(server, HarnessCloseChatRequest(session_id=sid))  # type: ignore[arg-type]
    )
    assert await _wait_for(in_teardown.is_set), "close never reached the slow teardown"
    assert sid not in _session_to_runtime(server)  # map already popped — the race window

    # Race a re-open of the SAME sid into that window.
    open_task = asyncio.create_task(
        harness_open_chat(
            server,  # type: ignore[arg-type]
            HarnessOpenChatRequest(session_id=sid, create=False, project_path=abs_path),
        )
    )
    # A correctly-serialized open is BLOCKED on the op-lock; the buggy one would have
    # already raced the .lock and finished (with LockHeldError) by now.
    await asyncio.sleep(0.05)
    assert not open_task.done(), "open should WAIT on the op-lock, not race the held .lock"

    # Release the teardown → close finishes (.lock freed) → the waiting open re-opens.
    gate.set()
    await close_task
    resp = await open_task  # must NOT raise LockHeldError / RuntimeError
    assert resp.session_id == sid
    assert sid in _session_to_runtime(server)  # re-opened + addressable
    assert rt.open_session(sid) is not None

    await harness_close_chat(server, HarnessCloseChatRequest(session_id=sid))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# open_chat re-attach: cancels the reaper, keeps the session.
# --------------------------------------------------------------------------- #


async def test_reattach_cancels_reaper_and_keeps_session(tmp_path: Path) -> None:
    server = _FakeServer()
    rt, session, sid, abs_path = await _open_resident_chat(server, tmp_path)
    gate = asyncio.Event()
    _submit_hanging_job(session, gate)

    await harness_close_chat(server, HarnessCloseChatRequest(session_id=sid))  # type: ignore[arg-type]
    assert sid in _resident_reapers(server)

    # Re-attach to the resident chat → reaper cancelled, session unchanged + writable
    # (same live object), a fresh forwarder bound for the UI.
    resp = await harness_open_chat(
        server,  # type: ignore[arg-type]
        HarnessOpenChatRequest(session_id=sid, create=False, project_path=abs_path),
    )
    assert resp.session_id == sid
    assert sid not in _resident_reapers(server)  # reaper cancelled
    assert rt.open_session(sid) is session  # same live session
    assert sid in _session_to_runtime(server)  # still resident
    assert sid in _event_forwarders(server)  # UI stream re-bound

    # The job is still running — re-attach never touched it.
    assert session.has_running_background

    # Cleanup: release the job + tear the session down.
    gate.set()
    await harness_close_chat(server, HarnessCloseChatRequest(session_id=sid))  # type: ignore[arg-type]
    assert await _wait_for(lambda: sid not in _session_to_runtime(server))


# --------------------------------------------------------------------------- #
# delete_chat: force teardown, even with resident jobs.
# --------------------------------------------------------------------------- #


async def test_delete_chat_force_tears_down_resident_jobs(tmp_path: Path) -> None:
    server = _FakeServer()
    rt, session, sid, abs_path = await _open_resident_chat(server, tmp_path)
    gate = asyncio.Event()  # never set — the job hangs until drained
    _submit_hanging_job(session, gate)

    await harness_close_chat(server, HarnessCloseChatRequest(session_id=sid))  # type: ignore[arg-type]
    assert sid in _resident_reapers(server)

    # Delete is a FORCE teardown: cancels the reaper, drains the running job, closes
    # + removes the session — it must never leave a resident chat behind.
    await harness_delete_chat(
        server,  # type: ignore[arg-type]
        HarnessDeleteChatRequest(session_id=sid, project_path=abs_path),
    )
    assert sid not in _resident_reapers(server)
    assert sid not in _session_to_runtime(server)
    assert rt.open_session(sid) is None
    assert not session.has_running_background  # the hanging job was drained (cancelled)


async def test_concurrent_delete_and_open_same_sid_serialize(tmp_path: Path) -> None:
    """delete_chat is a FORCE teardown (pop map → cancel forwarder → close → delete on
    disk). Without holding the per-session op-lock it could race a concurrent re-open
    that attaches a forwarder and returns a LIVE session to the client while delete
    tears it down + deletes it underneath them. This pins that delete serializes under
    the SAME lock as open/close — a concurrent open WAITS out the teardown."""
    server = _FakeServer()
    rt, session, sid, abs_path = await _open_resident_chat(server, tmp_path)

    # Gate the session's close so delete's teardown is observably in-flight (op-lock +
    # .lock held, map popped) when the concurrent open runs.
    gate = asyncio.Event()
    in_teardown = asyncio.Event()
    orig_close = session.close

    async def _slow_close() -> None:
        in_teardown.set()
        await gate.wait()
        await orig_close()

    session.close = _slow_close  # type: ignore[method-assign]

    delete_task = asyncio.create_task(
        harness_delete_chat(
            server,  # type: ignore[arg-type]
            HarnessDeleteChatRequest(session_id=sid, project_path=abs_path),
        )
    )
    assert await _wait_for(in_teardown.is_set), "delete never reached the slow teardown"
    assert sid not in _session_to_runtime(server)  # map popped — the race window

    open_task = asyncio.create_task(
        harness_open_chat(
            server,  # type: ignore[arg-type]
            HarnessOpenChatRequest(session_id=sid, create=False, project_path=abs_path),
        )
    )
    # A serialized open is BLOCKED on the op-lock; the unguarded one would already have
    # raced the held .lock and finished (LockHeldError) by now.
    await asyncio.sleep(0.05)
    assert not open_task.done(), "open should WAIT on the op-lock while delete tears down"

    # Release the teardown → delete finishes (chat deleted) → the waiting open runs last
    # (against a now-deleted chat) — never handed a live session mid-delete.
    gate.set()
    await delete_task
    with contextlib.suppress(Exception):
        await open_task  # opening a just-deleted chat may error — that's fine
    assert sid not in _session_to_runtime(server)  # delete won; not left resident
    assert rt.open_session(sid) is None  # the session was fully torn down, never re-attached
