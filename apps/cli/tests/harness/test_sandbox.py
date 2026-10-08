"""The per-chat sandbox launch in the two-state model (gVisor or none): what the
command is under each runtime, what runs before and after the spawn, and what a
chat is refused for.

The per-chat uid and cgroup are kept in BOTH modes (resource/ownership
controls, not the boundary); the boundary is runsc-or-nothing. The gVisor
runtime's runsc argv, OCI config, per-chat ``/etc`` files and network steps are
pinned as pure data — a real runsc run needs a Linux host with gVisor and the
staged rootfs, and lives in ``test_sandbox_live.py``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

import pytest
from alkera_cli.files.chat_fs import TreeIdentity
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.adapters import opencode_http
from alkera_cli.harness.sandbox_env import SEED_PIP_SOURCE, seed_pip_argv
from alkera_cli.harness.sandbox_layout import COMMAND_TMP_SUBDIR, command_tmp_dir
from alkera_cli.harness.spawn import sandbox_spec
from alkera_cli.harness.system_prompt import PYTHON_ENV_NAME
from alkera_core.process import popen_args
from alkera_core.sandbox_tiers import DEDICATED_GUARD, POOL_FLOOR, TIER_LIMITS

CHAT = "chat_0123456789abcdef"
FOLDER = Path("/opt/alkera-work/.alkera/chats/chat_0123456789abcdef/sandbox")
CHAT_DIR = FOLDER.parent
RUNTIME = CHAT_DIR / ".runtime"
ENV = RUNTIME / "envs" / "alkera"
#: Where the agent sees the default environment under gVisor.
ENV_INSIDE = f"{sb.ENVS_MOUNT}/alkera"
OVERLAY = CHAT_DIR / ".overlay"
CONFIG = Path("/root/.alkera/harness/chat_0123456789abcdef")
CONFIG_DIR = CONFIG / "config"
STATE = CONFIG / "state"
AGENT_DIR = Path("/root/.alkera/cache/opencode/abc")
RG_DIR = Path("/opt/alkera/rg")
BUNDLE = CONFIG / "runsc"
ROOTFS = Path("/opt/alkera/rootfs/current")
AGENT_ARGV = ("/root/.alkera/cache/opencode/abc/opencode", "serve", "--hostname", "0.0.0.0")
UID = 20017


def spec(
    mode: sb.SandboxMode = "gvisor",
    cgroup: sb.CgroupDriver = "systemd",
    **overrides: object,
) -> sb.SandboxSpec:
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
        "bundle": BUNDLE,
        "rootfs": ROOTFS,
        "overlay_dir": OVERLAY,
        "default_env": ENV,
        "path_dirs": (sb.RIPGREP_MOUNT,),
        "daemon_ports": (41234,),
        "resolvers": ("172.31.0.2",),
        "agent_env": {
            "ALKERA_GATEWAY_URL": "https://gw.example",
            "PATH": "/host/bin:/usr/bin",
            "ALKERA_SHELL_ENV_RESTORE": '{"PATH": "/host"}',
            "PWD": "/opt/alkera-work",
            "HOSTNAME": "ip-10-0-0-7",
        },
    }
    base.update(overrides)
    return sb.SandboxSpec(**base)  # type: ignore[arg-type]


def shell_steps(steps: Sequence[sb.Step]) -> list[tuple[str, ...]]:
    return [s.argv for s in steps if isinstance(s, sb.ShellStep)]


def head(step: sb.Step) -> str:
    """What a step runs: the program, or ``uid:<program>`` for one run as the
    chat's uid (after the drop and the environment it is given)."""
    if isinstance(step, sb.WriteStep):
        return f"write:{step.path.name}"
    if isinstance(step, sb.RepairStep):
        return f"repair:{step.tree.name}"
    argv = step.argv
    if argv[0] != "setpriv":
        return argv[0]
    rest = argv[argv.index("--") + 1 :]
    if tuple(rest[:4]) == sb.umask_prefix():
        rest = rest[4:]
    at = 0
    if rest[0] == "/usr/bin/env":
        at = 1
        while "=" in rest[at]:
            at += 1
    return f"uid:{rest[at]}"


def writes(steps: Sequence[sb.Step]) -> dict[Path, str]:
    return {s.path: s.content for s in steps if isinstance(s, sb.WriteStep)}


def oci_of(launch: sb.SandboxLaunch) -> dict[str, object]:
    config = writes(launch.before)[BUNDLE / "config.json"]
    loaded: dict[str, object] = json.loads(config)
    return loaded


# ---------------------------------------------------------------------------
# NoneRuntime: uid + cgroup, no boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cgroup", ["systemd", "cgroupfs"])
def test_none_runtime_applies_uid_and_cgroup_but_no_runsc(cgroup: sb.CgroupDriver) -> None:
    launch = sb.NoneRuntime().compose_launch(spec("none", cgroup), AGENT_ARGV)
    assert launch.mode == "none"
    argv = launch.wrap(AGENT_ARGV)
    # The uid drop is always there; runsc never is.
    assert "setpriv" in argv
    assert f"--reuid={UID}" in argv
    assert "runsc" not in " ".join(argv)
    assert argv[-len(AGENT_ARGV) :] == list(AGENT_ARGV)
    if cgroup == "systemd":
        assert "systemd-run" in argv
        assert "--slice=alkera-chat-chat0123456789abcdef.slice" in argv
    else:
        assert "systemd-run" not in argv
        assert launch.cgroup_procs == sb.chat_cgroup_path(CHAT) / "cgroup.procs"


def test_none_runtime_pdeathsig_is_set_after_the_uid_change() -> None:
    argv = sb.NoneRuntime().compose_launch(spec("none"), AGENT_ARGV).wrap(AGENT_ARGV)
    reuid = argv.index(f"--reuid={UID}")
    pdeath = argv.index("--pdeathsig")
    # The kernel clears the parent-death signal on a credential change, so
    # --pdeathsig must come after the reuid.
    assert pdeath > reuid


def test_none_runtime_before_steps_own_the_trees_and_make_the_cgroup() -> None:
    launch = sb.NoneRuntime().compose_launch(spec("none"), AGENT_ARGV)
    argvs = shell_steps(launch.before)
    assert any(a[0] == "chown" for a in argvs)
    assert any(a[0] == "systemctl" and a[1] == "set-property" for a in argvs)
    # After exit stops the slice so anything still in it goes with it.
    stop = shell_steps(launch.after_exit)
    assert any(a[0] == "systemctl" and a[1] == "stop" for a in stop)


def test_none_runtime_home_is_the_folders_host_path_when_it_cannot_alias() -> None:
    """With no mount namespace to bind the folder at the short path, HOME is
    the folder itself — a home that exists — and the agent sees host paths."""
    launch = sb.NoneRuntime().compose_launch(spec("none", mount_alias=False), AGENT_ARGV)
    assert launch.env["HOME"] == sb.host_path(FOLDER)
    assert launch.agent_home is None
    assert "unshare" not in " ".join(launch.wrap(AGENT_ARGV))


def test_none_runtime_binds_the_folder_at_home_in_a_private_mount_namespace() -> None:
    """On a box that can, the folder is bound at ``/home/alkera`` inside a
    private mount namespace — before the uid drop (mount needs root), invisible
    to the host — so the agent sees the same short path a gVisor chat does."""
    launch = sb.NoneRuntime().compose_launch(spec("none", mount_alias=True), AGENT_ARGV)
    argv = launch.wrap(AGENT_ARGV)
    unshare = argv.index("unshare")
    assert argv[unshare : unshare + 5] == ["unshare", "--mount", "--propagation", "private", "--"]
    # The bind names the folder and the home positionally, never interpolated
    # into the shell text.
    sh = argv.index("/bin/sh")
    assert argv[sh + 1] == "-c"
    assert "$1" in argv[sh + 2] and "$2" in argv[sh + 2] and 'exec "$@"' in argv[sh + 2]
    assert argv[sh + 4 : sh + 6] == [sb.host_path(FOLDER), "/home/alkera"]
    assert argv.index("setpriv") > unshare  # root for the mount, then the drop
    assert argv[-len(AGENT_ARGV) :] == list(AGENT_ARGV)
    assert launch.env["HOME"] == "/home/alkera"
    assert launch.agent_home == "/home/alkera"
    assert ("mkdir", "-p", "/home/alkera") in shell_steps(launch.before)


def test_none_runtime_activates_the_default_environment_by_prefixing_path() -> None:
    launch = sb.NoneRuntime().compose_launch(spec("none"), AGENT_ARGV)
    assert launch.path_prefix == (f"{sb.host_path(ENV)}/bin",)
    assert launch.env["VIRTUAL_ENV"] == sb.host_path(ENV)
    applied = launch.apply_env({"PATH": "/usr/bin", "HOME": "/root"})
    assert applied["PATH"].startswith(f"{sb.host_path(ENV)}/bin:")
    assert applied["PATH"].endswith("/usr/bin")
    assert applied["HOME"] != "/root"


