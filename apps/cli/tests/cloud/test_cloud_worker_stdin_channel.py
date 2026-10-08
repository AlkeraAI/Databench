"""An org worker's standard input is its supervisor's socketpair, and
``runsc exec`` shuts down a socket it inherits as stdin, which cut the worker
off right after its first kernel sandbox came up. The worker moves the
channel off stdin before anything runs, and every step it runs is handed
``/dev/null`` as stdin besides.

Each case runs in a child process whose stdin is one end of a socketpair (as
the worker's is): the program it runs stands in for ``runsc``, shutting its
stdin down when that is a socket. The test then checks the channel still
carries a message both ways.
"""

from __future__ import annotations

import select
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

#: Stands in for ``runsc exec``: shuts down the socket it got as stdin.
_SHUTS_DOWN_STDIN = (
    "import socket\n"
    "try:\n"
    "    socket.socket(fileno=0).shutdown(socket.SHUT_RDWR)\n"
    "except OSError:\n"
    "    pass\n"
)


def _child(body: str) -> tuple[subprocess.CompletedProcess[str], socket.socket, socket.socket]:
    """Run ``body`` in a python child whose stdin is ``theirs``."""
    mine, theirs = socket.socketpair()
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        stdin=theirs.fileno(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return done, mine, theirs


def _channel_alive(mine: socket.socket, theirs: socket.socket) -> bool:
    try:
        mine.sendall(b"ping\n")
    except OSError:
        return False
    ready, _, _ = select.select([theirs], [], [], 2)
    return bool(ready) and theirs.recv(16) == b"ping\n"


_STEP = (_SHUTS_DOWN_STDIN,)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            "from alkera_cli.harness.sandbox_steps import run_argv; run_argv(ARGV)",
            id="sandbox-steps",
        ),
        pytest.param(
            "from alkera_cli.notebooks.launch_container import _run; _run(ARGV)",
            id="kernel-launch",
        ),
        pytest.param(
            "from alkera_cli.notebooks.system_install import _run; _run(ARGV)",
            id="system-install",
        ),
        pytest.param(
            "from alkera_cli.cloud.org_namespace import _run; _run(ARGV)",
            id="org-namespace",
        ),
        pytest.param(
            "from alkera_core.host_isolation import run_status; run_status(ARGV)",
            id="sandbox-probe",
        ),
        pytest.param(
            "from alkera_cli.harness.sandbox_processes import run_probe; run_probe(ARGV)",
            id="sandbox-processes",
        ),
    ],
)
def test_a_step_never_hands_its_stdin_to_the_program_it_runs(call: str) -> None:
    body = f"import sys\nARGV = [sys.executable, '-c', {_SHUTS_DOWN_STDIN!r}]\n{call}\n"
    done, mine, theirs = _child(body)
    try:
        assert done.returncode == 0, done.stderr
        assert _channel_alive(mine, theirs)
    finally:
        mine.close()
        theirs.close()


def test_the_worker_moves_its_channel_off_stdin_before_anything_runs() -> None:
    """Even a program started with no stdin of its own (an adapter, a
    library) cannot reach the channel: stdin is ``/dev/null`` by then, and
    the channel's new fd is not inherited."""
    body = f"""
        import os, socket, subprocess, sys
        from alkera_cli.org_worker_channel import take_control_channel
        fd = take_control_channel(0)
        assert fd > 2 and not os.get_inheritable(fd)
        assert os.path.samestat(os.fstat(0), os.stat(os.devnull))
        subprocess.run([sys.executable, "-c", {_SHUTS_DOWN_STDIN!r}], check=True)
        channel = socket.socket(fileno=fd)
        channel.sendall(b"still here\\n")
        print(channel.recv(16).decode().strip())
    """
    mine, theirs = socket.socketpair()
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(body)],
            stdin=theirs.fileno(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        theirs.close()
        ready, _, _ = select.select([mine], [], [], 30)
        assert ready and mine.recv(32) == b"still here\n"
        mine.sendall(b"pong\n")
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
        assert out.strip() == "pong"
    finally:
        mine.close()


def test_an_idle_sleep_probe_leaves_the_worker_its_supervisor() -> None:
    """The idle-sleep path asks the container for its process table through
    ``runsc exec`` (:class:`RunscLister`, with its production runner). Driven
    with a stand-in runsc that shuts down a socket it gets as stdin, from a
    process whose stdin is still the supervisor's socketpair (nothing moved it
    first): the probe hands runsc ``/dev/null``, so the channel survives the
    probe and still carries a message both ways."""
    body = f"""
        import sys
        from pathlib import Path
        from alkera_cli.harness.sandbox_processes import RunscLister
        runsc = Path(sys.argv[1])
        runsc.write_text("#!" + sys.executable + "\\n" + {_SHUTS_DOWN_STDIN!r})
        runsc.chmod(0o755)
        lister = RunscLister(runsc=str(runsc), root="/run/runsc", container="c1", uid=20001)
        lister.list_processes()
    """
    mine, theirs = socket.socketpair()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(body), str(Path(tmp) / "runsc")],
                stdin=theirs.fileno(),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        assert done.returncode == 0, done.stderr
        assert _channel_alive(mine, theirs)
    finally:
        mine.close()
        theirs.close()


def test_the_entry_moves_a_worker_s_channel_before_the_command_tree_loads() -> None:
    """The process entry moves the channel off stdin for ``cloud-mirror
    worker`` before it imports anything of the CLI, and tells the command the
    fd it moved to: what the CLI does on launch (its update check) can never
    hand the channel to a program it runs."""
    body = """
        import os, sys
        from alkera_cli import entry
        argv = ["alkera", "cloud-mirror", "worker", "--org-root", "/x", "--control-fd", "0"]
        entry._guard_the_worker_channel(argv)
        moved = int(argv[-1])
        assert moved > 2 and not os.get_inheritable(moved), argv
        assert os.path.samestat(os.fstat(0), os.stat(os.devnull))
        assert sorted(m for m in sys.modules if m.startswith("alkera_cli.")) == [
            "alkera_cli.entry",
            "alkera_cli.org_worker_channel",
            # Only the package marker, for its COMMAND constant: the
            # supervisor package imports nothing on its own.
            "alkera_cli.supervisor",
        ], sorted(m for m in sys.modules if m.startswith("alkera_cli."))
        other = ["alkera", "chat", "--control-fd", "0"]
        entry._guard_the_worker_channel(other)
        assert other[-1] == "0"
    """
    done, mine, theirs = _child(body)
    try:
        assert done.returncode == 0, done.stderr
    finally:
        mine.close()
        theirs.close()
