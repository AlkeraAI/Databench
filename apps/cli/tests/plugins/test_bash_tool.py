"""The parent-hosted ``bash`` tool, driven through ``ToolRegistry.dispatch`` (the
path both adapters use).

Pins the foreground run, the backgrounded run (stub → native ``BashResult``), the
workdir confinement, and that the tool actually wires the security gate (a
sensitive read is refused). The executor + the gate are exhausted in
test_bash_exec / test_bash_gate.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.background import BackgroundJobRegistry
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
from alkera_cli.plugins.plugin_base.permissions import CREDENTIAL_PATH_GATE_ENV
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.permissions.refusal_words import NOT_GRANTED
from alkera_core.project.directory import ProjectDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")


class _Broker:
    def __init__(self, option: str) -> None:
        self._option = option
        self.prompts = 0

    async def resolve(self, request: Any) -> str:
        self.prompts += 1
        return self._option


def _registry(tmp_path: Path) -> ToolRegistry:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    register_bash_tools(registry)
    return registry


def _alkera_dir(tmp_path: Path) -> str:
    return str(tmp_path / ".alkera")


async def _await_terminal(bg: BackgroundJobRegistry, job_id: str, *, tries: int = 150) -> Any:
    for _ in range(tries):
        job = bg.get(job_id)
        if job is not None and job.is_terminal:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("background job never reached a terminal state")


async def test_bash_foreground_runs_and_returns_output(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "echo hi", "description": "say hi"},
        alkera_dir=_alkera_dir(tmp_path),
    )
    assert out["output"] == "hi\n"
    assert out["exit_code"] == 0
    assert not out["job_id"]


async def test_bash_runs_in_default_workspace_root(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "pwd", "description": "print cwd"},
        alkera_dir=_alkera_dir(tmp_path),
    )
    # Default cwd is the workspace root (the dir containing .alkera/).
    assert out["output"].rstrip("\n") == str(tmp_path)


async def test_bash_workdir_subdir_is_honored(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "pwd", "description": "cwd", "workdir": "sub"},
        alkera_dir=_alkera_dir(tmp_path),
    )
    assert out["output"].rstrip("\n") == str(tmp_path / "sub")


async def test_bash_workdir_escape_is_refused(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "ls", "description": "list", "workdir": "../../etc"},
        alkera_dir=_alkera_dir(tmp_path),
    )
    assert "outside the workspace" in out["error"]


def _fence(tmp_path: Path) -> Any:
    """A bounded session's fence whose root the agent sees at ``/home/alkera``."""
    from alkera_cli.cloud.fence import SessionFence

    return SessionFence(
        root=tmp_path,
        folder=tmp_path,
        working_dir=tmp_path,
        aliases=(("/home/alkera", tmp_path),),
        system_roots=("/usr", "/bin"),
    )


async def test_bash_workdir_spelled_under_the_agents_home_is_honored(tmp_path: Path) -> None:
    """A bounded chat's model names its working directory the way it sees it;
    the tool maps that spelling to the host directory it is bound to before
    confining it, instead of refusing the agent's own root as foreign."""
    (tmp_path / "sub").mkdir()
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "pwd", "description": "cwd", "workdir": "/home/alkera/sub"},
        alkera_dir=_alkera_dir(tmp_path),
        fence=_fence(tmp_path),
    )
    assert out.get("error") is None, out
    assert out["output"].rstrip("\n") == str(tmp_path / "sub")


async def test_bash_workdir_that_climbs_out_of_the_agents_home_is_refused(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "ls", "description": "list", "workdir": "/home/alkera/../../etc"},
        alkera_dir=_alkera_dir(tmp_path),
        fence=_fence(tmp_path),
    )
    assert "outside the workspace" in out["error"]
    assert str(tmp_path) not in out["error"]


async def test_bash_sensitive_command_is_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tool wires gate_shell_action: with the credential-path gate switched on
    # (it is off by default), a sensitive read is escalated → prompted → refused
    # by the reject broker.
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    broker = _Broker("reject_once")
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "cat ~/.ssh/id_rsa", "description": "read key"},
        alkera_dir=_alkera_dir(tmp_path),
        broker=broker,
        permission_mode="default",
    )
    # A person's refusal is their sentence, whole, so the model reads a no and
    # not a policy to work around.
    assert out["error"] == NOT_GRANTED
    assert broker.prompts == 1


async def test_bash_background_returns_stub_then_runs_to_native_result(tmp_path: Path) -> None:
    bg = BackgroundJobRegistry()
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "echo done", "description": "echo", "background": True},
        alkera_dir=_alkera_dir(tmp_path),
        background=bg,
    )
    assert out["job_id"]
    assert "background" in out["note"].lower()
    assert out["output"] == ""  # no output on the immediate stub

    job = await _await_terminal(bg, out["job_id"])
    assert job.state == "completed"
    result = job.result  # a native BashResult
    assert result.output == "done\n"
    assert result.exit_code == 0


async def test_bash_background_captures_input_for_the_terminal_card(tmp_path: Path) -> None:
    """The real bash tool threads the originating command + cwd onto the job (via
    submit's ``input=``) so the terminal transcript card can show what ran."""
    bg = BackgroundJobRegistry()
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "echo done", "description": "echo", "background": True},
        alkera_dir=_alkera_dir(tmp_path),
        background=bg,
    )
    job = await _await_terminal(bg, out["job_id"])
    assert job.input == {"command": "echo done", "cwd": str(tmp_path)}


