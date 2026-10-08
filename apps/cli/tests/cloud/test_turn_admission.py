"""The box asks the server before a member's relay drives the chat, and a
server that does not decide never lets a message through: it is asked again
on a short backoff and, still undecided, the message is held."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.rest import CloudApiError
from alkera_cli.cloud.turn_admission import HELD_DETAIL, NO_AUTHOR_DETAIL, turn_refusal

CHAT = "chat-1"
AUTHOR = "member-1"
OWNER = "owner-1"
HELD = f"This message was not run: {HELD_DETAIL}"


class _Server:
    """The backend's send-admission route, answering each ask in turn from a
    script; the last answer repeats. An answer is a body (decided), an int
    (an error status) or an exception (the request never completed). Only
    ``allowed`` names are let send when the body says so per user."""

    def __init__(self, *answers: Any, allowed: frozenset[str] | None = None) -> None:
        self._answers = list(answers)
        self._allowed = allowed

    async def send_admission(self, chat_id: str, *, user_id: str) -> dict[str, Any]:
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            raise CloudApiError(answer, {"detail": "x"}, method="GET", path="/send-admission")
        if self._allowed is not None:
            return {"allowed": user_id in self._allowed, "message": f"{user_id} may not send"}
        return dict(answer)


async def _no_wait(_seconds: float) -> None:
    return None


def _timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("timed out")


async def _refusal(server: _Server, relay: dict[str, Any], **kwargs: Any) -> str | None:
    return await turn_refusal(server, CHAT, relay, sleep=_no_wait, **kwargs)


@pytest.mark.parametrize(
    "undecided",
    [
        pytest.param(lambda: 503, id="503"),
        pytest.param(lambda: 502, id="502"),
        pytest.param(lambda: 429, id="throttled"),
        pytest.param(_timeout, id="timeout"),
        pytest.param(lambda: httpx.ConnectError("refused"), id="unreachable"),
    ],
)
async def test_a_server_that_never_decides_holds_the_message(
    undecided: Callable[[], Any],
) -> None:
    server = _Server(undecided())
    assert await _refusal(server, {"user_id": AUTHOR}) == HELD


@pytest.mark.parametrize(
    ("first", "then", "expected"),
    [
        pytest.param(503, {"allowed": True}, None, id="503-then-allowed-runs"),
        pytest.param(
            _timeout(),
            {"allowed": False, "message": "Your access was removed."},
            "This message was not run: Your access was removed.",
            id="timeout-then-refused-is-refused",
        ),
    ],
)
async def test_a_server_that_decides_on_a_later_ask_is_heard(
    first: Any, then: dict[str, Any], expected: str | None
) -> None:
    assert await _refusal(_Server(first, then), {"user_id": AUTHOR}) == expected


async def test_the_retries_are_bounded_and_back_off() -> None:
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    result = await turn_refusal(
        _Server(503), CHAT, {"user_id": AUTHOR}, sleep=_sleep, backoff=(0.25, 1.0)
    )
    assert result == HELD
    assert waits == [0.25, 1.0]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param({"allowed": True}, None, id="allowed"),
        pytest.param(404, None, id="a-server-from-before-the-check"),
        pytest.param(
            {"allowed": False, "message": "You need edit access."},
            "This message was not run: You need edit access.",
            id="refused-with-the-servers-sentence",
        ),
        pytest.param(
            {"allowed": False},
            "This message was not run: its author may no longer send here",
            id="refused-without-a-sentence",
        ),
        pytest.param(
            401, "This message was not run: this machine's credential was refused", id="401"
        ),
        pytest.param(
            403,
            "This message was not run: the server says this chat is not this machine's to run",
            id="403",
        ),
    ],
)
async def test_what_a_deciding_server_says_is_what_happens(
    answer: Any, expected: str | None
) -> None:
    assert await _refusal(_Server(answer), {"user_id": AUTHOR}) == expected


@pytest.mark.parametrize(
    ("relay", "owner", "expected"),
    [
        pytest.param({"user_id": ""}, OWNER, None, id="empty-author-asked-as-the-owner"),
        pytest.param({}, OWNER, None, id="missing-author-asked-as-the-owner"),
        pytest.param(
            {"user_id": ""},
            "someone-else",
            "This message was not run: someone-else may not send",
            id="empty-author-and-the-owner-may-not",
        ),
        pytest.param(
            {"user_id": ""},
            None,
            f"This message was not run: {NO_AUTHOR_DETAIL}",
            id="nobody-to-ask-about-is-refused",
        ),
        pytest.param(
            {"user_id": "intruder"},
            OWNER,
            "This message was not run: intruder may not send",
            id="a-named-author-is-asked-about-never-the-owner",
        ),
    ],
)
async def test_a_relay_without_an_author_is_checked_never_skipped(
    relay: dict[str, Any], owner: str | None, expected: str | None
) -> None:
    server = _Server({"allowed": True}, allowed=frozenset({OWNER}))
    assert await _refusal(server, relay, owner_user_id=owner) == expected
