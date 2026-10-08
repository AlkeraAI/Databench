"""A chat that wakes with a fresh environment gets it back from the workspace's
spec, or is told plainly that it was reset; a failed restore is reported.
The restore runs through the chat's own launch (here the bare one, on a host
with no sandbox controls), never in-process."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from _helpers.pyenv import UV, build_wheel, make_venv, offline_env, run_py, uv_install
from alkera_cli.cloud.fence import SessionFence
from alkera_cli.environment import render_spec, write_spec
from alkera_cli.environment.recreate import RecreateReport
from alkera_cli.environment.service import EnvironmentService
from alkera_cli.environment.spec import EnvironmentSpec, IndexRef, PackageSpec, PythonInfo
from alkera_cli.environment.wake import (
    OFFERED_NOTICE,
    RESET_NOTICE,
    RESTORED_NOTICE,
    RESTORING_NOTICE,
    EnvironmentWakes,
    environment_is_empty,
)
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.session_launches import reset_launches_for_tests
from alkera_cli.plugins.plugin_base.environment_tool import chat_launch
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the box's chat launch is POSIX-only")
needs_uv = pytest.mark.skipif(UV is None, reason="needs uv on PATH")


# --- the registry -------------------------------------------------------------


def _report(*missing: str) -> RecreateReport:
    data: dict[str, Any] = {"target_env": "/e", "dry_run": False, "already_satisfied": False}
    data["not_recreated"] = [{"kind": "not_installed", "name": m} for m in missing]
    return RecreateReport.model_validate(data)


async def test_restoring_is_said_every_turn_then_the_outcome_once() -> None:
    wakes = EnvironmentWakes()
    gate = asyncio.Event()

    async def _restore() -> RecreateReport:
        await gate.wait()
        return _report()

    wakes.begin("s", _restore)
    assert wakes.notice("s") == RESTORING_NOTICE
    assert wakes.notice("s") == RESTORING_NOTICE
    gate.set()
    await wakes.wait("s")
    assert wakes.notice("s") == RESTORED_NOTICE
    assert wakes.notice("s") is None


@pytest.mark.parametrize(
    "outcome,detail",
    [
        pytest.param("missing", "not recreated: alpha", id="packages-missing"),
        pytest.param("raises", "uv is broken", id="restore-raised"),
    ],
)
async def test_a_failed_restore_is_reported_once_with_why(outcome: str, detail: str) -> None:
    wakes = EnvironmentWakes()

    async def _restore() -> RecreateReport:
        if outcome == "raises":
            raise RuntimeError("uv is broken")
        return _report("alpha")

    wakes.begin("s", _restore)
    await wakes.wait("s")
    notice = wakes.notice("s")
    assert notice is not None and detail in notice and "not be installed" in notice
    assert wakes.notice("s") is None


def test_a_reset_is_said_once() -> None:
    wakes = EnvironmentWakes()
    wakes.mark_reset("s")
    assert wakes.notice("s") == RESET_NOTICE
    assert wakes.notice("s") is None
    assert wakes.notice("other") is None


# --- what counts as empty -----------------------------------------------------


def _site(env: Path, *dists: str) -> Path:
    site = env / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    for dist in dists:
        (site / f"{dist}.dist-info").mkdir()
    return site


@pytest.mark.parametrize(
    "dists,empty",
    [
        pytest.param(None, True, id="missing"),
        pytest.param((), True, id="no-packages"),
        pytest.param(("pip-25.0", "setuptools-80.1", "wheel-0.45"), True, id="seed-only"),
        pytest.param(("pip-25.0", "pandas-2.2.3"), False, id="has-a-package"),
        pytest.param(("my_lib-0.1",), False, id="underscored-name"),
    ],
)
def test_environment_is_empty(tmp_path: Path, dists: tuple[str, ...] | None, empty: bool) -> None:
    env = tmp_path / "env"
    if dists is not None:
        _site(env, *dists)
    assert environment_is_empty(env) is empty


def test_a_site_packages_linked_elsewhere_is_not_read(tmp_path: Path) -> None:
    elsewhere = _site(tmp_path / "other", "pandas-2.2.3")
    lib = tmp_path / "env" / "lib" / "python3.12"
    lib.mkdir(parents=True)
    os.symlink(elsewhere, lib / "site-packages")
    assert environment_is_empty(tmp_path / "env") is True


# --- a woken chat -------------------------------------------------------------


class WokenChat:
    """A cloud chat's tree on a box: its folder, and the runtime state where
    the agent's start made a fresh, empty default environment."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_launches_for_tests()
        self.env_vars = offline_env(tmp_path)
        for key in [k for k in os.environ if k.startswith(("UV_", "PIP_"))]:
            monkeypatch.delenv(key)
        for key, value in self.env_vars.items():
            if key.startswith(("UV_", "PIP_")):
                monkeypatch.setenv(key, value)
        self.alkera = tmp_path / "box" / ".alkera"
        self.folder = self.alkera / "chats" / "chat-a"
        self.working = self.folder / "scratch"
        self.working.mkdir(parents=True)
        self.env = self.folder / ".runtime" / "envs" / "alkera"
        self.python = make_venv(self.env, self.env_vars)
        self.wheels = tmp_path / "wheels"
        build_wheel(self.wheels, "alpha", "1.0")
        self.fence = SessionFence(
            root=tmp_path / "box",
            folder=self.folder,
            working_dir=self.working,
            own_trees=(self.env.parent,),
        )
        self.service = EnvironmentService()

    def spec(self, version: str = "1.0", *, captured_here: bool = True) -> None:
        spec = EnvironmentSpec(
            env_kind="venv",
            python=PythonInfo(version=f"{sys.version_info[0]}.{sys.version_info[1]}.0"),
            packages=[PackageSpec(name="alpha", version=version, requested=True)],
            indexes=[IndexRef(url=self.wheels.as_posix(), kind="find_links")],
        )
        write_spec(self.working, spec)
        if captured_here:
            self.service.record_capture(self.alkera, "chat-a", render_spec(spec))

    async def wake(self, *, resumed: bool = True) -> None:
        await self.service.start(
            session_id="chat-a",
            fence=self.fence,
            alkera_dir=self.alkera,
            resumed=resumed,
            launch_for=chat_launch,
        )
        await self.service.wait("chat-a")

    def notice(self) -> str | None:
        return self.service.notice("chat-a")

    def state(self) -> str | None:
        return self.service.wakes.state("chat-a")