async def test_bash_background_flag_without_registry_runs_foreground(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "echo fg", "description": "echo", "background": True},
        alkera_dir=_alkera_dir(tmp_path),
        background=None,  # no registry (a subagent) → run foreground
    )
    assert not out["job_id"]
    assert out["output"] == "fg\n"


# --------------------------------------------------------------------------- #
# Foreground abort (turn cancel → reap the process group).
# --------------------------------------------------------------------------- #


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_pgid(pidfile: Path) -> int | None:
    """Read the pid the shell wrote, or None if not written yet. SYNC so the blocking
    Path I/O stays out of the async poll loop (ruff ASYNC240)."""
    if not pidfile.exists():
        return None
    text = pidfile.read_text(encoding="utf-8").strip()
    return int(text) if text else None


async def _wait_pgid_file(pidfile: Path, *, tries: int = 250) -> int:
    """Poll until the shell wrote its own pid (== process-group id, since the shell
    is spawned as the session/group leader) and return it."""
    for _ in range(tries):
        pgid = _read_pgid(pidfile)
        if pgid is not None:
            return pgid
        await asyncio.sleep(0.02)
    raise AssertionError("the command never wrote its pid")


async def _wait_group_gone(pgid: int, *, tries: int = 250) -> bool:
    for _ in range(tries):
        if not _group_alive(pgid):
            return True
        await asyncio.sleep(0.02)
    return not _group_alive(pgid)


async def test_foreground_bash_abort_reaps_the_process_group(tmp_path: Path) -> None:
    """A turn cancel (the ``abort`` Event being set) reaps a still-running FOREGROUND
    bash: the tool returns a 'command cancelled' error AND the command's process
    group is killed. The in-flight loopback-MCP call is detached (it is NOT cancelled
    on a turn cancel), so the Event is the only reap path — this is the regression
    that proves the seam. bypass mode keeps the gate out of the way (gate behavior is
    exhausted in test_bash_gate)."""
    abort = asyncio.Event()
    pidfile = tmp_path / "pgid"
    fut = asyncio.ensure_future(
        _registry(tmp_path).dispatch(
            "bash",
            {"command": f"echo $$ > {pidfile}; sleep 30", "description": "sleep"},
            permission_mode="bypass",
            alkera_dir=_alkera_dir(tmp_path),
            abort=abort,
        )
    )
    pgid = await _wait_pgid_file(pidfile)
    assert _group_alive(pgid)  # the command is genuinely running in its own group

    abort.set()  # Ctrl-C — signal the foreground tool
    out = await fut
    assert "cancelled" in str(out.get("error", "")).lower()
    assert await _wait_group_gone(pgid), "the process group was not reaped after abort"


async def test_foreground_bash_runs_normally_when_abort_present_but_unset(tmp_path: Path) -> None:
    """An ``abort`` Event that is never set changes nothing — the command runs to
    completion and the abort-waiter task is reaped (no leak)."""
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "echo hi", "description": "say hi"},
        alkera_dir=_alkera_dir(tmp_path),
        abort=asyncio.Event(),
    )
    assert out["output"] == "hi\n"
    assert out["exit_code"] == 0
    assert not out["job_id"]


async def test_foreground_bash_abort_already_set_cancels_immediately(tmp_path: Path) -> None:
    """An ``abort`` already set when the command starts cancels it at once rather
    than waiting out the sleep."""
    abort = asyncio.Event()
    abort.set()
    out = await _registry(tmp_path).dispatch(
        "bash",
        {"command": "sleep 30", "description": "sleep"},
        permission_mode="bypass",
        alkera_dir=_alkera_dir(tmp_path),
        abort=abort,
    )
    assert "cancelled" in str(out.get("error", "")).lower()


async def _root_spawn(prompt: str, **_kw: object) -> Any:
    """A spawn binding marks a root session, where the background tools are served."""
    raise AssertionError("never spawned")


async def test_background_status_shows_a_running_jobs_output_so_far(tmp_path: Path) -> None:
    """A job still running shows what it has printed: ``output_preview`` is
    filled from the job's captured output while it runs, not only once it ends."""
    from alkera_cli.plugins.plugin_base.background_tools import register_background_tools

    bg = BackgroundJobRegistry()
    registry = _registry(tmp_path)
    register_background_tools(registry)
    out = await registry.dispatch(
        "bash",
        {
            "command": "echo tick-1; echo tick-2; sleep 30",
            "description": "ticker",
            "background": True,
        },
        alkera_dir=_alkera_dir(tmp_path),
        background=bg,
    )
    job_id = out["job_id"]
    try:
        view: dict[str, Any] = {}
        for _ in range(200):
            status = await registry.dispatch(
                "background_status", {"job_id": job_id}, spawn=_root_spawn, background=bg
            )
            view = status["jobs"][0]
            if "tick-2" in (view.get("output_preview") or ""):
                break
            await asyncio.sleep(0.02)
        assert view["state"] == "running"
        assert view["output_preview"] == "tick-1\ntick-2"
    finally:
        await bg.cancel(job_id)
