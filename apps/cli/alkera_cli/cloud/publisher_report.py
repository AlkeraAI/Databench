"""The box's word on a chat (``publishing``, ``waiting``, ``refused``,
``asleep``), sent so that a backend under load does not lose it.

A lost report is not harmless: a lost ``publishing`` left a reader's wake
standing and the box re-took the chat after every sleep; a lost ``asleep``
left the chat reading as served. A cut connection was already sent again once
by the REST client; an answer that is a passing fault (a 5xx such as the
backend's ``db_lock_timeout``, a 429, a transport error) is now sent again a
bounded number of times too. A refusal that is about the report (a 4xx) is
not. A later report on the same chat ends the retries of an earlier one, so
an old word can never land after a newer one. The box sends the first attempt
on the caller's path (its ordering is the caller's) and the retries off it, so
a backend that is down never holds a chat's start or sleep.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from functools import partial
from typing import Any, Protocol

import httpx

from alkera_cli.cloud.faults import passing_fault
from alkera_cli.cloud.rest import CloudApiError

logger = logging.getLogger(__name__)

#: The waits before each further attempt; their count bounds the attempts.
RETRY_WAITS: tuple[float, ...] = (0.5, 1.5, 4.0)


class PublisherReporter(Protocol):
    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
        workspace_sandbox: str | None = None,
        workspace_memory_mb: int | None = None,
    ) -> Any: ...


async def send_report(
    rest: PublisherReporter,
    chat_id: str,
    state: str,
    *,
    reason: str = "",
    kind: str | None = None,
    ending: str | None = None,
    fields: Mapping[str, Any] | None = None,
    sleep: Callable[[float], Awaitable[None]],
    current: Callable[[], bool],
    waits: tuple[float, ...] = (0.0, *RETRY_WAITS),
) -> bool | None:
    """Send one report, once per entry of ``waits`` (each attempt after its
    wait), for as long as the failure is a passing fault and ``current()``
    says no later report on the chat has begun.

    ``True`` landed; ``False`` refused for good, or overtaken by a later
    report; ``None`` a passing fault outlasted ``waits`` (the caller may send
    it again later). Never raises for the backend's answer. ``fields`` are
    what else the report carries (a workspace member's facets)."""
    extra: dict[str, Any] = dict(fields or {})
    if ending is not None:
        extra["ending"] = ending
    if kind is not None:
        extra["refusal_kind"] = kind
    for wait in waits:
        if wait:
            await sleep(wait)
        if not current():
            return False
        try:
            await rest.report_publisher_state(chat_id, state=state, reason=reason, **extra)
            return True
        except (CloudApiError, httpx.HTTPError, OSError, ValueError) as exc:
            if not passing_fault(exc):
                logger.warning(
                    "chat %s: the publisher state (%s) was refused: %s", chat_id, state, exc
                )
                return False
            logger.info("chat %s: the publisher state (%s) did not land: %s", chat_id, state, exc)
    return None


class Reporter:
    """The box's reports, one chat at a time: each is sent once on the
    caller's path (its ordering is the caller's) and, on a passing fault,
    again through ``later`` (a background task), so a backend that is down
    never holds a chat's start or sleep. A later report on a chat ends an
    earlier one's retries."""

    def __init__(
        self,
        rest: Callable[[], PublisherReporter],
        *,
        sleep: Callable[[float], Awaitable[None]],
        later: Callable[[Coroutine[Any, Any, None], str], object],
    ) -> None:
        self._rest = rest
        self._sleep = sleep
        self._later = later
        self._turns: dict[str, int] = {}

    async def __call__(
        self,
        chat_id: str,
        state: str,
        reason: str = "",
        *,
        ending: str | None = None,
        kind: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        turn = self._turns[chat_id] = self._turns.get(chat_id, 0) + 1
        send = partial(
            send_report,
            self._rest(),
            chat_id,
            state,
            reason=reason,
            kind=kind,
            ending=ending,
            fields=dict(fields or {}),
            sleep=self._sleep,
            current=lambda: self._turns.get(chat_id) == turn,
        )
        if await send(waits=(0.0,)) is None:
            self._later(_again(send), f"cloud-report:{chat_id}")


async def _again(send: Callable[..., Awaitable[bool | None]]) -> None:
    await send(waits=RETRY_WAITS)


__all__ = ["RETRY_WAITS", "PublisherReporter", "Reporter", "send_report"]
