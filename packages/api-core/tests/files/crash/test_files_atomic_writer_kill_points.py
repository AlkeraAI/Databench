"""A SIGKILL at any AtomicWriter checkpoint leaves old-or-new bytes, never a mix."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from _harness import NO_KILL, run_until_killed
from _scenarios import (
    AFTER_MARKER,
    NEW_BYTES,
    OLD_BYTES,
    TARGET_NAME,
)
from alkera_core.files.sync.atomic import CHECKPOINTS

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

# The contract, written out independently of the writer: the rename is the
# instant the target flips. Anything before it must still read as the old
# state; anything at or after it reads as the new bytes. A checkpoint added to
# CHECKPOINTS without a row here raises KeyError and fails the parametrization.
_TARGET_AFTER_KILL: dict[str, bytes | None] = {
    "atomic.after_write": None,
    "atomic.after_fsync_file": None,
    "atomic.after_rename": NEW_BYTES,
    "atomic.after_fsync_dir": NEW_BYTES,
    NO_KILL: NEW_BYTES,
}

_KILL_POINTS = [
    pytest.param(NO_KILL, id=NO_KILL),
    *(
        pytest.param(name, id=name.replace("atomic.", ""), marks=requires_sigkill)
        for name in CHECKPOINTS
    ),
]

_TEMP_NAME = re.compile(rf"^\.{re.escape(TARGET_NAME)}\.[0-9a-f]+\.tmp$")


def _leftover_temps(workdir: Path) -> list[str]:
    return sorted(p.name for p in workdir.iterdir() if _TEMP_NAME.match(p.name))


def _unexpected_files(workdir: Path) -> list[str]:
    allowed = {TARGET_NAME, AFTER_MARKER, "before.marker"}
    return sorted(
        p.name for p in workdir.iterdir() if p.name not in allowed and not _TEMP_NAME.match(p.name)
    )


@pytest.mark.parametrize("kill_at", _KILL_POINTS)
def test_overwrite_leaves_exactly_the_old_or_the_new_bytes(kill_at: str, workdir: Path) -> None:
    run_until_killed("overwrite_existing", kill_at=kill_at, workdir=workdir)

    target = workdir / TARGET_NAME
    assert target.exists(), "the pre-existing target must never disappear"
    expected = _TARGET_AFTER_KILL[kill_at] or OLD_BYTES
    assert target.read_bytes() == expected

    # A leftover temp file is allowed (nothing sweeps it after a SIGKILL) but a
    # half-written file under any other name is not.
    assert _unexpected_files(workdir) == []
    print(f"kill_at={kill_at} leftover temps: {_leftover_temps(workdir)}")


@pytest.mark.parametrize("kill_at", _KILL_POINTS)
def test_new_target_is_absent_or_complete(kill_at: str, workdir: Path) -> None:
    run_until_killed("write_new_file", kill_at=kill_at, workdir=workdir)

    target = workdir / TARGET_NAME
    expected = _TARGET_AFTER_KILL[kill_at]
    if expected is None:
        assert not target.exists(), "the target must not appear before the rename"
    else:
        assert target.read_bytes() == expected

    assert _unexpected_files(workdir) == []
    print(f"kill_at={kill_at} leftover temps: {_leftover_temps(workdir)}")
