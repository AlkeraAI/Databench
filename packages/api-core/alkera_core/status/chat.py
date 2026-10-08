"""Where a chat stands, from what its worker reports and not only from where
it was placed.

A machine row with a fresh heartbeat proves the box's root process reaches the
API. It does not prove the chat's worker is making progress: a chat read
"ready" for minutes while nothing ran, and "Working…" long after its machine
was stopped. So the judgment here reads progress evidence first: the stamp the
worker renews every few seconds while a turn runs, how long a sent message has
waited for a turn to start, and why the last turn ended. Placement (the
machine's state, whether the box holds the chat's session) decides only what
progress cannot.

Pure: every fact arrives in :class:`ChatEvidence` and the clock is a parameter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from alkera_core.chat_refusals import ChatRefusalKind, refusal_kind_of
from alkera_core.status.contract import Generic, ReasonDef, StateDef, StatusFact, register

SUBJECT: Final = "chat"

#: The machine statuses under which the bound box is up and answering.
_ANSWERING: Final[frozenset[str]] = frozenset({"ready", "draining"})

CHAT_STATUS = register(
    SUBJECT,
    {
        "working": StateDef("Working", "info", "The agent is working."),
        "stalled": StateDef(
            "Stalled",
            "warning",
            "The agent has stopped reporting progress.",
            {
                "turn_silent": ReasonDef("The agent has stopped reporting progress."),
                "turn_not_started": ReasonDef(
                    "{machine} has the message and hasn't started on it."
                ),
                "wake_overdue": ReasonDef("{machine} hasn't picked this chat up."),
            },
        ),
        "awake": StateDef(
            "Awake",
            "success",
            "Ready for a message.",
            {
                "machine_draining": ReasonDef(
                    "{machine} is being replaced. This chat continues on the next machine."
                ),
                "machine_restarting": ReasonDef(
                    "{machine} is restarting and picks this chat up again when it is back."
                ),
            },
        ),
        "queued": StateDef("Queued", "neutral", "Queued until a slot on its machine frees up."),
        "waking": StateDef(
            "Waking",
            "info",
            "Waking the chat.",
            {
                "machine_starting": ReasonDef(
                    "{machine} is starting. Your message is sent as soon as it is ready.",
                    label="Starting",
                ),
            },
        ),
        "starting": StateDef(
            "Starting",
            "info",
            "Starting the chat.",
            {
                "machine_starting": ReasonDef(
                    "{machine} is starting. Your message is sent as soon as it is ready."
                ),
            },
        ),
        "asleep": StateDef("Asleep", "muted", "Asleep. A message wakes it."),
        "stopped": StateDef(
            "Stopped",
            "warning",
            "The last turn was stopped before it finished.",
            {
                "credits_exhausted": ReasonDef(
                    "Stopped because the organization ran out of credit."
                ),
                "machine_stopped": ReasonDef("Stopped because {machine} was stopped."),
                "box_asleep": ReasonDef("Stopped because {machine} was put to sleep."),
                "box_lost": ReasonDef("Stopped because {machine} stopped responding."),
                "access_removed": ReasonDef("Stopped because its owner no longer has access."),
                "moved": ReasonDef("Stopped to move the workspace to another machine."),
                "turn_lost": ReasonDef("Stopped because the agent stopped reporting progress."),
                "not_taken": ReasonDef("No machine picked this up. Send it again to retry."),
                "no_slot": ReasonDef("No slot freed up on its machine. Send it again to retry."),
                "stopped_by_person": ReasonDef("Stopped."),
                "refused": ReasonDef("Stopped because the machine couldn't run this chat."),
            },
        ),
        # The box has not run the chat and tries again on its own: a wait,
        # not a verdict, so it is not said as one.
        "waiting": StateDef(
            "Waiting",
            "info",
            "Waiting for the machine.",
            {
                "not_yet": ReasonDef("The chat continues as soon as the machine can run it."),
                "workspace_elsewhere": ReasonDef("Its workspace is still open on another machine."),
                "files_unreachable": ReasonDef(
                    "The machine serving it can't reach the chat's files right now."
                ),
                "not_opened": ReasonDef(
                    "The machine could not open it yet and tries again on its own."
                ),
                "not_started": ReasonDef(
                    "The machine could not start it and tries again on its own."
                ),
                "not_resumed": ReasonDef(
                    "The machine could not resume it and tries again on its own."
                ),
            },
        ),
        "unavailable": StateDef(
            "Can't run",
            "danger",
            "This chat can't run right now.",
            {
                "no_machine": ReasonDef("No machine can serve your organization right now."),
                "machine_unreachable": ReasonDef("{machine} isn't responding."),
                "machine_released": ReasonDef(
                    "The machine this chat ran on is no longer active. A new machine will be used."
                ),
                "refused": ReasonDef("The machine serving it will pick it up again once it can."),
                "refused_folder_gone": ReasonDef(
                    "The chat's folder is no longer in the drive. "
                    "Its files were saved to your drive."
                ),
                "refused_save": ReasonDef("The chat's files could not be saved to its folder."),
                "refused_moving": ReasonDef("It is moving to another machine."),
                "workspace_held_elsewhere": ReasonDef(
                    "This workspace is open on {holder}. Move it to run it here."
                ),
            },
        ),
    },
)

#: How each kind of refusal (``alkera_core.chat_refusals``) reads: its state
#: and reason. The box's own sentence is written for whoever runs the box and
#: can name the platform's hosts, so it is never read: the box names the kind
#: beside it. A wait reads as ``waiting``, a verdict as ``unavailable``. Total
#: over the kinds (a test holds it), so a new kind gets its words here.
REFUSAL_READS: Final[Mapping[ChatRefusalKind, tuple[str, str]]] = {
    "workspace_elsewhere": ("waiting", "workspace_elsewhere"),
    "files_unreachable": ("waiting", "files_unreachable"),
    "transcript_unopened": ("waiting", "not_opened"),
    "start_failed": ("waiting", "not_started"),
    "resume_failed": ("waiting", "not_resumed"),
    "moving": ("unavailable", "refused_moving"),
    "not_allowed": ("unavailable", "refused"),
    "sandbox_refused": ("unavailable", "refused"),
    "workspace_unservable": ("unavailable", "refused"),
    "folder_gone": ("unavailable", "refused_folder_gone"),
    "files_unsaved": ("unavailable", "refused_save"),
}


def refusal_read(kind: str | None) -> tuple[str, str]:
    """The state and reason a refusal of ``kind`` reads as. None, or a kind
    this server does not know, is a wait with no reason: reading it as a
    verdict would say a chat can't run that may run in a moment."""
    known = refusal_kind_of(kind)
    return ("waiting", "not_yet") if known is None else REFUSAL_READS[known]


