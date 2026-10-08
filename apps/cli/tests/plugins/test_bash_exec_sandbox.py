"""A command run on a chat's behalf runs inside the chat's sandbox: the bare
no-sandbox launch (and no launch) is a byte-identical pass-through, a real
launch wraps the argv and the environment and places the pid, and the per-chat
uid + cgroup are kept in both modes while the boundary is gVisor-or-nothing."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files.chat_fs import ChatTree, ChatTreeError
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness import sandbox_steps
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.session_launches import reset_launches_for_tests
from alkera_cli.plugins.plugin_base import bash_exec
from alkera_cli.plugins.plugin_base.agent_tree import SpillTarget, tool_output
from alkera_cli.plugins.plugin_base.bash_exec import run_command, session_sandbox
from alkera_cli.plugins.plugin_base.tool import ToolContext, ToolError
from alkera_core.process import unlaunched

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell executor")


@pytest.fixture
def spawn_log(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    real = asyncio.create_subprocess_exec

    async def recording(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real(*argv, **kwargs)
        log.append(
            {
                "argv": unlaunched(argv),
                "env": kwargs.get("env"),
                "cwd": kwargs.get("cwd"),
                "pid": proc.pid,
            }
        )
        return proc

    monkeypatch.setattr(bash_exec.asyncio, "create_subprocess_exec", recording)
    return log


def spill(tmp_path: Path) -> Callable[[], SpillTarget]:
    return lambda: SpillTarget(ChatTree(tmp_path), "spill.txt")


def _text(path: Path) -> str:
    return path.read_text()


def _gone(path: str) -> bool:
    return not os.path.exists(path)


def _ctx(**fields: Any) -> ToolContext:
    """A tool context with the handles these cases read; a local session
    unless a fence is given."""
    return ToolContext(registry=None, blobs=None, **fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_launch_and_the_bare_no_sandbox_spawn_the_same_bytes(
    tmp_path: Path, spawn_log: list[dict[str, Any]]
) -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}
    plain = await run_command(
        "echo $HOME", cwd=str(tmp_path), env=env, shell="/bin/sh", make_spill=spill(tmp_path)
    )
    none = await run_command(
        "echo $HOME",
        cwd=str(tmp_path),
        env=env,
        shell="/bin/sh",
        make_spill=spill(tmp_path),
        sandbox=sb.NO_SANDBOX,
    )
    assert plain.output == none.output == f"{tmp_path}\n"
    assert spawn_log[0]["argv"] == ["/bin/sh", "-c", "echo $HOME"]
    assert spawn_log[1]["argv"] == spawn_log[0]["argv"]
    assert spawn_log[1]["env"] == spawn_log[0]["env"] == env
    assert spawn_log[1]["cwd"] == spawn_log[0]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_a_real_launch_wraps_the_argv_sets_home_runs_the_steps_and_places_the_pid(
    tmp_path: Path, spawn_log: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        sandbox_steps, "run_argv", lambda argv, **_kw: (ran.append(tuple(argv)), 0)[1]
    )
    procs = tmp_path / "cgroup.procs"
    procs.write_text("")
    launch = sb.SandboxLaunch(
        mode="none",
        prefix=("/usr/bin/env", "ALKERA_WRAPPED=1"),
        env={"HOME": "/home/alkera"},
        before=(sb.ShellStep(("chown", "-R", "20017:20017", str(tmp_path))),),
        cgroup_procs=procs,
    )
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}
    result = await run_command(
        "echo wrapped=$ALKERA_WRAPPED home=$HOME",
        cwd=str(tmp_path),
        env=env,
        shell="/bin/sh",
        make_spill=spill(tmp_path),
        sandbox=launch,
    )
    assert result.output == "wrapped=1 home=/home/alkera\n"
    assert spawn_log[0]["argv"] == [
        "/usr/bin/env",
        "ALKERA_WRAPPED=1",
        "/bin/sh",
        "-c",
        "echo wrapped=$ALKERA_WRAPPED home=$HOME",
    ]
    assert spawn_log[0]["env"]["HOME"] == "/home/alkera"
    assert ran == [("chown", "-R", "20017:20017", str(tmp_path))]
    assert procs.read_text() == str(spawn_log[0]["pid"])


@pytest.mark.asyncio
async def test_a_failed_ownership_step_refuses_the_command_before_it_runs(
    tmp_path: Path, spawn_log: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_steps, "run_argv", lambda argv, **_kw: 1)
    launch = sb.SandboxLaunch(mode="none", before=(sb.ShellStep(("chown", "-R", "1:1", "/x")),))
    with pytest.raises(sb.SandboxRefusedError):
        await run_command(
            "echo no",
            cwd=str(tmp_path),
            env={},
            shell="/bin/sh",
            make_spill=spill(tmp_path),
            sandbox=launch,
        )
    assert spawn_log == []


# -- the per-session launch ----------------------------------------------------


def cap_of(
    *,
    gvisor: bool,
    controls: bool,
    cgroup: sb.CgroupDriver = "systemd",
    env: bool = False,
    mount_ns: bool = False,
) -> SandboxCapability:
    return SandboxCapability(
        platform="linux",
        root=controls,
        setpriv="/usr/bin/setpriv" if controls else None,
        setfacl="/usr/bin/setfacl" if controls else None,
        runsc="/usr/bin/runsc" if gvisor else None,
        cgroup=cgroup if controls else "none",
        reason="injected",
        rootfs="/opt/alkera/rootfs/current" if gvisor else None,
        ip="/usr/sbin/ip" if gvisor else None,
        nft="/usr/sbin/nft" if gvisor else None,
        unshare="/usr/bin/unshare" if mount_ns else None,
        mount_ns=mount_ns,
        uv="/usr/local/bin/uv" if env else None,
        python_home="/opt/alkera/python/current" if env else None,
        resolvers=("172.31.0.2",),
    )


@pytest.fixture(autouse=True)
def fresh_sessions(monkeypatch: pytest.MonkeyPatch) -> Any:
    reset_launches_for_tests()
    for name in (sb.ENV_MODE, sb.ENV_DEDICATED):
        monkeypatch.delenv(name, raising=False)
    yield
    reset_launches_for_tests()


def test_a_local_session_gets_no_sandbox_and_no_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def explode() -> SandboxCapability:
        raise AssertionError("no probe for a local session")

    monkeypatch.setattr(bash_exec, "current_capability", explode)
    assert session_sandbox("chat_a", folder=tmp_path, fenced=False) is sb.NO_SANDBOX
    assert session_sandbox("chat_a", folder=None, fenced=True) is sb.NO_SANDBOX


def test_a_bare_container_none_node_runs_the_command_unwrapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A RunPod-style container (no root, no cgroup): a ``none`` node applies
    nothing, so the command just runs — no systemd, no uid drop, no runsc."""
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=False)
    )
    monkeypatch.setenv(sb.ENV_MODE, "none")
    launch = session_sandbox("chat_c", folder=tmp_path, fenced=True)
    assert launch is sb.NO_SANDBOX
    assert launch.wrap(["/bin/sh", "-c", "ls"]) == ["/bin/sh", "-c", "ls"]


