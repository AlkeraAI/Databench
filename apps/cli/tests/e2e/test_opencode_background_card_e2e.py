"""E2E proof: a backgrounded bash job surfaces its result as a transcript card.

Drives a REAL opencode subprocess whose scripted model calls our parent-hosted
shell — advertised under the bare native name ``bash`` — with ``background=true``.
The job returns a stub immediately, runs detached, and on completion the runtime
publishes the two-card lifecycle frames.
This pins the END-TO-END contract the unit tests can't: that the real on_submit +
terminal callbacks fire and the RESULT actually lands on the bus —

* a RUNNING ``ToolCall`` keyed ``bgjob:<id>`` at submit (the START breadcrumb where
  it launched), and on completion a separate FINISH card ``bgdone:<id>`` (``ToolCall``
  + ``ToolCallUpdate``) carrying the command's real output at the transcript tail.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import FOLLOWUP_KEY, text_chunks, tool_call_chunks
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.schemas.chat import PermissionRequest, ToolCall, ToolCallUpdate

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="the parent-hosted shell is POSIX-only"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}


async def test_backgrounded_bash_result_surfaces_as_a_card(tmp_path: Any) -> None:
    # opencode's native ShellTool is dropped → the ``bash`` the model calls is OURS.
    script = {
        "*": tool_call_chunks(
            "bash",
            {"command": "echo BG-RESULT-OK", "description": "echo", "background": True},
        ),
        FOLLOWUP_KEY: text_chunks("the background job finished"),
    }

    async def _allow(_request: PermissionRequest) -> str:
        return "allow_once"

    start_calls: list[ToolCall] = []
    done_updates: list[ToolCallUpdate] = []

    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, _server):
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(_allow, default_timeout_seconds=30.0)
        )
        sub = session.subscribe()

        async def _pump() -> None:
            async for ev in sub:
                if isinstance(ev, ToolCall) and ev.tool_call_id.startswith("bgjob:"):
                    start_calls.append(ev)
                elif isinstance(ev, ToolCallUpdate) and ev.tool_call_id.startswith("bgdone:"):
                    done_updates.append(ev)

        pump = asyncio.create_task(_pump())
        try:
            await session.send_prompt("run the echo in the background", model=_MODEL)
            # Wait for the FINISH card's update carrying the result (the job is quick).
            for _ in range(250):
                if any(u.output and "BG-RESULT-OK" in str(u.output) for u in done_updates):
                    break
                await asyncio.sleep(0.02)
        finally:
            pump.cancel()
            await runtime.close_chat(sid)

    # The START breadcrumb opened RUNNING at submit...
    assert any(c.status == "running" for c in start_calls), (
        f"no running bgjob ToolCall: {[c.status for c in start_calls]!r}"
    )
    # ...and the finished RESULT surfaced on the FINISH card (the user's report).
    assert any(u.output and "BG-RESULT-OK" in str(u.output) for u in done_updates), (
        f"the background result never surfaced on a bgdone ToolCallUpdate: "
        f"{[str(u.output)[:80] for u in done_updates]!r}"
    )
