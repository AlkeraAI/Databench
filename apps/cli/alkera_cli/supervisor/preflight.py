"""What the host must already give the supervisor before it starts a worker.

Each org worker's user namespace maps the org's slot of host ids through
``newuidmap`` / ``newgidmap``, which admit only ranges ``/etc/subuid`` and
``/etc/subgid`` grant root. The box's prerequisites script grants the whole
block once; a box whose staged prerequisites predate that step has no grant,
and every worker it started would die at its first map with no word for why.
So the supervisor checks the grant once, at startup, and names what is
missing instead of starting any worker.

The other mechanisms a worker needs (a delegated cgroup, the mount and
network namespaces) are found out by trying them, in the isolation probe.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

from alkera_cli.supervisor.slots import MAX_SLOTS, ORG_UID_BASE, ORG_UID_SPAN

#: The files ``newuidmap`` and ``newgidmap`` read root's grants from.
ID_GRANT_FILES: Final = (Path("/etc/subuid"), Path("/etc/subgid"))
#: The host ids every slot this box can hold maps, end exclusive.
ORG_ID_BLOCK: Final = (ORG_UID_BASE, ORG_UID_BASE + MAX_SLOTS * ORG_UID_SPAN)
#: What an operator runs to put the grants back.
REMEDY: Final = "re-run sandbox-prereqs.sh from the build the box runs"

Reader = Callable[[Path], str]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _covers(text: str, low: int, high: int) -> bool:
    """Whether root's entries in ``text`` together grant every id in ``[low, high)``."""
    spans: list[tuple[int, int]] = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3 or parts[0] not in ("root", "0"):
            continue
        if not (parts[1].isdigit() and parts[2].isdigit()):
            continue
        start = int(parts[1])
        spans.append((start, start + int(parts[2])))
    reached = low
    for start, end in sorted(spans):
        if start > reached:
            break
        reached = max(reached, end)
        if reached >= high:
            return True
    return reached >= high


def missing_prerequisites(read: Reader = _read) -> list[str]:
    """One sentence for each prerequisite the host lacks; empty when it has them all."""
    low, high = ORG_ID_BLOCK
    return [
        f"{path} grants root no ids {low} to {high - 1}, which the org workers' "
        f"user namespaces map; {REMEDY}"
        for path in ID_GRANT_FILES
        if not _covers(read(path), low, high)
    ]


__all__ = ["ID_GRANT_FILES", "ORG_ID_BLOCK", "REMEDY", "missing_prerequisites"]