def test_a_none_node_command_shares_the_agent_uid_and_slice_without_runsc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=True)
    )
    monkeypatch.setenv(sb.ENV_MODE, "none")
    calls: list[str] = []
    monkeypatch.setattr(
        bash_exec, "ensure_chat_uid", lambda chat_id, **_: (calls.append(chat_id), 20077)[1]
    )
    launch = session_sandbox("chat_00112233aabbccdd", folder=tmp_path, fenced=True)
    again = session_sandbox("chat_00112233aabbccdd", folder=tmp_path, fenced=True)
    assert launch is again and calls == ["chat_00112233aabbccdd"]  # built once, cached
    assert launch.mode == "none"
    assert "--reuid=20077" in launch.prefix
    assert "--slice=alkera-chat-chat00112233aabbccdd.slice" in launch.prefix
    assert not any(part.startswith("--unit=") for part in launch.prefix)
    assert "runsc" not in " ".join(launch.prefix)


def test_a_gvisor_node_command_execs_into_the_agents_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bash_exec, "current_capability", lambda: cap_of(gvisor=True, controls=True))
    monkeypatch.setenv(sb.ENV_MODE, "gvisor")
    monkeypatch.setattr(bash_exec, "ensure_chat_uid", lambda chat_id, **_: 20077)
    launch = session_sandbox(
        "chat_00112233aabbccdd", folder=tmp_path, fenced=True, state_dir=tmp_path / ".runtime"
    )
    assert launch.mode == "gvisor"
    command = launch.wrap(["/bin/sh", "-c", "ls"])
    assert command[0].endswith("runsc") and "exec" in command
    assert sb.chat_container("chat_00112233aabbccdd") in command
    assert "--user=20077:20077" in command
    assert "--cwd=/home/alkera" in command
    assert command[-3:] == ["/bin/sh", "-c", "ls"]
    # The command's environment rides into the container through env -i after
    # the container id, the sandbox's own names (HOME, the container PATH)
    # over the caller's, and nothing of the container's own.
    child_env = launch.apply_env({"HOME": str(tmp_path), "PATH": "/host/bin", "TERM": "dumb"})
    carried = launch.wrap(["/bin/sh", "-c", "ls"], env=child_env)
    container = carried.index(sb.chat_container("chat_00112233aabbccdd"))
    assert carried[container + 1 : container + 3] == [sb.CONTAINER_ENV, "-i"]
    names = carried[container + 3 : -3]
    assert "HOME=/home/alkera" in names and "TERM=dumb" in names
    assert not any(f.startswith("PATH=/host/bin") for f in names)
    assert "--env" not in carried


