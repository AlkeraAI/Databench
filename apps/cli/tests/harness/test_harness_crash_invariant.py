"""The opencode adapter seam: what a subscriber sees when a turn is cancelled or
the agent dies under it.

Two invariants meet here. Every open part is synthesized closed, so the UI never
renders an in-progress message forever; and the close publishes exactly one
terminal carrying the transport attempt it settles, so the runtime ends that turn
and no other. Everything drives the public operations against a stub transport,
because the bugs live in the ordering between them.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from _mocks.adapter_seam import FakeProc, drain, feed, opencode_adapter, send, statuses
from _mocks.opencode_transport import NATIVE_SESSION, StubTransport
from alkera_cli.harness.adapter import HarnessCrashError, HarnessGoneError
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.adapters.opencode_translate import _OpenPart
from alkera_cli.harness.sandbox import SandboxLaunch, WriteStep, memory_limit_exceeded
from alkera_core.schemas.chat import (
    PartCreated,
    ReasoningPart,
    SessionStatusChanged,
    TextPart,
)


@pytest.fixture
def adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    """A started adapter pinned to a native session, talking to the stub."""
    return opencode_adapter(tmp_path, session_id="crash-sid", wired=True)


@pytest.fixture
def transport(adapter: OpencodeHttpAdapter) -> StubTransport:
    return adapter._state.http_client  # type: ignore[return-value]


ABORT_PATH = f"/session/{NATIVE_SESSION}/abort"
PROMPT_PATH = f"/session/{NATIVE_SESSION}/prompt_async"


# ---------------------------------------------------------------------------
# The abort window: the tail arrives before the request that caused it answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancel_ends_only_a_live_run_and_its_tail_never_leaks(
    adapter: OpencodeHttpAdapter, transport: StubTransport
) -> None:
    """A cancel ends the run in flight and nothing else: at idle it publishes
    nothing, and the tail opencode publishes inside the abort POST never reaches
    the UI, whichever side of the abort the next prompt lands."""
    sub = adapter.subscribe()
    await adapter.cancel()  # cold idle: nothing ever ran

    await send(adapter, "A")
    await feed(adapter, "busy")

    async def _tail() -> None:
        await feed(adapter, "abort", "idle", "session.idle", "idle", "session.idle")

    transport.on_post[ABORT_PATH] = _tail
    await adapter.cancel()

    await send(adapter, "B")
    await feed(adapter, "busy", "idle", "session.idle")

    async def _user_types_again() -> None:
        await send(adapter, "C")

    transport.on_post[ABORT_PATH] = _user_types_again
    await adapter.cancel()  # warm idle: B already ended, C lands inside the window
    await feed(adapter, "busy", "idle")

    assert statuses(await drain(sub)) == [
        ("running", "A"),
        ("aborted", "A"),
        ("running", "B"),
        ("idle", "B"),
        ("running", "C"),
        ("idle", "C"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opened", "in_post", "after", "expected"),
    [
        pytest.param(("busy", "idle"), (), ("busy", "idle"), ("A", "A"), id="no run"),
        pytest.param(("busy", "idle"), ("busy",), ("idle",), ("A", "B"), id="running"),
        pytest.param(("busy",), ("idle",), ("busy", "idle"), ("B", "B"), id="ended"),
    ],
)
async def test_a_failed_post_unwinds_to_what_the_run_actually_did(
    adapter: OpencodeHttpAdapter,
    transport: StubTransport,
    opened: tuple[str, ...],
    in_post: tuple[str, ...],
    after: tuple[str, ...],
    expected: tuple[str, str],
) -> None:
    """A failed prompt POST unwinds the attempt to whatever the run actually did
    while the request was in flight."""
    sub = adapter.subscribe()
    await send(adapter, "A")
    await feed(adapter, *opened)

    async def _meanwhile() -> None:
        await feed(adapter, *in_post)

    transport.on_post[PROMPT_PATH] = _meanwhile
    transport.raises[PROMPT_PATH] = httpx.ReadTimeout("stub timed out")
    with pytest.raises(HarnessCrashError):
        await send(adapter, "B")
    await feed(adapter, *after)

    # Whose the run's own terminal is, and whose the run after the raise is.
    ended, resumed = expected
    seen = statuses(await drain(sub))
    assert seen[0] == ("running", "A")
    assert seen[1] == ("idle", ended)
    assert seen[2:] == [("running", resumed), ("idle", resumed)]


@pytest.mark.asyncio
async def test_a_prompt_that_never_started_is_settled_by_the_crash(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The crash terminal carries the attempt whose run never opened."""
    sub = adapter.subscribe()
    await send(adapter, "A")
    await feed(adapter, "busy", "idle")
    await send(adapter, "B")

    adapter._state.stopping = False
    adapter._state.proc = FakeProc(1)  # type: ignore[assignment]
    await adapter._watch_process()

    assert statuses(await drain(sub))[-1] == ("error", "B")