@needs_uv
async def test_a_woken_chat_with_a_spec_gets_its_packages_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    chat.spec()

    await chat.wake()

    assert chat.notice() == RESTORED_NOTICE
    out = await asyncio.to_thread(
        run_py, chat.python, "import alpha; print(alpha.VERSION)", chat.env_vars
    )
    assert out == "1.0"


@needs_uv
async def test_a_woken_chat_without_a_spec_is_told_it_was_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    await chat.wake()
    assert chat.notice() == RESET_NOTICE


@needs_uv
async def test_a_spec_this_chat_did_not_capture_is_offered_never_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    chat.spec(captured_here=False)

    await chat.wake()

    assert chat.notice() == OFFERED_NOTICE
    probe = "import importlib.util as u; print(u.find_spec('alpha') is None)"
    assert await asyncio.to_thread(run_py, chat.python, probe, chat.env_vars) == "True"


@needs_uv
async def test_a_spec_edited_after_its_capture_is_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    chat.spec()
    spec_file = chat.working / ".alkera-environment.json"
    spec_file.write_text(
        spec_file.read_text(encoding="utf-8").replace("1.0", "1.1"), encoding="utf-8"
    )

    await chat.wake()

    assert chat.notice() == OFFERED_NOTICE


@needs_uv
async def test_a_first_start_without_a_spec_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    await chat.wake(resumed=False)
    assert chat.notice() is None


