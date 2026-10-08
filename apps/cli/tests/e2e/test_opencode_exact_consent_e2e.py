"""E2E proof: "Always allow this exact command" on ``rm`` reaches that line only.

A REAL opencode subprocess whose scripted model runs ``rm -rf a/``, then the
same line again, then ``rm -rf b/``. The card for the first offers "Always allow
this exact command" and the test answers it; the second identical line runs with
no card; the third, a different ``rm``, raises one, which the test refuses.

Unit tests pin the decision on plain data. What only a real subprocess proves is
that the grant survives the whole path — the card the model's call raised, the
answer the broker returned, the rule the engine wrote, and the next tool call
decided by that rule — with the directories on disk as the witness.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import FOLLOWUP_KEY, text_chunks, tool_call_chunks
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.schemas.chat import Event, PermissionRequest, SessionStatusChanged

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="the shell e2e runs rm on POSIX"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}
_EXACT = "Always allow this exact command"


async def _turn(session: Any, prompt: str, *, budget_seconds: float = 90.0) -> list[str]:
    """Send ``prompt`` and wait out the one turn it starts; the statuses seen."""
    sub = session.subscribe()
    await session.send_prompt(prompt, model=_MODEL)
    statuses: list[str] = []
    async with asyncio.timeout(budget_seconds):
        while True:
            ev: Event = await anext(sub)
            if isinstance(ev, SessionStatusChanged):
                statuses.append(ev.status)
                if ev.status in ("idle", "error") and "running" in statuses:
                    return statuses


def _bash(command: str) -> list[object]:
    return tool_call_chunks("bash", {"command": command, "description": "remove a directory"})


async def test_an_exact_consent_on_rm_covers_the_same_line_and_no_other(tmp_path: Path) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "file.txt").write_text("x")

    # The first turn routes via "*" (an injected mode prompt can change the last
    # user message); each later turn's call is queued ahead of the script.
    script = {"*": _bash("rm -rf a/"), FOLLOWUP_KEY: text_chunks("done")}
    cards: list[tuple[str, dict[str, str]]] = []

    async def _person(request: PermissionRequest) -> str:
        command = request.patterns[0] if request.patterns else ""
        cards.append((command, {o.option_id: o.name for o in request.options}))
        return "allow_always" if len(cards) == 1 else "reject_once"

    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, server):
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(_person, default_timeout_seconds=60.0)
        )
        session.set_permission_mode("default")
        try:
            await _turn(session, "delete a")
            assert [command for command, _ in cards] == ["rm -rf a/"]
            assert cards[0][1].get("allow_always") == _EXACT
            assert not (tmp_path / "a").exists()

            # The identical line again: the grant decides it, nobody is asked.
            (tmp_path / "a").mkdir()
            server.queue_chat_responses([_bash("rm -rf a/"), text_chunks("done")])
            await _turn(session, "delete a again")
            assert len(cards) == 1
            assert not (tmp_path / "a").exists()

            # A different rm: a card, refused, and the directory stays.
            server.queue_chat_responses([_bash("rm -rf b/"), text_chunks("done")])
            await _turn(session, "delete b")
            assert [command for command, _ in cards] == ["rm -rf a/", "rm -rf b/"]
            assert (tmp_path / "b" / "file.txt").exists()
        finally:
            await runtime.close_chat(sid)
