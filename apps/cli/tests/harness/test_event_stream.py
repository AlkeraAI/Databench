"""`ChatEventStream`: history-then-live, de-duped by event_id."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

from alkera_cli.harness.event_stream import ChatEventStream
from alkera_core.schemas.chat import Event, SessionStatusChanged


def _ev(event_id: str) -> SessionStatusChanged:
    return SessionStatusChanged(
        event_id=event_id, time=datetime.now(UTC), session_id="s", status="running"
    )


async def _aiter(items: list[Event]) -> AsyncIterator[Event]:
    for item in items:
        yield item


class _FakeSession:
    """Minimal stand-in for ChatSession: a persisted prefix + a live bus."""

    def __init__(self, history: list[Event], live: list[Event]) -> None:
        self._history = history
        self._live = live

    def events(self) -> Iterator[Event]:
        return iter(self._history)

    def subscribe(self) -> AsyncIterator[Event]:
        return _aiter(self._live)


async def test_for_session_yields_history_then_live_deduped() -> None:
    # history = [a, b]; the live bus re-delivers b (snapshot/subscribe overlap) and
    # delivers a fresh c. live() must drop the b overlap and yield only c.
    a, b, c = _ev("a"), _ev("b"), _ev("c")
    stream = ChatEventStream.for_session(_FakeSession([a, b], [b, c]))  # type: ignore[arg-type]

    assert [e.event_id for e in stream.history] == ["a", "b"]
    live = [e.event_id async for e in stream.live()]
    assert live == ["c"]


async def test_replay_only_has_history_and_no_live() -> None:
    stream = ChatEventStream.replay([_ev("a"), _ev("b")])
    assert [e.event_id for e in stream.history] == ["a", "b"]
    assert [e async for e in stream.live()] == []


async def test_live_with_no_overlap_yields_all_new() -> None:
    a = _ev("a")
    stream = ChatEventStream.for_session(_FakeSession([a], [_ev("x"), _ev("y")]))  # type: ignore[arg-type]
    live = [e.event_id async for e in stream.live()]
    assert live == ["x", "y"]
