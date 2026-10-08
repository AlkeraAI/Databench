"""Scaffolding shared by the adapter-seam suites: build an adapter, send a
stamped prompt, drain the bus, project what a subscriber saw.

Every one of these existed per-file before, which is how three copies of the
drain drifted apart. Frames live next door in `opencode_transport`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alkera_cli.harness.adapter import PromptInput, SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_core.schemas.chat import SessionStatusChanged, TurnFinished

from _mocks.opencode_transport import NATIVE_SESSION, StubTransport, native_frames

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from alkera_core.schemas.chat import Event


#: The agent config a gateway-routed chat carries: a declared provider and a
#: model on it. A turn is admitted only on a declared provider, so every adapter
#: built here can run one; the model gate has its own suite.
GATEWAY_AGENT_CONFIG: dict[str, Any] = {
    "provider": {
        "mock": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://127.0.0.1:1/v1", "apiKey": "t"},
            "models": {"mock-model": {"name": "mock"}},
        }
    },
    "model": "mock/mock-model",
}


def opencode_adapter(
    tmp_path: Path, *, session_id: str = "our-sid", wired: bool = False
) -> OpencodeHttpAdapter:
    """An adapter over a real `EventBus` and no subprocess. `wired` also starts it
    against a `StubTransport`, for the suites that drive `send_prompt`/`cancel`."""
    config = SessionConfig(
        session_id=session_id,
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native={"agent_config": dict(GATEWAY_AGENT_CONFIG)},
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    if wired:
        adapter._state.http_client = StubTransport()  # type: ignore[assignment]
        adapter._state.started = True
        adapter._state.opencode_session_id = NATIVE_SESSION
    return adapter


async def send(adapter: Any, turn_id: str) -> None:
    """One prompt, stamped, through the public send. `send_prompt` is the only
    writer of the attempt carrier, so no test pokes it by hand."""
    await adapter.send_prompt(PromptInput(text=f"prompt {turn_id}", turn_id=turn_id))


async def feed(adapter: OpencodeHttpAdapter, *kinds: str) -> None:
    """Deliver native frames the way the SSE pump does."""
    for frame in native_frames(*kinds):
        await adapter._handle_native(frame)


async def drain(sub: AsyncIterator[Event] | Any, *, max_events: int = 50) -> list[Event]:
    """Everything published until the bus goes quiet (a 50ms gap), capped so a
    chatty adapter cannot hang the suite."""
    events: list[Event] = []
    while len(events) < max_events:
        try:
            async with asyncio.timeout(0.05):
                events.append(await anext(sub))
        except (TimeoutError, StopAsyncIteration):
            break
    return events


def statuses(events: list[Event], *, terminals_only: bool = False) -> list[tuple[str, str | None]]:
    """The (status, attempt) of every status a subscriber saw. `terminals_only`
    drops the `running` a send publishes, for suites that assert only endings."""
    return [
        (e.status, e.turn_id)
        for e in events
        if isinstance(e, SessionStatusChanged) and not (terminals_only and e.status == "running")
    ]


def finished(events: list[Event]) -> list[str]:
    """The attempt of every `TurnFinished`, in order."""
    return [e.turn_id for e in events if isinstance(e, TurnFinished)]


class FakeProc:
    """Stand-in for the agent subprocess: an awaitable `wait()` and nothing else."""

    def __init__(self, rc: int) -> None:
        self._rc = rc

    async def wait(self) -> int:
        return self._rc
