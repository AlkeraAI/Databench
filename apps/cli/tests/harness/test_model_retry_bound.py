"""A turn whose model call keeps failing ends, failed, instead of retrying for
ever.

The agent retries a failed model call on its own with no cap (opencode's retry
schedule stops only on an error it does not consider retryable), and each
attempt reaches the harness as a ``Retrying`` event. A gateway that could not
be reached from a node retried silently for the whole of a turn's budget. The
runtime now stops the agent after a bounded number of consecutive retries, or
a bounded time spent retrying, and settles the turn as failed with the kind of
failure and the count — never the provider's own words.

Driven against a ``FakeAdapter``, whose ``feed`` publishes verbatim.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import fake_runtime
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.model_retry import ModelRetryBudget, failure_kind, give_up_text
from alkera_cli.harness.runtime import ChatSession
from alkera_core.schemas.chat import (
    Event,
    Heartbeat,
    PartCreated,
    Retrying,
    SessionStatusChanged,
    TextPart,
)

_T = datetime(2026, 9, 27, tzinfo=UTC)
#: What bun's fetch says when the gateway's port is closed — with the URL in it,
#: which must never reach the reader.
_UNREACHABLE = "Unable to connect. Is the computer able to access the url? http://10.0.4.7:8081/v1"


async def _open(tmp_path: Path) -> tuple[HarnessRuntime, ChatSession, FakeAdapter]:
    rt, factory = fake_runtime(tmp_path)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    return rt, session, factory.adapters[-1]


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch):
    """Pin the bounds small; ``window`` off unless a test sets it."""

    def pin(attempts: int | None = 3, window: float | None = None) -> None:
        monkeypatch.setattr("alkera_cli.harness.runtime.MAX_MODEL_RETRIES", attempts)
        monkeypatch.setattr("alkera_cli.harness.runtime.MODEL_RETRY_WINDOW_SECONDS", window)

    return pin


def _retry(n: int, reason: str = _UNREACHABLE) -> Retrying:
    return Retrying(
        event_id=f"retry-{n}-{reason[:4]}",
        time=_T,
        session_id="s",
        attempt=n,
        reason=reason,
        next_attempt_in_ms=2000 * n,
    )


def _answer(tag: str) -> PartCreated:
    return PartCreated(
        event_id=f"answer-{tag}",
        time=_T,
        session_id="s",
        part=TextPart(part_id=f"p-{tag}", message_id="m1", text="partial answer"),
    )


async def _running(adapter: FakeAdapter) -> None:
    await adapter.feed(
        SessionStatusChanged(
            event_id="run",
            time=_T,
            session_id="s",
            status="running",
            turn_id=adapter.sent_prompts[-1].turn_id,
        )
    )


async def _wait_until(predicate, *, timeout_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _failure(session: ChatSession) -> SessionStatusChanged | None:
    assert session.turn_settlements == 1
    return session.settlement(0)


def _names_nothing_of_the_provider(text: str) -> None:
    assert "http" not in text and "10.0.4.7" not in text and ":8081" not in text, text
    assert "Unable to connect" not in text, "the provider's own words reached the reader"


# --------------------------------------------------------------------------- #
# the runtime
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_cap_th_retry_stops_the_agent_and_fails_the_turn(tmp_path: Path, bound) -> None:
    bound(attempts=3)
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")
        attempt = adapter.sent_prompts[0].turn_id
        await _running(adapter)
        for n in (1, 2, 3):
            await adapter.feed(_retry(n))
        await _wait_until(lambda: session.turn_settlements == 1)

        assert adapter.cancel_count == 1, "the agent was not told to stop"
        assert not session.turn_active
        failed = _failure(session)
        assert failed is not None and failed.status == "error" and failed.turn_id == attempt
        assert failed.detail == (
            "The model could not be reached after 3 attempts; the turn was stopped."
        )
        _names_nothing_of_the_provider(failed.detail)
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_one_retry_short_of_the_cap_leaves_the_turn_running(tmp_path: Path, bound) -> None:
    bound(attempts=3)
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")
        await _running(adapter)
        for n in (1, 2):
            await adapter.feed(_retry(n))
        await asyncio.sleep(0.1)

        assert session.turn_active and session.turn_settlements == 0
        assert adapter.cancel_count == 0
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_model_answer_between_retries_starts_the_count_again(tmp_path: Path, bound) -> None:
    bound(attempts=3)
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")
        await _running(adapter)
        await adapter.feed(_retry(1))
        await adapter.feed(_retry(2))
        await adapter.feed(_answer("a"))  # a call got through
        await adapter.feed(_retry(1))
        await adapter.feed(_retry(2))
        await asyncio.sleep(0.1)
        assert session.turn_active and adapter.cancel_count == 0, "the count did not reset"

        await adapter.feed(_retry(3))
        await _wait_until(lambda: session.turn_settlements == 1)
        failed = _failure(session)
        assert failed is not None and failed.detail is not None
        assert "after 3 attempts" in failed.detail
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_a_repeated_report_of_one_attempt_is_not_a_new_failure(tmp_path: Path, bound) -> None:
    bound(attempts=3)
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")
        await _running(adapter)
        for n in (1, 1, 2, 2):
            await adapter.feed(_retry(n))
        await asyncio.sleep(0.1)
        assert session.turn_active and adapter.cancel_count == 0
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_the_time_spent_retrying_is_bounded_too(
    tmp_path: Path, bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's next attempt may be scheduled past the window (a provider's
    retry-after); the harness's liveness beat is what crosses it. The window's
    clock is stepped by hand: freezing the ambient clock would freeze the event
    loop the turn runs on as well."""
    bound(attempts=None, window=60.0)
    now = [1000.0]
    monkeypatch.setattr("alkera_cli.harness.runtime.MODEL_RETRY_CLOCK", lambda: now[0])
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("do the thing")
        await _running(adapter)
        await adapter.feed(_retry(1, "Provider is overloaded"))
        # The turn pump is its own task: let it read the retry at this time.
        await asyncio.sleep(0.1)
        now[0] += 59
        await adapter.feed(_beat("hb-1"))
        await asyncio.sleep(0.1)
        assert session.turn_active, "stopped before the window was spent"

        now[0] += 2
        await adapter.feed(_beat("hb-2"))
        await _wait_until(lambda: session.turn_settlements == 1)
        failed = _failure(session)
        assert failed is not None and failed.status == "error"
        assert failed.detail == (
            "The provider was overloaded after 1 attempt in 1 minute; the turn was stopped."
        )
        assert adapter.cancel_count == 1
    finally:
        await rt.close_chat(session.session_id)


