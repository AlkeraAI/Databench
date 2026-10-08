"""Round-3 read-only review: the cloud session's shell.

The mirror opens the analyst session in ``read_only`` and its docstring (and
``cloud/fence.py``'s) say the harness refuses a shell command. The parent-hosted
``bash`` tool is advertised to the model with an ``allow`` permission rule, so it
never raises the permission ask the PathFence judges; its own gate auto-allows a
READ-effect command and only a substring scan stands between the model and the
box's secrets. On the demo box ``ALKERA_HOME`` is ``/opt/alkera-home`` — no
``.alkera`` marker — and the daemon's environment is inherited by every child.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")


class _RejectingBroker:
    def __init__(self) -> None:
        self.prompts = 0

    async def resolve(self, request: Any) -> str:
        self.prompts += 1
        return "reject_once"


def _registry(root: Path) -> ToolRegistry:
    project = ProjectDirectory(root / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)
    return registry


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """The demo box's layout: ALKERA_HOME outside the workspace, spelled without a dot."""
    home = tmp_path / "opt" / "alkera-home"
    home.mkdir(parents=True)
    (home / "auth.yml").write_text("api_url: https://x\ntoken: BOX-DEVICE-JWT-SECRET\n")
    monkeypatch.setenv("ALKERA_HOME", str(home))
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    work = tmp_path / "opt" / "alkera-work"
    work.mkdir(parents=True)
    return home, work


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat {home}/auth.yml", id="cat-auth-yml-absolute"),
        pytest.param("head -c 4096 {home}/auth.yml", id="head-auth-yml"),
        pytest.param("cat {home}/auth.y*", id="cat-auth-glob"),
        pytest.param("ls {home} && cat {home}/*.yml", id="ls-then-cat"),
        # The floor is a substring scan for the resolved ALKERA_HOME; every
        # spelling below names the same file without containing that string.
        pytest.param("cd {home_parent} && cat alkera-home/auth.yml", id="cd-then-relative"),
        pytest.param("cat {home_parent}/*/auth.yml", id="parent-glob"),
        pytest.param("cat {home_parent}/alkera-hom*/auth.yml", id="dirname-glob"),
        pytest.param("cat $ALKERA_HOME/auth.yml", id="env-var-spelling"),
        pytest.param("find {home_parent} -name auth.yml -exec cat {{}} +", id="find-exec-cat"),
        pytest.param("grep -r token {home_parent}", id="grep-recursive"),
    ],
)
async def test_read_only_session_shell_cannot_read_the_box_token(
    box: tuple[Path, Path], command: str
) -> None:
    home, work = box
    broker = _RejectingBroker()
    out = await _registry(work).dispatch(
        "bash",
        {"command": command.format(home=home, home_parent=home.parent), "description": "read"},
        alkera_dir=str(work / ".alkera"),
        broker=broker,
        permission_mode="read_only",
    )
    assert "BOX-DEVICE-JWT-SECRET" not in (out.get("output") or ""), out


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("printenv", id="printenv"),
        pytest.param("env", id="env"),
        pytest.param("printenv ALKERA_LEASED_SECRET", id="printenv-one"),
    ],
)
async def test_read_only_session_shell_cannot_dump_the_daemon_environment(
    box: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _home, work = box
    monkeypatch.setenv("ALKERA_LEASED_SECRET", "LEASED-PASSWORD-VALUE")
    out = await _registry(work).dispatch(
        "bash",
        {"command": command, "description": "env"},
        alkera_dir=str(work / ".alkera"),
        broker=_RejectingBroker(),
        permission_mode="read_only",
    )
    assert "LEASED-PASSWORD-VALUE" not in (out.get("output") or ""), out
