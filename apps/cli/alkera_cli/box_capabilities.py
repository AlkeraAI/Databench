"""What this build of the box can report, and which part of it says so.

The vocabulary is :class:`~alkera_core.compute.box_contract.BoxCapability`,
shared with the backend. :data:`CLAIMS` names, for every member, the part of
the box that claims it, so the single daemon's heartbeat and the supervisor's
are both built from it and cannot drift apart:

* ``MIRROR``: the chat mirror answers it, in whichever process runs the
  mirror (the single daemon, or each org worker under the supervisor), so
  both heartbeats carry it;
* ``SUPERVISOR``: only the supervisor does it;
* ``HARDWARE``: claimed from what the machine's sample shows (the GPU
  readings in ``cloud.machine_resources``);
* ``PROBE``: claimed only from what a startup probe found, by the probe's
  report (``IsolationReport.capabilities``), never from a list.

A release build's manifest publishes every claimable member.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from alkera_core.compute.box_contract import BoxCapability


class ClaimedBy(StrEnum):
    """The part of the box that says a capability."""

    MIRROR = "mirror"
    SUPERVISOR = "supervisor"
    HARDWARE = "hardware"
    PROBE = "probe"


#: Every capability, by what claims it. A new member of the enum needs a row;
#: a test refuses one without.
CLAIMS: Final[dict[BoxCapability, ClaimedBy]] = {
    # A shared workspace's chats run in one sandbox under one lease.
    BoxCapability.WORKSPACES: ClaimedBy.MIRROR,
    # A model switch lands on the next turn.
    BoxCapability.MODEL_SWITCH_V1: ClaimedBy.MIRROR,
    # The machine channel's ``flush`` is answered: a moving workspace waits
    # for the push.
    BoxCapability.FOLDER_FLUSH_V1: ClaimedBy.MIRROR,
    BoxCapability.ORG_WORKERS: ClaimedBy.SUPERVISOR,
    # The supervisor grows the volume's filesystem into a volume grown under
    # it, and each org's share with it.
    BoxCapability.DISK_GROW_ONLINE: ClaimedBy.SUPERVISOR,
    BoxCapability.GPU: ClaimedBy.HARDWARE,
    BoxCapability.GPU_PASSTHROUGH: ClaimedBy.HARDWARE,
    BoxCapability.ORG_ISOLATION: ClaimedBy.PROBE,
}


def claimed_by(*parts: ClaimedBy) -> tuple[BoxCapability, ...]:
    """The capabilities ``parts`` claim, in the enum's order."""
    return tuple(c for c in BoxCapability if CLAIMS.get(c) in parts)


#: What every heartbeat that serves chats carries.
MIRROR_CAPABILITIES: Final = claimed_by(ClaimedBy.MIRROR)
#: The single daemon's own list; its pulse adds the hardware claims.
DAEMON_CAPABILITIES: Final = MIRROR_CAPABILITIES
#: The supervisor's own list: its workers run the mirror, and it runs them.
SUPERVISOR_CAPABILITIES: Final = claimed_by(ClaimedBy.MIRROR, ClaimedBy.SUPERVISOR)

__all__ = [
    "CLAIMS",
    "DAEMON_CAPABILITIES",
    "MIRROR_CAPABILITIES",
    "SUPERVISOR_CAPABILITIES",
    "ClaimedBy",
    "claimed_by",
]
