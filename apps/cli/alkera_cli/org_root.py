"""An org's data root: the one writable tree its worker has.

``<orgs root>/<slot>/`` holds everything the org's worker writes: its project
(``.alkera``), its ``ALKERA_HOME``, ``HOME``, ``TMPDIR`` and logs. It is owned
by the slot's base uid, 0700, so no other org's uids can enter it; the orgs
root above it is root's, 0711, so a worker can walk to its own root by name
and list nobody's.

The supervisor creates the root and nothing below it: it never opens a path
beneath an org root. The worker makes its subdirectories itself, as their
owner, when it starts (:func:`ensure_org_subdirs`). Each one is made relative to a descriptor
on its parent and refused if it is a link, so a root a worker could once write
cannot point the supervisor somewhere else.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from alkera_core.compute.box_layout import ORG_ROOT_MODE, ORGS_ROOT_MODE

from alkera_cli.org_slot import Slot

#: The subdirectories every org root starts with.
ORG_SUBDIRS: Final = ("work", "home", "tmp", "logs")


class OrgRootError(RuntimeError):
    """An org root is not what the supervisor made, and is not used."""


@dataclass(frozen=True, slots=True)
class OrgRoot:
    """Where one slot's worker keeps everything."""

    path: Path

    @property
    def work(self) -> Path:
        """The worker's working directory: its ``.alkera`` project lives here."""
        return self.path / "work"

    @property
    def home(self) -> Path:
        """``ALKERA_HOME`` and ``HOME``."""
        return self.path / "home"

    @property
    def tmp(self) -> Path:
        return self.path / "tmp"

    @property
    def logs(self) -> Path:
        return self.path / "logs"


def _open_dir(name: str, *, parent: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _ensure_child(parent: int, name: str, *, uid: int, gid: int, mode: int) -> int:
    """A directory ``name`` under ``parent`` owned by ``uid`` with ``mode``,
    made if absent. A link or a non-directory there is refused."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    try:
        fd = _open_dir(name, parent=parent)
    except OSError as exc:
        raise OrgRootError(f"{name} is not a directory the supervisor made: {exc}") from exc
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):  # pragma: no cover - O_DIRECTORY refuses it
        os.close(fd)
        raise OrgRootError(f"{name} is not a directory")
    if (info.st_uid, info.st_gid) != (uid, gid):
        os.fchown(fd, uid, gid)
    if stat.S_IMODE(info.st_mode) != mode:
        os.fchmod(fd, mode)
    return fd


def ensure_org_subdirs(root: OrgRoot) -> None:
    """The worker's side: its subdirectories, owner-only, made by the worker
    inside its own namespace (where the root's owner is its uid 0)."""
    for path in (root.work, root.home, root.tmp, root.logs):
        path.mkdir(mode=ORG_ROOT_MODE, exist_ok=True)
        path.chmod(ORG_ROOT_MODE)


def ensure_org_root(
    orgs_root: Path,
    slot: Slot,
    *,
    root_owner: tuple[int, int] = (0, 0),
    owner: tuple[int, int] | None = None,
) -> OrgRoot:
    """Make ``<orgs_root>/<slot>``, owned by the slot's base uid, 0700, and the
    orgs root itself, owned by ``root_owner`` (the supervisor),
    0711. Idempotent; owners and modes that drifted are put back. ``owner``
    stands in for the slot's base uid where the caller may not give files
    away (a test)."""
    orgs_root.mkdir(parents=True, exist_ok=True, mode=ORGS_ROOT_MODE)
    top = os.open(orgs_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(top)
        if (info.st_uid, info.st_gid) != root_owner:
            os.fchown(top, *root_owner)
        if stat.S_IMODE(info.st_mode) != ORGS_ROOT_MODE:
            os.fchmod(top, ORGS_ROOT_MODE)
        uid, gid = (slot.uid_base, slot.uid_base) if owner is None else owner
        os.close(_ensure_child(top, str(slot.index), uid=uid, gid=gid, mode=ORG_ROOT_MODE))
    finally:
        os.close(top)
    return OrgRoot(orgs_root / str(slot.index))


__all__ = [
    "ORGS_ROOT_MODE",
    "ORG_ROOT_MODE",
    "ORG_SUBDIRS",
    "OrgRoot",
    "OrgRootError",
    "ensure_org_root",
    "ensure_org_subdirs",
]
