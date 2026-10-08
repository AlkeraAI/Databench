"""A real opencode whose provider answers 503 to every model call.

opencode retries a 5xx with growing delays and no cap on the attempts, so
without a bound in the harness the turn below runs until the test gives up on
it. Through the whole runtime (``HarnessRuntime`` → ``ChatSession`` → the real
bun-driven subprocess → the scripted mock provider) the turn must instead end
failed after the pinned number of retries, the agent must stop calling the
provider, and the failure must carry none of the provider's words, its URL or
its port.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import text_chunks
from alkera_core.schemas.chat import Event, Retrying, SessionStatusChanged

pytestmark = pytest.mark.opencode_e2e

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}
_MARKER = "UNAVAILABLETURN"
#: Two retries at opencode's schedule (2 s, then 4 s) plus a cold start: well
#: inside this, and far short of the no-cap schedule the turn had before.
_BUDGET_SECONDS = 90.0


def _agent_steps(requests: list[dict[str, Any]]) -> int:
    """How many of ``requests`` were the agent's own model calls."""
    return sum(1 for r in requests if "title generator" not in json.dumps(r.get("messages", [])))


@pytest.mark.asyncio
async def test_a_provider_that_answers_503_every_time_ends_the_turn_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("alkera_cli.harness.runtime.MAX_MODEL_RETRIES", 2)
    monkeypatch.setattr("alkera_cli.harness.runtime.MODEL_RETRY_WINDOW_SECONDS", None)
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script={"*": text_chunks("ok")},
        error_on_marker=_MARKER,
        error_status=503,
    ) as (runtime, sid, server):
        session = await runtime.open_chat(sid)
        sub = session.subscribe()
        await session.send_prompt(f"please {_MARKER}", model=_MODEL)

        seen: list[Event] = []
        failed: SessionStatusChanged | None = None
        async with asyncio.timeout(_BUDGET_SECONDS):
            async for ev in sub:
                seen.append(ev)
                if isinstance(ev, SessionStatusChanged) and ev.status == "error":
                    failed = ev
                    break

        assert failed is not None
        retries = [e for e in seen if isinstance(e, Retrying)]
        assert len(retries) >= 2, "opencode never reported a retry"
        assert failed.detail is not None
        assert failed.detail.endswith("after 2 attempts; the turn was stopped."), failed.detail
        for leaked in ("http", "127.0.0.1", f":{server.port}", "mock injected error", "{"):
            assert leaked not in failed.detail, f"{leaked!r} reached the reader: {failed.detail}"
        assert not session.turn_active

        # The agent was stopped, not merely reported: its turn asks the provider
        # no more. (opencode's title call is its own request with its own
        # retries, outside the turn, so it is not counted.)
        asked = _agent_steps(server.requests)
        await asyncio.sleep(10.0)
        assert _agent_steps(server.requests) == asked, "the agent kept calling the provider"
        await runtime.close_chat(sid)
