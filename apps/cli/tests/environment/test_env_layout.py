"""The environment tools under the one-root layout: the agent sees its root at
the sandbox home and the environments at ``/opt/alkera/envs``, so everything
sent into the sandbox is spelled that way and everything that comes back is
judged by host path; a FIFO cannot hang a chat's start; only the working
directory's alias is the workspace; and a cancel stops the command inside the
sandbox, not just the host side of the launch."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.fence import SessionFence
from alkera_cli.environment import (
    Command,
    CommandResult,
    render_environment_instructions,
    write_spec,
)
from alkera_cli.environment.capture import build_spec
from alkera_cli.environment.probe import MARKER, ProbeResult
from alkera_cli.environment.service import EnvironmentService
from alkera_cli.environment.spec import EnvironmentSpec, PackageSpec, PythonInfo
from alkera_cli.harness.sandbox import SandboxLaunch
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base import environment_tool as tool_module
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.environment_tool import SandboxRunner, workspace_aliases
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the box's chat launch is POSIX-only")

HOME = "/home/alkera"
ENVS = "/opt/alkera/envs"


class Box:
    """A chat's trees on a box, with the fence the mirror builds for them."""

    def __init__(self, tmp_path: Path) -> None:
        self.alkera = tmp_path / "box" / ".alkera"
        self.folder = self.alkera / "chats" / "chat-a"
        self.working = self.folder / "scratch"
        self.envs = self.folder / ".runtime" / "envs"
        self.working.mkdir(parents=True)
        self.envs.mkdir(parents=True)
        self.fence = SessionFence(
            root=tmp_path / "box",
            folder=self.working,
            working_dir=self.working,
            aliases=((HOME, self.working), (ENVS, self.envs), ("/opt/alkera/harness/x", tmp_path)),
            own_trees=(self.envs,),
        )


def test_only_the_working_directorys_alias_is_the_workspace(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert workspace_aliases(box.fence) == (str(box.working), HOME)


def test_a_package_inside_the_environment_is_not_recorded_as_workspace_relative(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path)
    probe = ProbeResult.model_validate(
        {
            "root": HOME,
            "host": {"sys_platform": "linux", "machine": "x86_64"},
            "env": {
                "python": {"version": "3.12.4", "prefix": f"{ENVS}/alkera", "base_prefix": "/usr"},
                "pyvenv_cfg": {},
                "distributions": [
                    {
                        "name": "inenv",
                        "version": "1",
                        "direct_url": {
                            "url": f"file://{ENVS}/alkera/src/inenv",
                            "dir_info": {"editable": True},
                        },
                    },
                    {
                        "name": "mylib",
                        "version": "1",
                        "direct_url": {
                            "url": f"file://{HOME}/libs/mylib",
                            "dir_info": {"editable": True},
                        },
                    },
                ],
            },
        }
    )
    spec = build_spec(probe, roots=workspace_aliases(box.fence), captured_at=_now())
    assert spec.package("mylib").path == "libs/mylib"  # type: ignore[union-attr]
    assert spec.package("inenv") is None
    assert [(i.kind, i.name) for i in spec.not_portable] == [
        ("editable_outside_workspace", "inenv")
    ]


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 10, 4, tzinfo=UTC)


class _Broker:
    async def resolve(self, request: Any) -> str:
        return "allow_once"


class RecordingRunner:
    """The sandbox boundary: records what is sent in, answers as the
    container would (paths spelled the agent's way)."""

    def __init__(self, box: Box) -> None:
        self.box = box
        self.commands: list[Command] = []

    async def which(self, name: str) -> str | None:
        return f"/usr/local/bin/{name}"

    async def run(self, command: Command) -> CommandResult:
        self.commands.append(command)
        if "--root" in command.argv:
            python = command.argv[0]
            payload: dict[str, Any] = {"root": command.argv[command.argv.index("--root") + 1]}
            payload["host"] = {"sys_platform": "linux", "machine": "x86_64"}
            payload["paths"] = {}
            if python == "python3":
                payload["env"] = {
                    "python": {
                        "version": "3.12.4",
                        "prefix": f"{ENVS}/alkera",
                        "executable": f"{ENVS}/alkera/bin/python3",
                        "base_prefix": "/usr",
                    },
                    "pyvenv_cfg": {},
                    "distributions": [],
                }
                return CommandResult(exit_code=0, output=MARKER + json.dumps(payload))
            if python.startswith(f"{ENVS}/alkera/"):
                payload["env"] = {
                    "python": {
                        "version": "3.12.4",
                        "prefix": f"{ENVS}/alkera",
                        "base_prefix": "/usr",
                    },
                    "pyvenv_cfg": {},
                    "distributions": [],
                }
                return CommandResult(exit_code=0, output=MARKER + json.dumps(payload))
            return CommandResult(exit_code=127, output="not found")
        return CommandResult(exit_code=127 if command.argv[1:2] == ("-c",) else 0, output="")


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Box, RecordingRunner]:
    b = Box(tmp_path)
    runner = RecordingRunner(b)
    monkeypatch.setattr(tool_module, "_chat_runner", lambda *a, **k: runner)
    return b, runner


