"""A workspace's sandbox, as the box measures it and takes it down.

The sessions that are members of one workspace run under its tree identity
(:mod:`alkera_cli.harness.sandbox_scope`). This module answers the two questions
the box asks about all of them together: how much memory the workspace holds
(what it reports to the backend), and what to take down when the workspace is
put away.

In the ``per_chat`` topology each member's container goes with its agent
server, so putting the workspace away leaves nothing to take down and its memory
is the sum of its members' cgroups. In the ``shared`` topology the workspace's
container outlives any one member and is taken down here.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from alkera_core.process import run_captured

from alkera_cli.harness.sandbox_layout import (
    chat_cgroup_path,
    chat_container,
    chat_slice_cgroup_path,
)
from alkera_cli.harness.sandbox_scope import members_of, scope_of

logger = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")

#: What takes a shared container down, registered by the module that runs
#: one; a box that runs none has nothing to take down.
_TEARDOWNS: list[Callable[[str], Awaitable[None]]] = []


def register_teardown(teardown: Callable[[str], Awaitable[None]]) -> None:
    """Add what takes a workspace's shared sandbox down when it is put away."""
    if teardown not in _TEARDOWNS:
        _TEARDOWNS.append(teardown)


def _working_set(group: Path) -> int | None:
    """Bytes the cgroup holds that the kernel would not simply drop: its
    ``memory.current`` less its inactive file cache."""
    try:
        current = int((group / "memory.current").read_text().strip())
    except (OSError, ValueError):
        return None
    inactive = 0
    try:
        for line in (group / "memory.stat").read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == "inactive_file":
                inactive = int(value.strip())
                break
    except (OSError, ValueError):
        inactive = 0
    return max(0, current - inactive)


def _group_of(container: str, cgroup_root: Path) -> Path | None:
    """The cgroup a container's processes are counted in, whichever driver
    made it."""
    for candidate in (
        chat_slice_cgroup_path(container, cgroup_root),
        chat_cgroup_path(container, cgroup_root),
    ):
        if candidate.is_dir():
            return candidate
    return None


#: How long one ``runsc events -stats`` may take before the reading is skipped.
RUNSC_STATS_TIMEOUT_SECONDS = 3.0


def runsc_usage_bytes(
    container: str,
    *,
    runsc: str = "runsc",
    root: str = "/run/alkera-runsc",
    run: Callable[..., subprocess.CompletedProcess[str]] = run_captured,
) -> int | None:
    """What gVisor itself says the container's sandbox holds, in bytes: the
    reading on a host whose cgroups the containers are not counted in (a
    local box in Docker). ``None`` when runsc is absent, slow or says
    nothing."""
    try:
        done = run(
            [runsc, f"--root={root}", "events", "-stats", chat_container(container)],
            capture_output=True,
            text=True,
            timeout=RUNSC_STATS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        usage = json.loads(done.stdout)["data"]["memory"]["usage"]["usage"]
    except (ValueError, KeyError, TypeError):
        return None
    return usage if isinstance(usage, int) and usage >= 0 else None


def workspace_memory_mb(
    key: str,
    *,
    cgroup_root: Path = CGROUP_ROOT,
    runsc_usage: Callable[[str], int | None] = runsc_usage_bytes,
) -> int | None:
    """MiB the workspace's sandbox holds right now: its shared container's
    cgroup, or the sum of its members' own, read from gVisor where the cgroup
    counts nothing; ``None`` when nothing could be read."""
    containers = {scope_of(session).container for session in members_of(key)} or {key}
    total = 0
    read_any = False
    for container in sorted(containers):
        group = _group_of(container, cgroup_root)
        used = _working_set(group) if group is not None else None
        if not used:
            used = runsc_usage(container)
        if used is None:
            continue
        total += used
        read_any = True
    return total // (1024 * 1024) if read_any else None


async def release_workspace_sandbox(key: str) -> None:
    """Take down whatever of the workspace's sandbox outlives its members."""
    for teardown in list(_TEARDOWNS):
        try:
            await teardown(key)
        except Exception:
            logger.exception("workspace %s: a sandbox teardown failed", key)


__all__ = ["register_teardown", "release_workspace_sandbox", "workspace_memory_mb"]
