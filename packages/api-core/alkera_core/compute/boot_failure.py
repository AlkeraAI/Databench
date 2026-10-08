"""What a boot that never registered records, and how a card reads it back.

The node reconcile ends a machine whose node never claimed it within the boot
timeout (:mod:`alkera_core.compute.node_reconcile`). Its provider may know why
from the node's own log (an attached host's ``alkera-node`` failing to reach
the API, say); that error is recorded on the allocation with the edge, and the
machine card shows it (:func:`alkera_core.compute.org_machines.machine_card`).
This module is the one owner of that record's format.
"""

from __future__ import annotations

from typing import Final

#: The allocation's ``error`` when its node never claimed it.
NEVER_CLAIMED: Final = "the daemon never claimed the machine"

_LAST_ERROR: Final = "; last error: "

#: The most of a node's error that is kept.
NODE_ERROR_MAX: Final = 500


def never_claimed_error(node_error: str) -> str:
    """The ``error`` a boot timeout records, with the node's last logged error
    when its provider read one."""
    detail = " ".join(node_error.split())[:NODE_ERROR_MAX]
    return f"{NEVER_CLAIMED}{_LAST_ERROR}{detail}" if detail else NEVER_CLAIMED


def node_error_of(error: str | None) -> str:
    """The node's own error recorded by :func:`never_claimed_error`, or empty."""
    text = error or ""
    if not text.startswith(NEVER_CLAIMED + _LAST_ERROR):
        return ""
    return text.removeprefix(NEVER_CLAIMED + _LAST_ERROR)


__all__ = ["NEVER_CLAIMED", "NODE_ERROR_MAX", "never_claimed_error", "node_error_of"]
