"""The backward page over a chat's event log — the pure function under
``harness.open_chat``'s ``tail`` and ``harness.list_events``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alkera_cli.harness.transcript_page import message_of, page_before, starts_turn
from alkera_core.config import settings
from alkera_core.schemas.chat import (
    Event,
    MessageCompleted,
    MessageCreated,
    PromptCancelled,
    ToolCall,
)

_T = datetime(2026, 5, 26, tzinfo=UTC)
SID = "s1"


def _user(index: int) -> Event:
    return MessageCreated(
        event_id=f"u{index}", time=_T, session_id=SID, message_id=f"m{index}u", role="user"
    )


def _assistant(index: int, step: int) -> Event:
    return ToolCall(
        event_id=f"t{index}.{step}",
        time=_T,
        session_id=SID,
        tool_call_id=f"tc{index}.{step}",
        message_id=f"m{index}a",
        tool_name="bash",
        input={"command": "true"},
    )


def _log(turns: list[int]) -> list[Event]:
    """One user message then ``count`` assistant events per turn."""
    events: list[Event] = []
    for index, count in enumerate(turns):
        events.append(_user(index))
        events.extend(_assistant(index, step) for step in range(count))
    return events


def _ids(events: list[Event]) -> list[str]:
    return [event.event_id for event in events]


def test_a_persons_message_starts_a_turn_and_nothing_else_does() -> None:
    assert starts_turn(_user(1)) is True
    assert starts_turn(_assistant(1, 0)) is False
    assistant_message = MessageCreated(
        event_id="a", time=_T, session_id=SID, message_id="ma", role="assistant"
    )
    assert starts_turn(assistant_message) is False
    assert (
        starts_turn(MessageCompleted(event_id="c", time=_T, session_id=SID, message_id="ma"))
        is False
    )


def test_the_newest_page_reaches_back_to_a_turn_start_within_reach() -> None:
    log = _log([2, 2, 2])  # u0 t0.0 t0.1 | u1 t1.0 t1.1 | u2 t2.0 t2.1  (ordinals 1..9)
    page = page_before(log, before=None, limit=2)
    # Two events would start at t2.0; u2 is one below, so the page opens on it.
    assert _ids(page.events) == ["u2", "t2.0", "t2.1"]
    assert (page.oldest_seq, page.has_older) == (7, True)
    assert page.cut is False, "the whole log sits inside the budget, so nothing hides"


def test_walking_backward_covers_the_log_exactly_once() -> None:
    log = _log([1, 5, 30, 2, 0, 9])
    total = len(log)
    seen: list[str] = []
    page = page_before(log, before=None, limit=7)
    guard = 0
    while True:
        seen = _ids(page.events) + seen
        assert page.oldest_seq is not None
        assert page.oldest_seq == total - len(seen) + 1
        if not page.has_older:
            break
        page = page_before(log, before=page.oldest_seq, limit=7)
        guard += 1
        assert guard < 100
    assert seen == _ids(log)


def test_a_turn_longer_than_the_whole_budget_is_cut_at_the_budget() -> None:
    """The reach is the server's, so the daemon swallows a long turn the same
    way — until the turn outruns the whole descent budget, and then the page
    stops there and says it is cut."""
    budget = 5 * (settings.chat_page_turn_reach + settings.chat_page_message_reach)
    log = _log([1, budget * 2, 1])
    page = page_before(log, before=None, limit=5)
    assert page.cut is True, "it opens inside the turn and inside its message"
    assert page.oldest_seq == len(log) - 5 + 1 - budget
    assert len(page.events) == 5 + budget, "limit plus the budget is the ceiling"
    assert page.has_older is True
    below = page_before(log, before=page.oldest_seq, limit=5)
    assert below.events[-1].event_id == log[page.oldest_seq - 2].event_id
    assert below.has_older is True


def test_a_page_opening_inside_an_answer_reaches_the_event_that_opened_it() -> None:
    """The daemon runs the same rule as the server: a page whose first events
    belong to a message opened below it drops to that opening event, and then
    on to the person's message the turn began at."""
    log: list[Event] = [_user(0)]
    log.append(
        MessageCreated(event_id="a0", time=_T, session_id=SID, message_id="m0a", role="assistant")
    )
    log.extend(_assistant(0, step) for step in range(8))
    page = page_before(log, before=None, limit=3)
    assert _ids(page.events) == _ids(log), "down to the answer's open row, then the turn"
    assert (page.oldest_seq, page.has_older, page.cut) == (1, False, False)


def test_a_prompt_cancelled_event_belongs_to_no_message() -> None:
    """Its ``message_id`` names the PROMPT it cancels, which no
    ``message.created`` ever opens."""
    cancelled = PromptCancelled(
        event_id="x", time=_T, session_id=SID, message_id="usr:web-1", reason="stopped"
    )
    assert message_of(cancelled) is None
    assert message_of(_user(0)) == "m0u"


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        pytest.param(1, ([], None, False), id="below-the-first-event"),
        pytest.param(0, ([], None, False), id="zero"),
    ],
)
def test_a_page_below_the_first_event_is_empty(before: int, expected: tuple) -> None:
    log = _log([2])
    page = page_before(log, before=before, limit=3)
    assert (page.events, page.oldest_seq, page.has_older) == expected


def test_an_empty_log_has_no_page() -> None:
    page = page_before([], before=None, limit=3)
    assert (page.events, page.oldest_seq, page.has_older) == ([], None, False)


def test_a_before_past_the_end_reads_the_newest_page() -> None:
    log = _log([1, 1])  # ordinals 1..4
    page = page_before(log, before=99, limit=10)
    assert _ids(page.events) == _ids(log)
    assert (page.oldest_seq, page.has_older) == (1, False)


def test_a_limit_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="limit"):
        page_before(_log([1]), before=None, limit=0)
