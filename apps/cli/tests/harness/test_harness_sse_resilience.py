"""What it takes for the adapter to call a running agent crashed.

A chat turn may run for hours or days. Over that long a life the `/event` stream
breaks and is remade many times — a GC pause, a read error, a box under memory
pressure — while opencode keeps working and replays nothing. So the question this
suite asks of every failure is the only one that matters: is the agent still
there? A count of past reconnects is not an answer, and used to be one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from _mocks.adapter_seam import drain, opencode_adapter, send
from _mocks.opencode_transport import NATIVE_SESSION, StubTransport, native_frames
from alkera_cli.harness.adapters import opencode_http
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_core.schemas.chat import SessionStatusChanged

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeProc:
    """A child process whose liveness the test decides."""

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode


@pytest.fixture
def adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    return opencode_adapter(tmp_path, session_id="sse-sid", wired=True)


@pytest.fixture
def transport(adapter: OpencodeHttpAdapter) -> StubTransport:
    return adapter._state.http_client  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnect delays are a seam, not behavior: zero them so a suite that
    exercises many reconnects runs at loop speed."""
    monkeypatch.setattr(opencode_http, "SSE_RETRY_DELAYS_MS", (0, 0, 0))


def _scripted_stream(
    adapter: OpencodeHttpAdapter, rounds: list[str], *, stop_after: int | None = None
) -> Callable[[], Any]:
    """A stand-in for one `/event` connection, driven by a script of rounds.

    ``"frame"`` delivers a native frame and then breaks the connection the way a
    read error does; ``"dead"`` breaks without ever serving one. The scripted
    connection stops the consumer once the script is spent, so the loop — which
    is deliberately unbounded now — still terminates under test.
    """
    calls = {"n": 0}

    async def _once() -> None:
        idx = calls["n"]
        calls["n"] += 1
        if idx >= len(rounds) or (stop_after is not None and idx >= stop_after):
            adapter._state.stopping = True
            return
        adapter._state.sse_frames_this_connect = 0
        if rounds[idx] == "frame":
            adapter._state.sse_frames_this_connect += 1
            for frame in native_frames("busy", session=NATIVE_SESSION):
                await adapter._handle_native(frame)
        raise httpx.ReadError("stream broke")

    adapter._sse_calls = calls  # type: ignore[attr-defined]
    return _once


def _errors(events: list[Any]) -> list[SessionStatusChanged]:
    """The terminals that tell a reader the turn ended in a crash."""
    return [e for e in events if isinstance(e, SessionStatusChanged) and e.status == "error"]


# ---------------------------------------------------------------------------
# The regression: transport errors accumulate over a session that may last days
# ---------------------------------------------------------------------------


