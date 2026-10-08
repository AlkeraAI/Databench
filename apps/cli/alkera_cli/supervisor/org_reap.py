"""Removing an org's data from a box once the org has left it.

An org that has had no chat routed to the box for :data:`REAP_IDLE_SECONDS`
loses everything it kept here: its root (working trees, transcripts, stores,
HOME with any cached cloud login) and its view of the rootfs. Only then is its
slot given back (:meth:`~alkera_cli.supervisor.slots.SlotTable.release`): a
slot whose files remain still owns their ids.

The removal runs as root over a tree the org's worker wrote, so it opens every
directory relative to its parent's descriptor and never follows a link: a
link the worker planted is removed as a link, whatever it points at. It never
crosses into another filesystem either: a mount found beneath the tree stops
the removal, and the slot is kept.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Final

from alkera_core.compute import box_logs

from alkera_cli.supervisor.org_events import emit_for
from alkera_cli.supervisor.org_rootfs import org_rootfs_path, unmount
from alkera_cli.supervisor.slots import Slot, SlotTable

#: How long an org may have had no chat routed to the box before its data
#: goes.
REAP_IDLE_SECONDS: Final = 14 * 24 * 3600.0
#: How often the supervisor looks for an org to reap.
REAP_EVERY_SECONDS: Final = 3600.0
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ReapError(RuntimeError):
    """An org's data could not be removed whole; its slot is kept."""


def _empty(fd: int, device: int) -> None:
    """Remove everything beneath the directory open at ``fd``."""
    with os.scandir(fd) as entries:
        names = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    for name, is_dir in names:
        if not is_dir:
            os.unlink(name, dir_fd=fd)
            continue
        child = os.open(name, _DIR_FLAGS, dir_fd=fd)
        try:
            if os.fstat(child).st_dev != device:
                raise ReapError(f"a mount beneath the tree at {name!r}")
            _empty(child, device)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=fd)


def remove_tree(parent: Path, name: str) -> None:
    """Remove ``parent/name`` and everything beneath it; nothing there is
    nothing to do. ``parent`` itself must not be a link."""
    try:
        top = os.open(parent, _DIR_FLAGS)
    except FileNotFoundError:
        return
    try:
        try:
            info = os.stat(name, dir_fd=top, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=top)
            return
        fd = os.open(name, _DIR_FLAGS, dir_fd=top)
        try:
            if os.fstat(fd).st_dev != os.fstat(top).st_dev:
                raise ReapError(f"{name!r} is a mount of its own")
            _empty(fd, info.st_dev)
        except RecursionError as exc:
            raise ReapError(f"{name!r} is nested past what can be removed") from exc
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=top)
    finally:
        os.close(top)


def host_mountpoints() -> list[str]:
    """Every mountpoint in this process's mount namespace (none where there is
    no ``/proc``)."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return []
    # Field 5 is the mountpoint, its blanks and backslashes octal-escaped.
    return [
        re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), line.split()[4])
        for line in text.splitlines()
        if len(line.split()) > 4
    ]


def _refuse_mounts(path: Path, mountpoints: list[str]) -> None:
    """A bind mount on the same filesystem shares its device number, so the
    walk's own check cannot see it: the mount table does."""
    real = os.path.realpath(path)
    inside = [m for m in mountpoints if m == real or m.startswith(f"{real}/")]
    if inside:
        raise ReapError(f"mounted beneath {path}: {', '.join(sorted(inside))}")


def remove_org_data(
    orgs_root: Path,
    slot: Slot,
    *,
    is_mount: Callable[[Path], bool] = os.path.ismount,
    detach: Callable[[Path], None] = unmount,
    mountpoints: Callable[[], list[str]] = host_mountpoints,
) -> None:
    """Remove the slot's root, then its rootfs (detached when it is a mount,
    removed when it is a copy). Raises :class:`ReapError` or :class:`OSError`
    when anything of either is left."""
    root = orgs_root / str(slot.index)
    _refuse_mounts(root, mountpoints())
    remove_tree(orgs_root, str(slot.index))
    rootfs = org_rootfs_path(orgs_root, slot)
    if is_mount(rootfs):
        detach(rootfs)
    _refuse_mounts(rootfs, mountpoints())
    remove_tree(rootfs.parent, rootfs.name)
    remove_tree(rootfs.parent, f".{rootfs.name}.copying")
    left = [p for p in (root, rootfs) if os.path.lexists(p)]
    if left:
        raise ReapError(f"still on disk: {', '.join(map(str, left))}")


async def reap_idle_orgs(
    slots: SlotTable,
    *,
    busy: Collection[str],
    wall: float,
    removable: Callable[[Slot], bool],
    remove: Callable[[Slot], None],
) -> list[str]:
    """Remove the data of every org that is not ``busy`` (routed here, or
    with a worker) and has had no chat routed here for
    :data:`REAP_IDLE_SECONDS` by the wall clock ``wall``, and whose worker is
    gone (``removable``); only then give its slot back. A slot whose data
    could not all be removed is kept, and tried again on a later pass. The
    orgs released."""
    released: list[str] = []
    for slot in slots.slots():
        if slot.org_id in busy:
            continue
        seen = slots.seen_at(slot)
        if seen is None:  # a slot from before the box kept the time
            slots.touch(slot, wall)
            continue
        if wall - seen < REAP_IDLE_SECONDS or not await asyncio.to_thread(removable, slot):
            continue
        try:
            await asyncio.to_thread(remove, slot)
        except (OSError, ReapError) as exc:
            emit_for(slot, box_logs.ORG_REAP_FAILED, level="error", error=str(exc))
            continue
        slots.release(slot.org_id)
        released.append(slot.org_id)
        emit_for(slot, box_logs.ORG_REAPED, idle_seconds=wall - seen)
    return released


__all__ = [
    "REAP_EVERY_SECONDS",
    "REAP_IDLE_SECONDS",
    "ReapError",
    "host_mountpoints",
    "reap_idle_orgs",
    "remove_org_data",
    "remove_tree",
]