def test_a_gvisor_box_with_no_runsc_refuses_the_command_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=True)
    )
    monkeypatch.setenv(sb.ENV_MODE, "gvisor")
    with pytest.raises(ToolError, match="gVisor"):
        session_sandbox("chat_b", folder=tmp_path, fenced=True)


class FakeBox:
    """The cgroup state of a box, as ``systemctl`` and cgroupfs writes leave it."""

    def __init__(self) -> None:
        self.slices: dict[str, dict[str, str]] = {}
        self.stopped: list[str] = []
        self.files: dict[str, str] = {}

    def run(self, argv: Sequence[str]) -> int:
        if argv[:3] == ("systemctl", "set-property", "--runtime"):
            self.slices.setdefault(argv[3], {}).update(part.split("=", 1) for part in argv[4:])
        elif argv[:2] == ("systemctl", "stop"):
            self.stopped.append(argv[2])
            self.slices.pop(argv[2], None)
        return 0

    def write(self, path: Path, content: str) -> None:
        self.files[path.as_posix()] = content

    def repair(self, step: sb.RepairStep) -> None:
        """Handing a tree to a chat's uid is root's; the box has no cgroup in it."""

    def apply(self, launch: sb.SandboxLaunch, *, pid: int | None = None) -> None:
        sb.run_steps(launch.before, run=self.run, write=self.write, repair=self.repair)
        if pid is not None:
            sb.run_steps(launch.after_spawn(pid), run=self.run, write=self.write)

    def exit(self, launch: sb.SandboxLaunch) -> None:
        sb.run_steps(launch.after_exit, run=self.run, write=self.write)


@pytest.mark.parametrize("driver", ["systemd", "cgroupfs"])
def test_a_none_node_command_keeps_the_limits_the_chat_was_started_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, driver: sb.CgroupDriver
) -> None:
    """The per-chat cgroup is kept in mode ``none``: a chat started with 8 GB and
    4 vCPU keeps them through its commands, though the box default is 2 GB and 1
    vCPU, and a finished command never removes the chat's cgroup from under its
    agent server."""
    chat = "chat_8gbchat"
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=True, cgroup=driver)
    )
    monkeypatch.setenv(sb.ENV_MODE, "none")
    monkeypatch.setattr(bash_exec, "ensure_chat_uid", lambda chat_id, **_: 20077)
    monkeypatch.setenv(sb.ENV_POOL_MEMORY_MB, "2048")
    monkeypatch.setenv(sb.ENV_POOL_VCPU, "1")
    box = FakeBox()
    agent = sb.NoneRuntime().compose_launch(
        sb.SandboxSpec(
            chat_id=chat,
            folder=tmp_path,
            uid=20077,
            vcpu=4,
            memory_mb=8192,
            home="/home/alkera",
            mode="none",
            cgroup=driver,
        )
    )
    box.apply(agent, pid=4242)
    before = (dict(box.slices), dict(box.files))

    command = session_sandbox(chat, folder=tmp_path, fenced=True)
    box.apply(command, pid=4343)
    box.exit(command)

    cg = sb.chat_cgroup_path(chat).as_posix()
    if driver == "systemd":
        assert box.slices == before[0]  # the command never re-set the limits
        assert box.slices[sb.chat_slice(chat)]["MemoryMax"] == "8192M"
        assert box.slices[sb.chat_slice(chat)]["CPUQuota"] == "400%"
        assert f"--slice={sb.chat_slice(chat)}" in command.prefix
    else:
        assert box.files[f"{cg}/memory.max"] == str(8192 * 1024 * 1024)
        assert box.files[f"{cg}/cpu.max"] == "400000 100000"
        assert box.files[f"{cg}/cgroup.procs"] == "4343"  # the command joined the chat's cgroup
    assert box.stopped == []  # a finished command never ends the chat's cgroup


