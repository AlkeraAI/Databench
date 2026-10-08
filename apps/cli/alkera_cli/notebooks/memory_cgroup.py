"""What the notebook memory guard reads on a box: the kernel sandbox's host cgroup.

The engine's guard polls a ``MemorySource`` every 100 ms and acts before the
limit (the sandbox thrashes for seconds at ``memory.max`` and there is no
second chance there). On a box the source is the kernel sandbox's own cgroup,
read from the host side: ``memory.current`` and ``memory.max``. Never
``/proc/meminfo`` inside the sandbox: a reader in the sandbox is scheduled by
the same sentry a burst saturates, and lost the race in the spike.

Out-of-memory is told from ``memory.events``: the host's group kill of the
sandbox shows as ``oom_kill`` and ``oom_group_kill`` there. The count this
source reports only ever grows, across the sandbox's restarts: each start
makes the cgroup afresh at the same path, so the sandbox reads a start's
final count before its cgroup is removed and carries it
(:meth:`~alkera_cli.notebooks.kernel_sandbox.KernelSandbox.oom_events`), and
the engine detects an OOM as an increment between two readings without
knowing the sandbox restarted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from alkera_cli.harness.sandbox_memory import memory_event

OOM_EVENTS = ("oom_kill", "oom_group_kill")


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def read_oom_events(cgroup: Path) -> int | None:
    """``oom_kill`` plus ``oom_group_kill`` in ``cgroup``'s ``memory.events``;
    ``None`` when the file cannot be read (the cgroup is gone)."""
    try:
        text = (cgroup / "memory.events").read_text()
    except OSError:
        return None
    return sum(memory_event(text, name) for name in OOM_EVENTS)


class CgroupView(Protocol):
    """What the source reads through: the kernel sandbox."""

    def cgroup_dir(self) -> Path | None:
        """The sandbox's host cgroup while it runs, ``None`` otherwise."""
        ...

    def oom_events(self) -> int:
        """Every OOM kill the sandbox's cgroups recorded, over all its starts."""
        ...


class CgroupSource:
    """``MemorySource`` over the kernel sandbox's host cgroup.

    ``limit`` is the memory limit the sandbox is started with: the answer
    while no cgroup is there to read, and where the cgroup says ``max``
    (which a kernel sandbox never has)."""

    def __init__(self, view: CgroupView, *, limit: int) -> None:
        if limit <= 0:
            raise ValueError("the kernel sandbox needs a positive memory limit")
        self._view = view
        self._limit = limit

    def usage_bytes(self) -> int:
        """``memory.current``; zero while the sandbox is not running."""
        where = self._view.cgroup_dir()
        if where is None:
            return 0
        current = _read_int(where / "memory.current")
        return current if current is not None and current > 0 else 0

    def limit_bytes(self) -> int:
        """``memory.max``, or the configured limit where it cannot be read."""
        where = self._view.cgroup_dir()
        if where is not None:
            found = _read_int(where / "memory.max")
            if found is not None and found > 0:
                return found
        return self._limit

    def oom_events(self) -> int:
        """Every ``oom_kill`` and ``oom_group_kill`` the kernel sandbox's
        cgroups recorded, over every start; only ever grows."""
        return self._view.oom_events()


__all__ = ["OOM_EVENTS", "CgroupSource", "CgroupView", "read_oom_events"]
