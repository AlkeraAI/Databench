"""The staged rootfs, as one org's user namespace sees it.

The box stages one rootfs, owned by host root, that every gVisor sandbox is
rooted at read-only. Inside an org worker host root is no id at all (the
worker's namespace maps only the slot's range), so the rootfs reads as owned
by ``nobody``. Agents only read it, but a package manager running as the
sandbox's root cannot copy a root-owned file up into its overlay when the
file's owner does not exist where it runs: ``dpkg`` fails on its own lock.

Each org is given the rootfs at a path of its own, outside its writable root
(:func:`org_rootfs_path`), owned as its own root:

* **id-mapped** (preferred): a read-only bind of the one staged tree whose
  ids are shifted into the slot's range. No copy; every org shares the same
  blocks. It is made in the host's mount namespace before the worker starts,
  so the worker inherits it as a locked mount: neither its read-only flag nor
  the mount itself can be undone from inside the org's user namespace.
* **copied** (where the filesystem cannot id-map a mount, as an overlay root
  in a container can not): a copy with every id shifted the same way, made
  once per staged build and read-only to the worker like every other path
  outside its root.

Nothing here opens a path beneath an org root: the org's rootfs is a sibling
of the orgs root, root's to make.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from alkera_core.process import SpawnSpec, spawn

from alkera_cli.supervisor.slots import ORG_UID_SPAN, Slot

#: Where each org's view of the rootfs lives: ``<this>/<slot>``, beside the
#: orgs root.
ORG_ROOTFS_DIR: Final = "org-rootfs"
#: The file a finished staging leaves in the rootfs (``sandbox-prereqs.sh``);
#: its content names the build.
ROOTFS_STAMP: Final = ".alkera-rootfs"

Outcome = Literal["present", "idmapped", "copied"]

_AT_FDCWD: Final = -100
_AT_EMPTY_PATH: Final = 0x1000
_AT_RECURSIVE: Final = 0x8000
_OPEN_TREE_CLONE: Final = 1
_OPEN_TREE_CLOEXEC: Final = os.O_CLOEXEC
_MOVE_MOUNT_F_EMPTY_PATH: Final = 0x4
_MOUNT_ATTR_RDONLY: Final = 0x1
_MOUNT_ATTR_NOSUID: Final = 0x2
_MOUNT_ATTR_NODEV: Final = 0x4
_MOUNT_ATTR_IDMAP: Final = 0x00100000
_MNT_DETACH: Final = 2
#: The new mount API's syscall numbers are the same on every architecture.
_SYS_OPEN_TREE: Final = 428
_SYS_MOVE_MOUNT: Final = 429
_SYS_MOUNT_SETATTR: Final = 442


class RootfsError(RuntimeError):
    """The org's rootfs could not be given to it."""


class _MountAttr(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


def org_rootfs_path(orgs_root: Path, slot: Slot) -> Path:
    return orgs_root.parent / ORG_ROOTFS_DIR / str(slot.index)


def stamp_of(tree: Path) -> str | None:
    try:
        return (tree / ROOTFS_STAMP).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _syscall(number: int, *args: object) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = int(libc.syscall(ctypes.c_long(number), *args))
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def _mapping_namespace(slot: Slot) -> subprocess.Popen[bytes]:
    """A process whose user namespace maps ``0..span`` to the slot's range,
    the way the worker's own does: the mapping the mount takes its ids from."""
    return spawn(
        SpawnSpec(
            argv=[
                "unshare",
                "--user",
                f"--map-users=0:{slot.uid_base}:{ORG_UID_SPAN}",
                f"--map-groups=0:{slot.uid_base}:{ORG_UID_SPAN}",
                "--",
                "sleep",
                "60",
            ],
            env=os.environ,
            stdout="devnull",
            stderr="devnull",
        )
    )


def idmap_bind(source: Path, target: Path, slot: Slot) -> None:
    """Bind ``source`` at ``target`` read-only (and nosuid, nodev), its ids
    shifted into ``slot``'s range. Raises :class:`OSError` where the kernel
    or the filesystem cannot id-map a mount."""
    helper = _mapping_namespace(slot)
    try:
        uid_map = Path(f"/proc/{helper.pid}/uid_map")
        for _ in range(100):
            with contextlib.suppress(OSError):
                if uid_map.read_text().strip():
                    break
            time.sleep(0.02)
        userns = os.open(f"/proc/{helper.pid}/ns/user", os.O_RDONLY | os.O_CLOEXEC)
        try:
            tree = _syscall(
                _SYS_OPEN_TREE,
                ctypes.c_int(_AT_FDCWD),
                ctypes.c_char_p(os.fsencode(source)),
                ctypes.c_uint(_OPEN_TREE_CLONE | _OPEN_TREE_CLOEXEC | _AT_RECURSIVE),
            )
            try:
                attr = _MountAttr(
                    _MOUNT_ATTR_RDONLY | _MOUNT_ATTR_NOSUID | _MOUNT_ATTR_NODEV | _MOUNT_ATTR_IDMAP,
                    0,
                    0,
                    userns,
                )
                _syscall(
                    _SYS_MOUNT_SETATTR,
                    ctypes.c_int(tree),
                    ctypes.c_char_p(b""),
                    ctypes.c_uint(_AT_EMPTY_PATH | _AT_RECURSIVE),
                    ctypes.byref(attr),
                    ctypes.c_size_t(ctypes.sizeof(attr)),
                )
                _syscall(
                    _SYS_MOVE_MOUNT,
                    ctypes.c_int(tree),
                    ctypes.c_char_p(b""),
                    ctypes.c_int(_AT_FDCWD),
                    ctypes.c_char_p(os.fsencode(target)),
                    ctypes.c_uint(_MOVE_MOUNT_F_EMPTY_PATH),
                )
            finally:
                os.close(tree)
        finally:
            os.close(userns)
    finally:
        helper.kill()
        helper.wait()


def unmount(target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.umount2(os.fsencode(target), _MNT_DETACH) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def shifted_copy(
    source: Path,
    target: Path,
    slot: Slot,
    *,
    chown: Callable[[str, int, int], None] = os.lchown,
) -> None:
    """Copy ``source`` to ``target`` (which must not exist), every file, link
    and directory with its ids shifted into ``slot``'s range and its mode
    kept. Device nodes, sockets and fifos are left out: the sandbox brings
    its own ``/dev``. An id past the span maps to the slot's ``nobody``."""

    def shift(path: str, info: os.stat_result) -> None:
        uid = info.st_uid if info.st_uid < ORG_UID_SPAN else ORG_UID_SPAN - 1
        gid = info.st_gid if info.st_gid < ORG_UID_SPAN else ORG_UID_SPAN - 1
        chown(path, slot.uid_base + uid, slot.uid_base + gid)

    for here, dirs, files in os.walk(source, followlinks=False):
        rel = os.path.relpath(here, source)
        dest = target if rel == "." else target / rel
        info = os.lstat(here)
        dest.mkdir(mode=stat.S_IMODE(info.st_mode), exist_ok=rel != ".")
        for name in [*files, *(d for d in dirs if os.path.islink(os.path.join(here, d)))]:
            src = os.path.join(here, name)
            dst = os.fspath(dest / name)
            entry = os.lstat(src)
            if stat.S_ISLNK(entry.st_mode):
                os.symlink(os.readlink(src), dst)
            elif stat.S_ISREG(entry.st_mode):
                shutil.copy2(src, dst, follow_symlinks=False)
            else:
                continue
            shift(dst, entry)
            if stat.S_ISREG(entry.st_mode):
                # A chown clears the set-id bits; the copy keeps them as staged.
                os.chmod(dst, stat.S_IMODE(entry.st_mode))
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(here, d))]
    for here, _dirs, _files in os.walk(target, topdown=False, followlinks=False):
        rel = os.path.relpath(here, target)
        original = source if rel == "." else source / rel
        info = os.lstat(original)
        os.chmod(here, stat.S_IMODE(info.st_mode))
        shift(here, info)