@pytest.mark.asyncio
async def test_a_gvisor_exec_launch_carries_the_commands_environment_on_the_argv(
    tmp_path: Path, spawn_log: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process ``runsc exec`` starts begins with the container's environment,
    the agent server's: its loopback password and inline config among it. The
    command's environment is therefore carried on the argv through ``env -i``,
    which replaces whatever the exec'd process began with. A shell stands in
    for runsc here and, like runsc, hands the command the environment it was
    itself started with; what the command sees is the composed one alone."""
    monkeypatch.setattr(sandbox_steps, "run_argv", lambda argv, **_kw: 0)
    monkeypatch.setenv("ALKERA_SERVER_PASSWORD", "the-servers-secret")
    shim = 'exec "$@"'
    launch = sb.SandboxLaunch(
        mode="gvisor",
        prefix=("/bin/sh", "-c", shim, "runsc-exec-shim"),
        exec_env=True,
        env={"HOME": "/home/alkera"},
    )
    result = await run_command(
        'echo "home=$HOME probe=$PROBE password=${ALKERA_SERVER_PASSWORD:-unset}"',
        cwd=str(tmp_path),
        env={"PROBE": "carried", "HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/bin")},
        shell="/bin/sh",
        make_spill=spill(tmp_path),
        sandbox=launch,
    )
    assert result.output == "home=/home/alkera probe=carried password=unset\n"
    argv = spawn_log[0]["argv"]
    assert argv[:6] == ["/bin/sh", "-c", shim, "runsc-exec-shim", sb.CONTAINER_ENV, "-i"]
    names = argv[6:-3]
    assert "HOME=/home/alkera" in names and "PROBE=carried" in names
    assert not any(name.startswith("ALKERA_SERVER_PASSWORD=") for name in names)
    assert argv[-3:] == [
        "/bin/sh",
        "-c",
        'echo "home=$HOME probe=$PROBE password=${ALKERA_SERVER_PASSWORD:-unset}"',
    ]


def test_the_default_environment_rides_from_the_state_dir_into_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The command runs with the same default Python environment as the agent
    server — the one under the chat's runtime state — first on its PATH."""
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=True, env=True)
    )
    monkeypatch.setenv(sb.ENV_MODE, "none")
    monkeypatch.setattr(bash_exec, "ensure_chat_uid", lambda chat_id, **_: 20077)
    state = tmp_path / ".runtime"
    launch = session_sandbox("chat_env", folder=tmp_path, fenced=True, state_dir=state)
    env_dir = sb.default_env_path(state)
    assert launch.path_prefix == (f"{env_dir.as_posix()}/bin",)
    assert launch.env["VIRTUAL_ENV"] == env_dir.as_posix()
    # A command never makes the environment: the agent server's launch did.
    assert all(not (isinstance(s, sb.ShellStep) and s.argv[0] == "uv") for s in launch.before)
    # Without a state dir there is no environment to name.
    reset_launches_for_tests()
    bare = session_sandbox("chat_env", folder=tmp_path, fenced=True)
    assert bare.path_prefix == () and "VIRTUAL_ENV" not in bare.env


