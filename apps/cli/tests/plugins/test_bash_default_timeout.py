"""The wall clock a shell command runs under when the model names no timeout.

None. A build, a test suite or a training run may take hours, and a command
killed part-way costs the turn everything it had done. A command with no
timeout ends only when it is stuck (``test_bash_stuck.py``). Every
parent-hosted sandbox that borrows the default (the shell, the graph Python
child) borrows the same one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.bash_exec import ExecResult
from alkera_cli.plugins.plugin_base.bash_ids import DEFAULT_TIMEOUT_MS
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")


def test_there_is_no_default_wall_clock() -> None:
    assert DEFAULT_TIMEOUT_MS is None


async def test_a_command_with_no_timeout_runs_under_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The number the tool hands the executor when the model names none."""
    from alkera_cli.plugins.plugin_base import bash_tool

    seen: list[int | None] = []

    async def _record(command: str, **kwargs: Any) -> ExecResult:
        seen.append(kwargs["timeout_ms"])
        return ExecResult(
            output="", exit_code=0, truncated=False, output_path=None, timed_out=False
        )

    monkeypatch.setattr(bash_tool, "run_command", _record)
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)

    await registry.dispatch(
        "bash",
        {"command": "echo hi", "description": "say hi"},
        alkera_dir=str(tmp_path / ".alkera"),
    )
    assert seen == [None]

    # A model that names its own budget still gets exactly that one.
    seen.clear()
    await registry.dispatch(
        "bash",
        {"command": "echo hi", "description": "say hi", "timeout": 2_000},
        alkera_dir=str(tmp_path / ".alkera"),
    )
    assert seen == [2_000]
