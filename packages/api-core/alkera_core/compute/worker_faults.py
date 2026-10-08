"""Why a box's org worker could not serve, in words both sides share.

The box's supervisor names each failure to start or keep an org's worker with
one :class:`WorkerFault`. The code travels three ways: on the shipped
``supervisor.worker.start_failed`` and ``supervisor.worker.crash_loop`` events
(as their ``reason``), and on the heartbeat as the box's fault when no org it
was asked to serve has a worker (``MachineHeartbeatRequest.fault``). Beside
the code goes a summary: the failure's own words, scrubbed of anything
secret-shaped and cut short (``alkera_core.compute.box_logs.scrub_text``), for
the console only.

A code this side does not know (a newer box's) reads as :attr:`WorkerFault.OTHER`.

Standard library only: the box's root process imports this.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class WorkerFault(StrEnum):
    """Why an org's worker is not serving."""

    #: The org's cgroup could not be made or delegated.
    CGROUP_REFUSED = "cgroup_refused"
    #: The worker process could not be spawned.
    SPAWN_FAILED = "spawn_failed"
    #: The worker's systemd unit never reached a main process.
    UNIT_FAILED = "unit_failed"
    #: The worker's network namespace or its link to the host could not be made.
    LINK_FAILED = "link_failed"
    #: The backend refused the org a worker credential.
    CREDENTIAL_REFUSED = "credential_refused"
    #: The box has no worker slot left for another org.
    SLOTS_EXHAUSTED = "slots_exhausted"
    #: A single-org box was asked to serve a second org and refused.
    SECOND_ORG_REFUSED = "second_org_refused"
    #: The worker started and exited soon after.
    EXITED = "exited"
    #: Anything else.
    OTHER = "other"


#: The words an org reads under "<machine> can't run chats." One sentence for
#: every cause: what the org can do is the same, and the cause is the
#: console's to show.
UNHEALTHY_MESSAGE: Final = "You are not billed for its running time until chats can start."


def fault_code(raw: object) -> WorkerFault:
    """The fault ``raw`` names, or :attr:`WorkerFault.OTHER`."""
    known = {item.value for item in WorkerFault}
    return WorkerFault(raw) if isinstance(raw, str) and raw in known else WorkerFault.OTHER


__all__ = [
    "UNHEALTHY_MESSAGE",
    "WorkerFault",
    "fault_code",
]
