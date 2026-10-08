"""Unit tests for `OpencodeHttpAdapter.clear()`.

`/clear` resets the conversation context by minting a brand-new, empty
opencode session and re-pinning to it (opencode has no clean "clear
context, keep files" route — `revert` rolls back the working tree). These
tests drive `clear()` against an in-process `httpx.MockTransport` — no real
subprocess — and assert the session swap, the synthesized
`ConversationCleared` event, the translator reset, and the failure
invariant (a creation failure leaves the existing session untouched).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from alkera_cli.harness.adapter import HarnessStartError, SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.adapters.opencode_translate import _OpenPart
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_core.schemas.chat import ConversationCleared, Event


@pytest.fixture
def adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="clear-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


async def _drain(sub, *, max_events: int = 50) -> list[Event]:
    events: list[Event] = []
    while len(events) < max_events:
        try:
            async with asyncio.timeout(0.05):
                events.append(await anext(sub))
        except (TimeoutError, StopAsyncIteration):
            break
    return events


def _wire_client(
    adapter: OpencodeHttpAdapter,
    handler,
) -> list[tuple[str, str]]:
    """Attach a MockTransport-backed client + mark the adapter ready.
    Returns a list that records (method, path) of every request made."""
    calls: list[tuple[str, str]] = []

    def _record(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return handler(request)

    transport = httpx.MockTransport(_record)
    adapter._state.http_client = httpx.AsyncClient(base_url="http://oc.test", transport=transport)
    adapter._state.started = True
    return calls


@pytest.mark.asyncio
async def test_clear_mints_new_session_swaps_pin_and_emits_event(
    adapter: OpencodeHttpAdapter,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "new-sid"})
        return httpx.Response(200, json={})

    calls = _wire_client(adapter, _handler)
    adapter._state.opencode_session_id = "old-sid"
    # Pretend two messages streamed before the clear.
    adapter._translator_ctx.message_order.extend(["u1", "a1"])
    adapter._translator_ctx.open_parts["p1"] = _OpenPart(
        message_id="a1", part_id="p1", part_type="text", buffer=["hi"]
    )

    sub = adapter.subscribe()
    await adapter.clear()
    events = await _drain(sub)

    # Pinned to the fresh session, and native_state persists THAT id so
    # resume re-attaches to the cleared session.
    assert adapter._state.opencode_session_id == "new-sid"
    assert adapter.native_state() == {"agent_session_id": "new-sid"}

    # Exactly one ConversationCleared, carrying the elided message ids.
    cleared = [e for e in events if isinstance(e, ConversationCleared)]
    assert len(cleared) == 1
    assert cleared[0].session_id == "clear-sid"  # our chat id, not opencode's
    assert cleared[0].cleared_message_ids == ["u1", "a1"]

    # Translator state forgotten — no stale ids bleed past the boundary.
    assert adapter._translator_ctx.message_order == []
    assert adapter._translator_ctx.open_parts == {}

    # New session created; the OLD session was aborted (not the new one).
    assert ("POST", "/session") in calls
    assert ("POST", "/session/old-sid/abort") in calls


@pytest.mark.asyncio
async def test_clear_failure_leaves_existing_session_untouched(
    adapter: OpencodeHttpAdapter,
) -> None:
    """If minting the fresh session fails, `clear()` raises and the adapter
    keeps pointing at the existing session — no half-cleared state, no
    spurious ConversationCleared."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={})

    _wire_client(adapter, _handler)
    adapter._state.opencode_session_id = "old-sid"
    adapter._translator_ctx.message_order.extend(["u1"])

    sub = adapter.subscribe()
    with pytest.raises(HarnessStartError):
        await adapter.clear()
    events = await _drain(sub)

    assert adapter._state.opencode_session_id == "old-sid"
    assert adapter._translator_ctx.message_order == ["u1"]
    assert not [e for e in events if isinstance(e, ConversationCleared)]
