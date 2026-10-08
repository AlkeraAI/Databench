"""The box's word on a chat survives a backend under load.

Seen in a stress run: the box's ``publishing`` report met a 503
(``db_lock_timeout``) and was dropped, so a reader's wake stayed on the chat
and the box re-took it after every sleep. A passing fault is now sent again a
bounded number of times; a refusal of the report itself is not, and a later
report on the same chat ends an earlier one's retries so an old word never
lands after a newer one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud.publisher_report import RETRY_WAITS, send_report
from alkera_cli.cloud.rest import CloudApiError

pytestmark = pytest.mark.asyncio


def _api(status: int) -> CloudApiError:
    return CloudApiError(status, {"code": "x"}, method="PUT", path="/publisher-state")


class _Backend:
    """Answers each report with the next scripted outcome."""

    def __init__(self, *outcomes: BaseException | None) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[tuple[str, str, str, str | None]] = []

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> Any:
        self.sent.append((chat_id, state, reason, ending))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return {}


class _Sleeps:
    def __init__(self) -> None:
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(_api(503), id="db-lock-timeout"),
        pytest.param(_api(502), id="bad-gateway"),
        pytest.param(_api(429), id="throttled"),
        pytest.param(httpx.ConnectError("refused"), id="no-connection"),
        pytest.param(httpx.ReadTimeout("slow"), id="no-answer-in-time"),
    ],
)
async def test_a_passing_fault_is_sent_again_until_it_lands(fault: BaseException) -> None:
    backend, sleeps = _Backend(fault, fault), _Sleeps()

    landed = await send_report(backend, "chat-1", "publishing", sleep=sleeps, current=lambda: True)

    assert landed is True
    assert [state for _c, state, _r, _e in backend.sent] == ["publishing"] * 3
    assert sleeps.waited == list(RETRY_WAITS[:2])


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(_api(422), id="an-older-backend-without-the-state"),
        pytest.param(_api(403), id="not-the-publisher"),
        pytest.param(_api(404), id="the-chat-is-gone"),
    ],
)
async def test_a_refusal_of_the_report_is_not_sent_again(refusal: BaseException) -> None:
    backend, sleeps = _Backend(refusal), _Sleeps()

    assert (
        await send_report(backend, "chat-1", "waiting", sleep=sleeps, current=lambda: True) is False
    )
    assert len(backend.sent) == 1 and sleeps.waited == []


async def test_the_attempts_are_bounded() -> None:
    backend, sleeps = _Backend(*[_api(503)] * 10), _Sleeps()

    assert (
        await send_report(backend, "chat-1", "asleep", sleep=sleeps, current=lambda: True) is None
    ), "not landed, and still a passing fault the box may send again"
    assert len(backend.sent) == len(RETRY_WAITS) + 1
    assert sleeps.waited == list(RETRY_WAITS)


async def test_a_later_report_ends_the_retries_of_an_earlier_one() -> None:
    """An ``awake`` sent again after the box already said ``asleep`` would
    leave the chat reading as served by a box that let it go."""
    backend, sleeps = _Backend(_api(503), _api(503)), _Sleeps()
    newest = {"turn": 1}

    async def _sleep(seconds: float) -> None:
        await sleeps(seconds)
        newest["turn"] = 2  # the box reported again while this one waited

    landed = await send_report(
        backend, "chat-1", "publishing", sleep=_sleep, current=lambda: newest["turn"] == 1
    )

    assert landed is False
    assert len(backend.sent) == 1, "the stale word was not sent again"


async def test_the_ending_and_reason_travel_with_every_attempt() -> None:
    backend = _Backend(_api(503))

    await send_report(
        backend,
        "chat-1",
        "asleep",
        reason="r",
        ending="evicted",
        sleep=_Sleeps(),
        current=lambda: True,
    )

    assert backend.sent == [("chat-1", "asleep", "r", "evicted")] * 2


async def test_the_box_never_lands_an_old_word_after_a_newer_one(tmp_path: Any) -> None:
    """Through the service: the box says ``publishing``, the backend answers
    503, and while the box waits to send it again the chat is slept and the
    box says ``asleep``. The ``publishing`` is not sent again: landing after
    the ``asleep`` it would leave the chat reading as served."""
    sent: list[tuple[str, str]] = []
    holder: dict[str, Any] = {}

    async def _report(
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> dict[str, Any]:
        sent.append((chat_id, state))
        if len(sent) == 1:
            raise _api(503)
        return {}

    async def _sleep(seconds: float) -> None:
        if not holder.get("reported"):
            holder["reported"] = True
            await holder["service"].report_publisher_state("chat-1", "asleep", ending="idle")

    service, _built = build_service(tmp_path, clock=Clock(), sleep=_sleep)
    holder["service"] = service
    service._rest.report_publisher_state = _report  # type: ignore[method-assign]

    await service.report_publisher_state("chat-1", "publishing")
    await service.settle_background()

    assert sent == [("chat-1", "publishing"), ("chat-1", "asleep")]


async def test_a_backend_that_is_down_never_holds_the_box(tmp_path: Any) -> None:
    """The first attempt is on the caller's path, so its ordering is the
    caller's; the retries are not. A start or a sleep that waited out every
    retry on an unreachable backend would hold the chat for seconds each."""
    never = asyncio.Event()
    sent: list[str] = []

    async def _report(chat_id: str, **_: Any) -> dict[str, Any]:
        sent.append(chat_id)
        raise httpx.ConnectError("down")

    async def _sleep(seconds: float) -> None:
        await never.wait()

    service, _built = build_service(tmp_path, clock=Clock(), sleep=_sleep)
    service._rest.report_publisher_state = _report  # type: ignore[method-assign]

    await asyncio.wait_for(service.report_publisher_state("chat-1", "publishing"), 1.0)
    assert sent == ["chat-1"]
