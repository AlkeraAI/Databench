"""A SIGKILL at any driver or writer checkpoint leaves the object absent or whole.

The invariant this pins is the store half of "a version is all-or-nothing": a
reader that comes back after the crash either finds nothing under the final key
or finds every byte the writer intended, never a prefix of them. Temp files may
survive a SIGKILL — nothing sweeps them — but they never carry the final key.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _harness import NO_KILL, run_until_killed
from _scenarios import OBJECT_BYTES, OBJECT_KEY, STORE_ROOT
from alkera_core.files.store.filesystem import FilesystemStore, kill_points

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

# Written out independently of the driver: the link IS the commit, so every
# point before it must leave the key unused and every point at or after it must
# leave the whole object under it. A checkpoint added to either the driver or
# the writer without a row here fails the parametrization with a KeyError.
_OBJECT_AFTER_KILL: dict[str, bool] = {
    "store.after_temp_write": False,
    "atomic.after_write": False,
    "atomic.after_fsync_file": False,
    "store.after_fsync": False,
    "atomic.after_link": True,
    "store.after_link": True,
    "atomic.after_fsync_dir": True,
    NO_KILL: True,
}

_KILL_POINTS = [
    pytest.param(NO_KILL, id=NO_KILL),
    *(pytest.param(name, id=name, marks=requires_sigkill) for name in kill_points("link")),
]


def _clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("kill_at", _KILL_POINTS)
def test_a_killed_put_leaves_the_object_absent_or_byte_identical(
    kill_at: str, workdir: Path
) -> None:
    run_until_killed("put_object", kill_at=kill_at, workdir=workdir)

    root = workdir / STORE_ROOT
    store = FilesystemStore(root, clock=_clock)
    info = asyncio.run(store.head(OBJECT_KEY))
    present = _OBJECT_AFTER_KILL[kill_at]

    if not present:
        assert info is None, f"the key must not be readable before the commit ({kill_at})"
        assert not (root / OBJECT_KEY).exists()
    else:
        assert info is not None
        assert info.size == len(OBJECT_BYTES)
        assert (root / OBJECT_KEY).read_bytes() == OBJECT_BYTES

    # Whatever else survived the kill is a temp file in the object's directory,
    # never a second, partial name that a lister would hand out as an object.
    leftovers = [name for name in _files(root) if name != OBJECT_KEY]
    assert all(Path(name).name.startswith(".") for name in leftovers), leftovers
    print(f"kill_at={kill_at} leftovers: {leftovers}")


@pytest.mark.parametrize("kill_at", _KILL_POINTS)
def test_a_killed_put_never_publishes_a_partial_object(kill_at: str, workdir: Path) -> None:
    """The bytes under the final key are never a prefix of the payload."""
    run_until_killed("put_object", kill_at=kill_at, workdir=workdir)

    target = workdir / STORE_ROOT / OBJECT_KEY
    if not target.exists():
        return
    written = target.read_bytes()
    assert len(written) == len(OBJECT_BYTES), "a truncated object was published"
    assert written == OBJECT_BYTES
