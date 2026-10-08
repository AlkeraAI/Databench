"""The notebook tools end to end through a REAL opencode subprocess.

A scripted mock provider plays the model; a real bun-driven opencode calls the
parent-hosted loopback tool server; the runtime serves the notebook tools over
the simulator's reference engine through its own ``notebook_host_factory``
seam. Across two turns the agent creates a notebook, adds a SQL cell over a
local frame, runs it, reads the traceback, then (after a person typed in the
same cell) fixes it and re-runs. Pinned: the gate per permission mode, the
untrusted wrapping of the traceback, the concurrent-edit notice, and the
digest on the next turn.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import text_chunks, tool_call_chunks
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_core.schemas.chat import PermissionRequest, SessionStatusChanged
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.tools import ActorRef, NotebookHost
from alkera_notebook.tools.models import ReplaceCellOp

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="parent-hosted alkera loopback is POSIX-only"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}
PATH = "analysis.alknb.py"
BOB = ActorRef(kind="person", id="person:bob", display_name="Bob")


class _Engines:
    """One reference workspace per root, as the runtime's host factory."""

    def __init__(self) -> None:
        self.workspaces: dict[str, ReferenceWorkspace] = {}

    def __call__(self, root: Path, actor: ActorRef) -> NotebookHost:
        ws = self.workspaces.setdefault(str(root), ReferenceWorkspace(root=str(root)))
        return ws.host(actor)

    @property
    def notebook(self) -> Any:
        (ws,) = self.workspaces.values()
        return ws.notebooks[PATH]


def _call(name: str, args: dict[str, Any], n: int) -> list[Any]:
    return tool_call_chunks("alkera_call_tool", {"name": name, "args": args}, call_id=f"call_{n}")


class _Broker:
    def __init__(self) -> None:
        self.asks: list[PermissionRequest] = []

    async def __call__(self, request: PermissionRequest) -> str:
        self.asks.append(request)
        return "allow_once"


async def _turn(session: Any, text: str) -> None:
    sub = session.subscribe()
    seen_running = False
    await session.send_prompt(text, model=_MODEL)
    async with asyncio.timeout(120):
        async for ev in sub:
            if isinstance(ev, SessionStatusChanged):
                if ev.status == "running":
                    seen_running = True
                elif ev.status in ("idle", "error") and seen_running:
                    return


def _script_only_the_agent(server: Any) -> None:
    """Serve the queued turns only to the agent's own requests: opencode also
    asks the model for a chat title (a request offering no tools), which must
    not consume a scripted tool call."""
    select = server._select_chat_chunks

    def chosen(request: dict[str, Any], last_user: str) -> Any:
        if not request.get("tools"):
            return text_chunks("Notebook work")
        return select(request, last_user)

    server._select_chat_chunks = chosen


def _tool_results(server: Any) -> list[str]:
    out: list[str] = []
    for request in server.requests:
        for message in request.get("messages", []):
            if message.get("role") == "tool":
                out.append(json.dumps(message.get("content")))
    return out


async def test_an_agent_builds_runs_reads_fixes_and_sees_the_digest(tmp_path: Path) -> None:
    engines = _Engines()
    broker = _Broker()
    first_turn = [
        _call(
            "notebook.create",
            {
                "path": PATH,
                "cells": [
                    {"kind": "setup", "source": "import pandas as pd\nimport alkera"},
                    {"source": "frame_a = pd.DataFrame({'v': [1, 2, 3]})", "name": "load"},
                ],
            },
            1,
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
            2,
        ),
        _call("notebook.run", {"path": PATH, "target": {"kind": "all"}}, 3),
        _call("notebook.output", {"path": PATH, "cell": "total", "part": "error"}, 4),
        text_chunks("The SQL cell failed: there is no column w."),
    ]
    async with opencode_e2e_runtime(
        tmp_path, mock_script={"*": text_chunks("(unscripted)")}, notebook_host_factory=engines
    ) as (runtime, sid, server):
        _script_only_the_agent(server)
        server.queue_chat_responses(first_turn)
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(broker, default_timeout_seconds=30.0)
        )
        try:
            await _turn(session, "build the notebook")
            nb = engines.notebook
            sql_id = next(c.id for c in nb.doc.live() if c.name == "total")
            assert nb.statuses()[sql_id] == "error"
            results = _tool_results(server)
            traceback = next(
                r for r in results if "Binder Error" in r or ("w" in r and "evalue" in r)
            )
            assert '\\"untrusted\\": true' in traceback

            # Bob types in the cell the agent is about to fix.
            bob = await engines.workspaces[next(iter(engines.workspaces))].host(BOB).open(PATH)
            await bob.apply(
                [ReplaceCellOp(cell_id=sql_id, source="SELECT sum(w) AS s FROM frame_a ")], None
            )

            server.queue_chat_responses(
                [
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
                        5,
                    ),
                    _call(
                        "notebook.run",
                        {"path": PATH, "target": {"kind": "cells", "ids": [sql_id]}},
                        6,
                    ),
                    text_chunks("Fixed: the total is 6."),
                ]
            )
            await _turn(session, "fix it")
        finally:
            await runtime.close_chat(sid)

    nb = engines.notebook
    assert nb.statuses()[sql_id] == "fresh"
    assert nb.kstate[sql_id].output.text.strip().endswith("6.0")
    # The agent's fix merged with Bob's typing: both are in the code that ran.
    assert nb.kstate[sql_id].submitted == "SELECT sum(v) AS s FROM frame_a "
    results = _tool_results(server)
    assert any("concurrent_edit" in r for r in results), "the agent was told Bob was typing"
    second_turn = [r for r in server.requests if "fix it" in json.dumps(r.get("messages", []))]
    assert any(
        "Notebook activity by others" in json.dumps(r)
        and f"Bob edited total ({sql_id})" in json.dumps(r)
        for r in second_turn
    ), "the next turn carries the digest of what Bob did"
    # The first plan holds Python cells (a write): asked in default mode, and the
    # prompt shows the code. The fix re-runs only the SQL cell, a read: unasked.
    run_asks = [a for a in broker.asks if (a.subject or {}).get("operation") == "notebook_run"]
    assert len(run_asks) == 1, [a.subject for a in broker.asks]
    asked_cells = ((run_asks[0].preview or {}).get("notebook") or {}).get("cells", [])
    assert any("pd.DataFrame" in cell["code"] for cell in asked_cells), asked_cells


@pytest.mark.parametrize(
    ("mode", "runs"),
    [
        pytest.param("read_only", False, id="read_only_refuses"),
        pytest.param("bypass", True, id="bypass_runs"),
    ],
)
async def test_the_run_gate_follows_the_chats_mode(tmp_path: Path, mode: str, runs: bool) -> None:
    engines = _Engines()
    broker = _Broker()
    async with opencode_e2e_runtime(
        tmp_path,
        mock_script={"*": text_chunks("(unscripted)")},
        notebook_host_factory=engines,
        permission_mode=mode,
    ) as (runtime, sid, server):
        _script_only_the_agent(server)
        server.queue_chat_responses(
            [
                _call("notebook.create", {"path": PATH, "cells": [{"source": "x = 41 + 1\nx"}]}, 1),
                _call("notebook.run", {"path": PATH, "target": {"kind": "all"}}, 2),
                text_chunks("done"),
            ]
        )
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(broker, default_timeout_seconds=30.0)
        )
        try:
            await _turn(session, "make and run it")
        finally:
            await runtime.close_chat(sid)
    assert broker.asks == []
    assert bool(engines.notebook.executions) is runs
    if not runs:
        assert any("permission denied" in r for r in _tool_results(server))
