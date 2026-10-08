"""The live window's trim: what it keeps, and what it costs to decide that.

The window is compacted inside ``apply_op``, which runs on the serving
worker's event loop with no executor under it — so the cost of one trim is
paid by every socket and every request on that process. A publisher can fill
the window to the threshold in two frames, so the trim has to be linear in the
window it is trimming; the shape it replaced re-serialised the whole document
once per dropped event and took minutes at the real threshold.

Cheaper is only worth anything if it still cuts to the same window, so the
window this arrives at is pinned against a naive implementation of the rule.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.objects import chat_transcript
from alkera_core.objects.chat_transcript import compact_window, state_size, window_threshold
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st


def _naive_compact(state: dict[str, Any]) -> dict[str, Any]:
    """The rule, spelled the expensive way: drop the oldest event, re-serialise
    the whole state, repeat. This is the reference the fast trim must agree
    with, event for event and id for id."""
    events = list(state.get("events") or [])
    if not events or state_size(state) <= window_threshold():
        return state
    trimmed = state
    while len(events) > 1 and state_size(trimmed) > window_threshold():
        events = events[1:]
        trimmed = {
            **state,
            "events": events,
            "ids": {
                str(event.get("event_id")): index
                for index, event in enumerate(events)
                if isinstance(event, dict) and isinstance(event.get("event_id"), str)
            },
        }
    return trimmed


def _rebuild_ids(events: list[Any]) -> dict[str, int]:
    return {
        str(event.get("event_id")): index
        for index, event in enumerate(events)
        if isinstance(event, dict) and isinstance(event.get("event_id"), str)
    }


def _state(events: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "meta": {},
        **extra,
        "events": events,
        "ids": _rebuild_ids(events),
    }


def _event(index: int, *, size: int = 40) -> dict[str, Any]:
    return {"event_id": f"e{index}", "type": "message", "text": "x" * size}


class _Counter:
    """Counts whole-state serialisations, which is what the quadratic trim
    spent its time on."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, state: Any) -> int:
        self.calls += 1
        return state_size(state)


@pytest.fixture
def count_serialisations(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    counter = _Counter()
    monkeypatch.setattr(chat_transcript, "state_size", counter)
    return counter


# ---------------------------------------------------------------------------
# The cost
# ---------------------------------------------------------------------------


def test_a_trim_serialises_the_whole_state_at_most_twice(
    count_serialisations: _Counter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One serialisation decides whether a trim is needed; the trim itself
    needs none. A trim that re-serialised per dropped event lands here in the
    hundreds for this window and in the tens of thousands for the one below."""
    monkeypatch.setattr(settings, "realtime_doc_max_bytes", 8_000)
    state = _state([_event(index) for index in range(400)])
    assert state_size(state) > window_threshold(), "the window must actually need trimming"

    trimmed = compact_window(state)

    assert state_size(trimmed) <= window_threshold()
    assert len(trimmed["events"]) < 400
    assert count_serialisations.calls <= 2


def test_a_publisher_filling_the_window_in_two_frames_is_trimmed_in_one_pass(
    count_serialisations: _Counter,
) -> None:
    """The reachable bad case at the SHIPPED bounds: a window filled with tens
    of thousands of small events and then one frame-sized op on top. The rule
    is unchanged; only the work it takes to apply it is."""
    events: list[Any] = [_event(index) for index in range(20_000)]
    events.append({"event_id": "big", "type": "message", "text": "x" * 1_900_000})
    state = _state(events)
    assert state_size(state) > window_threshold()

    trimmed = compact_window(state)

    assert state_size(trimmed) <= window_threshold()
    assert count_serialisations.calls <= 2
    assert trimmed["events"][-1]["event_id"] == "big", "the newest event survives"


# ---------------------------------------------------------------------------
# The window it cuts to
# ---------------------------------------------------------------------------


def test_the_trim_cuts_to_the_window_the_naive_rule_would(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_doc_max_bytes", 6_000)
    state = _state([_event(index, size=index % 37) for index in range(300)])
    assert compact_window(state) == _naive_compact(state)


@pytest.mark.parametrize("seed", range(25))
def test_the_trim_agrees_with_the_naive_rule_on_awkward_windows(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows a publisher's snapshot can put in the row and an append never
    would: an entry that is not a dict, an event with no id, a repeated id
    (``ids`` keeps only its last position, which moves with the cut), and ids
    wide enough that their own digits change the size."""
    rng = random.Random(seed)
    monkeypatch.setattr(settings, "realtime_doc_max_bytes", rng.choice([600, 2_000, 9_000]))
    events: list[Any] = []
    for index in range(rng.randint(2, 260)):
        roll = rng.random()
        if roll < 0.05:
            events.append(rng.choice(["a string, not an event", 17, None]))
        elif roll < 0.10:
            events.append({"type": "message", "text": "x" * rng.randint(0, 30)})
        elif roll < 0.20 and events:
            events.append(_event(rng.randint(0, index), size=rng.randint(0, 40)))
        else:
            events.append(_event(index, size=rng.randint(0, 60)))
    state = _state(events, meta={"draft": "ééé" * rng.randint(0, 20)})
    assert compact_window(state) == _naive_compact(state)


@given(
    sizes=st.lists(st.integers(min_value=0, max_value=200), min_size=1, max_size=120),
    cap=st.integers(min_value=200, max_value=9_000),
    draft=st.text(max_size=60),
)
@hyp_settings(deadline=None, max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_the_trim_agrees_with_the_naive_rule(sizes: list[int], cap: int, draft: str) -> None:
    original = settings.realtime_doc_max_bytes
    settings.realtime_doc_max_bytes = cap
    try:
        state = _state(
            [_event(index, size=size) for index, size in enumerate(sizes)],
            meta={"draft": draft},
        )
        assert compact_window(state) == _naive_compact(state)
    finally:
        settings.realtime_doc_max_bytes = original


def test_the_trim_keeps_every_event_that_still_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trim reads candidate sizes off per-event numbers rather than
    serialising each candidate. If that arithmetic over-counted it would throw
    away events that fit; if it under-counted it would leave the state over the
    cap. So the window it keeps fits and the next-wider one does not."""
    monkeypatch.setattr(settings, "realtime_doc_max_bytes", 5_000)
    events = [_event(index, size=index % 23) for index in range(200)]
    trimmed = compact_window(_state(events))
    kept = len(trimmed["events"])
    assert 0 < kept < 200
    assert state_size(trimmed) <= window_threshold()
    wider = list(events[-(kept + 1) :])
    assert state_size(_state(wider)) > window_threshold(), "one more event would not have fit"