def test_under_gvisor_the_command_names_the_environment_where_the_container_holds_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent server's launch binds the environment at its internal path,
    and the command runs in that container: a ``PATH`` or ``VIRTUAL_ENV`` that
    named the host path would name nothing there (``python: command not
    found``, which is what the box said before the command's spec carried the
    same bind table as the server's)."""
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=True, controls=True, env=True)
    )
    monkeypatch.setenv(sb.ENV_MODE, "gvisor")
    monkeypatch.setattr(bash_exec, "ensure_chat_uid", lambda chat_id, **_: 20077)
    # The chat folder's layout: the working directory and the runtime state
    # side by side, never one inside the other.
    folder, state = tmp_path / "scratch", tmp_path / ".runtime"
    launch = session_sandbox("chat_gv", folder=folder, fenced=True, state_dir=state)
    inside = f"{sb.ENVS_MOUNT}/alkera"
    assert launch.env["VIRTUAL_ENV"] == inside
    assert launch.env["UV_PROJECT_ENVIRONMENT"] == inside
    assert launch.env["PATH"].startswith(f"{inside}/bin:")
    host = sb.default_env_path(state).as_posix()
    assert all(host not in value for value in launch.env.values()), launch.env


def test_a_none_node_that_can_alias_binds_the_folder_at_home_for_each_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=True, mount_ns=True)
    )
    monkeypatch.setenv(sb.ENV_MODE, "none")
    monkeypatch.setattr(bash_exec, "ensure_chat_uid", lambda chat_id, **_: 20077)
    launch = session_sandbox("chat_alias", folder=tmp_path, fenced=True)
    command = launch.wrap(["/bin/sh", "-c", "pwd"])
    assert "/usr/bin/unshare" in command
    assert tmp_path.as_posix() in command and "/home/alkera" in command
    assert command.index("/usr/bin/unshare") < command.index("/usr/bin/setpriv")
    assert launch.env["HOME"] == "/home/alkera" and launch.agent_home == "/home/alkera"


