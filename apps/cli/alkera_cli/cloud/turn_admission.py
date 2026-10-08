"""Whether a message accepted earlier may still start a turn.

The server decides who may send when the message arrives. Between that and the
box picking the message up, the author can lose the right to drive the chat
(a workspace share revoked, a chat share narrowed to view). So before a turn
starts, the box asks the server again, under the same send policy, whether the
message's author may still drive this chat, and runs nothing if not.

The same question is asked before the box acts on any other relay a member
drives the chat with: an answer to a parked ask, a query re-run, a promote.

* A server from before the check answers 404: read as allowed, since the send
  was admitted when it was made and that server has nothing more to say.
* A server that cannot be reached, or fails without deciding (a 5xx, a
  throttle, a timeout), is asked again a few times on a short backoff. If it
  still does not decide, the message is held: it does not start a turn, and
  the reader is told it was not run and may send it again. A slow backend is
  never the reason a revoked member's queued message runs.
* A refusal stops the turn, and so does a refused asker: a 401 (this machine's
  credential is no longer accepted) or a 403 (the chat is not this machine's to
  run) means the box has no standing to run the message at all.
* A relay that names no author is checked against the chat's owner, the one
  person the box runs the chat for; with no owner known either, it is refused.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import httpx

from alkera_cli.cloud.rest import CloudApiError

logger = logging.getLogger(__name__)

#: What the reader is told when a message is not run because its author lost
#: the right to drive the chat; the server's own sentence follows when it gave
#: one.
NOT_RUN_PREFIX = "This message was not run"

#: What the reader is told when the server refuses the asker itself.
_REFUSED_ASKER = {
    401: "this machine's credential was refused",
    403: "the server says this chat is not this machine's to run",
}
#: What the reader is told when the server never decided.
HELD_DETAIL = "the server could not confirm its author may still send here. Send it again"
#: What the reader is told when the relay names nobody the box could ask about.
NO_AUTHOR_DETAIL = "it names no author this machine could check"
#: The waits between the asks of a server that did not decide; one more ask
#: than there are waits.
ADMISSION_BACKOFF_S: tuple[float, ...] = (0.5, 1.5)


class AdmissionAsker(Protocol):
    async def send_admission(self, chat_id: str, *, user_id: str) -> dict[str, Any]: ...


class _UndecidedError(Exception):
    """The server was asked and did not decide."""


async def _ask(rest: AdmissionAsker, chat_id: str, author: str) -> dict[str, Any] | str | None:
    """One ask: the server's answer, the refusal line for a refused asker,
    ``None`` for a server without the route; :class:`_UndecidedError` otherwise."""
    try:
        return await rest.send_admission(chat_id, user_id=author)
    except CloudApiError as exc:
        if (refused := _REFUSED_ASKER.get(exc.status)) is not None:
            logger.warning(
                "chat %s: the turn admission refused this box: %s",
                chat_id,
                exc,
                extra={"chat_id": chat_id},
            )
            return f"{NOT_RUN_PREFIX}: {refused}"
        if exc.status == 404:
            return None
        raise _UndecidedError(f"answered {exc.status}") from exc
    except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
        raise _UndecidedError(f"{type(exc).__name__}: {exc}") from exc


async def turn_refusal(
    rest: AdmissionAsker,
    chat_id: str,
    relay: Mapping[str, Any],
    *,
    owner_user_id: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    backoff: tuple[float, ...] = ADMISSION_BACKOFF_S,
) -> str | None:
    """The transcript line refusing (or holding) this message's turn, or
    ``None`` to run it."""
    author = relay.get("user_id")
    if not isinstance(author, str) or not author:
        author = owner_user_id or ""
        if not author:
            logger.warning(
                "chat %s: a relay names no author and the chat's owner is unknown; not run",
                chat_id,
                extra={"chat_id": chat_id},
            )
            return f"{NOT_RUN_PREFIX}: {NO_AUTHOR_DETAIL}"
    waits = (*backoff, None)
    for attempt, wait in enumerate(waits, start=1):
        try:
            answer = await _ask(rest, chat_id, author)
        except _UndecidedError as exc:
            logger.warning(
                "chat %s: the turn admission was not answered (ask %d of %d): %s",
                chat_id,
                attempt,
                len(waits),
                exc,
                extra={"chat_id": chat_id},
            )
            if wait is None:
                return f"{NOT_RUN_PREFIX}: {HELD_DETAIL}"
            await sleep(wait)
            continue
        break
    if answer is None:
        return None
    if isinstance(answer, str):
        return answer
    if answer.get("allowed") is not False:
        return None
    reason = answer.get("message")
    logger.info(
        "chat %s: a message from %s is not run: %s",
        chat_id,
        author,
        answer.get("code") or "send refused",
        extra={"chat_id": chat_id},
    )
    detail = reason if isinstance(reason, str) and reason else "its author may no longer send here"
    return f"{NOT_RUN_PREFIX}: {detail}"


__all__ = [
    "ADMISSION_BACKOFF_S",
    "HELD_DETAIL",
    "NOT_RUN_PREFIX",
    "NO_AUTHOR_DETAIL",
    "AdmissionAsker",
    "turn_refusal",
]