def add_missing_dirs(
    source: Path,
    target: Path,
    slot: Slot,
    *,
    chown: Callable[[str, int, int], None] = os.lchown,
) -> list[str]:
    """Give a copy every directory the staged tree has gained since it was
    made, with its mode and its ids shifted as :func:`shifted_copy` shifts
    them; what the copy has already is left alone. The box adds the chats'
    mountpoints to the staged tree without restaging it, and a copy that lacks
    one refuses every bind a chat mounts there. The directories added."""
    added: list[str] = []
    for here, dirs, _files in os.walk(source, followlinks=False):
        rel = os.path.relpath(here, source)
        dest = target if rel == "." else target / rel
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(here, d))]
        for name in dirs:
            made = dest / name
            if os.path.lexists(made):
                continue
            info = os.lstat(os.path.join(here, name))
            made.mkdir(mode=stat.S_IMODE(info.st_mode))
            os.chmod(made, stat.S_IMODE(info.st_mode))
            uid = info.st_uid if info.st_uid < ORG_UID_SPAN else ORG_UID_SPAN - 1
            gid = info.st_gid if info.st_gid < ORG_UID_SPAN else ORG_UID_SPAN - 1
            chown(os.fspath(made), slot.uid_base + uid, slot.uid_base + gid)
            added.append(os.path.join(rel, name) if rel != "." else name)
    return added


def ensure_org_rootfs(
    source: Path,
    target: Path,
    slot: Slot,
    *,
    is_mount: Callable[[Path], bool] = os.path.ismount,
    bind: Callable[[Path, Path, Slot], None] = idmap_bind,
    detach: Callable[[Path], None] = unmount,
    copy: Callable[[Path, Path, Slot], None] = shifted_copy,
    complete: Callable[[Path, Path, Slot], object] = add_missing_dirs,
) -> Outcome:
    """Give ``slot``'s org the staged rootfs at ``target``: id-mapped where the
    host can, else a shifted copy. Kept as it is while it is the staged
    build's (its stamp matches); replaced when the box staged another."""
    real = source.resolve()
    wanted = stamp_of(real)
    if wanted is None:
        raise RootfsError(f"no staged rootfs at {source}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o711)
    # Each org walks to its own rootfs by name and lists nobody's.
    os.chmod(target.parent, 0o711)  # noqa: S103 -- traverse only, by design
    if is_mount(target):
        if stamp_of(target) == wanted:
            return "present"
        detach(target)
    elif target.exists() and stamp_of(target) == wanted:
        # A copy of the same build; an id-mapped bind shows the tree as it is.
        complete(real, target, slot)
        return "present"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            target.unlink()
        else:
            shutil.rmtree(target)
    target.mkdir(mode=0o755)
    try:
        bind(real, target, slot)
        return "idmapped"
    except OSError:
        pass
    staged = target.with_name(f".{target.name}.copying")
    if staged.exists():
        shutil.rmtree(staged)
    copy(real, staged, slot)
    target.rmdir()
    staged.rename(target)
    return "copied"


__all__ = [
    "ORG_ROOTFS_DIR",
    "ROOTFS_STAMP",
    "Outcome",
    "RootfsError",
    "add_missing_dirs",
    "ensure_org_rootfs",
    "idmap_bind",
    "org_rootfs_path",
    "shifted_copy",
    "stamp_of",
    "unmount",
]
