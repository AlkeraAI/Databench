"""A real ``runsc run`` of a chat's sandbox on a Linux host with gVisor.

The pure tests pin the composition as data; this proves it on a real kernel:
the launch's own steps make the chat's uid, its network namespace and its
bundle, ``runsc`` runs the exact command the daemon would run, and inside the
container the process is the chat's uid in the staged rootfs at
``/home/alkera`` with the default Python, reaching the daemon's port on the
host end of its veth pair and nothing else on the host, never the metadata
service, and out through NAT; a command ``exec``ed into the live container
carries its environment, reports the command's own exit code (also from the
ignored-``SIGCHLD`` state the compiled daemon starts in), runs the chat's
Python environment, and writes into the host's copy of the folder. It needs
root, ``runsc``, the staged rootfs and the box ruleset (``sandbox-prereqs.sh``
applied), so it runs in the ``live`` tier on the throwaway EC2 host and skips
gracefully everywhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.sandbox_probe import host_resolvers, probe

pytestmark = [pytest.mark.live]

SETTINGS = sb.SandboxSettings.from_env()
CAP = probe(settings=SETTINGS) if sys.platform == "linux" else None


def _skip_unless_live_host() -> None:
    if sys.platform != "linux":
        pytest.skip("a real runsc run needs a Linux host")
    assert CAP is not None
    if not CAP.root:
        pytest.skip("a real runsc run needs root (the daemon runs as root on a box)")
    if not CAP.gvisor:
        pytest.skip(f"the host cannot run gVisor: {CAP.reason}")
    ruleset = subprocess.run(
        [CAP.nft or "nft", "list", "set", *sb.NFT_TABLE.split(), sb.NFT_CHAT_PORTS_SET],
        capture_output=True,
        check=False,
    )
    if ruleset.returncode != 0:
        pytest.skip("the box ruleset is not loaded; install the box's sandbox prerequisites first")
    if not host_resolvers():
        pytest.skip("the host names no resolver a container can reach")


class _Daemon(http.server.BaseHTTPRequestHandler):
    """A stand-in for the daemon's tool server, bound on every address the way
    it is when a gVisor chat must reach it over the veth pair."""

    def do_GET(self) -> None:
        body = b"daemon-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


@pytest.fixture
def daemon_port() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _Daemon)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()


def _lay_out_chat(tmp_path: Path, tag: str, *, marker: str) -> dict[str, object]:
    """A chat laid out like a box lays one out — its records and folder under
    the work root, its config and state under the daemon's home, both parents
    shared with every other chat on the box — with a real uid."""
    chat_id = f"{tag}{uuid.uuid4().hex[:12]}"
    chat_dir = tmp_path / ".alkera" / "chats" / chat_id
    folder = chat_dir / "sandbox"
    runtime = chat_dir / sb.RUNTIME_STATE_SUBDIR
    config_root = tmp_path / "home" / "harness" / chat_id
    for path in (folder, runtime, config_root / "config", config_root / "state"):
        path.mkdir(parents=True)
    (folder / "marker.txt").write_text(f"{marker}\n")
    (config_root / "config" / "note.md").write_text("read-only config\n")
    return {
        "id": chat_id,
        "uid": sb.ensure_chat_uid(chat_id),
        "folder": folder,
        "runtime": runtime,
        "chat_dir": chat_dir,
        "config_root": config_root,
        "marker": marker,
    }


def _release_chat(chat: dict[str, object]) -> None:
    subprocess.run(["userdel", sb.chat_user(str(chat["id"]))], capture_output=True, check=False)


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[dict[str, object]]:
    """A chat laid out like a box lays one out, with a real uid, torn down after."""
    _skip_unless_live_host()
    laid_out = _lay_out_chat(tmp_path, "live", marker="hello-from-host")
    try:
        yield laid_out
    finally:
        _release_chat(laid_out)


@pytest.fixture
def other_chat(tmp_path: Path) -> Iterator[dict[str, object]]:
    """A second chat beside the first, under the same work root and daemon
    home — another org's chat on the same pool node."""
    _skip_unless_live_host()
    laid_out = _lay_out_chat(tmp_path, "other", marker="hello-from-the-other-chat")
    try:
        yield laid_out
    finally:
        _release_chat(laid_out)


