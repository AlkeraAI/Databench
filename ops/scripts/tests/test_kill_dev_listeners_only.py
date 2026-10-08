"""``make kill-dev`` kills the process LISTENING on a worktree port, nothing else.

A local developer box reaches the Mac's API through OrbStack, so on the Mac the
connections to the API port belong to OrbStack's own process. Matching every
process with a socket on the port (``lsof -ti tcp:PORT``) killed OrbStack and
took Docker down for every worktree. Here a real listener and a real client
connected to it share a port; only the listener may die.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

needs_lsof = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("lsof") is None, reason="needs lsof and a POSIX shell"
)

_LISTENER = """
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 0)); s.listen(8)
print(s.getsockname()[1], flush=True)
conns = []
while True:
    c, _ = s.accept(); conns.append(c)
"""

_CLIENT = """
import socket, sys, time
c = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
print("connected", flush=True)
while True:
    time.sleep(1)
"""


def _alive(proc: subprocess.Popen[str]) -> bool:
    return proc.poll() is None


@needs_lsof
def test_only_the_listener_on_a_worktree_port_is_killed(tmp_path: Path) -> None:
    script = "kill-dev.sh"
    listener = subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )
    client: subprocess.Popen[str] | None = None
    try:
        assert listener.stdout is not None
        port = int(listener.stdout.readline())
        client = subprocess.Popen(
            [sys.executable, "-c", _CLIENT, str(port)], stdout=subprocess.PIPE, text=True
        )
        assert client.stdout is not None and client.stdout.readline().strip() == "connected"

        # A throwaway checkout layout: the script cds two levels above itself and
        # reads this worktree's ports from .env.workspace.
        (tmp_path / "ops/scripts").mkdir(parents=True)
        target = tmp_path / "ops/scripts" / script
        shutil.copy(REPO / "ops/scripts" / script, target)
        unused = _free_port()
        (tmp_path / ".env.workspace").write_text(
            f"API_PORT={port}\nGATEWAY_PORT={unused}\nWEB_PORT={unused}\n"
            f"ALKERA_WORKER_HEALTH_PORT={unused}\nCOMPOSE_PROJECT_NAME=alkera-test-kill\n"
        )
        subprocess.run(["bash", str(target)], check=False, capture_output=True, timeout=30)

        deadline = time.monotonic() + 5
        while _alive(listener) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(listener), "the listener on the worktree port must be killed"
        assert _alive(client), "a process merely connected to the port must survive"
    finally:
        for proc in (listener, client):
            if proc is not None and _alive(proc):
                proc.kill()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_neither_kill_script_matches_connections_on_a_port() -> None:
    """kill-all-dev cannot run hermetically (it sweeps every worktree's ports), so
    its port lookup is held to the same listener-only form here."""
    for script in ("kill-dev.sh", "kill-all-dev.sh"):
        text = (REPO / "ops/scripts" / script).read_text()
        lookups = [line for line in text.splitlines() if "lsof -ti" in line]
        assert lookups and all("-sTCP:LISTEN" in line for line in lookups), script
