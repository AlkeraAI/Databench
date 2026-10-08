"""E2E proof: a FAILED alkera tool surfaces as a tool ERROR (status="error") through a
REAL opencode subprocess — not a green "completed" card.

The alkera loopback marks a failed tool call with MCP ``isError=True``. The MCP SDK resolves
an ``isError`` result (it is a normal result field, not a protocol error), so opencode would
otherwise treat it as a success and render a completed card with the error dumped in the
output. Our vendored ``convertMcpTool`` patch throws on ``isError`` so the AI SDK routes it to
a ``tool-error`` part → ``status:"error"`` → the translator's ``ToolCallUpdate.status`` →
the TUI/webview error card. This test pins that end-to-end mapping against real opencode.

Trigger: the model calls ``call_tool`` with an unknown inner tool, which dispatch rejects with
a flagged error before any gating — deterministic, needs no connection or approval.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import FOLLOWUP_KEY, text_chunks, tool_call_chunks
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.schemas.chat import PermissionRequest, ToolCallUpdate

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="parent-hosted alkera loopback is POSIX-only"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}


async def _allow(_request: PermissionRequest) -> str:
    return "allow_once"


async def test_errored_alkera_tool_surfaces_status_error(tmp_path: Any) -> None:
    # call_tool → an unknown inner tool → dispatch returns a flagged error → loopback isError.
    script = {
        "*": tool_call_chunks(
            "alkera_call_tool",
            {"name": "nope_does_not_exist", "args": {}},
        ),
        FOLLOWUP_KEY: text_chunks("the tool call failed"),
    }

    updates: list[ToolCallUpdate] = []

    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, _server):
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(_allow, default_timeout_seconds=30.0)
        )
        sub = session.subscribe()

        async def _pump() -> None:
            async for ev in sub:
                if isinstance(ev, ToolCallUpdate):
                    updates.append(ev)

        pump = asyncio.create_task(_pump())
        try:
            await session.send_prompt("call a tool that does not exist", model=_MODEL)
            for _ in range(500):
                if any(u.status == "error" for u in updates):
                    break
                await asyncio.sleep(0.02)
        finally:
            pump.cancel()
            await runtime.close_chat(sid)

    errored = [u for u in updates if u.status == "error"]
    assert errored, (
        "the failed alkera tool did not surface as status='error' — opencode rendered it as a "
        f"non-error card. Saw statuses: {[u.status for u in updates]!r}"
    )
    # ...and the error TEXT (the tool's own message) rides along for the card to render.
    assert any("unknown tool" in str(u.error_text) for u in errored), [
        str(u.error_text)[:120] for u in errored
    ]
