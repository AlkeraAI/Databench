"""Where a shortened tool result puts the rest of itself.

A command whose output passes 2000 lines or 50 KiB is tailed for the model and
the whole of it written to a file whose path rides back in the result. That path
is only useful if the model can then open it, and where the file lands decides
whether it can: the daemon's own state directory (``<root>/.alkera``) holds other
chats' transcripts and connector credentials, so a cloud session's fence refuses
every location inside it — bar the one directory that session owns.

These pin the local half: the path the tool reports is the file, and it sits
inside the directory the session was given to work in. The cloud half — the
fence's verdict on that same path — is in ``apps/cli/tests/cloud/test_cloud_fence.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")

#: Comfortably past both thresholds (2000 lines / 50 KiB).
_LOUD = "for i in $(seq 1 4000); do echo 'line-'$i; done"


def _spilled_text(output_path: str) -> str:
    """What the file a shortened result named actually holds — which is also the
    proof it is a file the reader can open. SYNC, so the blocking Path I/O stays
    out of the async test bodies (ruff ASYNC240)."""
    spilled = Path(output_path)
    assert spilled.is_file(), f"the reported path is not a file: {spilled}"
    return spilled.read_text()


def _registry(tmp_path: Path) -> ToolRegistry:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)
    return registry


async def test_a_shortened_result_names_a_file_that_holds_the_whole_output(
    tmp_path: Path,
) -> None:
    """The path in the result is a real file, it is named in the text the model
    reads, and it carries the output the tail dropped."""
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "work"
    sandbox.mkdir(parents=True)
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": _LOUD, "description": "print a lot"},
        alkera_dir=str(tmp_path / ".alkera"),
        sandbox_dir=str(sandbox),
    )

    assert out["truncated"] is True
    assert out["output_path"] in out["output"], "the model is told where the rest went"
    body = _spilled_text(out["output_path"])
    assert "line-1\n" in body and "line-4000\n" in body
    assert "line-1\n" not in out["output"], "the head is exactly what was dropped"


async def test_the_spilled_file_lands_in_the_directory_the_session_works_in(
    tmp_path: Path,
) -> None:
    """A cloud session may read its own working directory and nothing else under
    ``.alkera``. Spilling beside the other chats' records instead would hand the
    model a path its own fence refuses."""
    sandbox = tmp_path / ".alkera" / "chats" / "c1" / "work"
    sandbox.mkdir(parents=True)
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": _LOUD, "description": "print a lot"},
        alkera_dir=str(tmp_path / ".alkera"),
        sandbox_dir=str(sandbox),
    )
    assert sandbox in Path(out["output_path"]).parents


async def test_a_session_with_no_working_directory_still_spills_somewhere_readable(
    tmp_path: Path,
) -> None:
    """A tool call outside a chat (no sandbox) keeps the old location — under the
    project's own state dir, which an unfenced local session may read."""
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": _LOUD, "description": "print a lot"},
        alkera_dir=str(tmp_path / ".alkera"),
    )
    assert "line-4000\n" in _spilled_text(out["output_path"])
    assert (tmp_path / ".alkera") in Path(out["output_path"]).parents
