"""What the box reads after a chat's agent server exits: whether the kernel
killed it for memory, and how that kill is named to the reader.

Pure readers over the chat's cgroup files and the crash detail they produce;
:mod:`alkera_cli.harness.sandbox` composes the launch that names the files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from alkera_cli.harness.sandbox_layout import chat_cgroup_dir

if TYPE_CHECKING:
    from alkera_cli.harness.sandbox import SandboxSpec


def memory_events_path(spec: SandboxSpec) -> Path | None:
    """The cgroup v2 ``memory.events`` file that says whether the kernel killed
    something in the chat's cgroup for memory. Hierarchical, so the chat's
    slice (systemd) or its own group (cgroupfs) counts the container's kills
    too; ``None`` where the chat has no cgroup and so no memory limit."""
    cgroup = chat_cgroup_dir(spec.cgroup, spec.chat_id, spec.cgroup_root)
    return None if cgroup is None else cgroup / "memory.events"


def memory_event(text: str, event: str) -> int:
    """The ``event`` counter (``oom_kill``, ``oom_group_kill``, ...) in a
    ``memory.events`` file's text; ``0`` for a file that names none or is
    malformed (never a refusal: this is read after the fact, to name the
    reason, not to decide anything)."""
    for line in text.splitlines():
        name, _, value = line.strip().partition(" ")
        if name == event:
            try:
                return max(int(value.strip()), 0)
            except ValueError:
                return 0
    return 0


def oom_kills(text: str) -> int:
    """The ``oom_kill`` counter in a ``memory.events`` file's text."""
    return memory_event(text, "oom_kill")


def read_oom_kills(path: Path) -> int:
    """:func:`oom_kills` of the file at ``path``; ``0`` when the file is gone
    (the cgroup was already removed) or unreadable."""
    try:
        return oom_kills(path.read_text())
    except OSError:
        return 0


#: Exit statuses of an agent process the kernel ended with ``SIGKILL``: the
#: ``128 + 9`` a container runtime reports for its sandbox, and asyncio's own
#: reading of a child that took the signal itself.
SIGKILL_EXIT_STATUSES = frozenset({137, -9})


def read_oom_kills_under(path: Path) -> int | None:
    """The chat's ``oom_kill`` count as the cgroup tree says it: the highest of
    the file at ``path`` and the same file one level below it (the scope the
    container runtime opens under the chat's slice, where the kill lands
    first). ``None`` when none of them can be read: the cgroup is already
    gone, so the tree says nothing either way, which is not the same as
    saying zero."""
    counts: list[int] = []
    try:
        counts.append(oom_kills(path.read_text()))
    except OSError:
        pass
    try:
        children = sorted(path.parent.iterdir())
    except OSError:
        children = []
    for child in children:
        nested = child / path.name
        try:
            counts.append(oom_kills(nested.read_text()))
        except OSError:
            continue
    return max(counts) if counts else None


#: How the adapter's crash detail names a kill for memory, as data the cloud
#: side turns into the reader's sentence. The figure is the chat's limit in
#: MiB; ``memory_limit_exceeded`` reads it back.
MEMORY_LIMIT_DETAIL = "the chat's {memory_mb} MB memory limit was exceeded"
_MEMORY_LIMIT_DETAIL_RE = re.compile(r"the chat's (\d+) MB memory limit was exceeded")


def memory_limit_detail(memory_mb: int) -> str:
    return MEMORY_LIMIT_DETAIL.format(memory_mb=memory_mb)


def memory_limit_exceeded(detail: str) -> int | None:
    """The memory limit (MiB) a crash ``detail`` says was exceeded, or ``None``
    when the detail names no such kill."""
    match = _MEMORY_LIMIT_DETAIL_RE.search(detail)
    return int(match.group(1)) if match else None


__all__ = [
    "MEMORY_LIMIT_DETAIL",
    "SIGKILL_EXIT_STATUSES",
    "memory_event",
    "memory_events_path",
    "memory_limit_detail",
    "memory_limit_exceeded",
    "oom_kills",
    "read_oom_kills",
    "read_oom_kills_under",
]
