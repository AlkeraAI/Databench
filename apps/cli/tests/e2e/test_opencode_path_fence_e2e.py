"""E2E proof: a fenced read is refused through a REAL opencode subprocess, and the
turn keeps going.

The unit tests pin the decision (``ChatSession._decide_permission`` consults the
session's ``PathFence`` ahead of the read fast path). What only a real subprocess
can prove is the other half of the contract: a reject that carries a REASON is a
recoverable tool error on opencode (a ``CorrectedError``), so the model is told why
and gets another turn — where a reason-less reject would end the turn under the
user. That difference is invisible to a FakeAdapter.

Trigger: the model calls opencode's own ``read`` tool on a file OUTSIDE the project
root. Deterministic — no connection, no approval, no network beyond the mock
provider.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import (
    FOLLOWUP_KEY,
    MockOpenAIServer,
    text_chunks,
    tool_call_chunks,
)
from alkera_cli.cloud import fence
from alkera_cli.cloud.refusal import REFUSAL_COPY
from alkera_cli.harness.adapter import PathFence
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.schemas.chat import (
    Event,
    PartCreated,
    PermissionRequest,
    SessionStatusChanged,
    TextPart,
)

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="the fenced-read e2e is POSIX-only"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}
OUTSIDE = REFUSAL_COPY["outside_workspace"]
#: What is in the file the model is not allowed to read. If the read had happened,
#: this rides back to the provider in the tool result.
SECRET = "operator-token-DO-NOT-LEAK-9f21"
#: What the model says on its NEXT turn. Its presence in the provider's requests is
#: the proof the turn survived the refusal.
FOLLOWUP = "understood, staying inside the workspace"


async def _never_prompt(_request: PermissionRequest) -> str:
    raise AssertionError("a fenced read must never be put in front of a human")


async def _drain(sub: Any, *, budget_seconds: float = 90.0) -> list[Event]:
    """Drain exactly one turn: wait for `running`, then return on the next
    idle/error."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    out: list[Event] = []
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return out
        try:
            async with asyncio.timeout(remaining):
                event: Event = await anext(sub)
        except (TimeoutError, StopAsyncIteration):
            return out
        out.append(event)
        if isinstance(event, SessionStatusChanged):
            if event.status == "running":
                seen_running = True
            elif event.status in ("idle", "error") and seen_running:
                return out


def _requests_blob(server: MockOpenAIServer) -> str:
    """Everything the model saw across the turn — where a tool result, and so a
    denial reason, becomes visible."""
    return json.dumps(server.requests)


async def test_a_read_outside_the_workspace_is_refused_and_the_turn_continues(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path.parent / "outside-the-project.txt"
    secret_file.write_text(f"token: {SECRET}\n")
    script = {
        "*": tool_call_chunks("read", {"filePath": str(secret_file)}),
        FOLLOWUP_KEY: text_chunks(FOLLOWUP),
    }

    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, server):
        # A bounded session names its root; one that does not is refused
        # rather than run unsandboxed. Here the root is the project itself.
        session = await runtime.open_chat(
            sid,
            permission_broker=PermissionBroker(_never_prompt, default_timeout_seconds=30.0),
            path_fence=PathFence(
                escape=lambda request: fence.ask_escape(request, root=tmp_path),
                reason=OUTSIDE,
            ),
            working_dir=tmp_path,
        )
        session.set_permission_mode("read_only")
        sub = session.subscribe()
        try:
            await session.send_prompt("read that file next to the project", model=_MODEL)
            events = await _drain(sub)
            blob = _requests_blob(server)
        finally:
            await runtime.close_chat(sid)

    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert "idle" in statuses, f"the refused turn never settled (statuses={statuses})"
    assert "error" not in statuses, f"a fenced read must not error the turn ({statuses})"
    # The refusal reached the model as the read's own result…
    assert OUTSIDE in blob, "the refusal reason never reached the model"
    # …the turn survived it: the model was called AGAIN with that result in hand…
    assert any(request["messages"][-1].get("role") == "tool" for request in server.requests), (
        "the turn ended at the refusal instead of asking the model again"
    )
    # …it answered, and the answer reached the session's consumers…
    assert FOLLOWUP in [
        part.text
        for part in (e.part for e in events if isinstance(e, PartCreated))
        if isinstance(part, TextPart)
    ], "the follow-up the model spoke after the refusal never arrived"
    # …and the file was never opened, so its contents cannot be in the conversation.
    assert SECRET not in blob, "the fenced file's contents reached the model"
    assert secret_file.read_text(encoding="utf-8").count(SECRET) == 1, (
        "the file itself is untouched"
    )