@dataclass(frozen=True, slots=True)
class ChatBounds:
    """How long each wait may run before the status says it has stalled."""

    #: A working turn's stamp older than this is a turn nobody is reporting on.
    turn_silence: timedelta
    #: A message, or a wake, a machine that answers has not acted on for this long.
    turn_start: timedelta


@dataclass(frozen=True, slots=True)
class ChatEvidence:
    """What the server knows about one chat."""

    #: The machine's state in the chat's vocabulary
    #: (``placement.chat_machine_status``).
    machine_status: str
    #: Whether the bound machine's own row is up and heard from, whatever it
    #: last said about this chat.
    machine_answers: bool = False
    #: Whether the bound machine is restarting in place (the chat's own word
    #: for it is the drain it resembles).
    machine_restarting: bool = False
    #: The bound machine's name, when it has one the reader may see.
    machine_name: str = ""
    #: The kind of the box's refusal (``alkera_core.chat_refusals``).
    refusal_kind: str | None = None
    #: Whether the box last said it holds the chat's session.
    session_held: bool = False
    wake_requested_at: datetime | None = None
    slot_wait_at: datetime | None = None
    #: Whether the chat's document says a turn is running, and when the
    #: worker last renewed that word.
    turn_working: bool = False
    turn_stamped_at: datetime | None = None
    #: When the last message was sent, and whether nothing has answered it.
    prompt_at: datetime | None = None
    prompt_owed: bool = False
    #: Why and when the server last ended a turn out from under its box.
    turn_end_reason: str | None = None
    turn_end_at: datetime | None = None
    #: Whether anything was ever written to the chat.
    has_run: bool = False
    #: Whether a box has ever held the chat's session. A chat no box has held
    #: has nothing to wake: its first wait reads "starting".
    ever_held: bool = True


def _aged(moment: datetime | None, now: datetime, bound: timedelta) -> bool:
    return moment is not None and now - moment >= bound


