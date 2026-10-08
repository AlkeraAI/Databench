"""The Claude adapter seam: what a subscriber sees when a turn is cancelled, the
conversation is cleared, or the message pump dies.

Mirrors ``test_harness_crash_invariant.py`` (opencode): every open part is
synthesized closed as its own type, and the turn ends with a ``TurnFinished`` per
query closed plus one terminal ``SessionStatusChanged``.

Which query that terminal belongs to is the point of the FIFO. The CLI acks an
interrupt BEFORE delivering the interrupted query's result, so a cancel closes
the oldest query still open; a crash or `/clear` closes every one queued behind
it; a query the CLI never received is not in the FIFO at all. Everything drives
the public operations against a stub client, so a reimplementation that carries
identity differently but publishes the same events still passes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _mocks.adapter_seam import drain, finished, send, statuses
from alkera_cli.harness.adapter import (
    HarnessCrashError,
    HarnessStartError,
    SessionConfig,
)
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.adapters.claude_translate import _OpenPart
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_core.schemas.chat import (
    ConversationCleared,
    PartCreated,
    ReasoningPart,
    SessionStatusChanged,
    TurnFinished,
)
from claude_agent_sdk import CLIConnectionError, ResultMessage


class _StubClient:
    """Stands in for the SDK client. `receive_messages` is what the pump iterates:
    a test hands it messages to replay, or an error to drive the crash path."""

    def __init__(
        self, pump_error: Exception | None = None, replay: list[Any] | None = None
    ) -> None:
        self.interrupts = 0
        self.queries: list[str] = []
        self._pump_error = pump_error
        self._replay = replay or []

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def set_model(self, _model: str) -> None: ...

    async def disconnect(self) -> None: ...

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def receive_messages(self) -> Any:
        for message in self._replay:
            yield message
        if self._pump_error is not None or not self._replay:
            raise self._pump_error or RuntimeError("stub pump ended")


def _result() -> ResultMessage:
    """The one message the CLI sends per query, here a clean success."""
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="cc-sid",
    )


class _QuietClient(_StubClient):
    """What a `/clear` spawns: a fresh CLI that is simply waiting, so its pump
    neither ends nor crashes while the test looks at what the clear published."""

    async def receive_messages(self) -> Any:
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable, makes this an async generator


class _DyingClient(_StubClient):
    """The client a `/clear` is replacing: its pump blocks until the test lets the
    old subprocess go, so the death lands inside the clear's spawn window."""

    def __init__(self) -> None:
        super().__init__()
        self.die = asyncio.Event()

    async def receive_messages(self) -> Any:
        await self.die.wait()
        raise RuntimeError("the old subprocess went away")
        yield  # pragma: no cover - makes this an async generator


@pytest.fixture
def adapter(tmp_path: Path) -> ClaudeAgentAdapter:
    """A started adapter whose `/clear` spawns a stub instead of a real CLI, so
    the public operations run with nothing behind them."""
    config = SessionConfig(session_id="our-sid", project_dir=tmp_path, chat_dir=tmp_path / "chat")
    binary = ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path")
    built = ClaudeAgentAdapter(config, binary=binary, event_bus=EventBus())
    built._state.started = True
    built._state.client = _StubClient()  # type: ignore[assignment]

    async def _no_spawn(*, resume: bool) -> Any:
        built.spawning.set()
        await built.spawn_release.wait()
        return _QuietClient()

    built.spawning = asyncio.Event()  # type: ignore[attr-defined]
    built.spawn_release = asyncio.Event()  # type: ignore[attr-defined]
    built.spawn_release.set()  # type: ignore[attr-defined]
    built._spawn_client = _no_spawn  # type: ignore[method-assign]
    return built


# ---------------------------------------------------------------------------
# Cancel, clear, crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancel_closes_the_oldest_open_query_and_none_at_idle(
    adapter: ClaudeAgentAdapter,
) -> None:
    """A cancel at idle publishes nothing, and each later cancel closes the oldest
    query still open, never the head an earlier cancel already closed."""
    sub = adapter.subscribe()
    await adapter.cancel()  # idle: no query open, no honest stamp
    await send(adapter, "A")
    await adapter.cancel()
    await send(adapter, "B")
    await adapter.cancel()
    events = await drain(sub)

    assert finished(events) == ["A", "B"]
    assert statuses(events, terminals_only=True) == [("aborted", "A"), ("aborted", "B")]


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["clear", "pump crash"])
async def test_the_client_going_away_closes_every_queued_query(
    adapter: ClaudeAgentAdapter, by: str
) -> None:
    """The client going away finishes every queued query with the reason, and the
    one terminal carries the newest attempt."""
    crash = RuntimeError("pipe died")
    if by == "pump crash":
        adapter._state.client = _StubClient(pump_error=crash)  # type: ignore[assignment]
    sub = adapter.subscribe()
    await send(adapter, "A")
    await send(adapter, "B")

    if by == "clear":
        await adapter.clear()
    else:
        await adapter._pump()
    events = await drain(sub)

    reason = "error" if by == "pump crash" else "cancelled"
    detail = str(crash) if by == "pump crash" else None
    finished = [e for e in events if isinstance(e, TurnFinished)]
    assert [(e.turn_id, e.stop_reason, e.error_detail) for e in finished] == [
        ("A", reason, detail),
        ("B", reason, detail),
    ]
    terminal = statuses(events, terminals_only=True)
    assert terminal == [("error" if by == "pump crash" else "aborted", "B")]
    assert any(isinstance(e, ConversationCleared) for e in events) is (by == "clear")