@needs_uv
async def test_a_restore_that_cannot_install_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    chat.spec(version="9.9")

    await chat.wake()

    notice = chat.notice()
    assert notice is not None and "did not finish" in notice and "alpha" in notice


@needs_uv
async def test_an_environment_that_kept_its_packages_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    uv_install(chat.python, chat.env_vars, "--find-links", str(chat.wheels), "alpha==1.0")
    chat.spec(version="9.9")

    await chat.wake()

    assert chat.state() is None


async def test_local_sessions_and_subagents_never_wake_an_environment(tmp_path: Path) -> None:
    def _chat(parent: str | None) -> Any:
        manifest = SimpleNamespace(
            parent_session_id=parent,
            last_message_preview="hi",
            tokens_total=SimpleNamespace(input=1),
        )
        return SimpleNamespace(session_id="x", manifest=manifest)

    service = EnvironmentService()
    registry = SimpleNamespace(chat_launch_factory=lambda *a: pytest.fail("launched"))
    # Neither reaches the launch: a fence-less or child chat returns first.
    await service.start_session(_chat(None), None, tmp_path, registry)
    await service.start_session(
        _chat("root"), SimpleNamespace(session=object()), tmp_path, registry
    )
    assert registry.environment is service


async def test_the_turn_carries_the_wake_notice(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    rt = HarnessRuntime(project, adapter_factory=factory)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    try:
        rt.environment.wakes.mark_reset(sid)
        await session.send_prompt("first")
        await session.send_prompt("second")
        sent = factory.adapters[-1].sent_prompts
        assert RESET_NOTICE in (sent[0].system or "")
        assert RESET_NOTICE not in (sent[1].system or "")
    finally:
        await rt.close_chat(sid)


@needs_uv
async def test_the_runtime_service_wakes_a_fenced_root_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    manifest = SimpleNamespace(
        parent_session_id=None, last_message_preview="hi", tokens_total=SimpleNamespace(input=5)
    )
    started = SimpleNamespace(session_id="chat-a", manifest=manifest)
    registry = SimpleNamespace(chat_launch_factory=chat_launch)
    await chat.service.start_session(
        started, SimpleNamespace(session=chat.fence), chat.alkera, registry
    )
    assert chat.notice() == RESET_NOTICE


async def _alive(pid: int) -> bool:
    from alkera_core.process import process_alive

    for _ in range(50):
        if not process_alive(pid):
            return False
        await asyncio.sleep(0.05)
    return True


async def test_cancelling_a_wake_kills_its_install_and_the_next_wake_starts_again(
    tmp_path: Path,
) -> None:
    from alkera_cli.environment import Command, LocalRunner

    wakes = EnvironmentWakes()
    pidfile = tmp_path / "pid"

    async def _restore() -> RecreateReport:
        await LocalRunner().run(
            Command(argv=("sh", "-c", f'echo $$ > "{pidfile}"; exec sleep 30'), timeout_s=60)
        )
        return _report()

    wakes.begin("s", _restore)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text())

    await wakes.cancel("s")

    assert not await _alive(pid)
    assert wakes.notice("s") is None
    wakes.begin("s", _restore)
    assert wakes.notice("s") == RESTORING_NOTICE
    await wakes.cancel("s")


async def test_closing_the_session_stops_its_restore(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    rt = HarnessRuntime(project, adapter_factory=factory)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    await rt.open_chat(sid)
    stopped = asyncio.Event()

    async def _restore() -> RecreateReport:
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()
        return _report()

    rt.environment.wakes.begin(sid, _restore)
    await asyncio.sleep(0)
    await rt.close_chat(sid)

    assert stopped.is_set()
    assert rt.environment.wakes.state(sid) is None


async def test_every_tool_view_of_the_runtime_carries_its_environment_service(
    tmp_path: Path,
) -> None:
    # A session's tool view is rebuilt whenever the plugins change (an agent
    # writing files does that), so the service rides the shared build, not
    # one view.
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory()
    )
    first = await rt.tool_registry()
    again = await rt.tool_registry(chat_id="other")
    assert first.environment is rt.environment
    assert again.environment is rt.environment


