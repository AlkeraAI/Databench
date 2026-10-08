"""Where a workspace stands: its move, its file sync and its chats' turns.

A workspace read "awake" and "Live" off its chats' stored bindings while the
box holding it could not sync a byte, and showed the target machine's wake
states during a move with nothing saying it was moving. So the judgment reads
the move first, then what the box holding the workspace's folder last proved
(its lease beat), then the turns its chats are running, and only then where
it was placed.

Pure: every fact arrives in :class:`WorkspaceEvidence` and the clock is a
parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from alkera_core.status.contract import Generic, ReasonDef, StateDef, StatusFact, register

SUBJECT: Final = "workspace"

_ANSWERING: Final[frozenset[str]] = frozenset({"ready", "draining", "restarting"})
_NOT_COMING_BACK_ALONE: Final[frozenset[str]] = frozenset({"none"})

#: The reason a move reads with, by the state its row is in.
_MOVE_REASON: Final[dict[str, str]] = {
    "requested": "saving_chats",
    "draining": "saving_chats",
    "switching": "starting_target",
    "waking": "waking_on_target",
}

WORKSPACE_STATUS = register(
    SUBJECT,
    {
        "moving": StateDef(
            "Moving",
            "info",
            "Moving to {target}.",
            {
                "saving_chats": ReasonDef("Moving to {target}. Its chats are being saved first."),
                "starting_target": ReasonDef("Moving to {target}, which is starting."),
                "waking_on_target": ReasonDef("Moving to {target}. The workspace is waking there."),
            },
        ),
        "sync_paused": StateDef(
            "Sync paused",
            "warning",
            "{machine} has stopped syncing this workspace's files.",
            {
                "machine_unreachable": ReasonDef(
                    "{machine} isn't responding. The files here are the last copy it saved."
                ),
                "worker_silent": ReasonDef(
                    "{machine} has stopped syncing this workspace's files. "
                    "The files here are the last copy it saved."
                ),
            },
        ),
        "working": StateDef("Working", "info", "An agent is working in this workspace."),
        "stalled": StateDef(
            "Stalled",
            "warning",
            "An agent here has stopped reporting progress.",
            {"turn_silent": ReasonDef("An agent here has stopped reporting progress.")},
        ),
        "waking": StateDef(
            "Waking",
            "info",
            "Waking the workspace.",
            {
                "machine_starting": ReasonDef("{machine} is starting.", label="Starting"),
            },
        ),
        "awake": StateDef("Awake", "success", "Awake on {machine}."),
        "unavailable": StateDef(
            "Can't run",
            "danger",
            "This workspace can't run right now.",
            {
                "machine_unreachable": ReasonDef("{machine} isn't responding."),
                "machine_released": ReasonDef(
                    "The machine this workspace ran on is no longer active. "
                    "A new machine will be used."
                ),
            },
        ),
        "asleep": StateDef("Asleep", "muted", "Asleep. A message wakes it."),
    },
)


@dataclass(frozen=True, slots=True)
class WorkspaceBounds:
    #: A working turn's stamp older than this is a turn nobody is reporting on.
    turn_silence: timedelta
    #: A held folder whose box has not beaten for this long is not syncing.
    sync_pause: timedelta


@dataclass(frozen=True, slots=True)
class WorkspaceEvidence:
    """What the server knows about one workspace."""

    chat_count: int = 0
    #: The state of the machine the workspace is on (``compute.machines``
    #: vocabulary), or ``""`` when its chats name no one machine.
    machine_state: str = ""
    machine_name: str = ""
    #: Whether the box, or any chat's box, last said it holds a session here.
    awake: bool = False
    wake_requested_at: datetime | None = None
    #: The stamp of each chat here whose document says a turn is running.
    working_stamps: tuple[datetime | None, ...] = ()
    #: The workspace's unfinished move, if any.
    move_state: str | None = None
    move_target: str = ""
    move_since: datetime | None = None
    #: Whether a machine holds a live lease on the workspace's folder, when
    #: that lease last beat, and the name of the machine holding it.
    lease_held: bool = False
    lease_beat_at: datetime | None = None
    lease_machine_name: str = ""


def workspace_status(
    evidence: WorkspaceEvidence, *, now: datetime, bounds: WorkspaceBounds
) -> StatusFact | None:
    """The workspace's status, or ``None`` for one with no chat, no lease and
    no move: nothing has happened in it yet.

    First match wins: an unfinished move; a box that holds the folder and has
    stopped proving it syncs; a running turn (stalled once every running
    turn's stamp has gone quiet); a wake; a session a box holds; asleep.
    """
    say = WORKSPACE_STATUS.fact
    machine = evidence.machine_name or Generic("the machine")
    if evidence.move_state in _MOVE_REASON:
        return say(
            "moving",
            reason=_MOVE_REASON[evidence.move_state],
            since=evidence.move_since,
            target=evidence.move_target or Generic("another machine"),
        )

    unreachable = evidence.machine_state == "unreachable"
    if evidence.lease_held:
        holder = evidence.lease_machine_name or machine
        if unreachable:
            return say(
                "sync_paused",
                reason="machine_unreachable",
                since=evidence.lease_beat_at,
                machine=holder,
            )
        if evidence.lease_beat_at is not None and now - evidence.lease_beat_at >= bounds.sync_pause:
            return say(
                "sync_paused",
                reason="worker_silent",
                since=evidence.lease_beat_at,
                machine=holder,
            )
    elif unreachable and evidence.chat_count:
        return say("unavailable", reason="machine_unreachable", machine=machine)

    if evidence.working_stamps and evidence.machine_state in _ANSWERING:
        quiet = [
            stamp
            for stamp in evidence.working_stamps
            if stamp is not None and now - stamp >= bounds.turn_silence
        ]
        if len(quiet) == len(evidence.working_stamps):
            return say("stalled", reason="turn_silent", since=max(quiet))
        fresh = [
            stamp for stamp in evidence.working_stamps if stamp is not None and stamp not in quiet
        ]
        return say("working", recheck_at=max(fresh) + bounds.turn_silence if fresh else None)

    if evidence.wake_requested_at is not None or evidence.working_stamps:
        since = evidence.wake_requested_at
        if evidence.machine_state not in _ANSWERING:
            if evidence.machine_state in _NOT_COMING_BACK_ALONE and evidence.chat_count:
                return say("unavailable", reason="machine_released")
            return say("waking", reason="machine_starting", since=since, machine=machine)
        return say("waking", since=since)

    if evidence.awake and evidence.machine_state in _ANSWERING:
        beat_due = (
            evidence.lease_beat_at + bounds.sync_pause
            if evidence.lease_held and evidence.lease_beat_at is not None
            else None
        )
        return say("awake", recheck_at=beat_due, machine=machine)
    if not evidence.chat_count and not evidence.lease_held:
        return None
    return say("asleep")


__all__ = [
    "SUBJECT",
    "WORKSPACE_STATUS",
    "WorkspaceBounds",
    "WorkspaceEvidence",
    "workspace_status",
]
