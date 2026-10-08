"""E2E proof: a still-running FOREGROUND bash is reaped on a turn cancel.

Drives a REAL opencode subprocess whose scripted model runs a foreground ``bash``
that records its process-group id and then sleeps. The test cancels the turn and
asserts the command's process group is reaped within a few seconds.

This is the end-to-end proof of the abort seam. The in-flight loopback-MCP tool
call is NOT cancelled by a turn cancel (the stateless MCP server dispatches it on a
task decoupled from the request, and neither opencode's abort nor uvicorn delivers a
CancelledError into it), so the ChatSession's per-turn abort Event — SET in
``ChatSession.cancel()`` and raced by ``bash`` against ``run_command`` — is the ONLY
reap path. To confirm this is not coverage theater: removing ``self._turn_abort.set()``
from ``ChatSession.cancel()`` makes the command survive to its 30s sleep and this test
FAIL (the group stays alive).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import FOLLOWUP_KEY, text_chunks, tool_call_chunks
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.schemas.chat import PermissionRequest

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="POSIX process-group reaping"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_pgid(pidfile: Path) -> int | None:
    """Read the pgid the shell wrote, or None if not written yet. A SYNC helper so
    the blocking Path I/O stays out of the async poll loop (ruff ASYNC240)."""
    if not pidfile.exists():
        return None
    text = pidfile.read_text().strip()
    return int(text) if text else None


async def _wait_pgid(pidfile: Path, *, tries: int = 400) -> int:
    """Poll until the foreground bash wrote its pid (== process-group id, since the
    shell is spawned as the session/group leader) and return it."""
    for _ in range(tries):
        pgid = _read_pgid(pidfile)
        if pgid is not None:
            return pgid
        await asyncio.sleep(0.05)
    raise AssertionError("the foreground bash never wrote its pgid")


async def _wait_group_gone(pgid: int, *, tries: int = 120) -> bool:
    for _ in range(tries):
        if not _group_alive(pgid):
            return True
        await asyncio.sleep(0.05)
    return not _group_alive(pgid)


async def test_foreground_bash_is_reaped_on_turn_cancel(tmp_path: Path) -> None:
    pgidfile = tmp_path / "bash.pgid"
    # On POSIX the tool named "bash" IS ours: opencode's native ShellTool is dropped
    # from the advertised set and our loopback-MCP tool takes the name, so the model
    # calling "bash" reaches the parent-hosted shell whose reaping this test proves.
    script = {
        "*": tool_call_chunks(
            "bash",
            {"command": f"echo $$ > '{pgidfile}'; sleep 30", "description": "long sleep"},
        ),
        FOLLOWUP_KEY: text_chunks("done"),
    }

    async def _allow(_request: PermissionRequest) -> str:
        return "allow_once"

    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, _server):
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(_allow, default_timeout_seconds=30.0)
        )
        # Fire the turn as a task — the scripted command sleeps, so the turn does not
        # finish until we cancel it.
        send = asyncio.ensure_future(session.send_prompt("run the long command", model=_MODEL))
        try:
            pgid = await _wait_pgid(pgidfile)
            assert _group_alive(pgid)  # the foreground bash is genuinely running

            await session.cancel()  # Ctrl-C
            assert await _wait_group_gone(pgid), (
                "the foreground bash process group was NOT reaped on turn cancel"
            )
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(send, timeout=15.0)
            await runtime.close_chat(sid)