@pytest.mark.asyncio
async def test_a_command_enters_the_container_where_the_agent_sees_its_cwd(
    tmp_path: Path, spawn_log: list[dict[str, Any]]
) -> None:
    """A sandbox that mounts the working directory at the agent's home is
    entered at the directory the agent named, spelled that way, and ``PWD``
    says the same; the host's spelling of the directory is still where the
    wrapper itself is spawned, since runsc runs on the host."""
    launch = sb.SandboxLaunch(
        mode="gvisor",
        prefix=("/bin/sh", "-c", "exit 0", "wrapper"),
        tail=("alkera-chat-x",),
        exec_env=True,
        exec_cwd="/home/alkera",
        env={"HOME": "/home/alkera", "PWD": "/home/alkera"},
        agent_home="/home/alkera",
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    await run_command(
        "pwd",
        cwd=str(sub),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=spill(tmp_path),
        sandbox=launch,
        agent_cwd="/home/alkera/sub",
    )
    spawned = spawn_log[-1]
    assert spawned["cwd"] == str(sub)
    assert "--cwd=/home/alkera/sub" in spawned["argv"]
    assert "--cwd=/home/alkera" not in spawned["argv"]
    assert sb.CONTAINER_ENV in spawned["argv"] and "PWD=/home/alkera/sub" in spawned["argv"]
    assert spawned["env"]["PWD"] == "/home/alkera/sub"


@pytest.mark.asyncio
async def test_a_spilled_output_is_named_the_way_the_agent_sees_it(tmp_path: Path) -> None:
    """The banner that tells the model where the rest of its output went names
    a path the model can open: the sandbox's spelling when the caller has one,
    the host path when it has none."""
    from alkera_cli.plugins.plugin_base.bash_exec import ExecLimits

    work = tmp_path / "scratch"
    work.mkdir()
    spill_path = work / "tool-output" / "bash-abc.txt"
    target = SpillTarget(ChatTree(work), "tool-output/bash-abc.txt")
    limits = ExecLimits(max_lines=1, max_bytes=8)
    spelled = await run_command(
        "printf 'one\\ntwo\\nthree\\n'",
        cwd=str(work),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=lambda: target,
        limits=limits,
        spell=lambda path: "/home/alkera/" + path.relative_to(work).as_posix(),
    )
    assert spelled.truncated is True
    assert spelled.output_path == "/home/alkera/tool-output/bash-abc.txt"
    assert "Full output saved to: /home/alkera/tool-output/bash-abc.txt" in spelled.output
    assert str(work) not in spelled.output
    assert spill_path.read_text() == "one\ntwo\nthree\n"
    plain = await run_command(
        "printf 'one\\ntwo\\nthree\\n'",
        cwd=str(work),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=lambda: target,
        limits=limits,
    )
    assert plain.output_path == str(spill_path)


@pytest.mark.asyncio
async def test_a_command_short_enough_to_hand_back_whole_makes_no_tool_output_directory(
    tmp_path: Path,
) -> None:
    """``tool-output`` appears the first time an output spills, not when a
    command is run: a chat whose commands were all short has none, and a
    person's file list is not told of an empty directory the daemon made."""
    from alkera_cli.plugins.plugin_base.bash_exec import ExecLimits

    work = tmp_path / "scratch"
    work.mkdir()
    output = tool_output(_ctx(sandbox_dir=str(work)), fallback="alkera-bash")
    assert output.path == work / "tool-output"
    short = await run_command(
        "printf 'one\\n'",
        cwd=str(work),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=lambda: output.spill("bash"),
    )
    assert short.truncated is False and short.output == "one\n"
    assert sorted(p.name for p in work.iterdir()) == [], "nothing was made for a short output"
    long = await run_command(
        "printf 'one\\ntwo\\nthree\\n'",
        cwd=str(work),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=lambda: output.spill("bash"),
        limits=ExecLimits(max_lines=1, max_bytes=8),
    )
    assert long.truncated is True and long.output_path is not None
    spilled = Path(long.output_path)
    assert spilled.parent == work / "tool-output" and spilled.name.startswith("bash-")
    assert _text(spilled) == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_a_tool_output_directory_the_agent_replaced_with_a_link_is_refused(
    tmp_path: Path,
) -> None:
    """The daemon makes ``tool-output`` under the root as itself; the agent
    owns the root and can put a link there first. A link is refused at the
    first write, and nothing lands where it points."""
    elsewhere = tmp_path / "etc"
    elsewhere.mkdir()
    work = tmp_path / "scratch"
    work.mkdir()
    (work / "tool-output").symlink_to(elsewhere)
    output = tool_output(_ctx(sandbox_dir=str(work)), fallback="alkera-bash")
    with pytest.raises(ChatTreeError, match="tool-output is not a directory"):
        output.write_text("payload.json", "{}")
    with pytest.raises(ChatTreeError, match="tool-output is not a directory"):
        output.spill("bash").write_text("x")
    assert list(elsewhere.iterdir()) == []
    # The control: a plain directory, or none yet, is the directory as before.
    (work / "tool-output").unlink()
    made = output.write_text("payload.json", "{}")
    assert made == work / "tool-output" / "payload.json" and made.read_text() == "{}"
    assert output.path.is_dir() and not output.path.is_symlink()
    output.unlink("payload.json")
    assert not made.exists() and output.path.is_dir()


def test_the_tool_output_directory_sits_where_the_session_may_read(tmp_path: Path) -> None:
    """Under the session's working directory when it has one, under the
    project's ``.alkera`` for a call made outside a chat, in the temp
    directory under the tool's own name when there is no project at all."""
    work, alkera = tmp_path / "work", tmp_path / ".alkera"
    both = tool_output(_ctx(sandbox_dir=str(work), alkera_dir=str(alkera)), fallback="x")
    assert both.path == work / "tool-output"
    project = tool_output(_ctx(alkera_dir=str(alkera)), fallback="x")
    assert project.path == alkera / "tool-output"
    nowhere = tool_output(_ctx(), fallback="alkera-bash")
    assert nowhere.path.name == "alkera-bash" and nowhere.path.parent != tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits",
    [
        pytest.param({"max_lines": 2, "max_bytes": 8}, id="spilled-while-running"),
        pytest.param({"max_lines": 2, "max_bytes": None}, id="spilled-after-the-tail"),
    ],
)
async def test_a_spill_through_a_link_swapped_in_under_a_running_command_is_not_written(
    tmp_path: Path, limits: dict[str, int | None]
) -> None:
    """The directory was plain when the tool started and a link by the time
    the output spilled. Neither spill path writes through it: the window is
    all that is kept, the banner says so, and nothing lands at the target."""
    from alkera_cli.plugins.plugin_base.bash_exec import ExecLimits

    elsewhere = tmp_path / "etc"
    elsewhere.mkdir()
    work = tmp_path / "scratch"
    work.mkdir()
    (work / "tool-output").symlink_to(elsewhere)
    target = SpillTarget(ChatTree(work), "tool-output/bash-abc.txt")
    result = await run_command(
        "printf 'one\\ntwo\\nthree\\n'",
        cwd=str(work),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=lambda: target,
        limits=ExecLimits(**limits),  # type: ignore[arg-type]
    )
    assert result.truncated is True
    assert result.output_path is None
    assert "The rest was not kept: " in result.output
    assert "tool-output is not a directory" in result.output
    assert "Full output saved to" not in result.output
    assert list(elsewhere.iterdir()) == []
    # The window the model is shown is still the tail of what ran.
    assert result.output.endswith("three\n")


