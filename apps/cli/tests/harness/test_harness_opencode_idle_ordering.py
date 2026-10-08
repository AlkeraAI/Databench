"""Regression suite for the opencode adapter's idle-hold ordering.

opencode publishes `message.part.updated` from a fiber forked after the DB
commit (session/sync/index.ts `process`) but `session.status` inline in the
session's own fiber (session/status.ts `set`), so under machine load a tool
part's terminal update can reach the SSE stream AFTER the turn's idle
status. Consumers treat idle as end-of-turn, so an idle delivered while a
tool call is still in flight makes them miss the completion (the flaky e2e
symptom was "glob never completed").

These tests feed parsed SSE payloads through `_handle_native` and observe
only the adapter's EventBus, the surface every consumer subscribes to. The
pinned contract: a terminal ToolCallUpdate precedes the turn's idle, a
held idle flushes once its tool call actually closes, a part that never
closes is closed synthetically and the idle released at the grace bound,
and any OTHER status
arriving while a tool call is still open DROPS the held idle instead of
flushing it — the real idle re-arrives at true turn end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _mocks.adapter_seam import opencode_adapter
from _mocks.opencode_transport import StubTransport
from alkera_cli.harness.adapter import PromptInput
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_core.schemas.chat import (
    Event,
    SessionStatusChanged,
    ToolCall,
    ToolCallUpdate,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture
def adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    """Bare adapter, never started. `_handle_native` only touches the translator
    context and the bus, so no subprocess is needed."""
    return opencode_adapter(tmp_path)


def _event(type_: str, **props: Any) -> dict[str, Any]:
    """Wrap properties in opencode's `{type, properties}` envelope."""
    return {"type": type_, "properties": props}


def _tool_part(
    state: dict[str, Any], *, part_id: str = "prt_1", call_id: str = "call_1"
) -> dict[str, Any]:
    """ToolPart update (message-v2.ts ToolPart; `state` is the
    status-discriminated ToolState union). `part_id`/`call_id` let a test
    open a SECOND, distinct tool call alongside the default one."""
    return _event(
        "message.part.updated",
        part={
            "id": part_id,
            "messageID": "msg_1",
            "sessionID": "oc-sid",
            "type": "tool",
            "tool": "glob",
            "callID": call_id,
            "state": state,
        },
    )


def _pending() -> dict[str, Any]:
    return {"status": "pending"}


def _running() -> dict[str, Any]:
    return {"status": "running", "input": {"pattern": "*"}}


def _completed() -> dict[str, Any]:
    return {
        "status": "completed",
        "input": {"pattern": "*"},
        "output": "12 files",
        "time": {"start": 1, "end": 2},
    }


def _errored() -> dict[str, Any]:
    return {"status": "error", "input": {"pattern": "*"}, "error": "glob failed"}


def _session_status(type_: str) -> dict[str, Any]:
    return _event("session.status", sessionID="oc-sid", status={"type": type_})


def _session_idle() -> dict[str, Any]:
    """Deprecated upstream but still emitted at every turn end alongside
    `session.status {type: idle}`."""
    return _event("session.idle", sessionID="oc-sid")


def _session_error() -> dict[str, Any]:
    """session/session.ts: `{sessionID?, error?: AssistantErrorSchema}` —
    a REAL wire error, distinct from the synthesized close on cancel."""
    return _event(
        "session.error",
        sessionID="oc-sid",
        error={"name": "ProviderError", "data": {"message": "boom"}},
    )


async def _drain(adapter: OpencodeHttpAdapter, sub: AsyncIterator[Event]) -> list[Event]:
    """Close the bus and collect everything the subscriber saw. The close
    sentinel ends iteration cleanly, so this never hangs."""
    await adapter.event_bus.close()
    return [ev async for ev in sub]


@pytest.mark.parametrize(
    ("terminal_state", "terminal_status"),
    [
        pytest.param(_completed(), "completed", id="completed"),
        pytest.param(_errored(), "error", id="error"),
    ],
)
@pytest.mark.parametrize(
    "feed",
    [
        pytest.param(
            ("pending", "running", "status_idle", "legacy_idle", "terminal"),
            id="idle_races_ahead_of_terminal",
        ),
        pytest.param(
            ("pending", "running", "terminal", "status_idle", "legacy_idle"),
            id="healthy_order",
        ),
    ],
)
async def test_terminal_tool_update_precedes_idle(
    adapter: OpencodeHttpAdapter,
    feed: tuple[str, ...],
    terminal_state: dict[str, Any],
    terminal_status: str,
) -> None:
    """Whatever order the SSE stream delivers, the bus shows one canonical
    sequence: the tool call closes before the turn goes idle, and both
    idle events survive the hold (none lost, none duplicated)."""
    payloads = {
        "pending": _tool_part(_pending()),
        "running": _tool_part(_running()),
        "terminal": _tool_part(terminal_state),
        "status_idle": _session_status("idle"),
        "legacy_idle": _session_idle(),
    }
    sub = adapter.subscribe()
    for key in feed:
        await adapter._handle_native(payloads[key])

    events = await _drain(adapter, sub)

    assert [type(ev) for ev in events] == [
        ToolCall,
        ToolCallUpdate,
        ToolCallUpdate,
        SessionStatusChanged,
        SessionStatusChanged,
    ]
    opened, running, terminal, idle_a, idle_b = events
    assert opened.status == "pending"
    assert running.status == "running"
    assert terminal.status == terminal_status
    assert terminal.tool_call_id == "prt_1"
    assert idle_a.status == "idle"
    assert idle_b.status == "idle"


