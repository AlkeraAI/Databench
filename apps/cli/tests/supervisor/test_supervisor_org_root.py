"""An org's data root: made by the supervisor, closed to every other org."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from alkera_cli.org_root import (
    ORG_ROOT_MODE,
    ORGS_ROOT_MODE,
    OrgRoot,
    OrgRootError,
    ensure_org_root,
    ensure_org_subdirs,
)
from alkera_cli.supervisor.slots import Slot

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX owners and modes")

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _ensure(orgs: Path, index: int = 3) -> OrgRoot:
    me = (os.getuid(), os.getgid())
    return ensure_org_root(orgs, Slot(index, ORG), root_owner=me, owner=me)


def test_the_root_is_owner_only_and_the_orgs_root_lists_nobody(tmp_path: Path) -> None:
    orgs = tmp_path / "orgs"
    root = _ensure(orgs)
    assert root.path == orgs / "3"
    assert _mode(root.path) == ORG_ROOT_MODE == 0o700
    assert _mode(orgs) == ORGS_ROOT_MODE == 0o711
    # The supervisor makes the root and nothing in it.
    assert list(root.path.iterdir()) == []


def test_drifted_modes_are_put_back(tmp_path: Path) -> None:
    orgs = tmp_path / "orgs"
    root = _ensure(orgs)
    root.path.chmod(0o755)
    orgs.chmod(0o755)
    _ensure(orgs)
    assert (_mode(root.path), _mode(orgs)) == (0o700, 0o711)


def test_a_link_where_the_root_should_be_is_refused(tmp_path: Path) -> None:
    """A root a worker could once write must never point the supervisor at
    another tree: a link in its place is refused, never followed."""
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    elsewhere = tmp_path / "other-org"
    elsewhere.mkdir(mode=0o700)
    (orgs / "3").symlink_to(elsewhere)
    with pytest.raises(OrgRootError):
        _ensure(orgs)
    assert _mode(elsewhere) == 0o700


def test_the_worker_makes_its_own_subdirectories_owner_only(tmp_path: Path) -> None:
    root = _ensure(tmp_path / "orgs")
    previous = os.umask(0o022)
    try:
        ensure_org_subdirs(root)
    finally:
        os.umask(previous)
    for path in (root.work, root.home, root.tmp, root.logs):
        assert path.parent == root.path and _mode(path) == 0o700