# -- a subagent child's commands ---------------------------------------------------


def test_a_subagent_childs_commands_run_as_its_parents_uid_in_its_own_slice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child shares its parent's tree, so its commands run as the parent's
    identity; its slice (and under gVisor its container) stays its own, and
    the launch is cached under the child, not the parent."""
    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=True)
    )
    monkeypatch.setenv(sb.ENV_MODE, "none")
    calls: list[str] = []
    monkeypatch.setattr(
        bash_exec, "ensure_chat_uid", lambda chat_id, **_: (calls.append(chat_id), 20077)[1]
    )
    child, parent = "chat_child00000000000", "chat_parent0000000000"
    launch = session_sandbox(child, folder=tmp_path, fenced=True, owner_session_id=parent)
    assert calls == [parent]
    assert "--reuid=20077" in launch.prefix
    assert f"--slice={sb.chat_slice(child)}" in launch.prefix
    assert f"--slice={sb.chat_slice(parent)}" not in launch.prefix
    again = session_sandbox(child, folder=tmp_path, fenced=True, owner_session_id=parent)
    assert again is launch and calls == [parent]
    assert session_sandbox(parent, folder=tmp_path, fenced=True) is not launch


# -- ending a command inside its container ------------------------------------------


def _container_runsc(tmp_path: Path, *, alive: dict[str, int], stubborn: list[int]) -> Path:
    """A ``runsc`` whose ``exec`` runs the command on this host (writing the
    pid it claims the command has inside the container), whose table script
    answers from a process table in a state file, and whose ``kill --pid``
    removes a pid from it (a stubborn one dies to ``SIGKILL`` alone). Every
    kill and the pid file's path are recorded in the state file."""
    state = tmp_path / "container.json"
    state.write_text(
        json.dumps({"alive": alive, "stubborn": stubborn, "kills": [], "pid_file": ""})
    )
    script = tmp_path / "runsc"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"state = {str(state)!r}\n"
        "args = sys.argv[1:]\n"
        "with open(state) as f:\n"
        "    s = json.load(f)\n"
        "def save():\n"
        "    with open(state, 'w') as f:\n"
        "        json.dump(s, f)\n"
        "if 'kill' in args:\n"
        "    pid = next(a for a in args if a.startswith('--pid=')).removeprefix('--pid=')\n"
        "    s['kills'].append([int(pid), args[-1]])\n"
        "    if pid in s['alive'] and (args[-1] == 'SIGKILL' or int(pid) not in s['stubborn']):\n"
        "        del s['alive'][pid]\n"
        "    save()\n"
        "    sys.exit(0)\n"
        "if any('/proc/' in a for a in args):\n"
        "    print('99')\n"
        "    for pid, ppid in s['alive'].items():\n"
        "        print(f'{pid} (p{pid}) S {ppid} {pid} {pid} 0 0 0 0 0 0 0 1 0 0 0 20 0 1 0 1 1')\n"
        "    sys.exit(0)\n"
        "flag = next((a for a in args if a.startswith('--internal-pid-file=')), None)\n"
        "if flag:\n"
        "    s['pid_file'] = flag.removeprefix('--internal-pid-file=')\n"
        "    save()\n"
        "    with open(s['pid_file'], 'w') as f:\n"
        "        f.write('40\\n')\n"
        "i = len(args) - 1 - args[::-1].index('/bin/sh')\n"
        "os.execv('/bin/sh', args[i:])\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _container_launch(runsc: Path) -> sb.SandboxLaunch:
    return sb.SandboxLaunch(
        mode="gvisor",
        prefix=(str(runsc), "--root=/r", "exec"),
        tail=("alkera-chat-x",),
        exec_env=True,
        exec_cwd="/home/alkera",
        container="alkera-chat-x",
        runsc=str(runsc),
        runsc_root="/r",
        uid=7,
    )


