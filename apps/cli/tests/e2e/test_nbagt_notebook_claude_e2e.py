"""The notebook tools end to end through a REAL ``claude`` subprocess.

The claude agent (the SDK-bundled binary) runs against the scripted mock
Anthropic server, reaches the parent-hosted loopback tool server as its
``alkera`` MCP server, and calls the notebook tools served over the
simulator's reference engine through the runtime's ``notebook_host_factory``
seam. Two turns: build a notebook with a SQL cell over a local frame, run it
(asked: the plan holds Python cells), read the traceback; then, after a person
typed in the same cell, fix it (a concurrent-edit notice) and re-run; the
second turn's prompt carries the digest of what the person did.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from _helpers.claude_runner import _claude_env, _resolve_or_skip
from _mocks.mock_anthropic_server import MockAnthropicServer, text_events, tool_use_events
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.registry import CLAUDE_HARNESS
from alkera_cli.harness.runtime import HarnessRuntime
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionRequest, SessionStatusChanged
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.tools import ActorRef, NotebookHost
from alkera_notebook.tools.models import ReplaceCellOp

pytestmark = [pytest.mark.claude_e2e, pytest.mark.asyncio]

PATH = "analysis.alknb.py"
BOB = ActorRef(kind="person", id="person:bob", display_name="Bob")


def _call(name: str, args: dict[str, Any]) -> Sequence[Any]:
    return tool_use_events("mcp__alkera__call_tool", {"name": name, "args": args})


class _ScriptedAgent:
    """Answers the agent's requests (those offering tools) from a queue, and
    anything else (a title, a probe) with plain text."""

    def __init__(self, server: MockAnthropicServer) -> None:
        self.server = server
        self.queue: list[Sequence[Any]] = []

    def __call__(self, last_user: str) -> Sequence[Any]:
        del last_user
        request = self.server.requests[-1]
        if request.get("tools") and self.queue:
            return self.queue.pop(0)
        return text_events("ok")


async def _turn(session: Any, text: str) -> None:
    sub = session.subscribe()
    seen_running = False
    await session.send_prompt(text)
    async with asyncio.timeout(120):
        async for ev in sub:
            if isinstance(ev, SessionStatusChanged):
                if ev.status == "running":
                    seen_running = True
                elif ev.status in ("idle", "error") and seen_running:
                    return


def _tool_results(server: MockAnthropicServer) -> list[str]:
    out: list[str] = []
    for request in server.requests:
        for message in request.get("messages", []):
            content = message.get("content")
            if isinstance(content, list):
                out.extend(json.dumps(b) for b in content if b.get("type") == "tool_result")
    return out


async def test_claude_builds_runs_reads_fixes_and_sees_the_digest(tmp_path: Path) -> None:
    _resolve_or_skip()
    workspaces: dict[str, ReferenceWorkspace] = {}

    def factory(root: Path, actor: ActorRef) -> NotebookHost:
        return workspaces.setdefault(str(root), ReferenceWorkspace(root=str(root))).host(actor)

    asks: list[PermissionRequest] = []

    async def allow(request: PermissionRequest) -> str:
        asks.append(request)
        return "allow_once"

    server = MockAnthropicServer({"*": text_events("ok")})
    agent = _ScriptedAgent(server)
    server._select_events = agent  # type: ignore[method-assign]
    await server.start()
    project = ProjectDirectory(tmp_path / ".alkera")
    runtime = HarnessRuntime(project, notebook_host_factory=factory)
    chat = project.chats().create(title="e2e")
    sid = chat.session_id
    chat.manifest.harness_type = CLAUDE_HARNESS
    chat.manifest.harness = {**chat.manifest.harness, "claude_env": _claude_env(server.base_url)}
    chat.flush_manifest()
    chat.close()
    try:
        agent.queue = [
            _call(
                "notebook.create",
                {
                    "path": PATH,
                    "cells": [
                        {"kind": "setup", "source": "import pandas as pd\nimport alkera"},
                        {"source": "frame_a = pd.DataFrame({'v': [1, 2, 3]})", "name": "load"},
                    ],
                },
            ),
            _call(
                "notebook.edit",
                {
                    "path": PATH,
                    "ops": [
                        {
                            "op": "insert",
                            "kind": "sql",
                            "name": "total",
                            "source": "SELECT sum(w) AS s FROM frame_a",
                            "meta": {"output_var": "total_s"},
                        }
                    ],
                },
            ),
            _call("notebook.run", {"path": PATH, "target": {"kind": "all"}}),
            _call("notebook.output", {"path": PATH, "cell": "total", "part": "error"}),
            text_events("The SQL cell failed."),
        ]
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(allow, default_timeout_seconds=30.0)
        )
        await _turn(session, "build the notebook")
        (ws,) = workspaces.values()
        nb = ws.notebooks[PATH]
        sql_id = next(c.id for c in nb.doc.live() if c.name == "total")
        assert nb.statuses()[sql_id] == "error"
        assert any('\\"untrusted\\": true' in r and "evalue" in r for r in _tool_results(server))

        bob = await ws.host(BOB).open(PATH)
        await bob.apply(
            [ReplaceCellOp(cell_id=sql_id, source="SELECT sum(w) AS s FROM frame_a ")], None
        )
        agent.queue = [
            _call(
                "notebook.edit",
                {
                    "path": PATH,
                    "ops": [
                        {
                            "op": "edit",
                            "cell_id": sql_id,
                            "edits": [{"old": "sum(w)", "new": "sum(v)"}],
                        }
                    ],
                },
            ),
            _call("notebook.run", {"path": PATH, "target": {"kind": "cells", "ids": [sql_id]}}),
            text_events("Fixed."),
        ]
        await _turn(session, "fix it")
    finally:
        await runtime.close_all()
        await server.stop()

    assert nb.statuses()[sql_id] == "fresh"
    assert nb.kstate[sql_id].submitted == "SELECT sum(v) AS s FROM frame_a "
    assert any("concurrent_edit" in r for r in _tool_results(server))
    assert any(
        "Notebook activity by others" in json.dumps(r)
        and f"Bob edited total ({sql_id})" in json.dumps(r)
        for r in server.requests
    ), "the second turn carries the digest of what Bob did"
    run_asks = [a for a in asks if (a.subject or {}).get("operation") == "notebook_run"]
    assert len(run_asks) == 1, [a.subject for a in asks]
