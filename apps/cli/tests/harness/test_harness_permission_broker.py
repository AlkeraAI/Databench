"""Tests for `alkera_cli.harness.permission_broker.PermissionBroker`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from alkera_cli.harness.permission_broker import AskReconsideredError, PermissionBroker
from alkera_core.schemas.chat import (
    PermissionOption,
    PermissionRequest,
)

_T = datetime(2026, 5, 26, tzinfo=UTC)


def _request(req_id: str = "r1") -> PermissionRequest:
    return PermissionRequest(
        event_id=f"ev_{req_id}",
        time=_T,
        session_id="s1",
        request_id=req_id,
        permission_kind="run",
        options=[
            PermissionOption(option_id="allow_once", name="Allow once"),
            PermissionOption(option_id="reject_once", name="Reject once"),
        ],
    )


async def test_resolver_choice_returned_directly() -> None:
    async def always_allow(req: PermissionRequest):
        return "allow_once"

    broker = PermissionBroker(always_allow, default_timeout_seconds=1.0)
    assert await broker.resolve(_request()) == "allow_once"


async def test_timeout_auto_rejects() -> None:
    async def never_responds(req: PermissionRequest):
        await asyncio.sleep(10)
        return "allow_once"

    broker = PermissionBroker(never_responds, default_timeout_seconds=0.05)
    assert await broker.resolve(_request()) == "reject_once"


async def test_resolver_exception_auto_rejects() -> None:
    async def buggy(req: PermissionRequest):
        raise ValueError("boom")

    broker = PermissionBroker(buggy, default_timeout_seconds=1.0)
    assert await broker.resolve(_request()) == "reject_once"


async def test_no_timeout_when_set_to_none() -> None:
    """Webview default — wait indefinitely."""

    answer = "allow_always"

    async def slow_but_ok(req: PermissionRequest):
        await asyncio.sleep(0.05)
        return answer

    broker = PermissionBroker(slow_but_ok, default_timeout_seconds=None)
    # Even with timeout=None we shouldn't wait forever — 0.05s ≪ test timeout.
    assert await broker.resolve(_request()) == answer


async def test_handles_back_to_back_requests() -> None:
    """Brokers are reused across many requests in a session."""
    seen: list[str] = []

    async def record(req: PermissionRequest):
        seen.append(req.request_id)
        return "allow_once"

    broker = PermissionBroker(record)
    await broker.resolve(_request("r1"))
    await broker.resolve(_request("r2"))
    await broker.resolve(_request("r3"))
    assert seen == ["r1", "r2", "r3"]


# --------------------------------------------------------------------------- #
# reconsidering an ask: the prompt stays up for the waiter that comes back
# --------------------------------------------------------------------------- #


class _Prompts:
    """A resolver that shows a prompt per call and records how it ended."""

    def __init__(self) -> None:
        self.shown: list[str] = []
        self.open: dict[str, asyncio.Future[str]] = {}
        self.taken_down: list[str] = []

    async def __call__(self, req: PermissionRequest) -> str:
        self.shown.append(req.request_id)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.open[req.request_id] = fut
        try:
            return await fut
        except asyncio.CancelledError:
            self.taken_down.append(req.request_id)
            raise


async def _parked(
    broker: PermissionBroker, prompts: _Prompts, req: PermissionRequest
) -> asyncio.Task:
    waiter = asyncio.ensure_future(broker.decide(req))
    for _ in range(20):
        await asyncio.sleep(0)
    assert req.request_id in prompts.open
    return waiter


async def test_a_reconsidered_waiter_is_released_and_the_same_prompt_answers_its_return() -> None:
    prompts = _Prompts()
    broker = PermissionBroker(prompts)
    req = _request()
    waiter = await _parked(broker, prompts, req)

    assert broker.reconsider("r1") is True
    with pytest.raises(AskReconsideredError):
        await waiter
    assert prompts.taken_down == [], "the person is still looking at the prompt"
    assert not broker.has_pending_for("s1")

    back = asyncio.ensure_future(broker.decide(req))
    for _ in range(20):
        await asyncio.sleep(0)
    assert broker.has_pending_for("s1")
    prompts.open["r1"].set_result("reject_once")
    decided = await back
    assert (decided.option, decided.decided_by) == ("reject_once", "human")
    assert prompts.shown == ["r1"], "coming back never raises a second prompt"


async def test_a_withdrawn_prompt_is_taken_down() -> None:
    prompts = _Prompts()
    broker = PermissionBroker(prompts)
    waiter = await _parked(broker, prompts, _request())
    broker.reconsider("r1")
    with pytest.raises(AskReconsideredError):
        await waiter
    # Down by the time withdraw returns, with no further turn of the loop: the
    # caller answers the agent next, and no reader may still hold the ask.
    await broker.withdraw("r1")
    assert prompts.taken_down == ["r1"]


async def test_reconsider_leaves_an_ask_nobody_is_asked_or_already_answered_alone() -> None:
    prompts = _Prompts()
    broker = PermissionBroker(prompts)
    assert broker.reconsider("never-asked") is False

    waiter = await _parked(broker, prompts, _request())
    prompts.open["r1"].set_result("allow_once")
    for _ in range(5):
        await asyncio.sleep(0)
    assert broker.reconsider("r1") is False, "the person already answered it"
    assert (await waiter).option == "allow_once"


async def test_cancelling_the_waiter_still_takes_the_prompt_down() -> None:
    """Teardown: a waiter cancelled mid-ask (the chat closing) takes its prompt
    with it, as it did when the resolver was awaited in place."""
    prompts = _Prompts()
    broker = PermissionBroker(prompts)
    waiter = await _parked(broker, prompts, _request())
    waiter.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert prompts.taken_down == ["r1"]
    assert not broker.has_pending_for("s1")
