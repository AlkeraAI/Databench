"""Real-opencode contract test for the daemon's harness events.

Proves the exact daemon-harness contract an editor client consumes: a real
bun-driven opencode subprocess (mock OpenAI-compatible LLM) streams a
finalized assistant ``TextPart`` carrying the scripted text (framed by
``session.status_changed`` running→idle), and a ``tool.call`` event when the
model invokes a tool. The VS Code extension's engine translator, for one,
turns these into its host messages.

When ``ALKERA_CAPTURE_FIXTURE`` names a directory it ALSO writes the real IR
event stream (``event.model_dump(mode="json")``) there as
``opencode-turn.json`` and ``opencode-toolcall.json``, so a client's translator
test asserts against real wire shapes, not hand-mirrored unions. The extension
keeps its copies in its own ``src/engine/__fixtures__``; regenerate them
deliberately by pointing the variable there (the default run only asserts,
keeping CI's tree clean).

Marked ``opencode_e2e`` — skipped by default ``pytest``; runs under
``make e2e`` / the required ``e2e-opencode`` CI job.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import text_chunks, tool_call_chunks
from alkera_core.schemas.chat import (
    Event,
    PartCreated,
    SessionStatusChanged,
    TextPart,
    ToolCall,
)

pytestmark = pytest.mark.opencode_e2e

#: The provider the e2e runner's injected opencode config declares, and the only
#: one the adapter will run a turn on — a turn naming any other provider is
#: refused before it is posted (`HarnessModelError`), gateway-declared or not run.
_MODEL = {"provider_id": "mock", "model_id": "mock-model"}
_SCRIPTED_REPLY = "Hello from opencode"
_CAPTURE_VARIABLE = "ALKERA_CAPTURE_FIXTURE"


def _capture_dir() -> Path | None:
    """Where to write the captured streams, when a run asks for them."""
    named = os.environ.get(_CAPTURE_VARIABLE, "")
    return Path(named) if named else None


def _write_fixture(path: Path, events: list[Event]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [e.model_dump(mode="json") for e in events]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


async def _collect_one_turn(sub: object, *, budget_seconds: float = 45.0) -> list[Event]:
    """Drain exactly one turn: wait for `running`, return on the next
    idle/error. Mirrors test_opencode_e2e._drain_turn (kept local so this
    capture test stays self-contained)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    collected: list[Event] = []
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return collected
        try:
            async with asyncio.timeout(remaining):
                ev = await anext(sub)  # type: ignore[arg-type]
        except (TimeoutError, StopAsyncIteration):
            return collected
        collected.append(ev)
        if isinstance(ev, SessionStatusChanged):
            if ev.status == "running":
                seen_running = True
            elif ev.status in ("idle", "error") and seen_running:
                return collected


@pytest.mark.asyncio
async def test_opencode_streams_finalized_assistant_text(tmp_path: Path) -> None:
    """A real opencode turn is framed by session.status_changed
    running→idle and yields a finalized assistant TextPart with the
    scripted text — the contract the extension's harness.event
    subscription relies on. (The runtime layer frames turns via status,
    not turn.* events — see test_opencode_e2e._drain_turn.)"""
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script={"*": text_chunks(_SCRIPTED_REPLY)},
    ) as (runtime, sid, _server):
        session = await runtime.open_chat(sid)
        sub = session.subscribe()
        await session.send_prompt("say hello", model=_MODEL)
        events = await _collect_one_turn(sub)
        await runtime.close_chat(sid)

    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert "running" in statuses, "turn never reached `running`"
    assert "idle" in statuses, "turn never returned to `idle` (no terminal signal)"
    finalized_text = [
        e.part.text for e in events if isinstance(e, PartCreated) and isinstance(e.part, TextPart)
    ]
    assert any(_SCRIPTED_REPLY in t for t in finalized_text), (
        f"scripted reply not in any finalized TextPart: {finalized_text!r}"
    )

    if (capture := _capture_dir()) is not None:
        _write_fixture(capture / "opencode-turn.json", events)


async def _collect_until_tool_call(sub: object, *, budget_seconds: float = 30.0) -> list[Event]:
    """Collect events up to AND including the first ToolCall, then stop. The
    mock returns the same tool_call every turn, so opencode loops forever —
    bounding at the first ToolCall gives a clean, terminating fixture."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    collected: list[Event] = []
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return collected
        try:
            async with asyncio.timeout(remaining):
                ev = await anext(sub)  # type: ignore[arg-type]
        except (TimeoutError, StopAsyncIteration):
            return collected
        collected.append(ev)
        if isinstance(ev, ToolCall):
            return collected


@pytest.mark.asyncio
async def test_opencode_emits_tool_call_event(tmp_path: Path) -> None:
    """When the model invokes a tool, opencode emits a `tool.call` IR event
    (NOT a part.created of type tool_call) carrying `tool_name`. This is the
    event translateHarnessEvent turns into a tool_call chat card; assert the
    real shape so the translator's tool branch is proven against real wire
    data."""
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script={"*": tool_call_chunks("bash", {"command": "echo hi"})},
    ) as (runtime, sid, _server):
        session = await runtime.open_chat(sid)
        sub = session.subscribe()
        await session.send_prompt("run echo hi", model=_MODEL)
        events = await _collect_until_tool_call(sub)
        await runtime.close_chat(sid)

    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    assert tool_calls, f"no tool.call event emitted: {[type(e).__name__ for e in events]!r}"
    assert tool_calls[0].tool_name == "bash"
    assert tool_calls[0].event_type == "tool.call"

    if (capture := _capture_dir()) is not None:
        _write_fixture(capture / "opencode-toolcall.json", events)