async def test_transport_errors_over_a_long_session_never_end_a_live_turn(
    adapter: OpencodeHttpAdapter, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six transient errors, each on a connection that carried frames first, are
    six reconnects — not a crash. The agent answered between every one of them."""
    sub = adapter.subscribe()
    await send(adapter, "T1")
    adapter._state.proc = _FakeProc(None)  # type: ignore[assignment]

    monkeypatch.setattr(
        adapter, "_sse_loop_once", _scripted_stream(adapter, ["frame"] * 6), raising=False
    )
    await adapter._consume_sse()

    assert adapter._state.crashed is False
    events = await drain(sub)
    assert _errors(events) == []
    assert {e.status for e in events if isinstance(e, SessionStatusChanged)} == {"running"}
    assert adapter._sse_calls["n"] == 7  # type: ignore[attr-defined]


async def test_a_reconnect_that_serves_frames_resets_the_backoff(
    adapter: OpencodeHttpAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delay between attempts comes back down once the stream serves again —
    a working session must not crawl to a 4-second reconnect and stay there."""
    slept: list[int] = []

    async def _record(delay_ms: int) -> None:
        slept.append(delay_ms)

    monkeypatch.setattr(opencode_http, "SSE_RETRY_DELAYS_MS", (10, 20, 40))
    monkeypatch.setattr(opencode_http, "_sse_backoff", _record)
    await send(adapter, "T1")
    adapter._state.proc = _FakeProc(None)  # type: ignore[assignment]
    monkeypatch.setattr(
        adapter,
        "_sse_loop_once",
        _scripted_stream(adapter, ["dead", "dead", "dead", "frame", "dead"]),
        raising=False,
    )

    await adapter._consume_sse()

    # Three unserved attempts climb the ladder; the served one puts the next
    # attempt back on the bottom rung.
    assert slept == [10, 20, 40, 10, 20]


async def test_an_unserved_stream_keeps_retrying_while_the_agent_answers(
    adapter: OpencodeHttpAdapter, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that never serves is still not a crash while opencode answers its
    health endpoint — the stream is broken, the agent is not."""
    sub = adapter.subscribe()
    await send(adapter, "T1")
    adapter._state.proc = _FakeProc(None)  # type: ignore[assignment]
    monkeypatch.setattr(opencode_http, "SSE_UNHEALTHY_GIVE_UP_SECONDS", 0.0001)

    monkeypatch.setattr(
        adapter, "_sse_loop_once", _scripted_stream(adapter, ["dead"] * 8), raising=False
    )
    await adapter._consume_sse()

    assert transport.paths.count("/doc") == 8
    assert adapter._state.crashed is False
    assert _errors(await drain(sub)) == []


# ---------------------------------------------------------------------------
# What DOES end it: the agent is gone
# ---------------------------------------------------------------------------


async def test_a_dead_process_ends_the_turn(
    adapter: OpencodeHttpAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child exited: the turn is over, on the very first failed connection."""
    sub = adapter.subscribe()
    await send(adapter, "T1")
    adapter._state.proc = _FakeProc(1)  # type: ignore[assignment]

    monkeypatch.setattr(
        adapter, "_sse_loop_once", _scripted_stream(adapter, ["dead"] * 4), raising=False
    )
    await adapter._consume_sse()

    assert adapter._state.crashed is True
    errors = _errors(await drain(sub))
    assert [e.turn_id for e in errors] == ["T1"]
    assert adapter._sse_calls["n"] == 1  # type: ignore[attr-defined]


async def test_an_agent_that_answers_nothing_gives_up_after_the_window(
    adapter: OpencodeHttpAdapter, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alive but serving neither the stream nor its health endpoint, past the
    window: the only remaining honest reading is that the agent is wedged."""
    sub = adapter.subscribe()
    await send(adapter, "T1")
    adapter._state.proc = _FakeProc(None)  # type: ignore[assignment]
    transport.get_status["/doc"] = None
    monkeypatch.setattr(opencode_http, "SSE_UNHEALTHY_GIVE_UP_SECONDS", 0.005)

    async def _tick(_delay_ms: int) -> None:
        await asyncio.sleep(0.005)

    monkeypatch.setattr(opencode_http, "_sse_backoff", _tick)
    monkeypatch.setattr(
        adapter, "_sse_loop_once", _scripted_stream(adapter, ["dead"] * 20), raising=False
    )
    await adapter._consume_sse()

    assert adapter._state.crashed is True
    assert [e.turn_id for e in _errors(await drain(sub))] == ["T1"]


async def test_a_refusing_health_endpoint_never_gives_up_when_unbounded(
    adapter: OpencodeHttpAdapter, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the window removed, a running agent is never declared crashed by this
    path however long it refuses — the operator asked for exactly that."""
    sub = adapter.subscribe()
    await send(adapter, "T1")
    adapter._state.proc = _FakeProc(None)  # type: ignore[assignment]
    transport.get_status["/doc"] = None
    monkeypatch.setattr(opencode_http, "SSE_UNHEALTHY_GIVE_UP_SECONDS", None)

    monkeypatch.setattr(
        adapter, "_sse_loop_once", _scripted_stream(adapter, ["dead"] * 12), raising=False
    )
    await adapter._consume_sse()

    assert adapter._state.crashed is False
    assert _errors(await drain(sub)) == []


async def test_the_crash_is_declared_once_when_both_watchers_see_the_same_death(
    adapter: OpencodeHttpAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process waiter and the event stream can reach the same dead child; the
    reader must see one terminal, not two."""
    sub = adapter.subscribe()
    await send(adapter, "T1")
    adapter._state.proc = _FakeProc(2)  # type: ignore[assignment]

    monkeypatch.setattr(
        adapter, "_sse_loop_once", _scripted_stream(adapter, ["dead"] * 2), raising=False
    )
    await adapter._consume_sse()
    await adapter._declare_crash("agent exited unexpectedly (rc=2)")

    assert len(_errors(await drain(sub))) == 1


# ---------------------------------------------------------------------------
# Read budgets: a call that runs a model turn has none
# ---------------------------------------------------------------------------


async def test_compaction_is_posted_with_no_read_budget(
    adapter: OpencodeHttpAdapter, transport: StubTransport
) -> None:
    """Summarizing a near-full context is a model turn inside the request. A
    client-side read timeout on it reads a working agent as a crashed one."""
    adapter._state.last_model = {"provider_id": "mock", "model_id": "mock-model"}

    await adapter.compact()

    timeout = transport.post_timeouts[f"/session/{NATIVE_SESSION}/summarize"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read is None
    assert timeout.connect == 5.0


async def test_control_calls_keep_a_short_read_budget(
    adapter: OpencodeHttpAdapter, transport: StubTransport
) -> None:
    """A prompt submission or an abort returns as soon as opencode takes it, so a
    stall there really is a stalled server and keeps its bound."""
    await send(adapter, "T1")
    await adapter.cancel()

    assert transport.post_timeouts[f"/session/{NATIVE_SESSION}/prompt_async"] is None
    assert transport.post_timeouts[f"/session/{NATIVE_SESSION}/abort"] is None


async def test_the_event_stream_is_opened_with_its_own_read_budget(
    adapter: OpencodeHttpAdapter, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/event` is silent between beats, so a silence past its budget is the only
    way the adapter learns a connection is dead rather than idle — and it has to
    be shorter than a control call's, which is four times longer. The connection
    is opened with the knob an operator set, not with the client's default."""
    monkeypatch.setattr(opencode_http, "SSE_READ_TIMEOUT_SECONDS", 7.5)

    await adapter._sse_loop_once()

    timeout = transport.stream_timeouts["/event"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 7.5
    assert timeout.connect == 5.0