# --- one restore per shared environment ---------------------------------------


async def test_two_sessions_restoring_one_environment_run_one_restore() -> None:
    """Two wakes into one environment join one restore into it, and both read
    its one outcome."""
    wakes = EnvironmentWakes()
    gate = asyncio.Event()
    runs: list[str] = []

    async def _restore() -> RecreateReport:
        runs.append("run")
        await gate.wait()
        return _report()

    wakes.begin("a", _restore, target="/ws/env")
    assert wakes.restoring("/ws/env")
    wakes.begin("b", _restore, target="/ws/env")
    wakes.begin("elsewhere", _restore, target="/other/env")
    gate.set()
    for session in ("a", "b", "elsewhere"):
        await wakes.wait(session)

    assert runs == ["run", "run"], "one restore per environment, not one per chat"
    assert wakes.notice("a") == RESTORED_NOTICE
    assert wakes.notice("b") == RESTORED_NOTICE
    assert not wakes.restoring("/ws/env")


async def test_a_shared_restore_stops_only_when_nobody_waits_on_it() -> None:
    wakes = EnvironmentWakes()
    stopped = asyncio.Event()

    async def _restore() -> RecreateReport:
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()
        return _report()

    wakes.begin("a", _restore, target="/ws/env")
    wakes.begin("b", None, target="/ws/env")
    await asyncio.sleep(0)

    await wakes.cancel("a")
    assert not stopped.is_set(), "the other chat still waits on the install"
    assert wakes.state("b") == "restoring"

    await wakes.cancel("b")
    assert stopped.is_set()
    assert not wakes.restoring("/ws/env")


def test_joining_with_nothing_to_join_is_refused() -> None:
    with pytest.raises(ValueError):
        EnvironmentWakes().begin("b", None, target="/ws/env")


# --- what a wake restores, and when -------------------------------------------


class _Recording:
    """A chat's real runner, with the one step under test (the build of the
    workspace's editables, which writes into the shared tree) replaced by a
    stand-in that records when it runs and holds the tree for a while."""

    def __init__(self, inner: Any, session: str, log: list[tuple[str, str]]) -> None:
        self._inner = inner
        self._session = session
        self._log = log

    async def which(self, name: str) -> str | None:
        return await self._inner.which(name)  # type: ignore[no-any-return]

    async def run(self, command: Any) -> Any:
        from alkera_cli.environment import CommandResult

        if "{local}" not in command.argv:
            return await self._inner.run(command)
        self._log.append(("start", self._session))
        # Long enough that a second build started meanwhile is seen inside it.
        for _ in range(40):
            if sum(1 for e, _s in self._log if e == "start") > 1:
                break
            await asyncio.sleep(0.05)
        self._log.append(("end", self._session))
        return CommandResult(exit_code=0, output="")


def _overlapped(log: list[tuple[str, str]]) -> bool:
    inside = 0
    for event, _session in log:
        inside += 1 if event == "start" else -1
        if inside > 1:
            return True
    return False


