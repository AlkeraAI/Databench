"""What this process may really use of the machine it runs on.

A container sees its host's memory in ``/proc/meminfo`` and its host's cores
in ``os.cpu_count()``; what it may use is set by its cgroup. Sizing anything
from the host's figures (how many chats a box serves, how many org workers it
runs, how many threads an embedder takes) oversubscribes a container until the
kernel kills it. This module is the one reader of those facts: it resolves the
process's OWN cgroup through ``/proc/self/cgroup`` (a container without a
private cgroup namespace sits in a nested group, not at the root) and takes the
tightest limit of that group and every ancestor.

The roots are parameters so a test can lay out a fake ``/proc`` and
``/sys/fs/cgroup`` in a temporary directory.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: cgroup v1 spells "no limit" as the largest page-aligned 64-bit number, and
#: v2 as ``max``; a figure this large is no limit anyone set.
_NO_LIMIT: Final = 1 << 60


@dataclass(frozen=True)
class HostRoots:
    """Where the kernel's files are read from."""

    proc: Path = Path("/proc")
    cgroup: Path = Path("/sys/fs/cgroup")
    block: Path = Path("/sys/class/block")


DEFAULT_ROOTS: Final = HostRoots()


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii")
    except (OSError, ValueError):
        return None


def _limit(path: Path) -> int | None:
    """One byte count from a cgroup file, or ``None`` when it is absent,
    unreadable, ``max``, or so large it is no limit."""
    text = (_read(path) or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if 0 < value < _NO_LIMIT else None


def _own_groups(roots: HostRoots) -> tuple[str | None, str | None]:
    """``(v2 path, v1 memory path)`` of this process's cgroup, each relative
    to its hierarchy's root, from ``/proc/self/cgroup``."""
    v2: str | None = None
    v1: str | None = None
    for line in (_read(roots.proc / "self" / "cgroup") or "").splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "":
            v2 = path
        elif "memory" in controllers.split(","):
            v1 = path
    return v2, v1


def _lineage(top: Path, relative: str | None) -> list[Path]:
    """``top/relative`` and each ancestor up to ``top`` itself."""
    dirs = [top]
    if relative:
        current = top
        for part in Path(relative.lstrip("/")).parts:
            current = current / part
            dirs.append(current)
    return dirs


def cgroup_memory_limit_bytes(roots: HostRoots = DEFAULT_ROOTS) -> int | None:
    """The tightest memory limit on this process's cgroup and its ancestors
    (v2 ``memory.max``, v1 ``memory.limit_in_bytes``), or ``None``."""
    v2, v1 = _own_groups(roots)
    found = [
        limit
        for directory in _lineage(roots.cgroup, v2)
        if (limit := _limit(directory / "memory.max")) is not None
    ]
    found += [
        limit
        for directory in _lineage(roots.cgroup / "memory", v1)
        if (limit := _limit(directory / "memory.limit_in_bytes")) is not None
    ]
    return min(found) if found else None


def _meminfo(roots: HostRoots) -> dict[str, int]:
    fields: dict[str, int] = {}
    for line in (_read(roots.proc / "meminfo") or "").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[name] = int(parts[0]) * 1024
    return fields


def _psutil_memory() -> tuple[int, int] | None:
    """``(total, available)`` where there is no ``/proc/meminfo``. psutil is the
    CLI's dependency, not this package's, so it is optional here."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        memory = psutil.virtual_memory()
    except (OSError, RuntimeError, ValueError):
        return None
    return int(memory.total), int(memory.available)


def host_memory_bytes(roots: HostRoots = DEFAULT_ROOTS) -> int | None:
    """The machine's own memory (``MemTotal``, else the platform's figure)."""
    total = _meminfo(roots).get("MemTotal")
    if total:
        return total
    if roots != DEFAULT_ROOTS or sys.platform.startswith("linux"):
        return None
    found = _psutil_memory()
    return found[0] if found and found[0] > 0 else None


def effective_memory_bytes(roots: HostRoots = DEFAULT_ROOTS) -> int | None:
    """The memory this process may use: the tightest of its cgroup's limits
    and the machine's own memory, or ``None`` when nothing can say."""
    known = [
        value
        for value in (cgroup_memory_limit_bytes(roots), host_memory_bytes(roots))
        if value is not None
    ]
    return min(known) if known else None