def _spec(chat: dict[str, object], daemon_port: int, **overrides: object) -> sb.SandboxSpec:
    assert CAP is not None
    folder = chat["folder"]
    assert isinstance(folder, Path)
    runtime = chat["runtime"]
    assert isinstance(runtime, Path)
    config_root = chat["config_root"]
    assert isinstance(config_root, Path)
    chat_dir = chat["chat_dir"]
    assert isinstance(chat_dir, Path)
    base: dict[str, object] = {
        "chat_id": chat["id"],
        "folder": folder,
        "uid": chat["uid"],
        "vcpu": 1,
        "memory_mb": 1024,
        "home": SETTINGS.home,
        "mode": "gvisor",
        "cgroup": CAP.cgroup,
        "binds": sb.chat_binds(
            runtime_dir=runtime, agent_config_root=config_root, default_env=CAP.default_env
        ),
        "private_dirs": (chat_dir,),
        "bundle": config_root / "runsc",
        "rootfs": Path(CAP.rootfs or SETTINGS.rootfs),
        "overlay_dir": chat_dir / ".overlay",
        "default_env": sb.default_env_path(runtime) if CAP.default_env else None,
        "python_home": CAP.python_home or SETTINGS.python_home,
        "daemon_ports": (daemon_port,),
        "resolvers": CAP.resolvers,
        "chat_net": SETTINGS.chat_net,
        "runsc": CAP.runsc or "runsc",
        "setpriv": CAP.setpriv or "setpriv",
        "uv": CAP.uv or "uv",
        "ip": CAP.ip or "ip",
        "nft": CAP.nft or "nft",
        "agent_env": {"ALKERA_LIVE_PROBE": "baked"},
    }
    base.update(overrides)
    return sb.SandboxSpec(**base)  # type: ignore[arg-type]


