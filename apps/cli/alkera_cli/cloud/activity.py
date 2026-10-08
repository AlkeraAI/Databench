"""What a chat served on a box owes right now, as one of four states.

Every decision the box makes about a chat it holds — whether a stop waits for
it, whether it may be put to sleep, what the heartbeat and the status file
count — reads this one answer rather than re-deriving it from the mirror's
flags. The states are ordered by what a hand-back would cost:

* ``WORKING`` — something a sleep would destroy is in flight: a turn the agent
  is running, a message taken off the wire but not yet handed over, a relay
  still running. It and ``RUNNING_JOB`` hold a stopping box, and no idle window
  ever runs against either.
* ``RUNNING_JOB`` — ``WORKING``, with a background shell, query or subagent
  among what is in flight. A job cannot be restarted by the next process the
  way a turn can, so even a restart in place waits for it, up to the drain
  ceiling, rather than its short restart window. The status file counts it
  among ``chats_working``, which is what a roll reads a drain's wait from.
* ``AWAITING_USER`` — the only thing in flight is a tool or permission ask
  parked on a person. The ask is durable: it stays in the transcript and the
  next open re-offers it, so a hand-back loses the live session and nothing
  else. A stopping box hands it back at once; a running box keeps it for the
  parked-ask window, measured from the last time a reader was seen.
* ``IDLE`` — nothing is owed. The idle window runs from the moment the chat
  last moved.

A mirror still starting up is a lifecycle state (``ChatMirror.state``), not an
activity: it is never swept, and the service checks that separately.

The values are what the daemon status file reports (``RUNNING_JOB`` folded into
``WORKING``), so they are wire strings: add a state, never rename one.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class ChatActivity(StrEnum):
    WORKING = "working"
    RUNNING_JOB = "running_job"
    AWAITING_USER = "awaiting_user"
    IDLE = "idle"

    @property
    def holds_a_stop(self) -> bool:
        """Whether a stopping box waits for the chat before handing it back."""
        return self in (ChatActivity.WORKING, ChatActivity.RUNNING_JOB)

    @property
    def owes_anything(self) -> bool:
        """Whether anything at all is in flight — what ``chats_busy`` counts."""
        return self is not ChatActivity.IDLE


def activity_counts(held: Iterable[ChatActivity]) -> dict[str, int]:
    """What the status file says about the chats held on a box: every
    activity by name, plus ``chats_busy`` (anything in flight), which the
    roll scripts read from daemons older than the activity counts too."""
    activities = list(held)
    counts = {f"chats_{a.value}": activities.count(a) for a in ChatActivity}
    counts["chats_working"] += counts.pop("chats_running_job")  # a job is working too
    counts["chats_busy"] = sum(1 for a in activities if a.owes_anything)
    return counts
