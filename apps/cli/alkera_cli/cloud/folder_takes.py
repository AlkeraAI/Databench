"""Taking a chat's folder on a box, and waiting out a drive it cannot reach.

The take is the pull: the box that holds the lease is the box that may write
the chat. A folder another box holds stops the chat being served here; a take
that cannot reach the drive waits, longer each time, rather than paying a
connect timeout for every chat on every pass.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from alkera_sdk.client import AlkeraHTTPError

from alkera_cli.cloud.faults import refusal_kind, says_refused
from alkera_cli.cloud.folder import ChatFolders, FolderBusyError
from alkera_cli.cloud.start_failures import start_key
from alkera_cli.cloud.take_text import (
    take_failure_detail,
    take_failure_text,
    take_refusal_text,
)
from alkera_cli.host.backoff import exponential_delay

logger = logging.getLogger(__name__)

TAKE_FAILURE_BACKOFF_CAP_SECONDS = 300.0
"""Ceiling on the wait before a chat whose folder could not be REACHED is
taken again. A take that ends in a connection error — the Files content origin
the backend advertises does not resolve on this box, the drive is unreachable —
costs a full DNS or connect timeout and cannot succeed until something outside
this box changes. Retried on every pass it is paid again per chat per pass,
and enough such chats starve the pass long enough for other chats' leases to
lapse."""


@dataclass(slots=True)
class _TakeFailure:
    """What the box remembers of a chat whose folder it could not reach."""

    #: Failures in a row; the wait doubles with each.
    count: int
    #: The service clock reading at which the take may be tried again.
    retry_at: float
    #: The row facts the failure was recorded under (``start_key``): a reader
    #: saying something ends the wait at once.
    seen: tuple[Any, ...]
    #: Whether the failure has been put on the chat, so it is said once.
    said: bool = False


class FolderTakes:
    """See the module docstring. ``instance_of`` names the lease holder for
    a chat, ``report`` puts a verdict on a chat."""

    def __init__(
        self,
        *,
        folders: ChatFolders,
        instance_of: Callable[[str], str],
        report: Callable[..., Awaitable[Any]],
        clock: Callable[[], float],
        poll_interval: Callable[[], float],
    ) -> None:
        self._folders = folders
        self._instance_of = instance_of
        self._report = report
        self._clock = clock
        self._poll_interval = poll_interval
        #: Chats whose folder this box could not REACH: how many times in a
        #: row and when to try again. A take that ends in a connection error
        #: costs a connect timeout and cannot succeed until something outside
        #: this box changes, so the box stops paying for it on every pass.
        self._failures: dict[str, _TakeFailure] = {}
        #: Chats another box holds the folder of, by the holder it named: said
        #: once per chat and holder rather than on every poll tick.
        self._busy: dict[str, str | None] = {}

    def forget(self, chat_id: str) -> None:
        self._failures.pop(chat_id, None)

    async def take(self, chat_id: str, chat: dict[str, Any]) -> bool:
        """Take the chat's folder and pull it down. False means "not here".

        A folder another box still holds is the one refusal that stops the chat
        being served: two writers on one chat folder is exactly what the lease
        exists to prevent, and the box that has it will hand it back when the
        chat sleeps there.

        A take that cannot REACH the drive is different again, and is not paid
        for on every pass: see ``_unreachable``.
        """
        if not self._folders.enabled:
            return True
        if not self._may_retry(chat_id, chat):
            return False
        try:
            await asyncio.to_thread(
                self._folders.take, chat_id, chat, instance=self._instance_of(chat_id)
            )
        except FolderBusyError as busy:
            # The drive answered, so whatever could not be reached before can
            # be now: the wait is over even though the chat is not served here.
            self._failures.pop(chat_id, None)
            if self._busy.get(chat_id) != busy.holder:
                self._busy[chat_id] = busy.holder
                logger.info(
                    "chat %s is not served here: another machine holds its folder%s",
                    chat_id,
                    f" ({busy.holder})" if busy.holder else "",
                )
            return False
        except (httpx.HTTPError, AlkeraHTTPError, OSError) as exc:
            # Unreachable, or answered with a refusal the next attempt may not
            # repeat (the folder custody raises only those): the chat is not
            # served until a take lands, since a turn run with no lease writes
            # files only this box would ever hold.
            failure = self._unreachable(chat_id, chat, exc)
            # A passing fault (the 503 or 429 of the burst a new chat causes)
            # is waited out, not put on the chat: a refusal disables the
            # composer, and a message is what ends this wait. A verdict, or a
            # fault that outlasted its tries, is said to the reader; the
            # mirror start that follows a take clears it with "publishing".
            if not failure.said and says_refused(refusal_kind(exc), failure.count):
                failure.said = True
                await self._report(
                    chat_id, "refused", self._take_refusal(exc), kind="files_unreachable"
                )
            return False
        self._failures.pop(chat_id, None)
        self._busy.pop(chat_id, None)
        return True

    @staticmethod
    def _take_refusal(exc: BaseException) -> str:
        """What the reader is told when the folder could not be pulled: what
        kind of failure it was, never the host, the port or the URL."""
        return f"this chat's folder could not be taken: {take_refusal_text(exc)}"

    def _may_retry(self, chat_id: str, chat: dict[str, Any]) -> bool:
        """Whether the wait after an unreachable take is up, or news on the
        chat's row (a message, a reader opening it) has ended it early."""
        failure = self._failures.get(chat_id)
        if failure is None:
            return True
        if failure.seen != start_key(chat):
            return True
        return self._clock() >= failure.retry_at

    def _unreachable(self, chat_id: str, chat: dict[str, Any], exc: BaseException) -> _TakeFailure:
        """The drive could not be reached for this chat's folder: wait, longer
        each time, and say which host it was. Returns the record.

        A connection error is not an answer that a retry changes — a content
        origin that does not resolve on this box resolves no better fifteen
        seconds later — and the ask is not free: each one costs a DNS or
        connect timeout inside the discovery pass, and the pass is what the
        folders this box ALREADY holds were being beaten behind. So the wait
        doubles per failure to a cap, and the line is said once per wait rather
        than once per pass: the same box logged this failure 944 times in
        twenty-three minutes for 34 chats.
        """
        previous = self._failures.get(chat_id)
        count = (previous.count if previous is not None else 0) + 1
        wait = exponential_delay(
            count, first=self._poll_interval(), cap=TAKE_FAILURE_BACKOFF_CAP_SECONDS
        )
        failure = self._failures[chat_id] = _TakeFailure(
            count=count,
            retry_at=self._clock() + wait,
            seen=start_key(chat),
            said=previous is not None and previous.said,
        )
        logger.info(
            "chat %s: its folder could not be taken — %s (%s); it is tried again in %.0f s",
            chat_id,
            take_failure_text(exc, box="this box"),
            take_failure_detail(exc),
            wait,
        )
        return failure


__all__ = ["TAKE_FAILURE_BACKOFF_CAP_SECONDS", "FolderTakes"]
