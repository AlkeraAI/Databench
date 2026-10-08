"""Run a Files crash scenario in a subprocess and SIGKILL it at a checkpoint."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_SCENARIOS = Path(__file__).with_name("_scenarios.py")

# The child is launched without importing this test package: it runs the
# scenario file by path and calls the named function, so the tests tree never
# needs an ``__init__.py`` and the child cannot inherit pytest state.
_CHILD_BOOTSTRAP = (
    "import os, runpy, sys\n"
    "ns = runpy.run_path(sys.argv[1])\n"
    "ns[sys.argv[2]](os.environ['FILES_CRASH_DIR'])\n"
)

SIGKILL_RETURNCODE = -9
NO_KILL = "none"


@dataclass(frozen=True)
class CrashResult:
    """The observable outcome of one crash-harness run."""

    scenario: str
    kill_at: str
    returncode: int
    stdout: str
    stderr: str
    workdir: Path

    @property
    def killed(self) -> bool:
        return self.returncode == SIGKILL_RETURNCODE


def run_until_killed(scenario: str, *, kill_at: str, workdir: Path) -> CrashResult:
    """Run ``scenario`` in a child process, SIGKILLed at ``kill_at``.

    ``kill_at="none"`` runs the scenario to completion. Any other name must be
    reached, and the verdict is the child's own exit: ``-9`` is the checkpoint
    firing, any other status means it never fired and raises an
    ``AssertionError`` naming the checkpoint.

    The harness holds no deadline of its own. One measured in seconds says
    nothing about the scenario — on a loaded runner, where the child's first
    import alone can take most of a minute, it fires on a scenario that is
    merely slow, reports a checkpoint as unreached on the strength of a clock,
    and kills the child mid-scenario so a test reading the leftovers sees a
    crash the harness caused. The suite's own per-test limit is the one
    deadline, and it ends the test rather than inventing a verdict for it.
    """
    env = dict(os.environ)
    env["FILES_CRASH_AT"] = kill_at
    env["FILES_CRASH_DIR"] = str(workdir)
    argv = [sys.executable, "-c", _CHILD_BOOTSTRAP, str(_SCENARIOS), scenario]
    completed = subprocess.run(
        argv,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    result = CrashResult(
        scenario=scenario,
        kill_at=kill_at,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        workdir=workdir,
    )
    if kill_at == NO_KILL:
        if completed.returncode != 0:
            raise AssertionError(
                f"crash harness: scenario {scenario!r} was expected to complete "
                f"but exited {completed.returncode}\n{completed.stderr}"
            )
        return result
    if not result.killed:
        raise AssertionError(
            f"crash harness: checkpoint {kill_at!r} was never reached "
            f"(scenario {scenario!r} exited {completed.returncode})\n{completed.stderr}"
        )
    return result