@pytest.mark.asyncio
async def test_a_clear_whose_reconnect_fails_changes_nothing(
    adapter: ClaudeAgentAdapter,
) -> None:
    """A clear whose replacement spawn fails raises to the caller and leaves the
    conversation exactly as it was."""
    original = adapter._state.client

    async def _no_cli(*, resume: bool) -> Any:
        raise HarnessStartError("no CLI on this box")

    adapter._spawn_client = _no_cli  # type: ignore[method-assign]
    sub = adapter.subscribe()
    await send(adapter, "A")

    with pytest.raises(HarnessStartError):
        await adapter.clear()

    settled = await drain(sub)
    assert not any(isinstance(e, ConversationCleared) for e in settled)
    assert finished(settled) == []
    assert statuses(settled, terminals_only=True) == []

    # A is still the turn in flight, which the cancel that follows proves.
    await send(adapter, "B")
    await adapter.cancel()
    events = await drain(sub)

    assert finished(events) == ["A"]
    assert statuses(events, terminals_only=True) == [("aborted", "A")]
    assert original.queries == ["prompt A", "prompt B"]  # the old client, still serving


@pytest.mark.asyncio
async def test_the_old_pump_dying_inside_a_clear_does_not_poison_the_new_client(
    adapter: ClaudeAgentAdapter,
) -> None:
    """The old pump dying inside a clear's spawn window does not flag the adapter
    crashed."""
    dying = _DyingClient()
    adapter._state.client = dying  # type: ignore[assignment]
    pump = asyncio.create_task(adapter._pump())
    await asyncio.sleep(0)  # the pump is now reading from `dying`
    sub = adapter.subscribe()

    adapter.spawn_release.clear()
    clearing = asyncio.create_task(adapter.clear())
    await asyncio.wait_for(adapter.spawning.wait(), timeout=3.0)
    dying.die.set()  # the old subprocess goes away mid-spawn
    await pump
    adapter.spawn_release.set()
    await asyncio.wait_for(clearing, timeout=3.0)

    assert any(isinstance(e, ConversationCleared) for e in await drain(sub))
    assert not adapter._state.crashed

    await send(adapter, "after")  # the chat still works
    await adapter.cancel()
    assert finished(await drain(sub)) == ["after"]


@pytest.mark.asyncio
async def test_a_cancel_that_beats_a_failing_write_still_lets_the_next_query_end(
    adapter: ClaudeAgentAdapter,
) -> None:
    """A cancel that beats a failing write leaves no stale record for the next
    query's result to pop."""
    gate = asyncio.Event()

    class _BlockingClient(_StubClient):
        def __init__(self) -> None:
            super().__init__(replay=[_result()])
            self.first = True

        async def query(self, _text: str) -> None:
            if self.first:
                self.first = False
                await gate.wait()
                raise CLIConnectionError("the pipe broke while we waited")

    adapter._state.client = _BlockingClient()  # type: ignore[assignment]
    sub = adapter.subscribe()
    sending = asyncio.create_task(send(adapter, "A"))
    await asyncio.sleep(0.05)
    await adapter.cancel()
    gate.set()
    with pytest.raises(HarnessCrashError):
        await sending

    await send(adapter, "B")
    await adapter._pump()
    events = await drain(sub)

    assert finished(events) == ["A", "B"]
    assert statuses(events, terminals_only=True) == [("aborted", "A"), ("idle", "B")]


@pytest.mark.asyncio
async def test_a_crash_at_idle_still_reports_itself(adapter: ClaudeAgentAdapter) -> None:
    """A crash with nothing open still publishes its unstamped error terminal."""
    adapter._state.client = _StubClient(pump_error=RuntimeError("pipe died"))  # type: ignore[assignment]
    sub = adapter.subscribe()
    await adapter._pump()
    events = await drain(sub)

    assert finished(events) == []
    assert statuses(events, terminals_only=True) == [("error", None)]
    assert next(e for e in events if isinstance(e, SessionStatusChanged)).detail == "pipe died"


@pytest.mark.asyncio
async def test_cancel_closes_every_open_part_as_its_own_type(
    adapter: ClaudeAgentAdapter,
) -> None:
    """Cancel closes every open part as its own type, an announced-but-empty block
    included, and a reasoning part keeps its signature."""
    ctx = adapter._translator_ctx
    ctx.open_parts["p1"] = _OpenPart(
        message_id="m1", part_id="p1", part_type="text", buffer=["hel", "lo"]
    )
    ctx.open_parts["p2"] = _OpenPart(message_id="m1", part_id="p2", part_type="text")
    ctx.open_parts["r1"] = _OpenPart(
        message_id="m1", part_id="r1", part_type="reasoning", buffer=["think"], signature="sg"
    )

    sub = adapter.subscribe()
    await adapter.cancel()
    await adapter.cancel()
    parts = {e.part.part_id: e.part for e in await drain(sub) if isinstance(e, PartCreated)}

    assert {k: v.text for k, v in parts.items()} == {"p1": "hello", "p2": "", "r1": "think"}
    assert isinstance(parts["r1"], ReasoningPart)
    assert parts["r1"].signature == "sg"
