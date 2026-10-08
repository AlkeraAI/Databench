"""Where a box keeps its orgs' data, and the modes that let each org reach its own.

One owner for both sides of the contract: the node bootstrap
(:mod:`alkera_core.compute.bootstrap`) makes the work root and the orgs root,
whatever umask its provider launched it under (an attached host's launch runs
under 077), and the supervisor (``alkera_cli.org_root``) makes each org's root
below them. An org's worker runs as the slot's own uid, so every directory
above its root must let any uid through (``x``) without letting it list
(``r``): 0711. A work root left 0700 strands every worker at its first path.
"""

from __future__ import annotations

from typing import Final

#: The box's work root: its data volume where it has one.
WORK_ROOT: Final = "/opt/alkera-work"
#: Root's, traversable by every org's uid, listable by none.
WORK_ROOT_MODE: Final = 0o711
#: Every org's root lives directly under this, by slot.
ORGS_DIR: Final = "orgs"
ORGS_ROOT: Final = f"{WORK_ROOT}/{ORGS_DIR}"
ORGS_ROOT_MODE: Final = 0o711
#: One org's root: its slot's base uid's alone.
ORG_ROOT_MODE: Final = 0o700


__all__ = [
    "ORGS_DIR",
    "ORGS_ROOT",
    "ORGS_ROOT_MODE",
    "ORG_ROOT_MODE",
    "WORK_ROOT",
    "WORK_ROOT_MODE",
]
