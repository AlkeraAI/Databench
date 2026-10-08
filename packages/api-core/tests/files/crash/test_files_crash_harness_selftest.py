"""The crash harness itself: it really kills, only at the named point."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from _harness import NO_KILL, SIGKILL_RETURNCODE, run_until_killed
from _scenarios import AFTER_MARKER, BEFORE_MARKER, NEW_BYTES, TARGET_NAME

# The harness kills its child where a power loss would land: `os.kill(pid,
# signal.SIGKILL)` from inside the child, and a parent that asserts returncode
# -9. Windows has no SIGKILL and no way to stop a process at a point inside its
# own execution without letting its cleanup run, so the scenario child dies with
# an AttributeError and every checkpoint reads as unreached. The un-killed runs
# below have no such dependency and stay on.
requires_sigkill = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the crash harness SIGKILLs its own child at a checkpoint; Windows has no SIGKILL",
)


@requires_sigkill
def test_child_dies_by_sigkill_at_the_named_checkpoint(workdir: Path) -> None:
    result = run_until_killed("write_new_file", kill_at="atomic.after_fsync_file", workdir=workdir)

    assert result.returncode == SIGKILL_RETURNCODE
    assert result.killed
    assert (workdir / BEFORE_MARKER).exists(), "the scenario ran up to the checkpoint"
    assert not (workdir / AFTER_MARKER).exists(), "nothing ran after the checkpoint"


def test_kill_at_none_runs_the_scenario_to_completion(workdir: Path) -> None:
    result = run_until_killed("write_new_file", kill_at=NO_KILL, workdir=workdir)

    assert result.returncode == 0
    assert (workdir / AFTER_MARKER).read_bytes() == b"after"
    assert (workdir / TARGET_NAME).read_bytes() == NEW_BYTES


def test_a_checkpoint_that_is_never_reached_fails_naming_it(workdir: Path) -> None:
    with pytest.raises(AssertionError, match=r"atomic\.never_emitted") as raised:
        run_until_killed("write_new_file", kill_at="atomic.never_emitted", workdir=workdir)

    assert "exited 0" in str(raised.value), (
        "the verdict is the child's own exit — a harness that gave up on a clock "
        "would report a checkpoint as unreached without the scenario having ended"
    )
    assert (workdir / AFTER_MARKER).exists(), "the unreached name left the scenario intact"