@pytest.mark.asyncio
async def test_a_timed_out_command_is_ended_inside_its_container_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under gVisor the command's processes live in the agent's container,
    children of a ``runsc exec`` the host can only abandon. On the timeout,
    every process below the command's own pid (read from where runsc wrote
    it) gets TERM, a holdout gets KILL after the grace, the agent server is
    never signalled, and the pid file is gone afterwards."""
    runsc = _container_runsc(tmp_path, alive={"1": 0, "40": 1, "41": 40, "42": 41}, stubborn=[42])
    monkeypatch.setattr(bash_exec, "FORCE_KILL_SECONDS", 0.2)
    result = await run_command(
        "sleep 30",
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=spill(tmp_path),
        sandbox=_container_launch(runsc),
        timeout_ms=200,
    )
    assert result.timed_out is True and result.exit_code is None
    state = json.loads((tmp_path / "container.json").read_text())
    assert state["kills"] == [[40, "SIGTERM"], [41, "SIGTERM"], [42, "SIGTERM"], [42, "SIGKILL"]]
    assert state["alive"] == {"1": 0}, "the agent server alone is left"
    assert Path(state["pid_file"]).parent == Path(tempfile.gettempdir())
    assert Path(state["pid_file"]).name.startswith("alkera-exec-")
    assert _gone(state["pid_file"]), "the pid file is removed with the command"


@pytest.mark.asyncio
async def test_a_command_that_ends_in_time_sends_no_kill_into_its_container(
    tmp_path: Path,
) -> None:
    runsc = _container_runsc(tmp_path, alive={"1": 0}, stubborn=[])
    result = await run_command(
        "echo done",
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        shell="/bin/sh",
        make_spill=spill(tmp_path),
        sandbox=_container_launch(runsc),
    )
    assert result.output == "done\n" and result.exit_code == 0
    state = json.loads((tmp_path / "container.json").read_text())
    assert state["kills"] == [] and _gone(state["pid_file"])


@pytest.mark.asyncio
async def test_a_closed_session_s_next_command_builds_its_sandbox_afresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch a session's commands reuse names the container and slice its
    agent server ran in; once the session closes that server is gone, so the
    cached launch must go with it rather than be handed to the session's next
    open, and a box that serves chats for weeks must not keep one per chat it
    ever held."""
    from _adapter_factory import FakeAdapterFactory
    from alkera_cli.harness import HarnessRuntime
    from alkera_core.project.directory import ProjectDirectory

    monkeypatch.setattr(
        bash_exec, "current_capability", lambda: cap_of(gvisor=False, controls=True)
    )
    monkeypatch.setenv(sb.ENV_MODE, "none")
    monkeypatch.setattr(bash_exec, "ensure_chat_uid", lambda chat_id, **_: 20077)
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"), adapter_factory=FakeAdapterFactory()
    )
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    workdir = tmp_path / "work"
    workdir.mkdir()

    first = session_sandbox(sid, folder=workdir, fenced=True)
    assert session_sandbox(sid, folder=workdir, fenced=True) is first, "reused while open"
    await session.close()

    assert session_sandbox(sid, folder=workdir, fenced=True) is not first


@pytest.mark.parametrize(
    "module",
    [
        pytest.param("alkera_cli.plugins.plugin_base.bash_exec", id="the-shell-tool"),
        pytest.param(
            "alkera_cli.plugins.plugin_base.bash_tool", id="a-library-that-imports-the-shell-tool"
        ),
    ],
)
def test_the_shell_tool_imports_first_in_a_fresh_process(module: str) -> None:
    """The session forgets its launches through the harness's own cache, so
    importing the shell tool before the harness (as a tool module that wraps it
    does) must not close an import cycle through the
    runtime."""
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