async def _dispatch(
    b: Box, name: str, args: dict[str, Any], service: EnvironmentService | None = None
) -> dict[str, Any]:
    project = ProjectDirectory(b.alkera)
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)
    registry.environment = service
    return await registry.dispatch(
        name,
        args,
        broker=_Broker(),
        permission_mode="bypass",
        alkera_dir=b.alkera,
        fence=b.fence,
        session_id="chat-a",
        sandbox_dir=b.working,
    )


async def test_a_cloud_capture_names_the_root_as_the_agent_sees_it(
    box: tuple[Box, RecordingRunner],
) -> None:
    b, runner = box
    service = EnvironmentService()
    out = await _dispatch(b, "environment.capture", {}, service)
    assert "error" not in out, out
    probe = runner.commands[0]
    assert probe.argv[probe.argv.index("--root") + 1] == HOME
    written = (b.working / ".alkera-environment.json").read_text(encoding="utf-8")
    # The chat recorded the spec it captured, so its next wake may restore it.
    assert service.captured_here(b.alkera, "chat-a", written)


async def test_a_cloud_recreate_targets_the_agents_spelling_and_admits_it(
    box: tuple[Box, RecordingRunner],
) -> None:
    b, runner = box
    write_spec(
        b.working,
        EnvironmentSpec(
            env_kind="venv",
            python=PythonInfo(version="3.12.4"),
            packages=[
                PackageSpec(name="alpha", version="1.0"),
                PackageSpec(name="mylib", version="1", source="editable", path="libs/mylib"),
            ],
        ),
    )
    (b.working / "libs" / "mylib").mkdir(parents=True)

    planned = await _dispatch(b, "environment.recreate", {"dry_run": True})
    clone = await _dispatch(
        b, "environment.recreate", {"dry_run": True, "env_path": f"{ENVS}/clone"}
    )
    outside = await _dispatch(
        b, "environment.recreate", {"dry_run": True, "env_path": f"{HOME}/venv"}
    )

    assert "error" not in planned, planned
    assert planned["target_env"] == f"{ENVS}/alkera"
    assert "error" not in clone, clone
    sent = " ".join(" ".join(c.argv) + " " + " ".join(c.files.values()) for c in runner.commands)
    assert str(b.working) not in sent and str(b.envs) not in sent
    assert "own environment folders" in outside["error"]


def test_a_fifo_at_the_instructions_name_never_hangs(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "ENVIRONMENT.md")
    os.mkfifo(tmp_path / ".alkera-environment.json")
    result: list[str] = []
    reader = threading.Thread(
        target=lambda: result.append(render_environment_instructions(tmp_path, tools=True)),
        daemon=True,
    )
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():
        # Unwedge the reader so the test process can exit, then fail.
        for name in ("ENVIRONMENT.md", ".alkera-environment.json"):
            with open(tmp_path / name, "wb", buffering=0):
                pass
        pytest.fail("reading a FIFO hung the chat's start")
    assert result == [""]


DETACH = (
    "import subprocess, sys; "
    "p = subprocess.Popen(sys.argv[1:], start_new_session=True); "
    "sys.exit(p.wait())"
)


async def test_a_cancel_stops_the_command_inside_the_sandbox(tmp_path: Path) -> None:
    # A launch like runsc exec: the command runs in a session of its own, so
    # killing the host side's process group leaves it running.
    launch = SandboxLaunch(mode="none", prefix=(sys.executable, "-c", DETACH))
    runner = SandboxRunner(
        sandbox=launch, cwd=str(tmp_path), env=dict(os.environ), spill=lambda: tmp_path / "s"
    )
    pidfile = tmp_path / "child"
    task = asyncio.ensure_future(
        runner.run(Command(argv=("sh", "-c", f'sleep 300 & echo $! > "{pidfile}"; wait')))
    )
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    child = int(pidfile.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    from alkera_core.process import process_alive

    for _ in range(100):
        if not process_alive(child):
            break
        await asyncio.sleep(0.05)
    assert not process_alive(child), "the command kept running in the sandbox"
