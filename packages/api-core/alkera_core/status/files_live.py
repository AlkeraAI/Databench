"""Whether the files under a leased folder are the machine's live work or the
last copy it saved.

A lease means a machine holds a folder and writes it back, which is several
different things to a reader: the machine may be writing as it works (live),
holding the folder but saving it only from time to time, or still holding it
after it stopped answering or fell behind. Painting "Live" over the last three
is a promise the page cannot keep, and a person edits a copy the machine then
overwrites. The browser used to judge this itself from the lease's raw fields;
the judgment is the server's, from the same fields, with the machine's own
reachability added.

A folder under no lease has no status: the files are simply what Files holds.

Pure: every fact arrives in :class:`FilesLiveEvidence`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from alkera_core.status.contract import Generic, ReasonDef, StateDef, StatusFact, register

SUBJECT: Final = "files_live"

FILES_LIVE_STATUS = register(
    SUBJECT,
    {
        "live": StateDef(
            "Live",
            "success",
            "{machine} is writing these files as it works.",
            {
                "landing": ReasonDef(
                    "{machine} is writing these files as it works. "
                    "Some are still on their way to Files."
                ),
            },
        ),
        "sync_paused": StateDef(
            "Sync paused",
            "warning",
            "{machine} has stopped syncing. These are the files it last saved.",
            {
                "machine_unreachable": ReasonDef(
                    "{machine} isn't responding. These are the files it last saved."
                ),
                "worker_silent": ReasonDef(
                    "{machine} has stopped syncing. These are the files it last saved."
                ),
                "behind": ReasonDef(
                    "{machine} is behind on syncing. These are the files it last saved."
                ),
            },
        ),
        "saved_copy": StateDef(
            "Saved copy",
            "neutral",
            "{machine} holds this folder and saves it from time to time. "
            "These are the files it last saved.",
        ),
    },
)


@dataclass(frozen=True, slots=True)
class FilesLiveEvidence:
    """What the server knows about the lease over a folder."""

    #: The lease runs the live plane: the holder streams what it writes.
    live: bool
    #: The holder has beaten within a few of its heartbeats.
    beating: bool
    #: The holder beats but has not pushed for over two sync intervals.
    behind: bool
    #: The holder's machine has stopped answering the server.
    machine_unreachable: bool = False
    #: Files under the lease whose bytes are still on their way.
    landing: int = 0
    #: When the lease was taken and when the holder last pushed.
    since: datetime | None = None
    last_sync_at: datetime | None = None
    #: The machine's name, when the reader may see it.
    machine_name: str = ""


def files_live_status(evidence: FilesLiveEvidence) -> StatusFact:
    """The folder's status. First match wins: a lease that is not live is a
    saved copy; a machine that stopped answering, a holder gone quiet, or one
    that fell behind pauses the sync; otherwise the folder is live."""
    say = FILES_LIVE_STATUS.fact
    machine = evidence.machine_name or Generic("the machine")
    paused_since = evidence.last_sync_at or evidence.since
    if not evidence.live:
        return say("saved_copy", since=evidence.last_sync_at, machine=machine)
    if evidence.machine_unreachable:
        return say("sync_paused", reason="machine_unreachable", since=paused_since, machine=machine)
    if not evidence.beating:
        return say("sync_paused", reason="worker_silent", since=paused_since, machine=machine)
    if evidence.behind:
        return say("sync_paused", reason="behind", since=paused_since, machine=machine)
    if evidence.landing > 0:
        return say("live", reason="landing", since=evidence.since, machine=machine)
    return say("live", since=evidence.since, machine=machine)


__all__ = [
    "FILES_LIVE_STATUS",
    "SUBJECT",
    "FilesLiveEvidence",
    "files_live_status",
]