@needs_uv
async def test_two_chats_of_a_workspace_never_build_into_its_tree_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each chat has its own environment (the real launch places it in the
    chat's own runtime state), so two chats of one workspace waking together
    run two restores. What they share is the workspace tree, where an
    editable's build writes: those builds take turns."""
    reset_launches_for_tests()
    env_vars = offline_env(tmp_path)
    for key in [k for k in os.environ if k.startswith(("UV_", "PIP_"))]:
        monkeypatch.delenv(key)
    for key, value in env_vars.items():
        if key.startswith(("UV_", "PIP_")):
            monkeypatch.setenv(key, value)
    alkera = tmp_path / "box" / ".alkera"
    working = tmp_path / "box" / "workspace"
    working.mkdir(parents=True)
    pyproject = b'[project]\nname = "app"\nversion = "0.1"\n'
    (working / "pyproject.toml").write_bytes(pyproject)
    import hashlib

    from alkera_cli.environment.spec import ProjectFile

    spec = EnvironmentSpec(
        env_kind="venv",
        python=PythonInfo(version=f"{sys.version_info[0]}.{sys.version_info[1]}.0"),
        packages=[PackageSpec(name="app", version="0.1", source="editable", path=".")],
        project_files=[
            ProjectFile(
                path="pyproject.toml",
                kind="pyproject",
                sha256=hashlib.sha256(pyproject).hexdigest(),
            )
        ],
    )
    write_spec(working, spec)
    service = EnvironmentService()
    log: list[tuple[str, str]] = []
    fences: dict[str, SessionFence] = {}
    envs: dict[str, Path] = {}
    for session in ("chat-a", "chat-b"):
        service.record_capture(alkera, session, render_spec(spec))
        folder = alkera / "chats" / session
        envs[session] = folder / ".runtime" / "envs" / "alkera"
        make_venv(envs[session], env_vars)
        fences[session] = SessionFence(
            root=tmp_path / "box",
            folder=working,
            working_dir=working,
            own_trees=(envs[session].parent,),
        )

    def _launch(session: str, fence: Any, alkera_dir: Path) -> Any:
        real = chat_launch(session, fence, alkera_dir)
        assert real is not None
        assert real.default_env == envs[session]
        return type(real)(
            runner=_Recording(real.runner, session, log),
            default_env=real.default_env,
            python=real.python,
        )

    for session in ("chat-a", "chat-b"):
        await service.start(
            session_id=session,
            fence=fences[session],
            alkera_dir=alkera,
            resumed=True,
            launch_for=_launch,
        )
    await service.wait("chat-a")
    await service.wait("chat-b")

    assert [e for e, _s in log].count("start") == 2, "each chat restores its own environment"
    assert not _overlapped(log), f"two builds ran in one tree at once: {log}"


@needs_uv
async def test_the_restore_installs_the_spec_whose_digest_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provenance check and the install read the spec once: a spec
    swapped in after the text whose digest matched is never what installs."""
    from alkera_cli.environment import service as service_module

    chat = WokenChat(tmp_path, monkeypatch)
    chat.spec()
    checked = (chat.working / ".alkera-environment.json").read_text(encoding="utf-8")
    # What is on disk by the time anything reads it again: a spec this chat
    # never captured, asking for a version nobody can install.
    chat.spec(version="9.9", captured_here=False)
    monkeypatch.setattr(service_module, "read_workspace_text", lambda *_a, **_k: checked)

    await chat.wake()

    assert chat.notice() == RESTORED_NOTICE
    out = await asyncio.to_thread(
        run_py, chat.python, "import alpha; print(alpha.VERSION)", chat.env_vars
    )
    assert out == "1.0"


@needs_uv
async def test_a_restore_cut_off_midway_runs_again_at_the_next_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore stopped mid-install (the chat slept, the box went away)
    leaves a half-filled environment. The next wake must not take it for a
    whole one: it restores again, and the agent gets its packages."""
    chat = WokenChat(tmp_path, monkeypatch)
    build_wheel(chat.wheels, "beta", "1.0")
    chat.spec()
    started = asyncio.Event()

    class _Hangs:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def which(self, name: str) -> str | None:
            return await self._inner.which(name)  # type: ignore[no-any-return]

        async def run(self, command: Any) -> Any:
            if "{requirements}" in command.argv:
                started.set()
                await asyncio.sleep(3600)
            return await self._inner.run(command)

    def _hanging_launch(session: str, fence: Any, alkera_dir: Path) -> Any:
        real = chat_launch(session, fence, alkera_dir)
        assert real is not None
        return type(real)(
            runner=_Hangs(real.runner), default_env=real.default_env, python=real.python
        )

    await chat.service.start(
        session_id="chat-a",
        fence=chat.fence,
        alkera_dir=chat.alkera,
        resumed=True,
        launch_for=_hanging_launch,
    )
    await asyncio.wait_for(started.wait(), timeout=60)
    await chat.service.close("chat-a")
    # Half of what the restore would have put there arrived before it stopped.
    await asyncio.to_thread(
        uv_install, chat.python, chat.env_vars, "--find-links", str(chat.wheels), "beta==1.0"
    )
    assert not environment_is_empty(chat.env)

    await chat.wake()

    assert chat.notice() == RESTORED_NOTICE
    out = await asyncio.to_thread(
        run_py, chat.python, "import alpha; print(alpha.VERSION)", chat.env_vars
    )
    assert out == "1.0"
    # And once a restore ran to its end, a later wake leaves the environment be.
    chat.service = EnvironmentService()
    await chat.wake()
    assert chat.state() is None


@needs_uv
async def test_with_restores_off_a_captured_spec_is_offered_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = WokenChat(tmp_path, monkeypatch)
    chat.service = EnvironmentService(auto_restore=False)
    chat.spec()

    await chat.wake()

    assert chat.notice() == OFFERED_NOTICE
    probe = "import importlib.util as u; print(u.find_spec('alpha') is None)"
    assert await asyncio.to_thread(run_py, chat.python, probe, chat.env_vars) == "True"


@pytest.mark.parametrize(
    ("value", "on"),
    [
        pytest.param(None, True, id="unset-is-on"),
        pytest.param("1", True, id="one"),
        pytest.param("0", False, id="zero"),
        pytest.param("off", False, id="off"),
        pytest.param(" False ", False, id="false-any-case"),
        pytest.param("nope", True, id="anything-else-is-on"),
    ],
)
def test_the_restore_switch_is_read_from_the_box_environment(value: str | None, on: bool) -> None:
    from alkera_cli.environment.service import ENV_RESTORE, auto_restore_from_env

    assert auto_restore_from_env({} if value is None else {ENV_RESTORE: value}) is on


@needs_uv
async def test_an_unattended_restore_skips_a_requirements_file_edited_after_the_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project-only spec restores from requirements.txt. Edited since the
    capture (anyone who can write the workspace can), it is not run at the
    wake: the agent is told it did not finish, and nothing new is installed."""
    import hashlib

    from alkera_cli.environment.spec import ProjectFile

    chat = WokenChat(tmp_path, monkeypatch)
    captured = f"--find-links {chat.wheels.as_posix()}\n".encode()
    (chat.working / "requirements.txt").write_bytes(captured)
    spec = EnvironmentSpec(
        env_kind="venv",
        python=PythonInfo(version=f"{sys.version_info[0]}.{sys.version_info[1]}.0"),
        project_files=[
            ProjectFile(
                path="requirements.txt",
                kind="requirements",
                sha256=hashlib.sha256(captured).hexdigest(),
            )
        ],
    )
    write_spec(chat.working, spec)
    chat.service.record_capture(chat.alkera, "chat-a", render_spec(spec))
    (chat.working / "requirements.txt").write_bytes(captured + b"alpha==1.0\n")

    await chat.wake()

    notice = chat.notice()
    assert notice is not None and "requirements.txt" in notice
    probe = "import importlib.util as u; print(u.find_spec('alpha') is None)"
    assert await asyncio.to_thread(run_py, chat.python, probe, chat.env_vars) == "True"


async def test_a_finished_restore_is_not_kept_once_its_sessions_are_gone() -> None:
    """A box serves chats for weeks: a restore that finished, whose sessions
    have since gone, must not be held (with its whole report) for ever."""
    import gc
    import weakref

    wakes = EnvironmentWakes()
    reports: list[weakref.ref[RecreateReport]] = []

    async def _restore() -> RecreateReport:
        report = _report()
        reports.append(weakref.ref(report))
        return report

    for round_ in range(3):
        wakes.begin(f"s{round_}", _restore, target=f"/env/{round_}")
        await wakes.wait(f"s{round_}")
        wakes.forget(f"s{round_}")
    await asyncio.sleep(0)
    gc.collect()

    assert len(reports) == 3
    assert [ref() for ref in reports] == [None, None, None]
