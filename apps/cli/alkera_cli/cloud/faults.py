"""Whether a failure the box met is a passing fault: the one rule every
caller that waits, retries or reports reads, so a 503 is not a passing fault
on one path and a verdict on the next.

The same rule decides what a reader is told. A condition that heals by itself
(the drive answering 503 during the burst a new chat causes, the chat's
document not declared yet) is waited out with a doubling backoff and never put
on the chat as a refusal: the chat reads as starting or waking, its composer
stays open, and a message ends the wait. Only a verdict, or a passing fault
that has outlasted :data:`TRANSIENT_LIMIT` tries in a row, is said as one.

A refusal the server spelled out (404, 403, 409, 422) is the same refusal on
the next attempt. A connection that was refused, a timeout, a 5xx and a 429
are the API being briefly unavailable, which is exactly what a restart or a
burst of load looks like from here.
"""

from __future__ import annotations

from typing import Literal

import httpx
from alkera_sdk.client import AlkeraHTTPError

from alkera_cli.cloud.rest import CloudApiError


def _passing_status(status: int) -> bool:
    return status >= 500 or status == 429


def passing_fault(exc: BaseException) -> bool:
    """Whether trying again unchanged could plausibly work."""
    if isinstance(exc, CloudApiError | AlkeraHTTPError):
        return _passing_status(exc.status)
    if isinstance(exc, httpx.HTTPStatusError):
        return _passing_status(exc.response.status_code)
    return isinstance(exc, httpx.HTTPError | OSError)


#: A refusal's kind: ``transient`` heals by itself, ``verdict`` does not.
RefusalKind = Literal["transient", "verdict"]
#: Gateway refusal codes that heal by the next poll: the chat's document is
#: not declared yet, or the hello did not come back inside its window during
#: a reconnect.
TRANSIENT_CODES = frozenset({"not_found", "timeout"})
#: Tries in a row after which a passing fault is said on the chat after all.
#: With the doubling wait from the poll interval that is a few minutes: long
#: enough that the burst a new chat causes is over, short enough that a drive
#: this box can never reach is not hidden behind "Starting" for good.
TRANSIENT_LIMIT = 4


def refusal_kind(cause: str | BaseException) -> RefusalKind:
    """The kind of a refusal: a gateway code, or the exception a take or a
    start ended in."""
    if isinstance(cause, str):
        return "transient" if cause in TRANSIENT_CODES else "verdict"
    return "transient" if passing_fault(cause) else "verdict"


def says_refused(kind: RefusalKind, count: int) -> bool:
    """Whether the ``count``-th failure in a row of this kind is put on the
    chat as a refusal."""
    return kind == "verdict" or count >= TRANSIENT_LIMIT


__all__ = [
    "TRANSIENT_CODES",
    "TRANSIENT_LIMIT",
    "RefusalKind",
    "passing_fault",
    "refusal_kind",
    "says_refused",
]
