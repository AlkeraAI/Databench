"""The marker that stops a backend re-spilling a result the host already spilled.

A tool that outgrows the model's window keeps the whole output in a file of its
own and returns a tail plus that path. A backend that bounds tool results would
otherwise shorten the tail AGAIN into a file of its own and stamp that path on
the tool call — so the row's pointer names a copy of the preview while the real
output sits in a file the row never names. ``host_spill_pointer`` is what the
transports read to declare "already shortened, here is the file"; these pin which
results earn that declaration and which must not.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.bash_ids import MAX_BYTES
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.wire import host_spill_pointer
from alkera_core.project.directory import ProjectDirectory


def _size(path: str) -> int:
    """Bytes on disk at ``path`` (sync, so an async test doesn't block the loop)."""
    return Path(path).stat().st_size


class _Broker:
    """Approves whatever the shell gate asks about (these tests are about the
    spill pointer, not the gate — which is exhausted in test_bash_gate)."""

    async def resolve(self, request: Any) -> str:
        return "allow_once"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(
            {"truncated": True, "output_path": "/tmp/full.txt"}, "/tmp/full.txt", id="spilled"
        ),
        pytest.param(
            {"result": {"truncated": True, "output_path": "/tmp/full.txt"}},
            "/tmp/full.txt",
            id="through-call_tool-envelope",
        ),
        pytest.param(None, None, id="not-a-dict-none"),
        pytest.param("truncated", None, id="not-a-dict-str"),
        pytest.param(
            [{"truncated": True, "output_path": "/tmp/full.txt"}], None, id="not-a-dict-list"
        ),
        pytest.param({}, None, id="empty"),
        pytest.param({"output": "hi", "exit_code": 0}, None, id="ordinary-result"),
        # A result that fit needs no pointer, even when a path rides along: the
        # claim the marker makes is "this was CUT", not "a file exists".
        pytest.param(
            {"truncated": False, "output_path": "/tmp/full.txt"}, None, id="not-truncated"
        ),
        pytest.param(
            {"truncated": "yes", "output_path": "/tmp/full.txt"}, None, id="truncated-mistyped"
        ),
        pytest.param(
            {"truncated": 1, "output_path": "/tmp/full.txt"}, None, id="truncated-int-one"
        ),
        # Cut but with nothing to point at — the backend must shorten it itself
        # rather than hand the reader a path that reaches nothing.
        pytest.param({"truncated": True}, None, id="no-pointer"),
        pytest.param({"truncated": True, "output_path": None}, None, id="pointer-none"),
        pytest.param({"truncated": True, "output_path": ""}, None, id="pointer-empty"),
        pytest.param({"truncated": True, "output_path": 7}, None, id="pointer-mistyped"),
        # The generic delivery door's own spill: cut, but the rest is reachable by
        # blob handle rather than by path, so there is no path to keep.
        pytest.param(
            {"truncated": True, "preview": "…", "blob": {"sha256": "ab" * 32}, "tool": "sql.query"},
            None,
            id="door-blob-spill",
        ),
        pytest.param({"result": {"output": "hi"}}, None, id="envelope-not-truncated"),
        pytest.param({"result": "hi"}, None, id="envelope-not-a-dict"),
        pytest.param(
            {"result": {"result": {"truncated": True, "output_path": "/tmp/full.txt"}}},
            None,
            id="envelope-nested-twice",
        ),
    ],
)
def test_host_spill_pointer_branches(result: Any, expected: str | None) -> None:
    assert host_spill_pointer(result) == expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")
async def test_a_real_bash_spill_points_at_the_whole_output(tmp_path: Path) -> None:
    """The pointer the marker carries reaches every byte the command wrote.

    Driven through the real tool rather than a hand-built dict, so the marker and
    the executor cannot drift: if the spill ever stops holding the WHOLE output —
    or stops naming it — this fails.
    """
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)

    line = "x" * 79
    lines = (MAX_BYTES * 3) // 80
    written = (len(line) + 1) * lines
    result = await registry.dispatch(
        "bash",
        {"command": f"yes '{line}' | head -n {lines}", "description": "dump a lot"},
        alkera_dir=str(tmp_path / ".alkera"),
        broker=_Broker(),
    )

    assert result["truncated"] is True
    pointer = host_spill_pointer(result)
    assert pointer is not None, result
    assert await asyncio.to_thread(_size, pointer) == written
    # What the model was handed is the tail, not the output — the whole point of
    # the pointer being the only way back to the rest.
    assert len(result["output"].encode()) < written // 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")
async def test_a_small_bash_result_carries_no_pointer(tmp_path: Path) -> None:
    """A result that fit is not declared shortened — a backend is free to bound it."""
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)

    result = await registry.dispatch(
        "bash",
        {"command": "echo hi", "description": "say hi"},
        alkera_dir=str(tmp_path / ".alkera"),
        broker=_Broker(),
    )

    assert result["truncated"] is False
    assert host_spill_pointer(result) is None