async def test_idle_held_at_production_grace_default_still_waits_for_terminal_update(
    adapter: OpencodeHttpAdapter,
) -> None:
    """No override here — this runs at whatever grace the adapter ships
    with. `test_terminal_tool_update_precedes_idle` never yields to the
    loop between the held idle and the terminal update, so it can't tell
    a real grace apart from a zeroed-out one (the release task never gets
    scheduled either way). The `sleep(0)` here forces that loop turnover
    before the tool call closes, so a wrongly-zeroed default would race
    the idle out ahead of the terminal update it's still waiting on."""
    sub = adapter.subscribe()
    await adapter._handle_native(_tool_part(_pending()))
    await adapter._handle_native(_session_status("idle"))
    await asyncio.sleep(0)  # let the loop turn over before the tool call closes
    await adapter._handle_native(_tool_part(_completed()))

    events = await _drain(adapter, sub)

    assert [type(ev) for ev in events] == [ToolCall, ToolCallUpdate, SessionStatusChanged]
    _opened, terminal, idle = events
    assert terminal.status == "completed"
    assert idle.status == "idle"


async def test_idle_held_by_never_closing_part_releases_at_grace_bound(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A tool part that never reaches a terminal state must not hold the
    idle forever: the grace watchdog closes the part synthetically (the turn
    DID finish; only the closing frame was lost) and releases every held
    idle. The release can come no earlier than the grace, which proves the
    idle was actually held."""
    adapter._idle_hold_grace = 0.2
    sub = adapter.subscribe()
    await adapter._handle_native(_tool_part(_pending()))
    held_at = asyncio.get_running_loop().time()
    await adapter._handle_native(_session_status("idle"))
    await adapter._handle_native(_session_idle())

    # Bounded well under the shipped 5s grace: an implementation that ignores
    # the override and falls back to the default would time out HERE instead
    # of passing slowly.
    async with asyncio.timeout(1):
        opened = await anext(sub)
        closed = await anext(sub)
        released_a = await anext(sub)
        released_b = await anext(sub)
    waited = asyncio.get_running_loop().time() - held_at

    assert isinstance(opened, ToolCall)
    assert isinstance(closed, ToolCallUpdate)
    assert closed.status == "completed"
    assert closed.metadata.get("synthetic") is True
    assert isinstance(released_a, SessionStatusChanged)
    assert released_a.status == "idle"
    assert isinstance(released_b, SessionStatusChanged)
    assert released_b.status == "idle"
    assert 0.2 <= waited < 1.0
    assert await _drain(adapter, sub) == []


async def test_terminal_close_flushes_held_idle_before_a_later_status(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Once the tool call actually closes, the hold ends right there — a
    status fed afterward can't race ahead of the already-flushed idle.
    Contrasts the drop case below: nothing is open anymore by the time the
    next status arrives, so the wire-order delay really was pure
    ordering, not a signal to withhold."""
    adapter._idle_hold_grace = 60.0
    sub = adapter.subscribe()
    await adapter._handle_native(_tool_part(_pending()))
    await adapter._handle_native(_session_status("idle"))
    await adapter._handle_native(_tool_part(_completed()))
    await adapter._handle_native(_session_status("busy"))  # next turn begins

    async with asyncio.timeout(1):
        opened = await anext(sub)
        terminal = await anext(sub)
        idle = await anext(sub)
        busy = await anext(sub)

    assert isinstance(opened, ToolCall)
    assert isinstance(terminal, ToolCallUpdate)
    assert terminal.status == "completed"
    assert isinstance(idle, SessionStatusChanged)
    assert idle.status == "idle"
    assert isinstance(busy, SessionStatusChanged)
    assert busy.status == "running"


async def test_idle_stays_held_until_the_last_of_two_open_tool_calls_closes(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The hold counts every open tool call, not just the most recent one:
    with two parts in flight, idle waits for whichever closes LAST."""
    adapter._idle_hold_grace = 60.0
    sub = adapter.subscribe()
    await adapter._handle_native(_tool_part(_pending(), part_id="prt_1", call_id="call_1"))
    await adapter._handle_native(_tool_part(_pending(), part_id="prt_2", call_id="call_2"))
    await adapter._handle_native(_session_status("idle"))
    await adapter._handle_native(_tool_part(_completed(), part_id="prt_1", call_id="call_1"))

    async with asyncio.timeout(1):
        opened_1 = await anext(sub)
        opened_2 = await anext(sub)
        closed_1 = await anext(sub)

    assert isinstance(opened_1, ToolCall) and opened_1.tool_call_id == "prt_1"
    assert isinstance(opened_2, ToolCall) and opened_2.tool_call_id == "prt_2"
    assert isinstance(closed_1, ToolCallUpdate) and closed_1.tool_call_id == "prt_1"

    # The second part is still open — idle must still be held.
    await adapter._handle_native(_tool_part(_completed(), part_id="prt_2", call_id="call_2"))

    async with asyncio.timeout(1):
        closed_2 = await anext(sub)
        idle = await anext(sub)

    assert isinstance(closed_2, ToolCallUpdate) and closed_2.tool_call_id == "prt_2"
    assert isinstance(idle, SessionStatusChanged) and idle.status == "idle"
    assert await _drain(adapter, sub) == []


async def test_non_idle_status_while_tool_open_drops_the_held_idle(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A non-idle status ends the hold window immediately, but NOT by
    flushing the held idle first: consumers must never see idle before
    the tool results the turn is still producing. Busy publishes now; the
    held idle is dropped, never published. The turn's REAL idle re-arrives
    once the tool call actually closes, still preceded by its terminal
    update. The grace is set far beyond the collection timeout, so passing
    here proves the drop never waits on the timer."""
    adapter._idle_hold_grace = 60.0
    sub = adapter.subscribe()
    await adapter._handle_native(_tool_part(_pending()))
    await adapter._handle_native(_session_status("idle"))
    await adapter._handle_native(_session_status("busy"))

    async with asyncio.timeout(1):
        opened = await anext(sub)
        busy = await anext(sub)

    assert isinstance(opened, ToolCall)
    assert isinstance(busy, SessionStatusChanged)
    assert busy.status == "running"

    await adapter._handle_native(_tool_part(_completed()))
    await adapter._handle_native(_session_status("idle"))
    await adapter._handle_native(_session_idle())

    async with asyncio.timeout(1):
        terminal = await anext(sub)
        idle_a = await anext(sub)
        idle_b = await anext(sub)

    assert isinstance(terminal, ToolCallUpdate)
    assert terminal.status == "completed"
    assert terminal.tool_call_id == "prt_1"
    assert isinstance(idle_a, SessionStatusChanged) and idle_a.status == "idle"
    assert isinstance(idle_b, SessionStatusChanged) and idle_b.status == "idle"
    # The idle dropped before `busy` never resurfaces later.
    assert await _drain(adapter, sub) == []


async def test_native_error_status_while_tool_open_drops_the_held_idle(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A real wire `session.error` mid-tool-call is a terminal status too,
    not just the synthesized-abort case below: it publishes now and the
    held idle is dropped, never published — even after the grace elapses."""
    adapter._idle_hold_grace = 0.05
    sub = adapter.subscribe()
    await adapter._handle_native(_tool_part(_pending()))
    await adapter._handle_native(_session_status("idle"))
    await adapter._handle_native(_session_error())
    # Wait well past the grace: a timer the error failed to cancel would
    # publish the superseded idle inside this window.
    await asyncio.sleep(0.2)

    events = await _drain(adapter, sub)

    assert isinstance(events[0], ToolCall)
    statuses = [ev.status for ev in events if isinstance(ev, SessionStatusChanged)]
    assert statuses == ["error"]


async def test_a_cancel_drops_the_held_idle(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A cancel discards the held idle for good but speaks for its attempt, so the
    transcript resolves instead of sitting on `running` forever."""
    adapter._idle_hold_grace = 0.05
    adapter._state.started = True
    adapter._state.opencode_session_id = "oc-sid"
    adapter._state.http_client = StubTransport()  # type: ignore[assignment]
    sub = adapter.subscribe()
    await adapter.send_prompt(PromptInput(text="go", turn_id="A"))
    await adapter._handle_native(_session_status("busy"))
    await adapter._handle_native(_tool_part(_pending()))
    await adapter._handle_native(_session_status("idle"))
    await adapter.cancel()
    # Wait well past the grace: a timer the abort failed to cancel would
    # publish the superseded idle inside this window.
    await asyncio.sleep(0.2)

    events = await _drain(adapter, sub)

    statuses = [(ev.status, ev.turn_id) for ev in events if isinstance(ev, SessionStatusChanged)]
    assert statuses == [("running", "A"), ("aborted", "A")]


async def test_stop_while_idle_held_cancels_the_grace_timer_cleanly(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Stopping mid-hold must cancel the grace timer, not leave it running
    against a torn-down adapter. A short grace means a leaked timer fires
    inside this test's window instead of surviving past it undetected."""
    adapter._idle_hold_grace = 0.05
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    await adapter._handle_native(_tool_part(_pending()))
    await adapter._handle_native(_session_status("idle"))

    await adapter.stop()
    await asyncio.sleep(0.2)  # past the grace: a leaked timer would fire in here

    assert contexts == []