def host_memory_in_use(roots: HostRoots = DEFAULT_ROOTS) -> tuple[int, int] | None:
    """``(used, total)`` of the machine's memory, where used is total less
    what the kernel reports available; ``None`` when it cannot be read."""
    fields = _meminfo(roots)
    total = fields.get("MemTotal", 0)
    if total:
        return max(0, total - fields.get("MemAvailable", total)), total
    if roots != DEFAULT_ROOTS or sys.platform.startswith("linux"):
        return None
    found = _psutil_memory()
    if found is None or found[0] <= 0:
        return None
    return max(0, found[0] - found[1]), found[0]


def disk_bytes(path: Path) -> tuple[int, int]:
    """``(used, total)`` of the filesystem holding ``path`` (its nearest
    existing ancestor), counting space reserved for root as used; zeros when it
    cannot be read."""
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        disk = os.statvfs(path)
    except (OSError, AttributeError):
        return 0, 0
    total = disk.f_blocks * disk.f_frsize
    return max(0, total - disk.f_bavail * disk.f_frsize), total


@dataclass(frozen=True)
class VolumeDevice:
    """The block device a filesystem sits on, and how big the device is now:
    a provider can grow it under the mounted filesystem (an EBS volume)."""

    device: str
    size_bytes: int


def volume_device(path: Path, roots: HostRoots = DEFAULT_ROOTS) -> VolumeDevice | None:
    """The device under the filesystem holding ``path`` (the deepest mount
    above it), or ``None`` when that filesystem is not on a block device of
    this host (an overlay, a network volume) or cannot be read."""
    target = path.as_posix()
    found: tuple[str, str] | None = None
    for line in (_read(roots.proc / "self" / "mounts") or "").splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        device, mount = fields[0], fields[1]
        inside = target == mount or target.startswith(mount.rstrip("/") + "/")
        if inside and (found is None or len(mount) > len(found[1])):
            found = (device, mount)
    if found is None or not found[0].startswith("/dev/"):
        return None
    sectors = _read(roots.block / Path(found[0]).name / "size")
    if sectors is None or not sectors.strip().isdigit():
        return None
    # The kernel counts a block device in 512-byte sectors whatever its own.
    return VolumeDevice(found[0], int(sectors.strip()) * 512)


def _cpu_quota(path: Path) -> float | None:
    """``quota / period`` from a v2 ``cpu.max``, or ``None`` for ``max``."""
    parts = (_read(path) or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    quota, period = int(parts[0]), int(parts[1])
    return quota / period if quota > 0 and period > 0 else None


def _visible_cpus() -> int:
    """The cores this process may be scheduled on."""
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            return max(1, len(affinity(0)))
        except OSError:
            pass
    return max(1, os.cpu_count() or 1)


def effective_cpus(roots: HostRoots = DEFAULT_ROOTS, *, visible: int | None = None) -> float:
    """How many cores this process may use: the tightest ``cpu.max`` quota on
    its cgroup and its ancestors, else (and never above) the cores it may be
    scheduled on."""
    cores = float(_visible_cpus() if visible is None else visible)
    v2, _v1 = _own_groups(roots)
    quotas = [
        quota
        for directory in _lineage(roots.cgroup, v2)
        if (quota := _cpu_quota(directory / "cpu.max")) is not None
    ]
    return min([cores, *quotas])


__all__ = [
    "DEFAULT_ROOTS",
    "HostRoots",
    "VolumeDevice",
    "cgroup_memory_limit_bytes",
    "disk_bytes",
    "effective_cpus",
    "effective_memory_bytes",
    "host_memory_bytes",
    "host_memory_in_use",
    "volume_device",
]