def _host_ip() -> str:
    """The box's own primary address, which a chat must NOT be able to reach on
    a port it was not granted."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
        probe_socket.connect(("10.255.255.255", 1))
        return str(probe_socket.getsockname()[0])


#: What the container runs and writes into its own folder, one fact per line.
_PROBE_SCRIPT = """
exec >/home/alkera/results.txt 2>&1
echo uid=$(id -u)
echo user=$(whoami)
echo home=$HOME
echo pwd=$(pwd)
echo host=$(hostname)
echo baked=$ALKERA_LIVE_PROBE
echo rootfs=$([ -f /.alkera-rootfs ] && echo staged || echo host)
echo config=$(cat {config}/note.md)
echo config-write=$(touch {config}/x 2>/dev/null && echo allowed || echo refused)
echo python=$(python3 -c 'import sys; print(sys.version_info[:2])' 2>&1)
echo venv=$VIRTUAL_ENV
echo which-python=$(command -v python)
echo pip=$(python -m pip --version 2>&1 | cut -c1-3)
echo uv=$(command -v uv)
echo micromamba=$(command -v micromamba)
echo marker=$(cat /home/alkera/marker.txt)
echo written > /home/alkera/from-container.txt && echo write=ok
echo daemon=$(curl -s -m 5 http://{host_ip}:{port}/ || echo unreachable)
echo metadata=$(curl -s -m 3 http://169.254.169.254/ -o /dev/null && echo REACHED || echo blocked)
echo host-ssh=$(curl -s -m 3 http://{box_ip}:22/ -o /dev/null && echo REACHED || echo blocked)
echo dns=$(getent hosts pypi.org >/dev/null 2>&1 && echo ok || echo failed)
echo egress=$(curl -sI -m 15 https://pypi.org/simple/ >/dev/null 2>&1 && echo ok || echo failed)
"""


def _results(folder: Path) -> dict[str, str]:
    text = (folder / "results.txt").read_text()
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def test_a_real_runsc_run_of_the_composed_launch(
    chat: dict[str, object], daemon_port: int, tmp_path: Path
) -> None:
    spec = _spec(chat, daemon_port)
    net = spec.network
    folder = chat["folder"]
    assert isinstance(folder, Path)
    config_root = chat["config_root"]
    assert isinstance(config_root, Path)
    script = _PROBE_SCRIPT.format(
        config=(config_root / "config").as_posix(),
        host_ip=net.host_ip,
        port=daemon_port,
        box_ip=_host_ip(),
    )
    argv = ["/bin/sh", "-c", script]
    launch = sb.GvisorRuntime().compose_launch(spec, argv)
    try:
        sb.run_steps(launch.before)
        done = subprocess.run(
            launch.wrap(argv),
            cwd=launch.cwd,
            env={**os.environ, **launch.env},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 0, f"runsc run failed:\n{done.stdout}\n{done.stderr}"
        results = _results(folder)
    finally:
        sb.run_steps(launch.after_exit)

    uid = chat["uid"]
    # Who and where: the chat's uid, named alkera, in the staged rootfs at its home.
    assert results["uid"] == str(uid) and results["user"] == "alkera"
    assert results["home"] == results["pwd"] == "/home/alkera"
    assert results["host"] == sb.chat_container(str(chat["id"]))
    assert results["rootfs"] == "staged"
    assert results["baked"] == "baked"  # the agent's environment reached the container
    # The config tree is readable and not writable; the folder is both.
    assert results["config"] == "read-only config" and results["config-write"] == "refused"
    assert results["marker"] == "hello-from-host" and results["write"] == "ok"
    written = folder / "from-container.txt"
    assert written.read_text() == "written\n" and written.stat().st_uid == uid
    # The default environment: the rootfs's own Python, active.
    assert results["python"].startswith("(3, 1")
    assert results["uv"] and results["micromamba"]
    if spec.default_env is not None:
        # The environment at the path the container sees it, never the host's.
        assert results["venv"] == f"{sb.ENVS_MOUNT}/alkera" == spec.agent_path(spec.default_env)
        assert results["which-python"] == f"{sb.ENVS_MOUNT}/alkera/bin/python"
        assert results["pip"] == "pip"
    # The network: the daemon on the host end of the pair, nothing else on the
    # host, never the metadata service, and out through NAT with DNS.
    assert results["daemon"] == "daemon-ok"
    assert results["metadata"] == "blocked"
    assert results["host-ssh"] == "blocked"
    assert results["dns"] == "ok" and results["egress"] == "ok"
    # And after the exit, nothing of the chat's network is left on the host.
    assert not Path(net.namespace_path).exists()
    listing = subprocess.run(
        [spec.nft, "list", "set", *sb.NFT_TABLE.split(), sb.NFT_CHAT_PORTS_SET],
        capture_output=True,
        text=True,
        check=False,
    )
    assert net.host_if not in listing.stdout
    assert not (spec.overlay_dir or tmp_path / "never").exists()


@contextlib.contextmanager
def _live_container(
    spec: sb.SandboxSpec, argv: Sequence[str] = ("/bin/sh", "-c", "sleep 300")
) -> Iterator[sb.SandboxLaunch]:
    """The agent server's container held open — the way the daemon holds it
    while a chat is awake — for commands to ``exec`` into. Yields the shell
    launch a command on the chat's behalf is composed with. ``argv`` is what
    the container runs meanwhile; the default just waits."""
    argv = list(argv)
    launch = sb.GvisorRuntime().compose_launch(spec, argv)
    sb.run_steps(launch.before)
    server = subprocess.Popen(
        launch.wrap(argv),
        cwd=launch.cwd,
        env={**os.environ, **launch.env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            state = subprocess.run(
                [
                    spec.runsc,
                    f"--root={sb.host_path(spec.runsc_root)}",
                    "state",
                    sb.chat_container(spec.chat_id),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if state.returncode == 0 and json.loads(state.stdout).get("status") == "running":
                break
            if server.poll() is not None:
                detail = server.stderr.read().decode() if server.stderr else ""
                pytest.fail(f"the container exited early: {detail}")
            time.sleep(0.5)
        else:
            pytest.fail("the container never reached running")
        yield sb.GvisorRuntime().compose_launch(sb.shell_spec(spec))
    finally:
        sb.run_steps(launch.after_exit)
        if server.poll() is None:
            server.kill()
        server.wait(timeout=30)
        shutil.rmtree(spec.overlay_dir, ignore_errors=True) if spec.overlay_dir else None


def _exec(shell: sb.SandboxLaunch, spec: sb.SandboxSpec, command: str) -> tuple[int, str]:
    """A command through the exec launch, spawned the way the bash tool spawns
    it: the environment on the argv, its own session, stdin closed, output
    merged, the exit status read by asyncio."""
    env = shell.apply_env({"PATH": "/host/bin", "HOME": "/opt/host-home", "PROBE": "carried"})
    argv = shell.wrap(["/bin/sh", "-c", command], env=env)

    async def run() -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(spec.folder),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        assert proc.returncode is not None
        return proc.returncode, out.decode("utf-8", "replace")

    return asyncio.run(run())


def test_a_command_execed_into_the_live_container_carries_its_environment(
    chat: dict[str, object], daemon_port: int
) -> None:
    """The bash tool's path: the agent server holds the container open and each
    command ``runsc exec``s into it, its environment riding on the argv."""
    spec = _spec(chat, daemon_port)
    with _live_container(spec) as shell:
        code, out = _exec(
            shell, spec, 'echo "home=$HOME probe=$PROBE pwd=$(pwd) uid=$(id -u)"; echo $PATH'
        )
    assert code == 0, out
    first, path = out.strip().splitlines()[:2]
    assert first == f"home=/home/alkera probe=carried pwd=/home/alkera uid={chat['uid']}"
    assert "/host/bin" not in path and path.endswith(sb._CONTAINER_PATH)


EXITS = [
    pytest.param("true", 0, id="exit-0"),
    pytest.param("false", 1, id="exit-1"),
    pytest.param("echo wrote 3 bytes; exit 3", 3, id="exit-3-with-output"),
    pytest.param("no-such-command-here", 127, id="exit-127"),
]


@pytest.mark.parametrize(("command", "expected"), EXITS)
def test_a_command_execed_into_the_live_container_reports_its_own_exit_code(
    chat: dict[str, object], daemon_port: int, command: str, expected: int
) -> None:
    """What the reader is shown as "failed" is what the command said, never a
    sentinel of the box's own: the code crosses ``runsc exec`` and the spawn
    unchanged."""
    spec = _spec(chat, daemon_port)
    with _live_container(spec) as shell:
        code, out = _exec(shell, spec, command)
    assert code == expected, out
    if expected == 3:
        assert out == "wrote 3 bytes\n"


@pytest.mark.usefixtures("sigchld_restored")
@pytest.mark.parametrize(("command", "expected"), EXITS)
def test_the_daemons_own_start_state_is_undone_before_a_command_is_execed(
    chat: dict[str, object], daemon_port: int, command: str, expected: int
) -> None:
    """The compiled daemon starts with ``SIGCHLD`` ignored — set in C by the
    anti-debugger it is built with, after the interpreter has read every
    disposition into Python's own table — and in that state every command
    through this very ``exec`` came back 255. Reproduced here on the box the
    way the binary has it: the table says default, the kernel discards every
    child. Nothing undoes it by hand; the launch's own steps and the spawn
    reclaim it on the way, and the command's code arrives intact."""
    from alkera_core.process import children_reapable

    libc = ctypes.CDLL(None, use_errno=True)
    libc.signal.restype = ctypes.c_void_p
    libc.signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
    libc.signal(int(signal.SIGCHLD), ctypes.c_void_p(1))  # SIG_IGN, past Python's table
    assert signal.getsignal(signal.SIGCHLD) is signal.SIG_DFL, "the table does not see it"
    assert children_reapable() is False, "the kernel's disposition is what is read"
    assert subprocess.run(["/bin/false"], capture_output=True, check=False).returncode == 0
    spec = _spec(chat, daemon_port)
    with _live_container(spec) as shell:
        assert children_reapable() is True, "the launch's steps reclaimed it before running"
        code, out = _exec(shell, spec, command)
    assert code == expected, out


def test_the_execed_shell_runs_the_chats_python_environment(
    chat: dict[str, object], daemon_port: int
) -> None:
    """``python3``, ``pip``, ``uv`` and ``micromamba`` resolve in a shell tool,
    with the chat's own ``alkera`` environment active — the interpreter the
    prompt's PYTHON block promises, at the path it names."""
    spec = _spec(chat, daemon_port)
    if spec.default_env is None:
        pytest.skip("this host cannot make the default environment (no uv or python home)")
    env_path = spec.agent_path(spec.default_env)
    assert env_path == f"{sb.ENVS_MOUNT}/alkera"
    with _live_container(spec) as shell:
        code, out = _exec(
            shell,
            spec,
            "echo venv=$VIRTUAL_ENV; "
            "echo prefix=$(python3 -c 'import sys; print(sys.prefix)'); "
            "echo python3=$(command -v python3); "
            "echo pip=$(python -m pip --version 2>&1 | cut -c1-3); "
            "echo uv=$(command -v uv); "
            "echo micromamba=$(command -v micromamba)",
        )
    assert code == 0, out
    facts = dict(line.partition("=")[::2] for line in out.strip().splitlines())
    assert facts["venv"] == env_path
    assert facts["prefix"] == env_path, "the interpreter python3 resolves to is the venv's"
    assert facts["python3"] == f"{env_path}/bin/python3"
    assert facts["pip"] == "pip"
    assert facts["uv"] and facts["micromamba"]


def test_what_a_command_writes_in_the_container_is_the_hosts_file_and_back(
    chat: dict[str, object], daemon_port: int
) -> None:
    """The second hop of the chat folder: the container's ``/home/alkera`` IS
    the host folder the drive syncs — a write lands there, owned by the chat's
    uid, never in the root overlay's upper directory — and a file the host
    drops in (a pull from the drive) is there for the next command."""
    spec = _spec(chat, daemon_port)
    folder = chat["folder"]
    assert isinstance(folder, Path)
    with _live_container(spec) as shell:
        code, out = _exec(shell, spec, "mkdir -p scratch && echo from-exec > scratch/red.txt")
        assert code == 0, out
        written = folder / "scratch" / "red.txt"
        assert written.read_text() == "from-exec\n"
        assert written.stat().st_uid == chat["uid"]
        if spec.overlay_dir is not None:
            assert not list(spec.overlay_dir.rglob("red.txt")), "not in the overlay's upper dir"
        (folder / "scratch" / "uploads").mkdir()
        (folder / "scratch" / "uploads" / "paste-1-ab12.txt").write_text("from-host\n")
        code, out = _exec(shell, spec, "cat scratch/uploads/paste-1-ab12.txt")
    assert (code, out) == (0, "from-host\n")


#: A port each chat's container serves its own folder on, for the other chat
#: to be refused at. Any port: the chat net's forward rule drops by address.
_NEIGHBOUR_PORT = 8765


def test_two_chats_on_one_node_reach_nothing_of_each_other(
    chat: dict[str, object], other_chat: dict[str, object], daemon_port: int
) -> None:
    """Two chats of two orgs on one gVisor node, both live at once, each
    serving its own folder on a port inside its container. From inside either
    chat: the other chat's folder and records are not there at all (no mount
    of them exists, so their host paths name nothing), and the other chat's
    address does not answer (the box ruleset drops chat-to-chat traffic) —
    while the same listener answers its own chat on loopback, which is what
    makes the refusal the firewall's and not a listener that never came up."""
    spec_a, spec_b = _spec(chat, daemon_port), _spec(other_chat, daemon_port)
    serve = ("/bin/sh", "-c", f"cd /home/alkera && exec python3 -m http.server {_NEIGHBOUR_PORT}")
    with _live_container(spec_a, serve) as shell_a, _live_container(spec_b, serve) as shell_b:
        sides = (
            (shell_a, spec_a, chat, spec_b, other_chat),
            (shell_b, spec_b, other_chat, spec_a, chat),
        )
        for shell, spec, mine, their_spec, theirs in sides:
            # Control: my own listener answers me, with my own marker.
            code, out = _exec(
                shell, spec, f"curl -s -m 10 http://127.0.0.1:{_NEIGHBOUR_PORT}/marker.txt"
            )
            assert (code, out.strip()) == (0, str(mine["marker"])), out
            # Their folder and their records: no such path from in here. Nor
            # my own by its host path: the root is at /home/alkera and nowhere
            # else, and the host's layout is not in the container at all.
            their_folder = sb.host_path(Path(str(theirs["folder"])))
            their_chat_dir = sb.host_path(Path(str(theirs["chat_dir"])))
            code, out = _exec(shell, spec, f"cat {their_folder}/marker.txt")
            assert code != 0 and str(theirs["marker"]) not in out, out
            code, out = _exec(shell, spec, f"ls {their_chat_dir}")
            assert code != 0, out
            my_folder = sb.host_path(Path(str(mine["folder"])))
            code, out = _exec(
                shell, spec, f"cat {my_folder}/marker.txt; ls {sb.host_path(spec.folder.parent)}"
            )
            assert code != 0 and str(mine["marker"]) not in out, out
            # Their address: dropped on the way, whatever they serve.
            their_ip = their_spec.network.container_ip
            code, out = _exec(
                shell,
                spec,
                f"curl -s -m 10 http://{their_ip}:{_NEIGHBOUR_PORT}/marker.txt || echo refused",
            )
            assert str(theirs["marker"]) not in out and "refused" in out, out