def test_the_launch_path_is_joined_with_the_boxs_separator_whatever_the_composers_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``PATH`` the launch prefixes is the Linux box's; a composer whose own
    separator is ``;`` must still hand the agent ``a:b``."""
    import os

    monkeypatch.setattr(os, "pathsep", ";")
    launch = sb.NoneRuntime().compose_launch(spec("none"), AGENT_ARGV)
    applied = launch.apply_env({"PATH": "/usr/local/bin:/usr/bin"})
    assert applied["PATH"] == f"{sb.host_path(ENV)}/bin:/usr/local/bin:/usr/bin"
    assert ";" not in applied["PATH"]
    assert sb.SandboxLaunch(mode="none", path_prefix=("/a", "/b")).apply_env({})["PATH"] == "/a:/b"


def test_none_runtime_lends_the_chat_uid_the_agents_read_only_trees_once_per_agent_spawn() -> None:
    """With no container to mount them into, the agent opens its binary and
    ``rg`` itself as the chat's uid, through directories the daemon made under
    its own umask: the agent server's launch lends that uid read-and-traverse
    on the trees and traverse on their ancestors. A command beside the agent
    (no hold on the cgroup) and a gVisor launch (runsc mounts as root) do not."""
    launch = sb.NoneRuntime().compose_launch(spec("none"), AGENT_ARGV)
    argvs = shell_steps(launch.before)
    lend = ("setfacl", "-R", "-m", f"u:{UID}:rX", AGENT_DIR.as_posix(), RG_DIR.as_posix())
    assert lend in argvs
    ancestors = (
        "setfacl",
        "-m",
        f"u:{UID}:x",
        "/root/.alkera/cache/opencode",
        "/root/.alkera/cache",
        "/root/.alkera",
        "/root",
        "/opt/alkera",
        "/opt",
    )
    assert ancestors in argvs
    assert argvs.index(lend) > argvs.index(next(a for a in argvs if a[0] == "chown"))
    command = sb.NoneRuntime().compose_launch(sb.shell_spec(spec("none")), ("/bin/sh", "-c", "x"))
    assert not any(a[:2] == ("setfacl", "-R") for a in shell_steps(command.before))
    no_tools = spec("none", binds=sb.chat_binds(runtime_dir=RUNTIME, agent_config_root=CONFIG))
    assert sb.read_access_steps(no_tools) == ()
    under_runsc = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    assert not any(a[:2] == ("setfacl", "-R") for a in shell_steps(under_runsc.before))


def test_the_gvisor_launch_composition_is_unchanged_by_the_none_mode_controls() -> None:
    """The proven EC2 path: the gVisor before-steps are exactly the cgroup, the
    environment, the ownership, the bundle, the etc files, the OCI config and
    the network — nothing the ``none`` runtime adds for a host with no
    container."""
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    heads = [head(s) for s in launch.before]
    assert heads == [
        "systemctl",
        "/bin/sh",
        "repair:sandbox",
        "repair:envs",
        "repair:agent",
        "repair:state",
        "repair:.tmp",
        "chown",
        "chmod",
        "chmod",
        "setfacl",
        "uid:uv",
        f"uid:{sb.DEFAULT_PYTHON_HOME}/bin/python3",
        f"uid:{sb.DEFAULT_PYTHON_HOME}/bin/python3",
        "/bin/sh",
        "mkdir",
        "mkdir",
        "write:passwd",
        "write:group",
        "write:hosts",
        "write:hostname",
        "write:resolv.conf",
        "write:config.json",
        "ip",
        "ip",
        "ip",
        "ip",
        "ip",
        "ip",
        "ip",
        "ip",
        "ip",
        "ip",
        "nft",
    ]


def test_bare_no_sandbox_runs_the_command_untouched() -> None:
    assert sb.NO_SANDBOX.mode == "none"
    assert sb.NO_SANDBOX.wrap(AGENT_ARGV) == list(AGENT_ARGV)
    assert sb.NO_SANDBOX.wrap(AGENT_ARGV, env={"A": "b"}) == list(AGENT_ARGV)
    assert sb.NO_SANDBOX.apply_env({"A": "b"}) == {"A": "b"}
    assert sb.NO_SANDBOX.before == ()
    assert sb.NO_SANDBOX.after_exit == ()


# ---------------------------------------------------------------------------
# The default Python environment (both modes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime", [sb.NoneRuntime(), sb.GvisorRuntime()], ids=["none", "gvisor"])
def test_the_default_environment_is_made_idempotently_as_the_chats_uid(
    runtime: sb.SandboxRuntime,
) -> None:
    s = spec(runtime.mode)
    launch = runtime.compose_launch(s, AGENT_ARGV)
    steps = [st for st in launch.before if isinstance(st, sb.ShellStep)]
    drop = sb.uid_prefix(s)
    beside = sb.host_path(ENV.parent)
    given = (
        "/usr/bin/env",
        f"--chdir={beside}",
        f"HOME={beside}",
        f"UV_CACHE_DIR={beside}/.uv-cache",
    )
    venv_step = next(st for st in steps if head(st) == "uid:uv")
    # Made as the chat's uid, under the tree's shared umask (the environment is
    # every member's and kernel's to install into), from beside the environment
    # (the daemon's own working directory may be closed to the uid), with a
    # home and a cache the uid can write.
    umask = sb.umask_prefix()
    venv = venv_step.argv[len(drop) + len(umask) + len(given) :]
    assert venv_step.argv[: len(drop) + len(umask) + len(given)] == (*drop, *umask, *given)
    assert venv[:2] == ("uv", "venv")
    assert "--allow-existing" in venv  # a second spawn keeps what the first installed
    assert "--clear" not in venv
    assert venv[venv.index("--prompt") + 1] == sb.DEFAULT_ENV_NAME == PYTHON_ENV_NAME
    assert venv[venv.index("--python") + 1] == f"{sb.DEFAULT_PYTHON_HOME}/bin/python3"
    assert venv[-1] == sb.host_path(ENV)
    pip_step = next(st for st in steps if SEED_PIP_SOURCE in st.argv)
    assert pip_step.argv[: len(drop) + len(umask) + len(given)] == (*drop, *umask, *given)
    seed = pip_step.argv[len(drop) + len(umask) + len(given) :]
    # pip is seeded by the box's managed interpreter, isolated and without
    # site, never by the environment's own bin/python (the agent's to replace).
    assert seed == seed_pip_argv(f"{sb.DEFAULT_PYTHON_HOME}/bin/python3", sb.host_path(ENV))
    assert seed[1:4] == ("-I", "-S", "-c")
    # Made after the trees are the uid's, since the uid is what makes it.
    assert steps.index(venv_step) > next(i for i, st in enumerate(steps) if st.argv[0] == "chown")
    assert steps.index(pip_step) > steps.index(venv_step)
    # Neither step can refuse the chat: a chat without its environment still runs.
    assert venv_step.check is False and pip_step.check is False


@pytest.mark.parametrize("runtime", [sb.NoneRuntime(), sb.GvisorRuntime()], ids=["none", "gvisor"])
def test_nothing_the_daemon_runs_executes_or_follows_what_the_agent_can_write(
    runtime: sb.SandboxRuntime,
) -> None:
    """The chat's trees are the agent's to write, so a step that reaches into
    one does so as the chat's uid, with nothing kept, unless it is a step that
    hands the tree over (``mkdir``, ``chown``, ``chmod``, ``setfacl``: root's
    own tools, on paths root composed). The daemon running the environment's
    ``python``, or ``uv`` into the environment, as itself would run a ``.pth``
    the agent planted, a ``bin/python`` it replaced, or write through a link
    it left, as root on the host."""
    s = spec(runtime.mode)
    launch = runtime.compose_launch(s, AGENT_ARGV)
    owned = [sb.host_path(p) for p in s.folder_and_binds()]

    def in_owned(arg: str) -> bool:
        return any(arg == tree or arg.startswith(tree + "/") for tree in owned)

    as_uid: list[sb.ShellStep] = []
    for step in (*launch.before, *launch.after_exit):
        if isinstance(step, sb.WriteStep):
            assert not in_owned(sb.host_path(step.path)), step
            continue
        if isinstance(step, sb.RepairStep):
            # Root's walk of an owned tree runs in the daemon itself, never a
            # program the tree could hold.
            continue
        if step.argv[: len(sb.uid_prefix(s))] == sb.uid_prefix(s):
            as_uid.append(step)
            continue
        assert f"--reuid={UID}" not in step.argv, ("a partial drop", step.argv)
        if any(in_owned(arg) for arg in step.argv):
            assert step.argv[0] in {"/bin/sh", "setfacl"}, step.argv
            assert not in_owned(step.argv[0])
            if step.argv[0] == "/bin/sh":
                # Root's one reviewed script over an owned tree: making the
                # trees (root's own tools, the root checked not to be a link).
                assert step.argv[3] == "mkdir-owned", ("a shell over an owned tree", step.argv)
                assert step.argv[2] == sb.OWNED_MKDIR_SHELL
    # The steps that do go in as the uid are the environment's, and no other.
    expected = {
        "uid:uv",
        f"uid:{sb.DEFAULT_PYTHON_HOME}/bin/python3",
    }
    assert {head(step) for step in as_uid} == expected
    assert all("--clear-groups" in step.argv and "--no-new-privs" in step.argv for step in as_uid)


@pytest.mark.parametrize("runtime", [sb.NoneRuntime(), sb.GvisorRuntime()], ids=["none", "gvisor"])
def test_a_box_with_no_interpreter_makes_no_environment_and_names_none(
    runtime: sb.SandboxRuntime,
) -> None:
    launch = runtime.compose_launch(spec(runtime.mode, default_env=None), AGENT_ARGV)
    assert all(a[0] != "uv" for a in shell_steps(launch.before))
    assert "VIRTUAL_ENV" not in launch.env
    assert launch.path_prefix == ()


@pytest.mark.parametrize(
    ("mode", "env", "beside"),
    [
        pytest.param("gvisor", ENV_INSIDE, sb.ENVS_MOUNT, id="gvisor-under-the-prefix"),
        pytest.param("none", sb.host_path(ENV), sb.host_path(ENV.parent), id="none-at-host-path"),
    ],
)
def test_the_environment_names_live_beside_the_environment_not_in_the_users_folder(
    mode: sb.SandboxMode, env: str, beside: str
) -> None:
    """Spelled as the agent sees them: under gVisor the environments directory
    is bound at its fixed internal path and the host path names nothing in
    the container; on a ``none`` box the host path is the agent's path."""
    names = sb.environment_names(spec(mode))
    assert names["VIRTUAL_ENV"] == names["UV_PROJECT_ENVIRONMENT"] == env
    # The environment's own interpreter: uv reads UV_PYTHON as `--python`, which
    # outranks VIRTUAL_ENV for `uv pip`, so the base interpreter here would
    # send every install at the shared install instead of the chat's.
    assert names["UV_PYTHON"] == f"{env}/bin/python"
    for cache in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "MAMBA_ROOT_PREFIX"):
        assert names[cache].startswith(beside + "/"), cache
        assert not names[cache].startswith(sb.host_path(FOLDER))
        assert not names[cache].startswith("/home/alkera")


def _effective_env(launch: sb.SandboxLaunch) -> dict[str, str]:
    """What the launched process actually runs with: the OCI ``process.env``
    for a gVisor agent server (runsc does not pass its own environment in),
    the launch's names over an empty caller environment everywhere else."""
    if launch.command is not None and launch.mode == "gvisor":
        oci = oci_of(launch)
        process = oci["process"]
        assert isinstance(process, dict)
        return dict(item.split("=", 1) for item in process["env"])
    return launch.apply_env({})


@pytest.mark.parametrize("runtime", [sb.NoneRuntime(), sb.GvisorRuntime()], ids=["none", "gvisor"])
@pytest.mark.parametrize("owns", [True, False], ids=["agent-server", "shell-command"])
def test_every_launch_makes_the_chats_environment_the_target_of_uv_pip(
    runtime: sb.SandboxRuntime, owns: bool
) -> None:
    """Every spawn a chat has — its agent server, and each command run on its
    behalf, under either runtime — runs with the chat's environment active and
    with uv's interpreter pinned to that environment's own python. The base
    interpreter in ``UV_PYTHON`` outranked ``VIRTUAL_ENV`` for ``uv pip`` on
    every one of these paths."""
    env = _effective_env(runtime.compose_launch(spec(runtime.mode, owns_cgroup=owns), AGENT_ARGV))
    seen = ENV_INSIDE if runtime.mode == "gvisor" else sb.host_path(ENV)
    assert env["VIRTUAL_ENV"] == seen
    assert env["UV_PYTHON"] == f"{seen}/bin/python"
    assert env["PATH"].split(":")[0] == f"{seen}/bin"


def _base_python_home() -> Path | None:
    """A real base interpreter's prefix with ``bin/python3``, the shape the box's
    ``python_home`` has; ``None`` where this host's interpreter is laid out
    otherwise (Windows)."""
    home = Path(sys.base_prefix)
    return home if (home / "bin" / "python3").exists() else None


