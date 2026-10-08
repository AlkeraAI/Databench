"""Tool-events-before-idle ordering invariant.

The adapter contract (harness README, "Event ordering guarantees") promises
tool-call events precede ``SessionStatusChanged(idle)``. opencode itself does
not guarantee that on its wire: its session-status publisher races its part
store, so ``session.status(idle)`` can arrive a beat before the tool's final
``message.part.updated`` — observed on a slow Windows CI runner as "glob never
completed" in the gateway e2e (the drain loop stopped at idle and the completed
update was still in flight). The adapter re-establishes the ordering by holding
a raced idle until the closing update lands, with a grace watchdog for the
frame-LOST case (an SSE reconnect has no replay).

No subprocess, no HTTP — native frames are fed through the same
translate → publish path ``_iter_sse`` uses, and the bus output is inspected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters import opencode_http
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_core.schemas.chat import (
    Event,
    SessionStatusChanged,
    ToolCall,
    ToolCallUpdate,
)


@pytest.fixture
def adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="idle-order-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


async def _feed(adapter: OpencodeHttpAdapter, native: dict[str, Any]) -> None:
    """Run one native frame through the exact translate → ordered-publish
    path ``_iter_sse`` uses."""
    ir = adapter._translate(native)
    if ir is None:
        return
    for ev in ir if isinstance(ir, list) else [ir]:
        await adapter._publish_ordered(ev)


async def _watchdog_finished(adapter: OpencodeHttpAdapter, *, seconds: float = 10.0) -> None:
    """Wait until the armed idle watchdog has DONE its work.

    The cases below are about the ORDER the bus carries, and an order read
    after a fixed number of grace periods is an order read off the runner: the
    watchdog that has not been scheduled yet on a loaded box turns a sequence
    assertion into a timing one. The watchdog ends by publishing (or
    releasing) and then returning, so its own completion is the event those
    sequences are about. The ceiling is a stop, not a budget.
    """
    task = adapter._state.idle_watchdog_task
    assert task is not None, "no idle watchdog was armed to wait on"
    await asyncio.wait_for(asyncio.shield(task), timeout=seconds)


async def _drain(sub: Any, *, max_events: int = 50) -> list[Event]:
    events: list[Event] = []
    while len(events) < max_events:
        try:
            async with asyncio.timeout(0.05):
                events.append(await anext(sub))
        except (TimeoutError, StopAsyncIteration):
            break
    return events


def _tool_frame(
    status: str, *, part_id: str = "prt_1", output: str | None = None
) -> dict[str, Any]:
    state: dict[str, Any] = {"status": status, "input": {"pattern": "*"}}
    if output is not None:
        state["output"] = output
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": part_id,
                "messageID": "msg_1",
                "type": "tool",
                "tool": "glob",
                "callID": "call_1",
                "state": state,
            }
        },
    }


_IDLE_FRAME = {"type": "session.idle", "properties": {}}


def _kinds(events: list[Event]) -> list[str]:
    out = []
    for e in events:
        if isinstance(e, SessionStatusChanged):
            out.append(f"status:{e.status}")
        elif isinstance(e, ToolCallUpdate):
            out.append(f"update:{e.status}")
        elif isinstance(e, ToolCall):
            out.append("toolcall")
        else:
            out.append(type(e).__name__)
    return out


@pytest.mark.asyncio
async def test_raced_idle_publishes_after_the_tool_closes(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The regression: idle arrives on the wire BEFORE the tool's completed
    update. Consumers must still see the completed update first — a drain
    loop that stops at idle (they all do) must not miss the tool closure."""
    sub = adapter._bus.subscribe()
    await _feed(adapter, _tool_frame("pending"))
    await _feed(adapter, _IDLE_FRAME)  # raced ahead of the tool closure
    await _feed(adapter, _tool_frame("completed", output="3 files"))
    events = await _drain(sub)

    kinds = _kinds(events)
    assert kinds == ["toolcall", "update:completed", "status:idle"], kinds
    completed = next(e for e in events if isinstance(e, ToolCallUpdate))
    assert completed.output == "3 files"  # the REAL closure, not a synthesized one
    assert "synthetic" not in completed.metadata