def _due(moment: datetime | None, bound: timedelta) -> datetime | None:
    return None if moment is None else moment + bound


def chat_status(evidence: ChatEvidence, *, now: datetime, bounds: ChatBounds) -> StatusFact | None:
    """The chat's status, or ``None`` for a chat that has never run and owes
    nothing: nothing has happened to it that a word would describe.

    First match wins: a refusal the box named; a machine that cannot serve;
    a turn the server ended with nothing sent since; a running turn (stalled
    once its stamp goes quiet); a message or a wake waiting on its machine
    (stalled once a machine that answers has sat on it); then whether the box
    holds the session.
    """
    say = CHAT_STATUS.fact
    machine = evidence.machine_name or Generic("the machine")
    status = evidence.machine_status
    if status == "refused":
        state, reason = refusal_read(evidence.refusal_kind)
        return say(state, reason=reason)
    if status == "stranded":
        return say("unavailable", reason="machine_released")
    if status == "unreachable":
        return say("unavailable", reason="machine_unreachable", machine=machine)

    ended = evidence.turn_end_reason if evidence.turn_end_at is not None else None
    sent_since_end = (
        ended is not None
        and evidence.prompt_at is not None
        and evidence.turn_end_at is not None
        and evidence.prompt_at > evidence.turn_end_at
    )
    if ended is not None and not sent_since_end and not evidence.turn_working:
        definition = CHAT_STATUS.states["stopped"]
        reason = ended if ended in definition.reasons else ""
        return say("stopped", reason=reason, since=evidence.turn_end_at, machine=machine)

    holds = evidence.session_held and status in _ANSWERING
    if evidence.turn_working and holds:
        if _aged(evidence.turn_stamped_at, now, bounds.turn_silence):
            return say("stalled", reason="turn_silent", since=evidence.turn_stamped_at)
        return say("working", recheck_at=_due(evidence.turn_stamped_at, bounds.turn_silence))

    # A prompt the server already ended the turn of is not owed again.
    owed = (evidence.prompt_owed and (ended is None or sent_since_end)) or evidence.turn_working
    waiting_since = evidence.prompt_at if owed else evidence.wake_requested_at
    # A chat no box has ever held is being started, not woken.
    waiting = "waking" if evidence.ever_held else "starting"
    if owed or evidence.wake_requested_at is not None:
        if status == "none":
            # Not placed yet: the message places it. Which machine that will
            # be, and whether one can, is the placement's to say
            # (:func:`placement_status`).
            return say(waiting, since=waiting_since)
        if not evidence.machine_answers:
            return say(waiting, reason="machine_starting", since=waiting_since, machine=machine)
        if evidence.slot_wait_at is not None:
            return say("queued", since=evidence.slot_wait_at)
        if _aged(waiting_since, now, bounds.turn_start):
            reason = "turn_not_started" if owed else "wake_overdue"
            return say("stalled", reason=reason, since=waiting_since, machine=machine)
        return say(waiting, since=waiting_since, recheck_at=_due(waiting_since, bounds.turn_start))

    if status == "starting":
        return say("waking", reason="machine_starting", machine=machine)
    if holds:
        if evidence.machine_restarting:
            return say("awake", reason="machine_restarting", machine=machine)
        if status == "draining":
            return say("awake", reason="machine_draining", machine=machine)
        return say("awake")
    if not evidence.has_run:
        return None
    return say("asleep")


def placement_status(machine_status: str, *, machine_name: str = "") -> StatusFact | None:
    """What stands between a chat that is not on a machine yet (a new chat, or
    one made while nothing was up) and its first turn: the state of the machine
    its next message would be placed on. ``None`` when that machine is ready,
    or a shared one will take it."""
    say = CHAT_STATUS.fact
    machine = machine_name or Generic("the machine")
    if machine_status == "none":
        return say("unavailable", reason="no_machine")
    if machine_status == "unreachable":
        return say("unavailable", reason="machine_unreachable", machine=machine)
    if machine_status in ("starting", "asleep"):
        return say("waking", reason="machine_starting", machine=machine)
    return None


__all__ = [
    "CHAT_STATUS",
    "SUBJECT",
    "ChatBounds",
    "ChatEvidence",
    "chat_status",
    "placement_status",
    "refusal_read",
]