@pytest.mark.asyncio
async def test_cancel_finalizes_buffered_parts_and_drops_empty_ones(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Cancel closes each buffered part as its own type, drops token-less ones,
    and a second cancel re-emits nothing."""
    ctx = adapter._translator_ctx
    for part_id, part_type, buffer in (
        ("p1", "text", ["hello ", "world"]),
        ("r1", "reasoning", ["let me ", "think"]),
        ("p2", "text", []),
        ("r2", "reasoning", []),
    ):
        ctx.open_parts[part_id] = _OpenPart(
            message_id="m1", part_id=part_id, part_type=part_type, buffer=buffer
        )

    sub = adapter.subscribe()
    await adapter.cancel()
    await adapter.cancel()
    parts = [e.part for e in await drain(sub) if isinstance(e, PartCreated)]

    assert [(p.part_id, p.text) for p in parts] == [("p1", "hello world"), ("r1", "let me think")]
    assert isinstance(parts[0], TextPart)
    assert isinstance(parts[1], ReasoningPart)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "scope_events", "at_spawn", "rc", "names_the_limit"),
    [
        pytest.param("oom 1\noom_kill 1\n", None, 0, 137, True, id="killed-for-memory"),
        pytest.param("oom 0\noom_kill 0\n", None, 0, 137, False, id="no-kill"),
        pytest.param(
            "oom_kill 1\n", None, 1, 137, False, id="a-kill-from-an-earlier-life-is-not-this-ones"
        ),
        pytest.param("oom_kill 3\n", None, 1, 137, True, id="a-new-kill-over-the-baseline"),
        # The kill lands on the scope the runtime opened under the chat's
        # slice; a slice-level file that does not count it (a kernel mounted
        # with local events) must not hide it.
        pytest.param("oom_kill 0\n", "oom_kill 1\n", 0, 137, True, id="counted-on-the-scope-below"),
        # The tree is gone before the exit is accounted for: a SIGKILL nobody
        # here sent into a bounded cgroup is the kernel's, and the limit is
        # the reason; any other exit stays the bare exit.
        pytest.param(None, None, 0, 137, True, id="cgroup-gone-sigkill-is-the-limit"),
        pytest.param(None, None, 0, -9, True, id="cgroup-gone-signalled-sigkill-is-the-limit"),
        pytest.param(None, None, 0, 1, False, id="cgroup-gone-exit-1-is-not"),
        pytest.param(None, None, 0, -15, False, id="cgroup-gone-sigterm-is-not"),
        # A readable tree that says no kill is believed over the exit status.
        pytest.param("oom_kill 0\n", "oom_kill 0\n", 0, 137, False, id="readable-zero-beats-137"),
    ],
)
async def test_a_kill_for_memory_is_read_off_the_cgroup_before_the_cleanup(
    adapter: OpencodeHttpAdapter,
    tmp_path: Path,
    events: str | None,
    scope_events: str | None,
    at_spawn: int,
    rc: int,
    names_the_limit: bool,
) -> None:
    """When the agent dies under a sandbox with a memory limit, the crash
    detail names the limit exactly when the chat cgroup's ``oom_kill`` counter
    — on the chat's own group or the scope beneath it — rose during this
    agent's life, read before the after-exit steps remove the cgroup; when the
    tree is already gone, exactly when the kernel's SIGKILL ended it; and
    stays the bare exit otherwise."""
    slice_dir = tmp_path / "chat.slice"
    slice_dir.mkdir()
    memory_events = slice_dir / "memory.events"
    if events is not None:
        memory_events.write_text(events)
    if scope_events is not None:
        scope = slice_dir / "runtime.scope"
        scope.mkdir()
        (scope / "memory.events").write_text(scope_events)
    adapter._state.sandbox = SandboxLaunch(
        mode="gvisor",
        memory_mb=4096,
        memory_events=memory_events,
        after_exit=(WriteStep(tmp_path / "cleanup-ran", "yes"),),
    )
    adapter._state.oom_kills_at_spawn = at_spawn
    adapter._state.stopping = False
    adapter._state.proc = FakeProc(rc)  # type: ignore[assignment]

    sub = adapter.subscribe()
    await adapter._watch_process()

    events_seen = await drain(sub)
    detail = next(e for e in events_seen if isinstance(e, SessionStatusChanged)).detail or ""
    assert detail.startswith(f"agent exited unexpectedly (rc={rc})")
    assert (memory_limit_exceeded(detail) == 4096) is names_the_limit
    assert (tmp_path / "cleanup-ran").exists(), "the after-exit steps still ran"
    assert adapter._state.crashed is True


@pytest.mark.asyncio
async def test_a_kill_counted_while_the_stream_broke_survives_the_cgroups_removal(
    adapter: OpencodeHttpAdapter, tmp_path: Path
) -> None:
    """The counter is read at the first sign the agent is gone and kept: a
    cgroup systemd removes between the event stream breaking and the exit
    being accounted for still names the limit, whatever the exit status."""
    slice_dir = tmp_path / "chat.slice"
    slice_dir.mkdir()
    memory_events = slice_dir / "memory.events"
    memory_events.write_text("oom_kill 1\n")
    adapter._state.sandbox = SandboxLaunch(
        mode="gvisor", memory_mb=2048, memory_events=memory_events
    )
    adapter._state.oom_kills_at_spawn = 0
    adapter._state.stopping = False

    assert await adapter._note_oom_observed() == 1
    memory_events.unlink()
    slice_dir.rmdir()
    adapter._state.proc = FakeProc(1)  # type: ignore[assignment]

    sub = adapter.subscribe()
    await adapter._watch_process()

    detail = next(e for e in await drain(sub) if isinstance(e, SessionStatusChanged)).detail or ""
    assert memory_limit_exceeded(detail) == 2048


@pytest.mark.asyncio
async def test_after_a_crash_every_call_says_the_agent_is_gone(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A caller that owns the session can tell a dead agent (start a fresh
    one) from a turn that failed on a live one: the crashed adapter raises the
    typed subclass, which is still the crash error every older caller catches."""
    adapter._state.stopping = False
    adapter._state.proc = FakeProc(137)  # type: ignore[assignment]
    await adapter._watch_process()
    with pytest.raises(HarnessGoneError) as gone:
        await send(adapter, "again")
    assert isinstance(gone.value, HarnessCrashError)


@pytest.mark.asyncio
@pytest.mark.parametrize("rc", [-15, 1, 137, -1], ids=["sigterm", "exit1", "sigkill", "neg1"])
async def test_crash_detail_carries_the_exit_code(adapter: OpencodeHttpAdapter, rc: int) -> None:
    """The synthetic crash detail says the agent exited and carries the exit code,
    whatever it is."""
    adapter._state.stopping = False
    adapter._state.proc = FakeProc(rc)  # type: ignore[assignment]

    sub = adapter.subscribe()
    await adapter._watch_process()

    events = await drain(sub)
    detail = next(e for e in events if isinstance(e, SessionStatusChanged)).detail or ""
    assert detail == f"agent exited unexpectedly (rc={rc})"
    assert adapter._state.crashed is True
