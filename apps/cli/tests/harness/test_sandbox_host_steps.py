"""What a sandbox launch runs on the host, outside the boundary.

The steps a launch runs before the spawn and after the exit run on the host
(as root, or dropped to the chat's uid), never inside gVisor. The chat's trees
are the agent's to write, so a host step that runs a program from one of them,
or an interpreter that loads what the agent left there (a ``.pth`` in
``site-packages``, a module), would run the agent's code on the host's kernel
with the daemon's network in reach. These tests walk every host step of every
launch shape and prove, against a real environment, that a planted
``bin/python`` and a planted ``.pth`` never run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.sandbox_env import env_steps
from alkera_cli.harness.sandbox_ownership import refuse_agent_programs, uid_prefix

CHAT = "chat_0123456789abcdef"
FOLDER = Path("/opt/alkera-work/.alkera/chats/chat_0123456789abcdef/sandbox")
CHAT_DIR = FOLDER.parent
RUNTIME = CHAT_DIR / ".runtime"
ENV = RUNTIME / "envs" / "alkera"
CONFIG = Path("/root/.alkera/harness/chat_0123456789abcdef")
AGENT_DIR = Path("/root/.alkera/cache/opencode/abc")
RG_DIR = Path("/opt/alkera/rg")
AGENT_ARGV = ("/root/.alkera/cache/opencode/abc/opencode", "serve")
UID = 20017


def spec(mode: sb.SandboxMode, cgroup: sb.CgroupDriver, **overrides: object) -> sb.SandboxSpec:
    base: dict[str, object] = {
        "chat_id": CHAT,
        "folder": FOLDER,
        "uid": UID,
        "vcpu": 2,
        "memory_mb": 2048,
        "home": "/home/alkera",
        "mode": mode,
        "cgroup": cgroup,
        "binds": (
            *sb.chat_binds(runtime_dir=RUNTIME, agent_config_root=CONFIG),
            *sb.tool_binds(AGENT_DIR, ripgrep_dir=RG_DIR),
        ),
        "private_dirs": (CHAT_DIR,),
        "bundle": CONFIG / "runsc",
        "rootfs": Path("/opt/alkera/rootfs/current"),
        "overlay_dir": CHAT_DIR / ".overlay",
        "default_env": ENV,
        "daemon_ports": (41234,),
        "resolvers": ("172.31.0.2",),
    }
    base.update(overrides)
    return sb.SandboxSpec(**base)  # type: ignore[arg-type]


def _shapes() -> list[object]:
    shapes: list[object] = []
    for cgroup in ("systemd", "cgroupfs", "none"):
        shapes.append(pytest.param("gvisor", cgroup, False, False, id=f"gvisor-agent-{cgroup}"))
        shapes.append(pytest.param("gvisor", cgroup, False, True, id=f"gvisor-command-{cgroup}"))
        for alias in (False, True):
            tag = f"{cgroup}{'-aliased' if alias else ''}"
            shapes.append(pytest.param("none", cgroup, alias, False, id=f"none-agent-{tag}"))
            shapes.append(pytest.param("none", cgroup, alias, True, id=f"none-command-{tag}"))
    return shapes


def _runtime(mode: sb.SandboxMode) -> sb.SandboxRuntime:
    return sb.GvisorRuntime() if mode == "gvisor" else sb.NoneRuntime()


def _launch(
    mode: sb.SandboxMode, cgroup: sb.CgroupDriver, alias: bool, command: bool
) -> tuple[sb.SandboxSpec, sb.SandboxLaunch]:
    agent = spec(mode, cgroup, mount_alias=alias)
    if command:
        python = (Path("/opt/alkera/python/current"), Path("/opt/alkera/lib/alkera_cli"))
        shell = sb.shell_spec(agent, python=python)
        return shell, _runtime(mode).compose_launch(shell)
    return agent, _runtime(mode).compose_launch(agent, AGENT_ARGV)


def _writable(s: sb.SandboxSpec) -> list[str]:
    """Every tree the agent writes, as the host spells it: the folder, every
    owned bind (the launch's own and those of the launch it runs inside),
    the default environment."""
    owned = [b.source for b in (*s.binds, *s.inherited) if b.kind == "owned"]
    return [sb.host_path(p) for p in (s.folder, *owned, ENV)]


def _program(argv: Sequence[str]) -> tuple[str, ...]:
    """The program a host step runs: past the uid drop, the shared umask's
    wrapper shell (it execs what follows it) and the ``env`` that sets its
    directory and names."""
    rest = tuple(argv)
    if rest[0] == "setpriv":
        rest = rest[rest.index("--") + 1 :]
    if rest[:2] == ("/bin/sh", "-c") and rest[3] == "alkera-umask":
        assert rest[2].endswith('exec "$@"'), rest
        rest = rest[4:]
    if rest[0] == "/usr/bin/env":
        rest = rest[1:]
        while rest[0].startswith("-") or "=" in rest[0]:
            rest = rest[1:]
    return rest


@pytest.mark.parametrize(("mode", "cgroup", "alias", "command"), _shapes())
def test_no_host_step_runs_a_program_or_interpreter_the_agent_can_write(
    mode: sb.SandboxMode, cgroup: sb.CgroupDriver, alias: bool, command: bool
) -> None:
    s, launch = _launch(mode, cgroup, alias, command)
    trees = _writable(s)
    steps = [
        st
        for st in (*launch.before, *launch.after_exit, *launch.after_spawn(4242))
        if isinstance(st, sb.ShellStep)
    ]
    assert steps, "every shape runs host steps"
    interpreters = 0
    for step in steps:
        program = _program(step.argv)
        assert not any(program[0] == tree or program[0].startswith(tree + "/") for tree in trees), (
            "a host step runs what the agent can write",
            step.argv,
        )
        if PurePosixPath(program[0]).name.startswith("python"):
            interpreters += 1
            assert program[0] == f"{sb.DEFAULT_PYTHON_HOME}/bin/python3", step.argv
            # Inline code, isolated and without site: nothing under the
            # environment (a .pth, a module) is loaded by the interpreter.
            assert program[1:4] == ("-I", "-S", "-c"), step.argv
    if not command or mode == "none":
        # The environment's steps are in this shape: the walk saw them.
        assert interpreters >= 2


@pytest.mark.parametrize(
    "program",
    [
        pytest.param((f"{sb.host_path(ENV)}/bin/python", "-m", "ensurepip"), id="env-python"),
        pytest.param((f"{sb.host_path(ENV)}/bin/pip", "install", "x"), id="env-script"),
        pytest.param((f"{sb.host_path(FOLDER)}/run.sh",), id="folder-script"),
        pytest.param(
            (f"{sb.DEFAULT_PYTHON_HOME}/bin/python3", "-m", "ensurepip"), id="module-not-inline"
        ),
        pytest.param(
            (f"{sb.DEFAULT_PYTHON_HOME}/bin/python3", "-S", "-c", "x"), id="inline-not-isolated"
        ),
        pytest.param(
            (f"{sb.DEFAULT_PYTHON_HOME}/bin/python3", "-I", "-c", "x"), id="inline-with-site"
        ),
        pytest.param(
            (f"{sb.DEFAULT_PYTHON_HOME}/bin/python3", "-I", "-S", f"{sb.host_path(ENV)}/x.py"),
            id="script-file",
        ),
    ],
)
@pytest.mark.parametrize(
    "shape",
    ["as-uid", "as-uid-under-the-umask-wrapper", "as-root"],
)
def test_a_launch_that_would_run_what_the_agent_wrote_is_refused(
    program: tuple[str, ...], shape: str
) -> None:
    """Refused however the step is spelled: run as root, dropped to the uid,
    or dropped and then started under the shared umask's wrapper shell (which
    execs what follows it, so the wrapper is not the program)."""
    s = spec("gvisor", "systemd")
    env = ("/usr/bin/env", "--chdir=/x", "HOME=/x")
    argv = {
        "as-uid": (*uid_prefix(s), *env, *program),
        "as-uid-under-the-umask-wrapper": (*uid_prefix(s), *sb.umask_prefix(), *env, *program),
        "as-root": program,
    }[shape]
    step = sb.ShellStep(argv, check=False)
    with pytest.raises(sb.SandboxRefusedError, match="sandbox step would run"):
        refuse_agent_programs(s, (step,))


def test_the_managed_interpreter_running_inline_code_is_allowed() -> None:
    s = spec("gvisor", "systemd")
    step = sb.ShellStep(
        (*uid_prefix(s), f"{sb.DEFAULT_PYTHON_HOME}/bin/python3", "-I", "-S", "-c", "x", "/env")
    )
    refuse_agent_programs(s, (step,))


# ---------------------------------------------------------------------------
# A real environment, with what an agent would plant in it
# ---------------------------------------------------------------------------


def _plant(env: Path, marker: Path) -> None:
    """What the agent can do to its own environment between two spawns: a
    ``.pth`` that runs in every interpreter that starts there, and a
    ``bin/python`` of its own in place of the link to the managed one."""
    version = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    site = env / "lib" / version / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "zz_planted.pth").write_text(
        f"import os; open({str(marker)!r}, 'a').write('pth ')\n", encoding="utf-8"
    )
    python = env / "bin" / "python"
    # The link points at the shared interpreter: unlinked, never written through.
    python.unlink()
    python.write_text(f"#!/bin/sh\nprintf 'python ' >> {marker}\n", encoding="utf-8")
    python.chmod(0o755)


def _run_env_steps(s: sb.SandboxSpec, beside: Path) -> None:
    """Run the launch's environment steps the way the box does, past the uid
    drop (this test is not root): from beside the environment, with its home
    and uv's cache there."""
    env = {**os.environ, "HOME": str(beside), "UV_CACHE_DIR": str(beside / ".uv-cache")}
    for step in env_steps(s):
        assert isinstance(step, sb.ShellStep)
        done = subprocess.run(
            _program(step.argv), cwd=beside, env=env, capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, (step.argv[-1], done.stderr)


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX environment")
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_a_planted_python_and_pth_never_run_when_the_environment_is_made_again(
    tmp_path: Path,
) -> None:
    """The second spawn remakes the environment and seeds pip into it. Neither
    may run the environment's interpreter: it starts the ``.pth`` (and on a
    box where ``uv`` could not replace it, is the agent's own script), on the
    host, outside the container. pip still lands, with ``pip`` itself."""
    envs = tmp_path / "runtime" / "envs"
    envs.mkdir(parents=True)
    env = envs / "alkera"
    marker = tmp_path / "escaped"
    s = spec(
        "none",
        "none",
        folder=tmp_path / "sandbox",
        binds=sb.chat_binds(runtime_dir=tmp_path / "runtime", agent_config_root=tmp_path / "cfg"),
        default_env=env,
        python_home=sys.base_prefix,
        uv=shutil.which("uv") or "uv",
    )
    _run_env_steps(s, envs)
    _plant(env, marker)
    shutil.rmtree(next((env / "lib").glob("python*/site-packages/pip")))
    for dist in (env / "lib").glob("python*/site-packages/pip-*.dist-info"):
        shutil.rmtree(dist)

    _run_env_steps(s, envs)

    assert not marker.exists(), marker.read_text()
    assert (env / "bin" / "pip").is_file()
    assert list((env / "lib").glob("python*/site-packages/pip-*.dist-info"))
    # The scripts pip wrote run wherever the environment is mounted.
    assert (env / "bin" / "pip").read_bytes().startswith(b"#!/bin/sh\n")
