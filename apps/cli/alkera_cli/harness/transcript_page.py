"""A page of a chat's persisted events, read backward from a point.

The editor opens a chat on the newest page of its log and reads the pages above
it as the reader scrolls up. An event's key is its 1-based ordinal among the
events the log reader yields — the file is append-only, so the ordinal is
stable for the life of the chat.

Where the page OPENS is decided by
:func:`alkera_core.objects.transcript_page.align_boundary`, the same rule the
server's ``chat_messages`` pager drives, so a reader gets the same page
whichever side served it: the page opens on the row that started its first
turn AND on a row that opens every message the page carries, within the
``CHAT_PAGE_TURN_REACH`` / ``CHAT_PAGE_MESSAGE_REACH`` budget, and says ``cut``
when it could not get there.

Pure: a list in, a page out. The daemon's ``harness.open_chat`` (with ``tail``)
and ``harness.list_events`` both call this, so the two agree on alignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from alkera_core.config import settings
from alkera_core.objects.transcript_page import PageReach, PageRow, align_boundary
from alkera_core.schemas.chat import Event, MessageCreated, PartCreated, PromptCancelled

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    events: list[Event]
    #: The ordinal of ``events[0]``; ``None`` for an empty page.
    oldest_seq: int | None
    #: Whether an event older than ``oldest_seq`` exists.
    has_older: bool
    #: Whether the page opens mid-turn, or carries a message whose opening
    #: event it could not reach. The page below carries the rest.
    cut: bool = False


def starts_turn(event: Event) -> bool:
    """A person's message opens a turn."""
    return isinstance(event, MessageCreated) and event.role == "user"


def message_of(event: Event) -> str | None:
    """Which message this event belongs to, by the id it states.

    A ``part.created`` states it on the part it carries rather than on the
    event. A ``prompt.cancelled`` states the id of the PROMPT it cancels, which
    no ``message.created`` ever opens, so it belongs to no message here.
    """
    if isinstance(event, PromptCancelled):
        return None
    if isinstance(event, PartCreated):
        return event.part.message_id or None
    named = getattr(event, "message_id", None)
    return named if isinstance(named, str) and named else None


def page_before(events: Sequence[Event], *, before: int | None, limit: int) -> TranscriptPage:
    """The page of ``events`` (the whole log, in order) below ordinal ``before``
    (exclusive), or the newest page when ``before`` is ``None``."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    total = len(events)
    end = total if before is None else min(before - 1, total)
    if end <= 0:
        return TranscriptPage(events=[], oldest_seq=None, has_older=False)
    known = [
        PageRow(
            seq=index + 1,
            starts_turn=starts_turn(event),
            message_id=message_of(event),
            opens_message=isinstance(event, MessageCreated),
        )
        for index, event in enumerate(events[:end])
    ]
    reach = PageReach(
        limit=limit,
        turn=max(settings.chat_page_turn_reach, 1),
        message=max(settings.chat_page_message_reach, 1),
    )
    answer = align_boundary(known, page_low=max(end - limit, 0) + 1, oldest=1, reach=reach)
    start = answer.seq - 1
    return TranscriptPage(
        events=list(events[start:end]),
        oldest_seq=answer.seq,
        has_older=start > 0,
        cut=answer.cut,
    )