@pytest.mark.asyncio
async def test_wire_order_already_correct_passes_through_unchanged(
    adapter: OpencodeHttpAdapter,
) -> None:
    sub = adapter._bus.subscribe()
    await _feed(adapter, _tool_frame("pending"))
    await _feed(adapter, _tool_frame("completed", output="ok"))
    await _feed(adapter, _IDLE_FRAME)
    events = await _drain(sub)
    assert _kinds(events) == ["toolcall", "update:completed", "status:idle"]


@pytest.mark.asyncio
async def test_a_newer_status_supersedes_the_held_idle(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An error landing after the raced idle is the truth of the turn — the
    stale idle must be dropped, never resurrected after the error."""
    sub = adapter._bus.subscribe()
    await _feed(adapter, _tool_frame("pending"))
    await _feed(adapter, _IDLE_FRAME)
    await _feed(adapter, {"type": "session.error", "properties": {"error": "boom"}})
    events = await _drain(sub)

    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert statuses == ["error"], statuses
    assert adapter._state.held_idle is None
    assert adapter._state.idle_watchdog_task is None


@pytest.mark.asyncio
async def test_watchdog_synthesizes_closure_when_the_frame_is_lost(
    adapter: OpencodeHttpAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SSE reconnect can lose the closing frame outright (no replay). The
    hold must not wedge the session: after the grace the adapter closes the
    tool synthetically (the turn DID finish — opencode declared idle after
    feeding the tool result onward) and releases the idle. A late real frame
    for the synthetically-closed part is swallowed — it must not re-open the
    part as a spurious ToolCall after the turn already went idle."""
    monkeypatch.setattr(opencode_http, "IDLE_TOOL_CLOSE_GRACE_SECONDS", 0.05)
    sub = adapter._bus.subscribe()
    await _feed(adapter, _tool_frame("pending"))
    await _feed(adapter, _IDLE_FRAME)
    await _watchdog_finished(adapter)
    events = await _drain(sub)

    kinds = _kinds(events)
    assert kinds == ["toolcall", "update:completed", "status:idle"], kinds
    completed = next(e for e in events if isinstance(e, ToolCallUpdate))
    assert completed.metadata.get("synthetic") is True

    # The real frame limps in late: swallowed, nothing published.
    await _feed(adapter, _tool_frame("completed", output="late"))
    assert await _drain(sub) == []


@pytest.mark.asyncio
async def test_watchdog_defers_to_a_translated_closing_update_still_in_flight(
    adapter: OpencodeHttpAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate pops the tool part from ``open_parts`` a few awaits before
    the closing update actually publishes (the ``_iter_sse`` loop yields
    between the two). A watchdog expiring inside that gap sees "no open tool
    parts", has nothing to close, and must NOT release the held idle — doing
    so publishes idle ahead of the in-flight completed update, the exact
    inversion the hold exists to prevent. Observed on a loaded Windows CI
    runner (the closing frame chronically lags ~the grace period) as the
    gateway e2e's "glob never completed"."""
    monkeypatch.setattr(opencode_http, "IDLE_TOOL_CLOSE_GRACE_SECONDS", 0.05)
    sub = adapter._bus.subscribe()
    await _feed(adapter, _tool_frame("pending"))
    await _feed(adapter, _IDLE_FRAME)  # held, watchdog armed

    # A watchdog that has not been SCHEDULED yet has not deferred to anything,
    # and a case that slept past its grace without the box ever running it
    # would pass without touching the behaviour it names. The watchdog's own
    # look at what is still open is the moment it decides, so that look is
    # what the case waits for. Nothing else calls it between here and the
    # publish below.
    swept = asyncio.Event()
    looked_at_open_parts = adapter._open_tool_parts

    def _looked() -> Any:
        swept.set()
        return looked_at_open_parts()

    monkeypatch.setattr(adapter, "_open_tool_parts", _looked)

    # The closing frame arrives: translate pops the part NOW; its publish
    # only runs after the watchdog's grace has already expired.
    ir = adapter._translate(_tool_frame("completed", output="3 files"))
    assert ir is not None
    await asyncio.wait_for(swept.wait(), timeout=10)
    assert adapter._state.held_idle is not None, "the watchdog woke and deferred, as it must"
    for ev in ir if isinstance(ir, list) else [ir]:
        await adapter._publish_ordered(ev)
    events = await _drain(sub)

    kinds = _kinds(events)
    assert kinds == ["toolcall", "update:completed", "status:idle"], kinds
    completed = next(e for e in events if isinstance(e, ToolCallUpdate))
    assert completed.output == "3 files"  # the REAL closure won the race
    assert "synthetic" not in completed.metadata
    assert adapter._state.held_idle is None
    assert adapter._state.idle_watchdog_task is None


@pytest.mark.asyncio
async def test_watchdog_releases_a_held_idle_with_no_closure_in_flight(
    adapter: OpencodeHttpAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of the deferral above: once the part is gone from
    ``open_parts`` and its closing update has already reached the bus, NOTHING
    will publish again — so a still-held idle has nothing left to wait for and
    the watchdog must release it. Deferring unconditionally instead loses the
    idle for the life of the session: the turn finishes, the tool completes,
    and the consumer still sees the session as running. That is the Windows CI
    failure where a completed gateway turn drained as
    ``statuses=['running', 'running']``."""
    monkeypatch.setattr(opencode_http, "IDLE_TOOL_CLOSE_GRACE_SECONDS", 0.05)
    sub = adapter._bus.subscribe()
    await _feed(adapter, _tool_frame("pending"))
    await _feed(adapter, _IDLE_FRAME)  # held, watchdog armed
    assert adapter._state.held_idle is not None

    # The part leaves `open_parts` with no closure in flight behind it.
    adapter._translator_ctx.open_parts.pop("prt_1", None)
    assert not adapter._translator_ctx.unpublished_tool_closures

    await _watchdog_finished(adapter)
    events = await _drain(sub)

    assert _kinds(events) == ["toolcall", "status:idle"], _kinds(events)
    assert adapter._state.held_idle is None
    assert adapter._state.idle_watchdog_task is None


@pytest.mark.asyncio
async def test_first_sight_terminal_tool_part_closes_immediately(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An SSE reconnect has no replay, so a tool part's FIRST visible frame
    can already carry its terminal state. It must emit the full lifecycle
    (ToolCall + closing update with the real output) and not linger in
    ``open_parts`` — a lingering entry would hold every later idle for the
    full watchdog grace and then fabricate a synthetic closure over the
    real one."""
    sub = adapter._bus.subscribe()
    await _feed(adapter, _tool_frame("completed", output="2 files"))
    await _feed(adapter, _IDLE_FRAME)
    events = await _drain(sub)

    kinds = _kinds(events)
    assert kinds == ["toolcall", "update:completed", "status:idle"], kinds
    completed = next(e for e in events if isinstance(e, ToolCallUpdate))
    assert completed.output == "2 files"
    assert "synthetic" not in completed.metadata
    assert adapter._state.held_idle is None  # idle passed straight through
    assert not adapter._open_tool_parts()


@pytest.mark.asyncio
async def test_first_sight_error_tool_part_closes_immediately(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Same reconnect gap, error flavor: a first-sight frame already in
    ``error`` state closes with an error update, not a fabricated success."""
    frame = _tool_frame("error")
    frame["properties"]["part"]["state"]["error"] = "glob exploded"
    sub = adapter._bus.subscribe()
    await _feed(adapter, frame)
    await _feed(adapter, _IDLE_FRAME)
    events = await _drain(sub)

    updates = [e for e in events if isinstance(e, ToolCallUpdate)]
    assert len(updates) == 1
    assert updates[0].status != "completed"
    assert updates[0].error_text == "glob exploded"
    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert statuses == ["idle"], statuses
    assert not adapter._open_tool_parts()


@pytest.mark.asyncio
async def test_open_text_parts_do_not_hold_idle(adapter: OpencodeHttpAdapter) -> None:
    """Only TOOL parts gate the idle: text/reasoning parts have their own
    synthesize-close path, and holding idle for them would delay every
    ordinary turn's completion signal."""
    sub = adapter._bus.subscribe()
    await _feed(
        adapter,
        {
            "type": "message.part.updated",
            "properties": {
                "part": {"id": "txt_1", "messageID": "msg_1", "type": "text", "text": ""}
            },
        },
    )
    await _feed(adapter, _IDLE_FRAME)
    events = await _drain(sub)
    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert statuses == ["idle"], statuses
    assert adapter._state.held_idle is None
