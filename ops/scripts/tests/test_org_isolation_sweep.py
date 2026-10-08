"""The org isolation sweep judges a check it could not run as untested, never
as a pass and never as a failure.

An org's tool server listens only while the org has a live chat. With none,
the sweep used to probe ``/dev/tcp/<ip>/`` with an empty port: the control
"A connects to its own tool server" failed although every isolation check
held, and "B's tool port is refused" passed without testing anything."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "dev" / "org-isolation-sweep.sh"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)

HELD = (
    "writable=none other_root=denied other_log=denied other_pid=invisible node_env=denied "
    "host_ports=refused metadata=refused"
).split()


def _judge(*verdicts: str) -> tuple[str, int]:
    """Source the sweep (which then runs nothing) and judge one probe output."""
    done = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; judge W "$2"; echo "fail=$fail"',
            "sweep-test",
            str(SCRIPT),
            "\n".join(verdicts),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    out = done.stdout
    return out, int(out.rsplit("fail=", 1)[1])


@needs_bash
def test_an_org_with_no_live_chat_leaves_the_port_check_untested() -> None:
    out, fail = _judge(*HELD, "other_port=untested")
    assert fail == 0
    assert "--    W: other_port untested" in out
    assert "ok    W: other_port" not in out


@needs_bash
def test_a_refused_port_passes_and_a_connected_one_fails() -> None:
    out, fail = _judge(*HELD, "other_port=refused")
    assert fail == 0 and "ok    W: other_port=refused" in out
    out, fail = _judge(*HELD, "other_port=connected")
    assert fail == 1 and "FAIL  W: other_port=connected" in out


@needs_bash
def test_a_missing_verdict_is_a_failure() -> None:
    _out, fail = _judge(*HELD)
    assert fail == 1


@needs_bash
def test_sourcing_the_sweep_runs_nothing() -> None:
    done = subprocess.run(
        ["bash", "-c", 'source "$1"; echo sourced', "sweep-test", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert done.stdout.strip() == "sourced"
