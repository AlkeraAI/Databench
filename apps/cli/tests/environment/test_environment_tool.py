"""``environment.capture`` / ``environment.recreate`` through ``ToolRegistry
.dispatch`` (the path both adapters use), and the sandbox runner they use in a
cloud chat: every command, file and write goes through the chat's own launch."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import pytest
from _helpers.pyenv import UV, build_wheel, editable_project, make_venv, offline_env, uv_install
from alkera_cli.cloud.fence import SessionFence
from alkera_cli.environment import SPEC_FILENAME, Command, read_spec
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.harness.sandbox import NO_SANDBOX
from alkera_cli.harness.session_launches import reset_launches_for_tests
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.agent_tree import SpillTarget
from alkera_cli.plugins.plugin_base.bash_exec import run_command
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.environment_tool import SandboxRunner, sandbox_script
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only shell tools")
needs_uv = pytest.mark.skipif(UV is None, reason="needs uv on PATH")


class _Broker:
    def __init__(self, option: str = "allow_once") -> None:
        self.option = option
        self.prompts = 0

    async def resolve(self, request: Any) -> str:
        self.prompts += 1
        return self.option


class _Selective:
    """Answers each ask by what it asks about: refuses the ones whose text
    holds ``refuse``, allows the rest, and runs ``on_ask`` first."""

    def __init__(self, refuse: str, on_ask: Any = None) -> None:
        self.refuse = refuse
        self.on_ask = on_ask
        self.asked: list[str] = []

    async def resolve(self, request: Any) -> str:
        text = " ".join(request.patterns or []) + " " + json.dumps(request.subject, default=str)
        self.asked.append(text)
        if self.on_ask is not None:
            self.on_ask(text)
        return "reject_once" if self.refuse and self.refuse in text else "allow_once"


def _runner(tmp_path: Path) -> SandboxRunner:
    return SandboxRunner(
        sandbox=NO_SANDBOX,
        cwd=str(tmp_path),
        env=dict(os.environ),
        spill=lambda: SpillTarget(ChatTree(tmp_path), "spill.txt"),
    )


async def test_run_command_feeds_stdin_and_closes_it(tmp_path: Path) -> None:
    ex = await run_command(
        "cat; echo end",
        cwd=str(tmp_path),
        env=dict(os.environ),
        shell="/bin/sh",
        make_spill=lambda: SpillTarget(ChatTree(tmp_path), "s"),
        stdin=b"from stdin\n",
    )
    assert ex.output == "from stdin\nend\n"


async def test_run_command_without_stdin_reads_empty_input(tmp_path: Path) -> None:
    ex = await run_command(
        "cat; echo end",
        cwd=str(tmp_path),
        env=dict(os.environ),
        shell="/bin/sh",
        make_spill=lambda: SpillTarget(ChatTree(tmp_path), "s"),
    )
    assert ex.output == "end\n"


async def test_the_sandbox_runner_hands_files_over_by_content(tmp_path: Path) -> None:
    content = 'it\'s "quoted" $HOME `x`\nline 2\n'
    command = Command(
        argv=("sh", "-c", 'cat "$1"; printf "[%s]" "$2"', "sh", "{data}", "--flag={data}"),
        files={"data": content},
    )
    result = await _runner(tmp_path).run(command)
    assert result.exit_code == 0
    shown, flag = result.output.rsplit("[", 1)
    assert shown == content
    # A placeholder inside a word names the same unpacked file.
    assert flag.startswith("--flag=/tmp/alkera-env.") and flag.endswith("/data]")


async def test_the_sandbox_runner_removes_its_files_and_keeps_the_exit_code(tmp_path: Path) -> None:
    command = Command(argv=("sh", "-c", 'echo "$1"; exit 7', "sh", "{f}"), files={"f": "x"})
    result = await _runner(tmp_path).run(command)
    assert result.exit_code == 7
    assert not await asyncio.to_thread(Path(result.output.strip()).exists)


async def test_the_sandbox_runner_sets_env_and_cwd(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    command = Command(
        argv=("sh", "-c", 'echo "$X $(pwd)"'), env={"X": "a b"}, cwd=str(tmp_path / "sub")
    )
    result = await _runner(tmp_path).run(command)
    assert (
        result.output.strip() == f"a b {(tmp_path / 'sub').resolve()}"
        or result.output.strip() == f"a b {tmp_path / 'sub'}"
    )


async def test_the_sandbox_runner_resolves_programs_where_it_runs(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    assert await runner.which("sh") is not None
    assert await runner.which("definitely-not-a-program-xyz") is None


def test_an_unknown_placeholder_is_refused_not_run() -> None:
    with pytest.raises(KeyError):
        sandbox_script(Command(argv=("cat", "{missing}"), files={"other": "x"}))


def test_a_command_without_files_reads_nothing_from_stdin() -> None:
    assert "tar" not in sandbox_script(Command(argv=("true",)))


# --- the tools --------------------------------------------------------------


def _registry(alkera: Path) -> ToolRegistry:
    project = ProjectDirectory(alkera)
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)
    return registry


def test_the_tools_ride_with_bash(tmp_path: Path) -> None:
    registry = _registry(tmp_path / ".alkera")
    assert registry.tool_for("environment.capture") is not None
    assert registry.tool_for("environment.recreate") is not None


class LocalWorkspace:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "ws"
        self.root.mkdir()
        self.env = offline_env(tmp_path)
        wheels = tmp_path / "wheels"
        build_wheel(wheels, "alpha", "1.0")
        editable_project(self.root / "libs" / "mylib", "mylib")
        (self.root / "requirements.txt").write_text(
            f"--find-links {wheels.as_posix()}\nalpha==1.0\n", encoding="utf-8"
        )
        python = make_venv(self.root / ".venv", self.env)
        uv_install(python, self.env, "-r", str(self.root / "requirements.txt"))
        uv_install(python, self.env, "-e", str(self.root / "libs" / "mylib"))
        self.registry = _registry(self.root / ".alkera")

    async def call(self, name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        kw.setdefault("broker", _Broker())
        kw.setdefault("permission_mode", "default")
        return await self.registry.dispatch(name, args, alkera_dir=self.root / ".alkera", **kw)


@needs_uv
def test_capture_in_a_local_session_writes_the_spec_from_the_projects_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = LocalWorkspace(tmp_path)
    for key in [k for k in os.environ if k.startswith(("UV_", "PIP_"))]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("UV_OFFLINE", "1")

    out = asyncio.run(ws.call("environment.capture", {}))

    assert out["editable"] == ["mylib (libs/mylib)"]
    spec = read_spec(ws.root)
    assert spec is not None and spec.package("alpha").version == "1.0"  # type: ignore[union-attr]


@needs_uv
def test_capture_refused_by_the_person_writes_nothing(tmp_path: Path) -> None:
    ws = LocalWorkspace(tmp_path)
    broker = _Broker("reject_once")

    out = asyncio.run(ws.call("environment.capture", {}, broker=broker))

    assert "error" in out
    assert broker.prompts == 1
    assert not (ws.root / SPEC_FILENAME).exists()


@needs_uv
def test_recreate_in_a_local_session_needs_approval_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = LocalWorkspace(tmp_path)
    monkeypatch.setenv("UV_OFFLINE", "1")
    asyncio.run(ws.call("environment.capture", {}))
    target = tmp_path / "clone"

    planned = asyncio.run(
        ws.call("environment.recreate", {"dry_run": True, "env_path": str(target)})
    )
    assert planned["steps"][0]["purpose"] == "create_env"
    assert not target.exists()

    broker = _Selective(refuse=" venv ")
    refused = asyncio.run(ws.call("environment.recreate", {"env_path": str(target)}, broker=broker))
    assert "error" in refused
    assert any(" venv " in a for a in broker.asked)
    assert not target.exists()


@needs_uv
def test_a_local_capture_asks_before_it_runs_the_environments_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = LocalWorkspace(tmp_path)
    monkeypatch.setenv("UV_OFFLINE", "1")
    broker = _Selective(refuse=" -I ")

    out = asyncio.run(ws.call("environment.capture", {}, broker=broker))

    assert "error" in out
    assert not (ws.root / SPEC_FILENAME).exists()


@needs_uv
def test_what_runs_is_the_plan_that_was_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = LocalWorkspace(tmp_path)
    monkeypatch.setenv("UV_OFFLINE", "1")
    asyncio.run(ws.call("environment.capture", {}))
    spec_path = ws.root / SPEC_FILENAME

    def swap_spec(text: str) -> None:
        # While the person reads the approval, the spec changes on disk.
        if " venv " in text:
            data = json.loads(spec_path.read_text(encoding="utf-8"))
            data["packages"].append({"name": "late-addition", "version": "9.9"})
            spec_path.write_text(json.dumps(data), encoding="utf-8")

    broker = _Selective(refuse="", on_ask=swap_spec)
    target = tmp_path / "clone"
    out = asyncio.run(ws.call("environment.recreate", {"env_path": str(target)}, broker=broker))

    assert "error" not in out, out
    ran = " ".join(f"{s['command']} {json.dumps(s['inputs'])}" for s in out["steps"])
    assert "late-addition" not in ran
    assert out["not_recreated"] == []


def test_recreate_without_a_spec_says_to_capture_first(tmp_path: Path) -> None:
    out = asyncio.run(
        _registry(tmp_path / ".alkera").dispatch(
            "environment.recreate",
            {"dry_run": True},
            broker=_Broker(),
            alkera_dir=tmp_path / ".alkera",
        )
    )
    assert "environment.capture" in out["error"]


class FencedChat:
    """A cloud chat on a box with no sandbox controls (the runner still goes
    through the chat's launch, which here is the bare one)."""

    def __init__(self, tmp_path: Path) -> None:
        reset_launches_for_tests()
        self.alkera = tmp_path / "box" / ".alkera"
        self.folder = self.alkera / "chats" / "chat-a"
        self.working = self.folder / "scratch"
        self.envs = self.folder / ".runtime" / "envs"
        self.working.mkdir(parents=True)
        self.envs.mkdir(parents=True)
        self.fence = SessionFence(
            root=tmp_path / "box",
            folder=self.folder,
            working_dir=self.working,
            own_trees=(self.envs,),
        )
        self.registry = _registry(self.alkera)

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return await self.registry.dispatch(
            name,
            args,
            broker=_Broker(),
            permission_mode="default",
            alkera_dir=self.alkera,
            fence=self.fence,
            session_id="chat-a",
            sandbox_dir=self.working,
        )


def test_a_cloud_capture_writes_the_spec_through_the_chats_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = FencedChat(tmp_path)
    (chat.working / "requirements.txt").write_text("alpha==1.0\n", encoding="utf-8")
    # The sandbox's python3 is the chat's active environment; here, this one.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    out = asyncio.run(chat.call("environment.capture", {}))

    assert "error" not in out, out
    written = json.loads((chat.working / SPEC_FILENAME).read_text(encoding="utf-8"))
    assert [f["path"] for f in written["project_files"]] == ["requirements.txt"]
    assert written["python"]["version"] == platform.python_version()
    # Nothing was left behind in the synced folder but the spec itself.
    assert sorted(p.name for p in chat.working.iterdir()) == sorted(
        [
            SPEC_FILENAME,
            "requirements.txt",
            *(["tool-output"] if (chat.working / "tool-output").exists() else []),
        ]
    )


def test_a_cloud_recreate_outside_the_chats_own_trees_is_refused(tmp_path: Path) -> None:
    chat = FencedChat(tmp_path)
    (chat.working / SPEC_FILENAME).write_text('{"schema_version": "1.0.0"}', encoding="utf-8")

    outside = asyncio.run(
        chat.call("environment.recreate", {"dry_run": True, "env_path": str(chat.working / "venv")})
    )
    escape = asyncio.run(
        chat.call(
            "environment.recreate",
            {"dry_run": True, "env_path": str(chat.envs / ".." / ".." / "x")},
        )
    )

    assert "own environment folders" in outside["error"]
    assert "own environment folders" in escape["error"]


def _started(child: Path, pid: str) -> bool:
    return child.exists() and bool(child.read_text().strip()) and Path(pid).exists()


def _forget_token_files(pid: str) -> None:
    """The sandbox's files of one command (its pid file and its cancel
    record), which these tests write in this host's ``/tmp``."""
    for leftover in (pid, pid.removesuffix(".pid") + ".cancel"):
        Path(leftover).unlink(missing_ok=True)


async def _sh(script: str, tmp_path: Path) -> Any:
    return await run_command(
        script,
        cwd=str(tmp_path),
        env=dict(os.environ),
        shell="/bin/sh",
        make_spill=lambda: SpillTarget(ChatTree(tmp_path), "s"),
        timeout_ms=60_000,
    )


async def test_a_cancel_that_lands_before_the_command_names_its_group_still_stops_it(
    tmp_path: Path,
) -> None:
    """A cancel can reach the sandbox before the command has written the id of
    its process group: there is nothing to kill yet, and an install that
    starts a moment later would run on, unowned. The cancel is recorded, and
    the command reads it and stops instead of running."""
    import uuid

    from alkera_cli.plugins.plugin_base.environment_tool import kill_script, pid_file

    token = uuid.uuid4().hex
    marker = tmp_path / "ran"
    try:
        await _sh(kill_script(token), tmp_path)
        command = Command(argv=("sh", "-c", f'echo ran > "{marker}"; exec sleep 30'))
        ex = await _sh(sandbox_script(command, token=token), tmp_path)

        assert ex.exit_code != 0
        assert not marker.exists(), "the command ran although it was cancelled"
    finally:
        await asyncio.to_thread(_forget_token_files, pid_file(token))


async def test_a_cancel_of_a_running_command_kills_its_whole_group(tmp_path: Path) -> None:
    import uuid

    from alkera_cli.plugins.plugin_base.environment_tool import kill_script, pid_file

    token = uuid.uuid4().hex
    child = tmp_path / "child.pid"
    command = Command(argv=("sh", "-c", f'sleep 30 & echo $! > "{child}"; wait'))
    try:
        running = asyncio.ensure_future(_sh(sandbox_script(command, token=token), tmp_path))
        for _ in range(200):
            if await asyncio.to_thread(_started, child, pid_file(token)):
                break
            await asyncio.sleep(0.02)
        await _sh(kill_script(token), tmp_path)
        await asyncio.wait_for(running, timeout=20)
        from alkera_core.process import process_alive

        pid = int(child.read_text())
        for _ in range(100):
            if not process_alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not process_alive(pid), "the command's child outlived the cancel"
    finally:
        await asyncio.to_thread(_forget_token_files, pid_file(token))