def _beat(tag: str) -> Heartbeat:
    return Heartbeat(event_id=tag, time=_T, session_id="s", last_activity_ms=0)


@pytest.mark.asyncio
async def test_the_next_turn_starts_with_a_fresh_count(tmp_path: Path, bound) -> None:
    bound(attempts=3)
    rt, session, adapter = await _open(tmp_path)
    try:
        await session.send_prompt("first")
        await _running(adapter)
        await adapter.feed(_retry(1))
        await adapter.feed(_retry(2))
        await adapter.feed(
            SessionStatusChanged(
                event_id="idle-1",
                time=_T,
                session_id="s",
                status="idle",
                turn_id=adapter.sent_prompts[0].turn_id,
            )
        )
        await _wait_until(lambda: session.turn_settlements == 1)

        await session.send_prompt("second")
        await _running(adapter)
        await adapter.feed(_retry(1))
        await asyncio.sleep(0.1)
        assert session.turn_active and adapter.cancel_count == 0
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.asyncio
async def test_every_subscriber_sees_the_retries_and_then_the_failure(
    tmp_path: Path, bound
) -> None:
    """What a terminal client renders from: the session's own stream carries each
    ``Retrying`` unchanged and then the failed terminal with its reason."""
    bound(attempts=2)
    rt, session, adapter = await _open(tmp_path)
    seen: list[Event] = []

    async def watch() -> None:
        async for ev in session.subscribe():
            seen.append(ev)

    watcher = asyncio.create_task(watch())
    try:
        await asyncio.sleep(0)
        await session.send_prompt("do the thing")
        await _running(adapter)
        await adapter.feed(_retry(1))
        await adapter.feed(_retry(2))
        await _wait_until(
            lambda: any(isinstance(e, SessionStatusChanged) and e.status == "error" for e in seen)
        )
        assert [e.attempt for e in seen if isinstance(e, Retrying)] == [1, 2]
        (failed,) = [e for e in seen if isinstance(e, SessionStatusChanged) and e.status == "error"]
        assert failed.detail == (
            "The model could not be reached after 2 attempts; the turn was stopped."
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await rt.close_chat(session.session_id)


# --------------------------------------------------------------------------- #
# the pure core: the kind, and nothing the provider said
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "lead"),
    [
        pytest.param(_UNREACHABLE, "The model could not be reached", id="connection"),
        pytest.param("fetch failed", "The model could not be reached", id="fetch-failed"),
        pytest.param("Provider is overloaded", "The provider was overloaded", id="overloaded"),
        pytest.param("Rate Limited", "The provider refused the request", id="rate-limited"),
        pytest.param("Too Many Requests", "The provider refused the request", id="429"),
        pytest.param(
            "Service Unavailable: <html>upstream http://gw.internal:8081 down</html>",
            "The provider was unavailable",
            id="503-whose-body-names-a-host",
        ),
        pytest.param("Service Unavailable", "The provider was unavailable", id="503"),
        pytest.param("Internal Server Error", "The provider was unavailable", id="500"),
        pytest.param("something new", "The model call kept failing", id="unknown"),
        pytest.param(None, "The model call kept failing", id="no-reason"),
    ],
)
def test_the_failure_names_its_kind_and_count_and_nothing_else(
    reason: str | None, lead: str
) -> None:
    text = give_up_text(reason, 6, waited_seconds=None)
    assert text == f"{lead} after 6 attempts; the turn was stopped."
    assert not re.search(r"https?://|\d+\.\d+\.\d+\.\d+|<html>", text)


def test_an_overloaded_provider_is_not_read_as_unreachable() -> None:
    assert failure_kind("Provider is overloaded") == "overloaded"
    assert failure_kind("Rate Limited") != "unreachable"


def test_the_budget_counts_only_the_turn_it_is_fed() -> None:
    budget = ModelRetryBudget(max_attempts=2, max_seconds=None)
    assert budget.observe("t1", _retry(1), 0.0) is None
    # A different turn: the first turn's failure does not count against it.
    assert budget.observe("t2", _retry(1), 1.0) is None
    assert budget.observe("t2", _retry(2), 2.0) is not None


def test_no_bounds_never_stops() -> None:
    budget = ModelRetryBudget(max_attempts=None, max_seconds=None)
    for n in range(1, 200):
        assert budget.observe("t", _retry(n), float(n * 60)) is None