def test_a_plain_uv_pip_command_lands_in_the_chats_environment(tmp_path: Path) -> None:
    """The environment names a launch sets make a bare ``uv pip`` — no
    ``--python``, no activation — act on the chat's own environment, which is
    what the PYTHON block promises the model. Run against real uv and a real
    venv made from a real base interpreter: with the base interpreter in
    ``UV_PYTHON`` uv chose the base install instead (on a box, an externally
    managed one, so ``uv pip install`` exited 2). The ``none`` launch is the
    one whose names this host can run (a gVisor launch spells the environment
    where the container mounts it; the live suite runs that one)."""
    runtime = sb.NoneRuntime()
    uv = shutil.which("uv")
    python_home = _base_python_home()
    if uv is None or python_home is None:
        pytest.skip("needs uv on PATH and a POSIX base interpreter")
    env_dir = tmp_path / "chat" / ".runtime" / "envs" / "alkera"
    made = subprocess.run(
        [uv, "venv", "--quiet", "--python", str(python_home / "bin" / "python3"), str(env_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert made.returncode == 0, made.stderr
    folder = tmp_path / "chat" / "scratch"
    folder.mkdir()
    chat = spec(
        runtime.mode,
        folder=folder,
        default_env=env_dir,
        python_home=str(python_home),
        owns_cgroup=False,
    )
    launch = runtime.compose_launch(chat, AGENT_ARGV)
    ambient = {
        k: v for k, v in os.environ.items() if not k.startswith(("UV_", "VIRTUAL_ENV", "CONDA"))
    }
    env = launch.apply_env(ambient)
    listed = subprocess.run(
        [uv, "pip", "list"], cwd=folder, env=env, capture_output=True, text=True, check=False
    )
    assert listed.returncode == 0, listed.stderr
    assert f"environment at: {env_dir}" in listed.stderr, listed.stderr


def test_default_env_path_is_under_the_runtime_state_envs() -> None:
    assert sb.default_env_path(RUNTIME) == RUNTIME / sb.ENVS_SUBDIR / sb.DEFAULT_ENV_NAME
    assert sb.RUNTIME_STATE_SUBDIR == ".runtime"


# ---------------------------------------------------------------------------
# GvisorRuntime: the agent under runsc
# ---------------------------------------------------------------------------


def test_gvisor_agent_server_runs_under_runsc_with_the_bundle() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    assert launch.mode == "gvisor"
    # runsc owns the whole command; the agent argv lives in the OCI config, so
    # wrap() returns the runsc invocation, NOT prefix + argv.
    command = launch.wrap(AGENT_ARGV)
    assert command[0].endswith("runsc")
    assert f"--bundle={sb.host_path(BUNDLE)}" in command
    assert command[-1] == sb.chat_container(CHAT)
    assert command[-len(AGENT_ARGV) :] != list(AGENT_ARGV)
    # Global flags come before the subcommand: its own network stack, the root
    # overlay backed on disk, and shared file access so a file the daemon drops
    # into the folder while the container runs is seen.
    run = command.index("run")
    assert "--network=sandbox" in command[:run]
    assert f"--overlay2=root:dir={sb.host_path(OVERLAY)}" in command[:run]
    assert "--file-access-mounts=shared" in command[:run]
    assert launch.network is not None and launch.agent_home == "/home/alkera"


@pytest.mark.parametrize("cgroup", ["systemd", "cgroupfs"])
def test_runsc_reads_the_cgroups_path_with_the_driver_it_is_spelled_for(
    cgroup: sb.CgroupDriver,
) -> None:
    """A systemd ``slice:prefix:name`` path is only a systemd path under
    ``--systemd-cgroup``; without the flag runsc takes it for a cgroupfs path
    and places the sandbox outside the slice whose ``MemoryMax`` bounds it. A
    cgroupfs box hands runsc the chat's path from the cgroup root and no
    flag: runsc puts its mountpoint in front, so a host path came out doubled."""
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor", cgroup), AGENT_ARGV)
    command = launch.wrap(AGENT_ARGV)
    run = command.index("run")
    linux = oci_of(launch)["linux"]
    assert isinstance(linux, dict)
    if cgroup == "systemd":
        assert "--systemd-cgroup" in command[:run]
        assert linux["cgroupsPath"] == f"{sb.chat_slice(CHAT)}:alkera:{sb.chat_container(CHAT)}"
    else:
        assert "--systemd-cgroup" not in command
        assert linux["cgroupsPath"] == f"/alkera.slice/chat-{sb.chat_slug(CHAT)}"
        assert "/sys/fs/cgroup" not in linux["cgroupsPath"]


def test_gvisor_without_an_overlay_dir_keeps_the_root_overlay_in_memory() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor", overlay_dir=None), AGENT_ARGV)
    assert "--overlay2=root:memory" in launch.wrap(AGENT_ARGV)
    assert all(a[:2] != ("rm", "-rf") for a in shell_steps(launch.after_exit))


def test_gvisor_before_steps_write_the_oci_config_and_own_the_trees() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    oci = oci_of(launch)
    process = oci["process"]
    assert isinstance(process, dict)
    # The agent argv is baked into the OCI process, as its own uid, in its home,
    # under the tree's shared umask.
    assert process["args"] == [*sb.umask_prefix(), *AGENT_ARGV]
    assert process["user"] == {"uid": UID, "gid": UID}
    assert process["cwd"] == "/home/alkera"
    assert process["noNewPrivileges"] is True
    assert process["capabilities"]["bounding"] == []
    # The chown/cgroup steps still run first, then the bundle and the overlay
    # directories are made, then the network.
    argvs = shell_steps(launch.before)
    kinds = [a[0] for a in argvs]
    bundle_dir = argvs.index(("mkdir", "-p", sb.host_path(BUNDLE / "etc")))
    assert kinds.index("chown") < bundle_dir < kinds.index("ip")
    assert ("mkdir", "-p", sb.host_path(OVERLAY)) in argvs
    assert any(a[0] == "systemctl" and a[1] == "set-property" for a in argvs)


def test_gvisor_roots_at_the_staged_rootfs_and_mounts_no_host_os() -> None:
    oci = sb.gvisor_oci_spec(spec("gvisor"), AGENT_ARGV)
    assert oci["root"] == {"path": sb.host_path(ROOTFS), "readonly": True}
    mounts = oci["mounts"]
    assert isinstance(mounts, list)
    binds = {m["destination"]: m for m in mounts if m["type"] == "bind"}
    # The host's OS trees are not bound in: the rootfs is the OS.
    for host_tree in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/ssl"):
        assert host_tree not in binds, host_tree
    # The container has its own /proc, /dev, /sys, and private tmpfs mounts.
    kinds = {m["destination"]: m["type"] for m in mounts}
    assert kinds["/proc"] == "proc" and kinds["/sys"] == "sysfs" and kinds["/dev"] == "tmpfs"
    assert kinds["/tmp"] == kinds["/run"] == kinds["/dev/shm"] == "tmpfs"


def test_gvisor_oci_mounts_the_folder_at_home_once_and_each_internal_tree_at_its_place() -> None:
    oci = sb.gvisor_oci_spec(spec("gvisor"), AGENT_ARGV)
    mounts = oci["mounts"]
    assert isinstance(mounts, list)
    bound = {
        m["destination"]: (m["source"], "ro" in m["options"]) for m in mounts if m["type"] == "bind"
    }
    folder = sb.host_path(FOLDER)
    # The folder is the agent's root, at HOME, writable, and nowhere else.
    assert bound["/home/alkera"] == (folder, False)
    assert folder not in bound
    # The agent server's data, the environments and its state are writable at
    # their fixed internal paths; its config and the agent's tools read-only.
    assert bound[f"{sb.HARNESS_DATA_MOUNT}/agent"] == (sb.host_path(RUNTIME / "agent"), False)
    assert bound[sb.ENVS_MOUNT] == (sb.host_path(RUNTIME / "envs"), False)
    assert bound[sb.HARNESS_STATE_MOUNT] == (sb.host_path(STATE), False)
    assert bound[sb.HARNESS_CONFIG_MOUNT] == (sb.host_path(CONFIG_DIR), True)
    assert bound[sb.AGENT_MOUNT] == (sb.host_path(AGENT_DIR), True)
    assert bound[sb.RIPGREP_MOUNT] == (sb.host_path(RG_DIR), True)
    # No host path of the chat's is a destination: the runtime state as a
    # whole (the listen file and pid beside its parts), the config root and
    # the agent state under ALKERA_HOME, the chat folder.
    for host in (RUNTIME, CONFIG_DIR, STATE, AGENT_DIR, CHAT_DIR):
        assert sb.host_path(host) not in bound, host
    # The chat's records beside the folder, the runtime directory whole, the
    # overlay and the bundle are NOT mounted.
    sources = {m["source"] for m in mounts}
    for never in (CHAT_DIR, RUNTIME, OVERLAY, BUNDLE):
        assert sb.host_path(never) not in sources, never


def test_the_containers_view_is_the_root_the_os_and_the_internal_prefix() -> None:
    """The invariant the mount list exists to keep: every mount is the agent's
    root, a per-chat ``/etc`` file, a private filesystem of the container's
    own, or a tree under the one internal prefix. Nothing is bound at a host
    path, so the container shows nothing of the box's layout, and nothing of
    the chat's folder beside the root reaches it."""
    oci = sb.gvisor_oci_spec(spec("gvisor"), AGENT_ARGV)
    mounts = oci["mounts"]
    assert isinstance(mounts, list)
    private = {"/proc", "/dev", "/sys", "/tmp", "/run", "/dev/shm"}
    for mount in mounts:
        destination = mount["destination"]
        if mount["type"] != "bind":
            assert destination in private, destination
            continue
        allowed = (
            destination == "/home/alkera"
            or destination.startswith("/etc/")
            or destination.startswith(sb.INTERNAL_PREFIX + "/")
        )
        assert allowed, destination
        assert not destination.startswith(sb.host_path(CHAT_DIR)), destination
        assert not destination.startswith("/root/.alkera"), destination
    sources = {m["source"] for m in mounts if m["type"] == "bind"}
    chat_root = sb.host_path(CHAT_DIR) + "/"
    for source in sources:
        if source.startswith(chat_root):
            # Of the chat's folder, only its working directory and the two
            # runtime parts the agent server needs cross into the container.
            assert source in {
                sb.host_path(FOLDER),
                sb.host_path(RUNTIME / "agent"),
                sb.host_path(RUNTIME / "envs"),
            }, source
    for record in ("manifest.json", "chat.jsonl", "decisions.jsonl", "cost_ledger.jsonl"):
        assert sb.host_path(CHAT_DIR / record) not in sources


def test_gvisor_binds_the_per_chat_etc_files_from_the_bundle() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    written = writes(launch.before)
    etc = BUNDLE / "etc"
    passwd = written[etc / "passwd"]
    assert f"alkera:x:{UID}:{UID}:Alkera chat:/home/alkera:/bin/bash" in passwd
    assert f"alkera:x:{UID}:" in written[etc / "group"]
    assert written[etc / "hostname"].strip() == sb.chat_container(CHAT)
    assert f"{launch.network.container_ip} {sb.chat_container(CHAT)}" in written[etc / "hosts"]  # type: ignore[union-attr]
    assert written[etc / "resolv.conf"] == "nameserver 172.31.0.2\n"
    oci = oci_of(launch)
    mounts = oci["mounts"]
    assert isinstance(mounts, list)
    bound = {m["destination"]: m["source"] for m in mounts if m["type"] == "bind"}
    for name in ("passwd", "group", "hosts", "hostname", "resolv.conf"):
        assert bound[f"/etc/{name}"] == sb.host_path(etc / name)
        assert "ro" in next(m for m in mounts if m["destination"] == f"/etc/{name}")["options"]


def test_the_container_s_etc_files_are_readable_by_its_uid_under_the_daemon_s_umask() -> None:
    """The daemon runs under 077; written with its umask the container's
    ``passwd`` and ``resolv.conf`` came out 0600 root, and the chat's uid could
    neither name itself nor resolve its gateway."""
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    modes = {s.path.name: s.mode for s in launch.before if isinstance(s, sb.WriteStep)}
    for name in ("passwd", "group", "hosts", "hostname", "resolv.conf"):
        assert modes[name] == 0o644, name
    assert modes["config.json"] is None  # runsc's alone


def test_runsc_starts_under_the_umask_its_mountpoints_need_and_a_command_does_not() -> None:
    """runsc makes the bind mountpoints under ``/opt/alkera/harness`` with its
    own umask; under the daemon's 077 they are 0700 root and the chat's uid
    cannot walk to its config. A command exec'd into the live container makes
    no mountpoint and keeps the daemon's."""
    run = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    assert run.umask == sb.RUNSC_UMASK == 0o022
    command = sb.GvisorRuntime().compose_launch(sb.shell_spec(spec("gvisor")))
    assert command.umask is None
    assert sb.NoneRuntime().compose_launch(spec("none")).umask is None


def test_the_spawn_hands_the_launch_s_umask_to_the_child_and_no_other() -> None:
    run = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    _command, kwargs = popen_args(sandbox_spec(AGENT_ARGV, sandbox=run, env={}))
    assert kwargs["umask"] == 0o022
    _command, kwargs = popen_args(sandbox_spec(AGENT_ARGV, sandbox=sb.NO_SANDBOX, env={}))
    assert "umask" not in kwargs


def test_gvisor_oci_joins_the_chats_own_network_namespace_and_carries_the_limits() -> None:
    oci = sb.gvisor_oci_spec(spec("gvisor", "systemd"), AGENT_ARGV)
    linux = oci["linux"]
    assert isinstance(linux, dict)
    namespaces = {n["type"]: n for n in linux["namespaces"]}
    assert {"network", "pid", "mount", "ipc", "uts"} <= set(namespaces)
    assert namespaces["network"]["path"] == f"/run/netns/{sb.chat_netns(CHAT)}"
    assert linux["resources"]["cpu"]["quota"] == 2 * 100_000
    assert linux["resources"]["memory"]["limit"] == 2048 * 1024 * 1024
    # memory+swap equal to memory: the container has no swap at all.
    assert linux["resources"]["memory"]["swap"] == 2048 * 1024 * 1024
    assert linux["resources"]["pids"]["limit"] == sb.TASKS_MAX
    assert linux["cgroupsPath"].startswith("alkera-chat-chat0123456789abcdef.slice")


def test_gvisor_oci_bakes_the_agents_full_environment_under_the_sandboxs_own_names() -> None:
    oci = sb.gvisor_oci_spec(spec("gvisor"), AGENT_ARGV)
    process = oci["process"]
    assert isinstance(process, dict)
    env = dict(item.split("=", 1) for item in process["env"])
    # The container does not inherit runsc's environment; the agent's own is
    # baked in whole...
    assert env["ALKERA_GATEWAY_URL"] == "https://gw.example"
    # ...under the sandbox's own names: the home, the container PATH with the
    # default environment and the agent's tools first, and the environment.
    assert env["HOME"] == "/home/alkera"
    assert env["PATH"] == f"{ENV_INSIDE}/bin:{sb.RIPGREP_MOUNT}:{sb._CONTAINER_PATH}"
    assert "/host/bin" not in env["PATH"]
    assert env["VIRTUAL_ENV"] == ENV_INSIDE
    assert env["LANG"] == "C.UTF-8"
    # The agent server stands in its root; its own temporary files (the runtime
    # it is built with unpacks pieces of itself at start) go to the container's
    # private tmpfs, never into the root the drive syncs.
    assert env["PWD"] == "/home/alkera"
    assert env["TMPDIR"] == sb.CONTAINER_TMP == "/tmp"
    # A command on the chat's behalf keeps its temporary files beside the
    # default environment, on disk and outside the root the drive syncs: in
    # the root, an installer's scratch synced up and landed in the Trash.
    shell = sb.GvisorRuntime().compose_launch(sb.shell_spec(spec("gvisor")))
    assert shell.env["TMPDIR"] == f"{sb.ENVS_MOUNT}/{COMMAND_TMP_SUBDIR}"
    assert not shell.env["TMPDIR"].startswith(shell.env["HOME"])
    # ...and that directory is made, as the chat's own, before the command runs.
    default_env = spec("gvisor").default_env
    assert default_env is not None
    tmp_host = sb.host_path(command_tmp_dir(default_env))
    made = [s.argv for s in shell.before if isinstance(s, sb.ShellStep)]
    assert any("mkdir-owned" in argv and tmp_host in argv for argv in made)
    assert sb.RepairStep(Path(tmp_host), TreeIdentity(UID, UID)) in shell.before
    # What only describes the daemon's host environment does not go in: the
    # reverse diff, the directory the daemon was started in, the box's name.
    assert "ALKERA_SHELL_ENV_RESTORE" not in env
    assert "HOSTNAME" not in env
    assert "/opt/alkera-work" not in env.values()


def test_the_host_only_names_are_the_adapters_own_spelling() -> None:
    assert opencode_http.SHELL_ENV_RESTORE_VAR in sb.HOST_ONLY_ENV
    assert {"PWD", "OLDPWD", "HOSTNAME"} <= set(sb.HOST_ONLY_ENV)


@pytest.mark.parametrize(
    ("mode", "mount_alias", "host", "seen"),
    [
        pytest.param("gvisor", False, FOLDER / "a" / "b.csv", "/home/alkera/a/b.csv", id="folder"),
        pytest.param("gvisor", False, FOLDER, "/home/alkera", id="folder-itself"),
        pytest.param("gvisor", False, ENV / "bin" / "pip", f"{ENV_INSIDE}/bin/pip", id="env"),
        pytest.param(
            "gvisor",
            False,
            RUNTIME / "agent" / "agent.db",
            f"{sb.HARNESS_DATA_MOUNT}/agent/agent.db",
            id="agent-data",
        ),
        pytest.param(
            "gvisor",
            False,
            CONFIG_DIR / "global-instructions.md",
            f"{sb.HARNESS_CONFIG_MOUNT}/global-instructions.md",
            id="config",
        ),
        pytest.param(
            "gvisor",
            False,
            STATE / "listen-url",
            f"{sb.HARNESS_STATE_MOUNT}/listen-url",
            id="state",
        ),
        pytest.param(
            "gvisor", False, AGENT_DIR / "opencode", f"{sb.AGENT_MOUNT}/opencode", id="binary"
        ),
        pytest.param(
            "gvisor", False, RUNTIME / "pid", sb.host_path(RUNTIME / "pid"), id="runtime-unbound"
        ),
        pytest.param(
            "gvisor",
            False,
            CHAT_DIR / "manifest.json",
            sb.host_path(CHAT_DIR / "manifest.json"),
            id="record",
        ),
        pytest.param(
            "none", True, FOLDER / "x.txt", "/home/alkera/x.txt", id="none-aliased-folder"
        ),
        pytest.param("none", True, ENV, sb.host_path(ENV), id="none-aliased-env-stays"),
        pytest.param(
            "none", False, FOLDER / "x.txt", sb.host_path(FOLDER / "x.txt"), id="none-plain"
        ),
    ],
)
def test_agent_path_spells_a_host_path_where_the_agent_sees_it(
    mode: sb.SandboxMode, mount_alias: bool, host: Path, seen: str
) -> None:
    """One spelling for every path the daemon composes for the agent: the
    folder under the home where it is mounted there, a bound tree under its
    destination inside a container, and the host path wherever nothing moved
    it. A host path a bind does not cover (the chat's records, the runtime
    directory's own files) is spelled as the host spells it, which inside the
    container names nothing, exactly as it should."""
    assert spec(mode, mount_alias=mount_alias).agent_path(host) == seen


def _relocation_steps(launch: sb.SandboxLaunch) -> list[sb.ShellStep]:
    return [
        s for s in launch.before if isinstance(s, sb.ShellStep) and sb.RELOCATE_SOURCE in s.argv
    ]


def _exit_relocation_steps(launch: sb.SandboxLaunch) -> list[sb.ShellStep]:
    return [
        s for s in launch.after_exit if isinstance(s, sb.ShellStep) and sb.RELOCATE_SOURCE in s.argv
    ]


def test_the_environment_is_made_relocatable_on_every_spawn_and_after_every_exit() -> None:
    """The launch runs the relocation after pip's seed wrote the scripts
    with the host interpreter, and again once the agent server has exited (so
    what ``pip`` installed during the session is relocatable before a box
    could be rolled back): the managed interpreter runs the program, isolated
    and without ``site`` so nothing in the environment runs in it, as the
    chat's uid like the steps before it. Under gVisor the pairs name the
    environment's own move and the folder's (the host spelling an earlier
    build also showed); on a ``none`` box with the folder bound at the home,
    the folder's alone; on one with nothing bound, none, and the scripts are
    still rewritten into the form that runs wherever the environment is."""
    s = spec("gvisor")
    under_runsc = sb.GvisorRuntime().compose_launch(s, AGENT_ARGV)
    (relocate,) = _relocation_steps(under_runsc)
    assert relocate.check is False
    program = sb.relocate_argv(
        f"{sb.DEFAULT_PYTHON_HOME}/bin/python3",
        sb.host_path(ENV),
        [(sb.host_path(ENV), ENV_INSIDE), (sb.host_path(FOLDER), "/home/alkera")],
    )
    assert program[1:4] == ("-I", "-S", "-c")
    assert relocate.argv[-len(program) :] == program
    assert relocate.argv[: len(sb.uid_prefix(s))] == sb.uid_prefix(s)
    steps = [st for st in under_runsc.before if isinstance(st, sb.ShellStep)]
    assert steps.index(relocate) > next(
        i for i, st in enumerate(steps) if SEED_PIP_SOURCE in st.argv
    )
    (after,) = _exit_relocation_steps(under_runsc)
    assert after == relocate
    assert under_runsc.after_exit.index(after) == 0, "before the container is deleted"
    aliased = sb.NoneRuntime().compose_launch(spec("none", mount_alias=True), AGENT_ARGV)
    (relocate_aliased,) = _relocation_steps(aliased)
    assert relocate_aliased.argv[-2:] == (sb.host_path(FOLDER), "/home/alkera")
    assert relocate_aliased.argv[-3] == sb.host_path(ENV)
    assert _exit_relocation_steps(aliased) == [relocate_aliased]
    plain = sb.NoneRuntime().compose_launch(spec("none"), AGENT_ARGV)
    (relocate_plain,) = _relocation_steps(plain)
    assert relocate_plain.argv[-1] == sb.host_path(ENV), "no pairs: nothing moved"
    assert _exit_relocation_steps(plain) == [relocate_plain]
    command = sb.NoneRuntime().compose_launch(sb.shell_spec(spec("none")), ("/bin/sh", "-c", "x"))
    assert _exit_relocation_steps(command) == [], "a command's launch owns no exit"


def test_a_step_is_logged_by_its_program_not_its_inline_source() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    (relocate,) = _relocation_steps(launch)
    text = sb.argv_text(relocate.argv)
    assert "<script>" in text and "\n" not in text and "import os" not in text


def test_the_launch_takes_down_what_an_earlier_build_left_in_the_shared_rootfs() -> None:
    """runsc makes each bind's mountpoint inside the rootfs on the host, and the
    rootfs is shared: the build that bound the chat's trees at their host paths
    left every chat's folder and agent root there as empty directories, which
    every later chat could list. The launch removes them, empty directories
    only (``rmdir`` cannot take a file or a populated directory), and only for
    the trees whose destination moved; ``rg``, bound at the same path before
    and after, has nothing to take down."""
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    cleanup = next(a for a in shell_steps(launch.before) if a[0] == "/bin/sh" and "rmdir" in a[2])
    # Empty directories only, each with its empty parents, and never a failure:
    # nothing to take down is the usual case.
    assert "rmdir -p --ignore-fail-on-non-empty" in cleanup[2] and cleanup[2].endswith("exit 0")
    assert '[ -d "$d" ]' in cleanup[2]
    inside = set(cleanup[4:])
    root = sb.host_path(ROOTFS)
    # Each moved tree and its parent: the earlier build bound the runtime
    # state whole, so its old mountpoint is the parent of the parts bound now.
    assert inside == {
        f"{root}{sb.host_path(RUNTIME / 'agent')}",
        f"{root}{sb.host_path(RUNTIME / 'envs')}",
        f"{root}{sb.host_path(RUNTIME)}",
        f"{root}{sb.host_path(STATE)}",
        f"{root}{sb.host_path(CONFIG_DIR)}",
        f"{root}{sb.host_path(CONFIG)}",
        f"{root}{sb.host_path(AGENT_DIR)}",
        f"{root}{sb.host_path(AGENT_DIR.parent)}",
        f"{root}{sb.host_path(FOLDER)}",
        f"{root}{sb.host_path(CHAT_DIR)}",
    }
    assert f"{root}{sb.host_path(RG_DIR)}" not in inside
    step = next(
        s
        for s in launch.before
        if isinstance(s, sb.ShellStep) and s.argv[0] == "/bin/sh" and "rmdir" in s.argv[2]
    )
    assert step.check is False
    # Before the config is written and the network is made, after the trees are owned.
    argvs = shell_steps(launch.before)
    heads = [a[0] for a in argvs]
    assert heads.index("chown") < argvs.index(step.argv) < heads.index("ip")


def test_the_rootfs_cleanup_never_touches_the_hosts_own_root() -> None:
    at_root = spec("gvisor", rootfs=Path("/"))
    assert sb.legacy_mountpoint_steps(at_root) == ()
    assert sb.legacy_mountpoint_steps(spec("gvisor", rootfs=None)) == ()


def test_uv_makes_the_environment_relocatable() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    venv = next(a for a in shell_steps(launch.before) if "venv" in a)
    assert "--relocatable" in venv


OLD_ROOT = "/opt/alkera-work/.alkera/chats/chat_0123456789abcdef/scratch"
NEW_ROOT = "/home/alkera"


def _env_scripts(root: Path, old: str, *, old_root: str = OLD_ROOT) -> Path:
    env = root / "alkera"
    bin_dir = env / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pip").write_bytes(f"#!{old}/bin/python\n# pip\nimport sys\n".encode())
    (bin_dir / "pip3.12").write_bytes(f"#!{old}/bin/python3.12\nrun()\n".encode())
    (bin_dir / "isolated").write_bytes(f"#!{old}/bin/python -I -s\nrun()\n".encode())
    # A script this layout's own build wrote with the internal spelling.
    (bin_dir / "cowsay").write_bytes(f"#!{ENV_INSIDE}/bin/python\nmoo()\n".encode())
    (bin_dir / "activate").write_text(
        f'VIRTUAL_ENV="{old}"\nexport VIRTUAL_ENV\nPATH="{old}/bin:$PATH"\n', encoding="utf-8"
    )
    (bin_dir / "activate.fish").write_text(f"set -gx VIRTUAL_ENV '{old}'\n", encoding="utf-8")
    (bin_dir / "black").write_bytes(b"#!/usr/bin/env python3\nprint('x')\n")
    (bin_dir / "note.txt").write_bytes(f"see {old}/bin\n".encode())
    (bin_dir / "python").symlink_to("/opt/alkera/python/current/bin/python3")
    site = env / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    # An editable install made by the folder's old host spelling: setuptools
    # writes the project path into a .pth and a finder module.
    (site / "__editable__.pkg-1.0.pth").write_text(f"{old_root}/pkg\n", encoding="utf-8")
    (site / "__editable___pkg_1_0_finder.py").write_text(
        f"MAPPING = {{'pkg': '{old_root}/pkg'}}\n", encoding="utf-8"
    )
    (site / "distutils-precedence.pth").write_text("import _distutils_hack\n", encoding="utf-8")
    (site / "pkg_settings.py").write_text(f"ROOT = '{old_root}'\n", encoding="utf-8")
    return env


def _relocate(env: Path, pairs: list[tuple[str, str]]) -> int:
    """Run the relocation program the way the launch does, with this test's
    interpreter standing in for the box's managed one."""
    done = subprocess.run(
        sb.relocate_argv(sys.executable, str(env), pairs),
        capture_output=True,
        text=True,
        check=False,
        cwd=env.parent,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    return int(done.stdout.strip())


def _trampoline(interpreter: str, flags: str = "") -> bytes:
    """The location-independent first lines a relocated console script gets:
    ``sh`` ``exec``s the interpreter beside the script; Python reads the same
    two lines as one string and runs the body."""
    where = '"$(dirname -- "$(realpath -- "$0" 2>/dev/null || printf %s "$0")")"'
    return f"#!/bin/sh\n'''exec' {where}/{interpreter}{flags} \"$0\" \"$@\"\n' '''\n".encode()


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks and POSIX scripts")
def test_the_relocation_respells_what_names_an_old_path_and_nothing_else(tmp_path: Path) -> None:
    old = sb.host_path(tmp_path / "host" / "envs" / "alkera")
    new = ENV_INSIDE
    env = _env_scripts(tmp_path / "host" / "envs", old)
    pairs = [(old, new), (OLD_ROOT, NEW_ROOT)]
    changed = _relocate(env, pairs)
    bin_dir = env / "bin"
    # Console scripts: the shebang line becomes the trampoline (whichever of
    # the environment's spellings it named, flags kept), the body untouched.
    assert (bin_dir / "pip").read_bytes() == _trampoline("python") + b"# pip\nimport sys\n"
    assert (bin_dir / "pip3.12").read_bytes() == _trampoline("python3.12") + b"run()\n"
    assert (bin_dir / "isolated").read_bytes() == _trampoline("python", " -I -s") + b"run()\n"
    assert (bin_dir / "cowsay").read_bytes() == _trampoline("python") + b"moo()\n"
    # Activation scripts: every spelling of the path.
    assert (bin_dir / "activate").read_text(encoding="utf-8") == (
        f'VIRTUAL_ENV="{new}"\nexport VIRTUAL_ENV\nPATH="{new}/bin:$PATH"\n'
    )
    assert (bin_dir / "activate.fish").read_text(
        encoding="utf-8"
    ) == f"set -gx VIRTUAL_ENV '{new}'\n"
    # The editable install now names the root as the agent sees it.
    site = env / "lib" / "python3.12" / "site-packages"
    assert (site / "__editable__.pkg-1.0.pth").read_text(encoding="utf-8") == f"{NEW_ROOT}/pkg\n"
    assert (site / "__editable___pkg_1_0_finder.py").read_text(encoding="utf-8") == (
        f"MAPPING = {{'pkg': '{NEW_ROOT}/pkg'}}\n"
    )
    # A script of another interpreter, a file that is no script, a link, a .pth
    # that names no path, and a module (not a .pth, not a finder) are left
    # exactly as they were.
    assert (bin_dir / "black").read_bytes() == b"#!/usr/bin/env python3\nprint('x')\n"
    assert (bin_dir / "note.txt").read_bytes() == f"see {old}/bin\n".encode()
    assert (bin_dir / "python").is_symlink()
    assert (site / "distutils-precedence.pth").read_text(
        encoding="utf-8"
    ) == "import _distutils_hack\n"
    assert (site / "pkg_settings.py").read_text(encoding="utf-8") == f"ROOT = '{OLD_ROOT}'\n"
    assert changed == 8
    # Idempotent: a second spawn finds nothing to respell.
    assert _relocate(env, pairs) == 0
    assert _relocate(env, [(new, new)]) == 0
    assert _relocate(env, []) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks and POSIX scripts")
def test_an_environment_that_did_not_move_still_gets_relocatable_scripts(tmp_path: Path) -> None:
    """A ``none`` box runs the environment where it made it, so there is no
    pair; its scripts are still rewritten, since the chat may wake on a box
    that mounts the environment somewhere else."""
    old = sb.host_path(tmp_path / "host" / "envs" / "alkera")
    env = _env_scripts(tmp_path / "host" / "envs", old)
    assert _relocate(env, []) == 3
    assert (env / "bin" / "pip").read_bytes() == _trampoline("python") + b"# pip\nimport sys\n"
    assert (env / "bin" / "cowsay").read_bytes().startswith(b"#!/opt/alkera/envs"), (
        "a spelling that is nobody's on this box is left alone"
    )
    assert (env / "bin" / "activate").read_text(encoding="utf-8").startswith(f'VIRTUAL_ENV="{old}"')


@pytest.mark.skipif(sys.platform == "win32", reason="runs real scripts through /bin/sh")
def test_a_relocated_script_runs_wherever_the_environment_is_mounted(tmp_path: Path) -> None:
    """The point of the trampoline: a console script runs from the path the
    previous build mounts the environment at, from the path this build mounts
    it at, and from the previous one again after a rollback, each time with the
    interpreter beside it. The environment is a real directory with a real
    interpreter, moved between the two paths as the mounts would show it."""
    old_home = tmp_path / "host" / "envs"
    env = old_home / "alkera"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python3").symlink_to(sys.executable)
    tool = env / "bin" / "tool"
    tool.write_bytes(
        f"#!{env}/bin/python3\nimport sys\nprint(sys.argv[0])\nprint(sys.executable)\n".encode()
    )
    tool.chmod(0o755)
    new_home = tmp_path / "opt" / "envs"
    new_home.mkdir(parents=True)
    new = new_home / "alkera"
    assert _relocate(env, [(str(env), str(new))]) == 1

    def run(at: Path) -> list[str]:
        done = subprocess.run(
            [str(at / "bin" / "tool")], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.split()

    script, interpreter = run(env)
    assert script == str(env / "bin" / "tool") and Path(interpreter).parent == env / "bin"
    env.rename(new)
    script, interpreter = run(new)
    assert script == str(new / "bin" / "tool") and Path(interpreter).parent == new / "bin"
    new.rename(env)
    script, interpreter = run(env)
    assert script == str(env / "bin" / "tool") and Path(interpreter).parent == env / "bin"
    assert _relocate(env, [(str(env), str(new))]) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks and POSIX scripts")
def test_the_relocation_follows_no_link(tmp_path: Path) -> None:
    """A link in the environment's place, in ``bin``'s, or in a script's is left
    where it points: the program runs as the chat's uid and still touches
    nothing by a name the agent redirected."""
    old = sb.host_path(tmp_path / "host" / "envs" / "alkera")
    pairs = [(old, ENV_INSIDE)]
    real = _env_scripts(tmp_path / "real", old)
    before = (real / "bin" / "pip").read_bytes()
    linked_env = tmp_path / "envs" / "alkera"
    linked_env.parent.mkdir()
    linked_env.symlink_to(real)
    assert _relocate(linked_env, pairs) == 0
    linked_bin = tmp_path / "env2" / "alkera"
    linked_bin.mkdir(parents=True)
    (linked_bin / "bin").symlink_to(real / "bin")
    assert _relocate(linked_bin, pairs) == 0
    linked_script = tmp_path / "env3" / "alkera"
    (linked_script / "bin").mkdir(parents=True)
    (linked_script / "bin" / "pip").symlink_to(real / "bin" / "pip")
    assert _relocate(linked_script, pairs) == 0
    assert (real / "bin" / "pip").read_bytes() == before


def test_the_relocation_leaves_an_environment_it_cannot_read_alone(tmp_path: Path) -> None:
    assert _relocate(tmp_path / "missing", [("/a", "/b")]) == 0


def test_the_exec_wrapper_enters_the_container_where_the_caller_says() -> None:
    """A command run in a subdirectory of the root enters the container
    there, spelled as the container sees it; one that names no place enters
    at the agent's home."""
    launch = sb.GvisorRuntime().compose_launch(sb.shell_spec(spec("gvisor")))
    at_home = launch.wrap(["/bin/true"])
    assert "--cwd=/home/alkera" in at_home
    below = launch.wrap(["/bin/true"], cwd="/home/alkera/data")
    assert "--cwd=/home/alkera/data" in below and "--cwd=/home/alkera" not in below
    assert below.index("--cwd=/home/alkera/data") < below.index(sb.chat_container(CHAT))


def test_the_exec_wrapper_names_where_runsc_writes_the_commands_pid() -> None:
    """A command's launch carries the container it runs in, so its tree can
    be ended there: the wrapper asks runsc for the command's pid inside the
    container, as a host path the daemon reads; a launch with no container
    (the ``none`` mode) has no such flag and no container to name."""
    launch = sb.GvisorRuntime().compose_launch(sb.shell_spec(spec("gvisor")))
    assert launch.container == sb.chat_container(CHAT)
    assert launch.runsc == spec("gvisor").runsc and launch.uid == UID
    assert launch.runsc_root == sb.host_path(spec("gvisor").runsc_root)
    pid_file = Path("/tmp/alkera-exec-abc.pid")
    flagged = launch.wrap(["/bin/true"], pid_file=pid_file)
    assert f"--internal-pid-file={sb.host_path(pid_file)}" in flagged
    assert flagged.index(f"--internal-pid-file={sb.host_path(pid_file)}") < flagged.index(
        sb.chat_container(CHAT)
    )
    assert not any(a.startswith("--internal-pid-file=") for a in launch.wrap(["/bin/true"]))
    plain = sb.NoneRuntime().compose_launch(sb.shell_spec(spec("none")))
    assert plain.container is None
    assert not any(
        a.startswith("--internal-pid-file=") for a in plain.wrap(["/bin/true"], pid_file=pid_file)
    )


def test_gvisor_network_steps_make_the_namespace_the_pair_and_the_port_grant() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    net = launch.network
    assert net is not None
    argvs = shell_steps(launch.before)
    ip = [a for a in argvs if a[0] == "ip"]
    # A leftover from a crashed run goes first, unchecked; then the fresh namespace.
    leftovers = [s for s in launch.before if isinstance(s, sb.ShellStep) and s.argv[0] == "ip"][:2]
    assert [s.argv[1:3] for s in leftovers] == [("netns", "delete"), ("link", "delete")]
    assert all(not s.check for s in leftovers)
    assert ("ip", "netns", "add", net.namespace) in ip
    pair = next(a for a in ip if a[1:3] == ("link", "add"))
    assert pair[3] == net.host_if and "veth" in pair and pair[-2:] == ("netns", net.namespace)
    assert ("ip", "addr", "add", f"{net.host_ip}/30", "dev", net.host_if) in ip
    assert ("ip", "link", "set", net.host_if, "up") in ip
    assert ("ip", "-n", net.namespace, "addr", "add", f"{net.container_ip}/30", "dev", "eth0") in ip
    assert ("ip", "-n", net.namespace, "link", "set", "eth0", "up") in ip
    assert ("ip", "-n", net.namespace, "link", "set", "lo", "up") in ip
    assert ("ip", "-n", net.namespace, "route", "add", "default", "via", net.host_ip) in ip
    # The daemon's port is granted to this chat's host end and no other.
    grant = next(a for a in argvs if a[0] == "nft")
    assert grant[1:3] == ("add", "element")
    assert grant[-1] == f'{{ "{net.host_if}" . 41234 }}'
    assert sb.NFT_CHAT_PORTS_SET in grant
    # Made after the OCI config is written, since runsc reads both at run.
    written = [i for i, s in enumerate(launch.before) if isinstance(s, sb.WriteStep)]
    first_ip = next(
        i for i, s in enumerate(launch.before) if isinstance(s, sb.ShellStep) and s.argv[0] == "ip"
    )
    assert max(written) < first_ip


def test_gvisor_after_exit_deletes_the_container_the_network_the_cgroup_and_the_overlay() -> None:
    launch = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    net = launch.network
    assert net is not None
    steps = launch.after_exit
    argvs = shell_steps(steps)
    assert all(isinstance(s, sb.ShellStep) and not s.check for s in steps)  # cleanup never refuses
    kinds = [a[0] for a in argvs]
    assert any(a[0].endswith("runsc") and "delete" in a for a in argvs)
    revoke = next(a for a in argvs if a[0] == "nft")
    assert revoke[1:3] == ("delete", "element") and revoke[-1] == f'{{ "{net.host_if}" . 41234 }}'
    assert ("ip", "netns", "delete", net.namespace) in argvs
    assert any(a[0] == "systemctl" and a[1] == "stop" for a in argvs)
    assert ("rm", "-rf", sb.host_path(OVERLAY)) in argvs
    # The container goes before its network and its cgroup.
    assert kinds.index("runsc") < kinds.index("nft") < kinds.index("ip") < kinds.index("systemctl")


def test_gvisor_shell_command_execs_into_the_agents_container_with_its_environment() -> None:
    # A shell command run on the chat's behalf joins the agent's live container.
    shell = sb.shell_spec(spec("gvisor"))
    launch = sb.GvisorRuntime().compose_launch(shell)
    assert launch.command is None  # a prefix wrapper, so a command can be appended
    assert launch.exec_env is True
    env = launch.apply_env({"PATH": "/host/bin", "HOME": "/opt/host-home", "TERM": "xterm"})
    command = launch.wrap(["/bin/bash", "-c", "ls"], env=env)
    assert command[0].endswith("runsc")
    assert "exec" in command
    assert f"--user={UID}:{UID}" in command
    assert "--cwd=/home/alkera" in command
    # A process runsc exec starts begins with the container's own environment,
    # the agent server's (loopback password, inline config with the gateway
    # key); the command is started through ``env -i`` right after the container
    # id, with exactly the names the caller composed and nothing else.
    container = command.index(sb.chat_container(CHAT))
    assert command[container + 1 : container + 3] == [sb.CONTAINER_ENV, "-i"]
    names = command[container + 3 : -7]
    assert sorted(names) == sorted(f"{k}={v}" for k, v in env.items())
    assert "--env" not in command
    assert "HOME=/home/alkera" in names  # the sandbox's names win over the caller's
    assert f"PATH={ENV_INSIDE}/bin:{sb.RIPGREP_MOUNT}:{sb._CONTAINER_PATH}" in names
    assert "TERM=xterm" in names  # the caller's other names pass through
    # Run under the tree's shared umask, after its environment.
    assert command[-7:] == [*sb.umask_prefix(), "/bin/bash", "-c", "ls"]
    # The shell command does not own or make the cgroup — it joined the agent's.
    assert all(
        not (
            isinstance(s, sb.ShellStep) and s.argv[0] == "systemctl" and s.argv[1] == "set-property"
        )
        for s in launch.before
    )
    assert all(a[0] != "ip" for a in shell_steps(launch.before))  # nor the network


def test_an_exec_launch_with_no_environment_still_starts_from_an_empty_one() -> None:
    """A caller that composed nothing gets nothing: never the container's."""
    launch = sb.GvisorRuntime().compose_launch(sb.shell_spec(spec("gvisor")))
    command = launch.wrap(["/bin/true"])
    assert command[-8:] == [
        sb.chat_container(CHAT),
        sb.CONTAINER_ENV,
        "-i",
        *sb.umask_prefix(),
        "/bin/true",
    ]


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        pytest.param({"bundle": None}, "bundle", id="no-bundle"),
        pytest.param({"rootfs": None}, "rootfs", id="no-staged-rootfs"),
        pytest.param({"resolvers": ()}, "resolver", id="no-reachable-resolver"),
        pytest.param({"uid": 60000}, "outside", id="uid-outside-the-range"),
    ],
)
def test_a_gvisor_agent_server_the_box_cannot_place_is_refused(
    overrides: dict[str, object], why: str
) -> None:
    with pytest.raises(sb.SandboxRefusedError, match=why):
        sb.GvisorRuntime().compose_launch(spec("gvisor", **overrides), AGENT_ARGV)


def test_a_gvisor_agent_server_without_argv_is_refused() -> None:
    with pytest.raises(sb.SandboxRefusedError):
        sb.GvisorRuntime().compose_launch(spec("gvisor"), ())


# ---------------------------------------------------------------------------
# The per-chat network plan
# ---------------------------------------------------------------------------


def test_the_network_is_a_slash_30_per_uid_named_from_the_uid() -> None:
    net = sb.plan_network(CHAT, UID)
    assert net.namespace == sb.chat_netns(CHAT) == sb.chat_container(CHAT)
    assert net.host_if == f"vc{UID}" and len(net.host_if) <= 15
    assert net.container_if == "eth0" and net.prefix == 30
    block = ipaddress.ip_network(f"{net.host_ip}/30", strict=False)
    assert ipaddress.ip_address(net.container_ip) in block
    assert int(ipaddress.ip_address(net.container_ip)) == int(ipaddress.ip_address(net.host_ip)) + 1
    # The i-th uid takes the i-th /30 of the block.
    offset = (UID - sb.UID_MIN) * 4
    assert (
        int(ipaddress.ip_address(net.host_ip))
        == int(ipaddress.ip_address("10.200.0.0")) + offset + 1
    )
    assert net.namespace_path == f"/run/netns/{net.namespace}"


def test_every_uid_in_the_range_gets_its_own_block_inside_the_chat_net() -> None:
    chat_net = ipaddress.ip_network(sb.DEFAULT_CHAT_NET)
    first, last = sb.plan_network("a", sb.UID_MIN), sb.plan_network("b", sb.UID_MAX)
    for net in (first, last):
        assert ipaddress.ip_address(net.host_ip) in chat_net
        assert ipaddress.ip_address(net.container_ip) in chat_net
    assert first.host_if != last.host_if and first.host_ip != last.host_ip
    with pytest.raises(sb.SandboxRefusedError):
        sb.plan_network("c", sb.UID_MIN - 1)
    with pytest.raises(sb.SandboxRefusedError):
        sb.plan_network("c", sb.UID_MAX + 1)


@pytest.mark.parametrize(
    "raw",
    ["", "not-a-net", "10.200.0.0/16", "10.200.0.1/14", "fd00::/64", "10.0.0.0/8"],
)
def test_a_chat_net_that_cannot_hold_the_range_or_is_malformed_reads_as_the_default(
    raw: str,
) -> None:
    block = sb.chat_network_block(raw)
    if raw == "10.0.0.0/8":
        assert str(block) == "10.0.0.0/8"  # wide enough, kept
    else:
        assert str(block) == sb.DEFAULT_CHAT_NET


def test_the_box_firewall_rules_for_the_chat_network_name_the_set_the_launch_fills() -> None:
    rules = sb.chat_network_rules()
    assert rules[0] == f"iifname . tcp dport @{sb.NFT_CHAT_PORTS_SET} accept"
    assert any(sb.METADATA_IPV4 in r and "drop" in r for r in rules)
    assert any(sb.METADATA_IPV6 in r and "drop" in r for r in rules)
    assert any(sb.DEFAULT_CHAT_NET in r and "drop" in r for r in rules)
    assert any("masquerade" in r for r in rules)


# ---------------------------------------------------------------------------
# Runtime selection: fail closed
# ---------------------------------------------------------------------------


def test_select_runtime_picks_the_mode() -> None:
    assert isinstance(sb.select_runtime("none", gvisor_ready=False), sb.NoneRuntime)
    assert isinstance(sb.select_runtime("none", gvisor_ready=True), sb.NoneRuntime)
    assert isinstance(sb.select_runtime("gvisor", gvisor_ready=True), sb.GvisorRuntime)


def test_a_gvisor_box_that_cannot_run_runsc_refuses_rather_than_downgrading() -> None:
    # The whole point of fail-closed: a box set to gvisor with no working runsc
    # must NOT silently run the agent unsandboxed.
    with pytest.raises(sb.SandboxRefusedError):
        sb.select_runtime("gvisor", gvisor_ready=False)


@pytest.mark.parametrize(
    ("mode", "gvisor", "mount_alias", "expected"),
    [
        pytest.param("gvisor", True, False, "/home/alkera", id="gvisor-ready"),
        pytest.param("gvisor", False, True, None, id="gvisor-not-ready-refuses-anyway"),
        pytest.param("none", False, True, "/home/alkera", id="none-with-a-mount-namespace"),
        pytest.param("none", True, False, None, id="none-without-one"),
    ],
)
def test_the_agents_home_alias_exists_only_where_the_launch_mounts_it(
    mode: sb.SandboxMode, gvisor: bool, mount_alias: bool, expected: str | None
) -> None:
    settings = sb.SandboxSettings(mode=mode)
    assert sb.agent_home(settings, gvisor=gvisor, mount_alias=mount_alias) == expected


# ---------------------------------------------------------------------------
# Path spelling, uid ownership, cgroup — kept mechanics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["gvisor", "none"])
def test_paths_are_spelled_for_the_linux_host_whatever_composes_the_launch(
    mode: sb.SandboxMode,
) -> None:
    """A path composed on a host whose ``Path`` joins with backslashes must
    still reach runsc, chown and setfacl as ``/opt/...``, never ``\\opt\\...``."""

    def win(path: Path) -> PureWindowsPath:
        return PureWindowsPath(path.as_posix())

    windows_spec = sb.SandboxSpec(
        chat_id=CHAT,
        folder=win(FOLDER),
        uid=UID,
        vcpu=2,
        memory_mb=2048,
        home="/home/alkera",
        mode=mode,
        cgroup="systemd",
        binds=(
            sb.Bind(win(RUNTIME / "agent"), f"{sb.HARNESS_DATA_MOUNT}/agent"),
            sb.Bind(win(CONFIG_DIR), sb.HARNESS_CONFIG_MOUNT, readonly=True, kind="config"),
        ),
        bundle=win(BUNDLE),
        rootfs=win(ROOTFS),
        overlay_dir=win(OVERLAY),
        default_env=win(ENV),
        resolvers=("172.31.0.2",),
    )
    runtime = sb.GvisorRuntime() if mode == "gvisor" else sb.NoneRuntime()
    launch = runtime.compose_launch(windows_spec, AGENT_ARGV)
    blob = json.dumps(
        [
            s.argv
            if isinstance(s, sb.ShellStep)
            else str(s.tree)
            if isinstance(s, sb.RepairStep)
            else (str(s.path), s.content)
            for s in launch.before
        ]
    )
    blob += " ".join(launch.wrap(AGENT_ARGV))
    blob += json.dumps(dict(launch.env))
    assert "\\\\opt" not in blob and "\\opt" not in blob
    assert "/opt/alkera-work" in blob


def test_uid_steps_own_close_and_lend_the_chat_trees_in_an_order_that_keeps_the_acls() -> None:
    every = list(sb.uid_steps(spec()))
    steps = shell_steps(every)
    repairs = [s for s in every if isinstance(s, sb.RepairStep)]
    # Every mode change precedes setfacl (a chmod rewrites the ACL mask).
    acl = next(
        i for i, s in enumerate(every) if isinstance(s, sb.ShellStep) and s.argv[0] == "setfacl"
    )
    changes = [
        i
        for i, s in enumerate(every)
        if isinstance(s, sb.RepairStep) or (isinstance(s, sb.ShellStep) and s.argv[0] == "chmod")
    ]
    assert max(changes) < acl
    # The shared trees (the folder and the environments, which agents and
    # kernels write under different uids) are walked only when their root is
    # not already group-shared and setgid; the agent's own trees are walked
    # every time. Each walk goes from opened directories, so no name swapped
    # mid-walk leads root out of the tree, and goes to the chat's uid.
    shared = (sb.host_path(FOLDER), sb.host_path(RUNTIME / "envs"))
    owned = (
        sb.host_path(RUNTIME / "agent"),
        sb.host_path(STATE),
        # Where its commands keep their temporary files, beside the environment.
        sb.host_path(RUNTIME / "envs" / COMMAND_TMP_SUBDIR),
    )
    chat = TreeIdentity(UID, UID)
    assert repairs == [
        *(sb.RepairStep(Path(tree), chat, when_wrong=True) for tree in shared),
        *(sb.RepairStep(Path(tree), chat) for tree in owned),
    ]
    # Made before they are owned: a bind's source has to exist for runsc. The
    # shell refuses a link in an owned tree's place rather than making below it.
    made = next(a for a in steps if a[0] == "/bin/sh" and a[3] == "mkdir-owned")
    assert made[4:] == (*shared, *owned) and 'if [ -L "$d" ]' in made[2] and "exit 1" in made[2]
    assert every.index(sb.ShellStep(made)) < every.index(repairs[0])
    # The config trees go to root, writable by root alone and readable by
    # whoever can reach them — gVisor's sentry honours the mode bits it is
    # shown, never a host ACL — while their ancestors stay closed.
    chown_root = next(a for a in steps if a[0] == "chown" and a[2] == "0:0")
    assert sb.host_path(CONFIG_DIR) in chown_root
    assert ("chmod", "-R", "u=rwX,go=rX", sb.host_path(CONFIG_DIR)) in steps
    traverse = next(a for a in steps if a[0] == "setfacl")
    assert traverse[1:3] == ("-m", f"u:{UID}:x")
    assert sb.host_path(CONFIG_DIR.parent) in traverse and sb.host_path(FOLDER.parent) in traverse
    assert "/" not in traverse
    # No read is granted on an ancestor, and nothing is granted on the config tree itself.
    assert all(a[2] != f"u:{UID}:rX" for a in steps if a[0] == "setfacl")


@pytest.mark.parametrize("cgroup", ["systemd", "cgroupfs"])
def test_cgroup_steps_set_the_chats_limits(cgroup: sb.CgroupDriver) -> None:
    steps = sb.cgroup_steps(spec(cgroup=cgroup))
    if cgroup == "systemd":
        argv = next(s.argv for s in steps if isinstance(s, sb.ShellStep))
        assert "MemoryMax=2048M" in argv
        # No swap behind the ceiling: a chat past its limit is stopped, not
        # paged out onto the box's disk.
        assert "MemorySwapMax=0" in argv
        assert "CPUQuota=200%" in argv
        assert f"TasksMax={sb.TASKS_MAX}" in argv
    else:
        written = {s.path.name: s.content for s in steps if isinstance(s, sb.WriteStep)}
        assert written["memory.max"] == str(2048 * 1024 * 1024)
        assert written["memory.swap.max"] == "0"
        assert written["cpu.max"] == "200000 100000"
        assert written["pids.max"] == str(sb.TASKS_MAX)
        swap = next(
            s for s in steps if isinstance(s, sb.WriteStep) and s.path.name == "memory.swap.max"
        )
        assert swap.check is False  # a kernel without swap accounting refuses nothing


@pytest.mark.parametrize(
    ("tier", "vcpu", "memory_mb"),
    [pytest.param(name, *TIER_LIMITS[name], id=name) for name in ("free", "plus", "pro")],
)
def test_the_launch_carries_each_tiers_figures_on_the_cgroup_and_in_the_oci_limits(
    tier: str, vcpu: int, memory_mb: int
) -> None:
    """The figures the chat row carries reach both bounds the launch sets: the
    slice's ``MemoryMax``/``CPUQuota`` and the container's OCI resources."""
    launch = sb.GvisorRuntime().compose_launch(
        spec("gvisor", vcpu=vcpu, memory_mb=memory_mb), AGENT_ARGV
    )
    first = launch.before[0]
    assert isinstance(first, sb.ShellStep)
    assert f"MemoryMax={memory_mb}M" in first.argv and f"CPUQuota={vcpu * 100}%" in first.argv
    linux = oci_of(launch)["linux"]
    assert isinstance(linux, dict)
    assert linux["resources"]["memory"] == {
        "limit": memory_mb * 1024 * 1024,
        "swap": memory_mb * 1024 * 1024,
    }
    assert linux["resources"]["cpu"]["quota"] == vcpu * 100_000
    assert launch.memory_mb == memory_mb


@pytest.mark.parametrize(
    ("mode", "cgroup", "expected"),
    [
        pytest.param(
            "gvisor",
            "systemd",
            Path("/sys/fs/cgroup/alkera.slice/alkera-chat.slice")
            / sb.chat_slice(CHAT)
            / "memory.events",
            id="gvisor-systemd",
        ),
        pytest.param(
            "gvisor",
            "cgroupfs",
            sb.chat_cgroup_path(CHAT) / "memory.events",
            id="gvisor-cgroupfs",
        ),
        pytest.param(
            "none",
            "systemd",
            Path("/sys/fs/cgroup/alkera.slice/alkera-chat.slice")
            / sb.chat_slice(CHAT)
            / "memory.events",
            id="none-systemd",
        ),
        pytest.param("none", "none", None, id="no-cgroup-no-limit"),
    ],
)
def test_the_launch_names_where_a_kill_for_memory_is_read_from(
    mode: sb.SandboxMode, cgroup: sb.CgroupDriver, expected: Path | None
) -> None:
    """After the agent exits, the daemon reads the chat cgroup's
    ``memory.events`` to tell an OOM kill from any other exit — the chat's
    slice under systemd (dash-nested, hierarchical, so the scope runsc opened
    under it counts), its own group under cgroupfs, nothing with no cgroup."""
    runtime: sb.SandboxRuntime = sb.GvisorRuntime() if mode == "gvisor" else sb.NoneRuntime()
    launch = runtime.compose_launch(spec(mode, cgroup), AGENT_ARGV)
    assert launch.memory_events == expected
    assert launch.memory_mb == (2048 if expected is not None else None)


def test_a_command_beside_the_agent_reads_no_memory_events_of_its_own() -> None:
    """The agent server's launch owns the cgroup and the reading of it; a
    shell command exec'd beside it neither bounds nor reports."""
    launch = sb.GvisorRuntime().compose_launch(sb.shell_spec(spec("gvisor")))
    assert launch.memory_events is None and launch.memory_mb is None


@pytest.mark.parametrize(
    ("text", "kills"),
    [
        pytest.param("low 0\nhigh 3\nmax 12\noom 1\noom_kill 1\n", 1, id="one-kill"),
        pytest.param("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n", 0, id="none"),
        pytest.param("oom_kill 4\noom_group_kill 1\n", 4, id="several"),
        pytest.param("", 0, id="empty"),
        pytest.param("oom_kill lots\n", 0, id="malformed"),
        pytest.param("oom 7\n", 0, id="oom-without-a-kill-is-not-a-kill"),
        pytest.param("  oom_kill 2  \n", 2, id="whitespace"),
    ],
)
def test_oom_kills_reads_the_counter_and_nothing_else(text: str, kills: int) -> None:
    assert sb.oom_kills(text) == kills


def test_read_oom_kills_reads_the_file_and_a_missing_file_is_zero(tmp_path: Path) -> None:
    events = tmp_path / "memory.events"
    events.write_text("oom_kill 2\n")
    assert sb.read_oom_kills(events) == 2
    assert sb.read_oom_kills(tmp_path / "gone" / "memory.events") == 0


@pytest.mark.parametrize("memory_mb", [2048, 4096, 8192, 512])
def test_the_memory_limit_detail_round_trips_its_figure(memory_mb: int) -> None:
    detail = f"agent exited unexpectedly (rc=137): {sb.memory_limit_detail(memory_mb)}"
    assert sb.memory_limit_exceeded(detail) == memory_mb


@pytest.mark.parametrize(
    "detail",
    [
        "agent exited unexpectedly (rc=137)",
        "agent process is gone",
        "SSE reconnect attempts exhausted",
        "memory limit",
    ],
)
def test_a_detail_that_names_no_kill_for_memory_reads_as_none(detail: str) -> None:
    assert sb.memory_limit_exceeded(detail) is None


# ---------------------------------------------------------------------------
# Users, slugs, settings
# ---------------------------------------------------------------------------


def test_useradd_argv_pins_the_system_allocator_to_the_reserved_range() -> None:
    argv = sb.useradd_argv(CHAT)
    assert "--system" in argv
    assert f"SYS_UID_MIN={sb.UID_MIN}" in argv
    assert f"SYS_UID_MAX={sb.UID_MAX}" in argv
    assert argv[-1] == sb.chat_user(CHAT)


def test_ensure_chat_uid_runs_useradd_only_for_a_missing_user() -> None:
    calls: list[Sequence[str]] = []

    def run(argv: Sequence[str]) -> int:
        calls.append(argv)
        return 0

    known = {sb.chat_user(CHAT): UID}
    assert sb.ensure_chat_uid(CHAT, run=run, lookup=known.get) == UID
    assert calls == []  # a known user is never re-created
    missing: dict[str, int] = {}

    def lookup(name: str) -> int | None:
        return missing.get(name)

    def run2(argv: Sequence[str]) -> int:
        missing[sb.chat_user(CHAT)] = UID
        return 0

    assert sb.ensure_chat_uid(CHAT, run=run2, lookup=lookup) == UID


@pytest.mark.parametrize(
    ("uid", "ok"),
    [(sb.UID_MIN, True), (sb.UID_MAX, True), (sb.UID_MIN - 1, False), (99, False)],
)
def test_ensure_chat_uid_refuses_a_uid_outside_the_reserved_range(uid: int, ok: bool) -> None:
    known = {sb.chat_user(CHAT): uid}
    if ok:
        assert sb.ensure_chat_uid(CHAT, run=lambda a: 0, lookup=known.get) == uid
    else:
        with pytest.raises(sb.SandboxRefusedError):
            sb.ensure_chat_uid(CHAT, run=lambda a: 0, lookup=known.get)


def test_two_long_ids_never_share_a_slug_by_truncation() -> None:
    a = sb.chat_slug("x" * 40 + "a")
    b = sb.chat_slug("x" * 40 + "b")
    assert a != b and len(a) <= 20 and len(b) <= 20


def test_settings_defaults_and_env_overrides() -> None:
    defaults = sb.SandboxSettings()
    assert defaults.mode == "none"
    assert defaults.rootfs == sb.DEFAULT_ROOTFS and defaults.python_home == sb.DEFAULT_PYTHON_HOME
    assert defaults.chat_net == sb.DEFAULT_CHAT_NET
    got = sb.SandboxSettings.from_env(
        {
            "ALKERA_SANDBOX_MODE": "gvisor",
            "ALKERA_SANDBOX_HOME": "/home/alkera",
            "ALKERA_SANDBOX_DEDICATED": "1",
            "ALKERA_SANDBOX_ROOTFS": "/srv/rootfs/abc",
            "ALKERA_SANDBOX_PYTHON": "/srv/python",
            "ALKERA_SANDBOX_NET": "10.0.0.0/8",
            "SANDBOX_POOL_VCPU": "3",
        }
    )
    assert got.mode == "gvisor"
    assert got.dedicated is True
    assert got.rootfs == "/srv/rootfs/abc" and got.python_home == "/srv/python"
    assert got.chat_net == "10.0.0.0/8"
    assert got.pool_vcpu == 3


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        pytest.param("ALKERA_SANDBOX_ROOTFS", "rootfs", id="relative-rootfs"),
        pytest.param("ALKERA_SANDBOX_PYTHON", "", id="blank-python"),
        pytest.param("ALKERA_SANDBOX_HOME", "home/alkera", id="relative-home"),
    ],
)
def test_a_path_setting_that_names_no_one_place_reads_as_the_default(name: str, raw: str) -> None:
    got = sb.SandboxSettings.from_env({name: raw})
    assert got == sb.SandboxSettings()


@pytest.mark.parametrize(
    "raw",
    ["", "full", "limits", "GVISOR ", "yes", "runsc"],
)
def test_an_unknown_mode_reads_as_the_default_never_a_silent_downgrade(raw: str) -> None:
    # The old level words, blanks and typos all fall back to the default (none
    # here) rather than being read as a different mode.
    got = sb.SandboxSettings.from_env({"ALKERA_SANDBOX_MODE": raw})
    if raw.strip().lower() == "gvisor":
        assert got.mode == "gvisor"
    else:
        assert got.mode == "none"


def test_the_box_defaults_are_the_servers_smallest_tier_and_the_dedicated_guard() -> None:
    """The pool floor a box applies to an unsized row is the free tier from the
    server's own table — the two ends cannot disagree — and a dedicated box
    applies the large leak guard instead."""
    assert (sb.DEFAULT_POOL_VCPU, sb.DEFAULT_POOL_MEMORY_MB) == POOL_FLOOR == TIER_LIMITS["free"]
    assert (sb.DEFAULT_DEDICATED_VCPU, sb.DEFAULT_DEDICATED_MEMORY_MB) == DEDICATED_GUARD
    assert sb.SandboxSettings().default_limits() == POOL_FLOOR
    assert sb.SandboxSettings(dedicated=True).default_limits() == DEDICATED_GUARD
    assert DEDICATED_GUARD > TIER_LIMITS["pro"] > POOL_FLOOR


@pytest.mark.parametrize(
    ("vcpu", "memory", "dedicated", "expected"),
    [
        pytest.param(None, None, False, POOL_FLOOR, id="unsized-on-the-pool-is-the-floor"),
        pytest.param(None, None, True, DEDICATED_GUARD, id="unsized-on-dedicated-is-the-guard"),
        pytest.param(4, 8192, False, (4, 8192), id="the-rows-figures-win"),
        pytest.param(4, 8192, True, (4, 8192), id="the-rows-figures-win-on-dedicated-too"),
        pytest.param(0, -1, False, POOL_FLOOR, id="nonsense-is-unsized"),
        pytest.param(2, None, False, (2, POOL_FLOOR[1]), id="each-field-on-its-own"),
    ],
)
def test_limits_for_a_chat_row(
    vcpu: int | None, memory: int | None, dedicated: bool, expected: tuple[int, int]
) -> None:
    assert sb.SandboxSettings(dedicated=dedicated).limits_for(vcpu, memory) == expected


def test_metadata_block_rules_match_the_chat_uid_range_for_both_families() -> None:
    v4, v6 = sb.metadata_block_rules()
    assert f"{sb.UID_MIN}-{sb.UID_MAX}" in v4
    assert sb.METADATA_IPV4 in v4
    assert sb.METADATA_IPV6 in v6


def test_shell_spec_keeps_the_agents_identity_and_view_and_drops_its_trees() -> None:
    shell = sb.shell_spec(spec("gvisor", mount_alias=True))
    assert shell.chat_id == CHAT and shell.uid == UID and shell.mode == "gvisor"
    assert shell.owns_cgroup is False
    assert shell.binds == () and shell.config_trees == ()
    assert shell.bundle is None and shell.rootfs is None
    # What decides the command's view rides along: home, the default
    # environment, the tool directories, the alias.
    assert shell.home == "/home/alkera"
    assert shell.default_env == ENV and shell.path_dirs == (sb.RIPGREP_MOUNT,)
    assert shell.mount_alias is True


def test_the_wrapper_is_the_command_up_to_the_agent_binary_never_its_environment() -> None:
    wrapping = sb.NoneRuntime().compose_launch(spec("none"), AGENT_ARGV)
    command = ["/usr/bin/setpriv", "--pdeathsig", "KILL", "--", *wrapping.wrap(AGENT_ARGV)]
    wrapper = sb.wrapper_argv(command, AGENT_ARGV)
    assert wrapper[:4] == ("/usr/bin/setpriv", "--pdeathsig", "KILL", "--")
    # The uid drop, then the shared umask the agent server starts under.
    assert wrapper[-5:] == ("--", *sb.umask_prefix()) and f"--reuid={UID}" in wrapper
    assert not set(AGENT_ARGV) & set(wrapper)
    # A runsc exec carries the command's environment through env -i: never repeated.
    exec_launch = sb.GvisorRuntime().compose_launch(sb.shell_spec(spec("gvisor")))
    argv = ("/bin/sh", "-c", "echo hi")
    carried = exec_launch.wrap(argv, env={"TOKEN": "s3cret", "HOME": "/home/alkera"})
    assert "TOKEN=s3cret" in carried  # the command itself must carry it
    wrapper = sb.wrapper_argv(carried, argv)
    assert sb.CONTAINER_ENV not in wrapper and not any("s3cret" in w for w in wrapper)
    assert wrapper[-1] == sb.chat_container(CHAT)
    # A baked command (the gVisor agent server) is the wrapper whole.
    baked = sb.GvisorRuntime().compose_launch(spec("gvisor"), AGENT_ARGV)
    assert sb.wrapper_argv(baked.wrap(AGENT_ARGV), AGENT_ARGV) == baked.command
    assert sb.wrapper_argv(["/opt/agent", "serve"], ["/opt/agent", "serve"]) == ()


def test_the_boxs_account_of_a_launch_names_its_shape_and_never_a_secret() -> None:
    launch = sb.NoneRuntime().compose_launch(spec("none", mount_alias=True), AGENT_ARGV)
    line = sb.explain_sandbox(
        mode="none",
        host="no runsc: uid and systemd cgroup only, mode none",
        spec=spec("none", mount_alias=True),
        launch=launch,
        wrapper=("setpriv", "--pdeathsig", "KILL", "--"),
    )
    assert line == (
        f"sandbox mode=none, cgroup=systemd, uid={UID}, alias=/home/alkera; "
        "host: no runsc: uid and systemd cgroup only, mode none; "
        "wrapper: setpriv --pdeathsig KILL --"
    )
    assert "gw.example" not in line and "ALKERA_SHELL_ENV_RESTORE" not in line
    no_alias = sb.NoneRuntime().compose_launch(spec("none", mount_alias=False), AGENT_ARGV)
    assert "alias=none" in sb.explain_sandbox(
        mode="none", host="h", spec=spec("none", mount_alias=False), launch=no_alias
    )
    # Before a spec exists (a refusal at planning), the mode and the host alone.
    assert (
        sb.explain_sandbox(mode="gvisor", host="no runsc") == "sandbox mode=gvisor; host: no runsc"
    )
    # An unsandboxed session that was spawned: no spec, and its wrapper if any.
    assert sb.explain_sandbox(mode="none", host="h", launch=sb.NO_SANDBOX) == (
        "sandbox mode=none; host: h; wrapper: none"
    )


def test_relative_to_container_spells_a_folder_path_under_the_home() -> None:
    assert sb.relative_to_container(FOLDER / "a" / "b.txt", folder=FOLDER, home="/home/alkera") == (
        "/home/alkera/a/b.txt"
    )
    assert sb.relative_to_container(FOLDER, folder=FOLDER, home="/home/alkera") == "/home/alkera"
    assert sb.relative_to_container(CHAT_DIR / "manifest.json", folder=FOLDER, home="/x") is None


# ---------------------------------------------------------------------------
# run_steps
# ---------------------------------------------------------------------------


def test_run_steps_stops_at_a_failed_checked_step_and_carries_past_an_unchecked_one() -> None:
    ran: list[str] = []

    def run(argv: Sequence[str]) -> int:
        ran.append(argv[0])
        return 1 if argv[0] == "boom" else 0

    with pytest.raises(sb.SandboxRefusedError):
        sb.run_steps(
            [sb.ShellStep(("ok",)), sb.ShellStep(("boom",)), sb.ShellStep(("after",))], run=run
        )
    assert ran == ["ok", "boom"]  # a checked failure stops the launch
    ran.clear()
    sb.run_steps([sb.ShellStep(("boom",), check=False), sb.ShellStep(("after",))], run=run)
    assert ran == ["boom", "after"]  # an unchecked failure is logged and passed


def test_run_steps_writes_files_and_a_failed_checked_write_refuses(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    sb.run_steps([sb.WriteStep(target, '{"ok": true}')])
    assert target.read_text() == '{"ok": true}'
    with pytest.raises(sb.SandboxRefusedError):
        sb.run_steps([sb.WriteStep(tmp_path / "missing" / "x", "y")])


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits are a POSIX concept")
def test_a_write_step_s_mode_wins_over_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o077)
    try:
        sb.run_steps([sb.WriteStep(tmp_path / "passwd", "x\n", mode=0o644)])
        sb.run_steps([sb.WriteStep(tmp_path / "config.json", "{}")])
    finally:
        os.umask(previous)
    assert (tmp_path / "passwd").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_the_bootstrap_gate_and_the_daemon_agree_on_where_the_rootfs_is() -> None:
    """The boot's gate probes the rootfs at the path the daemon later roots
    containers at, and reads the stamp the daemon's probe reads; spelled in two
    packages, pinned here."""
    from alkera_core.compute import bootstrap

    assert bootstrap.SANDBOX_ROOTFS == sb.DEFAULT_ROOTFS
    assert bootstrap.SANDBOX_ROOTFS_STAMP == sb.ROOTFS_STAMP


def test_a_step_whose_executable_is_missing_fails_like_any_other_step(tmp_path: Path) -> None:
    """An unchecked step may name a binary the box lacks — the default env's
    python when ``uv venv`` could not make it — and the launch goes on without
    it, as the docstring of ``env_steps`` promises. A checked step refuses the
    chat with the argv in the reason, never a bare ``OSError`` that reads as a
    harness that failed to start."""
    missing = str(tmp_path / "no-such-binary")
    sb.run_steps((sb.ShellStep((missing, "-m", "venv"), check=False),))
    with pytest.raises(sb.SandboxRefusedError, match="no-such-binary"):
        sb.run_steps((sb.ShellStep((missing,), check=True),))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_ownership_mode_keeps_what_the_chat_tree_lands_and_opens_the_rest(
    tmp_path: Path,
) -> None:
    """The chat tree lands files 0660 and directories 02770 (setgid, so a
    second uid of the group keeps making the group's files); the spawn's repair
    of the owned trees must leave those exactly as they are, and bring a
    file the daemon made as itself with a closed mode to the same openness,
    keeping an executable's bit for user and group."""
    import stat

    from alkera_cli.files.chat_fs import DIR_MODE, FILE_MODE

    tree = tmp_path / "tree"
    tree.mkdir()
    os.chmod(tree, DIR_MODE)
    edited = tree / "edited.txt"
    edited.write_text("x")
    os.chmod(edited, FILE_MODE)
    closed = tree / "spill.txt"
    closed.write_text("x")
    os.chmod(closed, 0o600)
    script = tree / "run.sh"
    script.write_text("x")
    os.chmod(script, 0o700)
    sb.run_steps((sb.RepairStep(tree, None),))
    modes = {p.name: stat.S_IMODE(p.lstat().st_mode) for p in (tree, edited, closed, script)}
    assert modes == {
        "tree": DIR_MODE,
        "edited.txt": FILE_MODE,
        "spill.txt": FILE_MODE,
        "run.sh": 0o770,
    }
