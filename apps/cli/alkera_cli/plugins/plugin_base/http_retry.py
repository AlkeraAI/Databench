"""Bounded 429 retry for vendor REST clients.

A lineage pass treats any transient failure as pass-aborting (a thinned listing
must never reconcile as an authoritative snapshot), so a single rate throttle
would otherwise cost the whole pass. A 429 gets a short bounded retry: honor
the server's ``Retry-After`` when parseable (capped, so one throttle can't
stall a pass for minutes), fall back to a small default, and give up after
``MAX_429_RETRIES`` so exhaustion still surfaces as the transient failure it
is. The sleep is injectable so tests pin the backoff.

The prose helpers below it keep every vendor client's refusal message carrying
the same two things: what the vendor said, bounded, and what httpx failed with.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from alkera_cli.host.backoff import parse_retry_after

MAX_429_RETRIES = 2
RETRY_AFTER_CAP_SECONDS = 30.0
RETRY_AFTER_DEFAULT_SECONDS = 5.0


def retry_after_seconds(
    resp: httpx.Response,
    *,
    cap: float = RETRY_AFTER_CAP_SECONDS,
    default: float = RETRY_AFTER_DEFAULT_SECONDS,
) -> float:
    """The pause before a 429 retry: the server's ``Retry-After`` delta-seconds
    when parseable and non-negative, else ``default``; always capped."""
    seconds = parse_retry_after(resp.headers.get("Retry-After"))
    return default if seconds is None else min(seconds, cap)


async def send_with_429_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int = MAX_429_RETRIES,
) -> httpx.Response:
    """Run ``send`` with the bounded 429 backoff. Returns the final response.
    The caller keeps its own status handling, so a still-429 after exhaustion
    surfaces through the client's normal error path."""
    for attempt in range(max_retries + 1):
        resp = await send()
        if resp.status_code != 429 or attempt == max_retries:
            return resp
        await sleep(retry_after_seconds(resp))
    return resp


def send_with_429_retry_sync(
    send: Callable[[], httpx.Response],
    *,
    sleep: Callable[[float], None],
    max_retries: int = MAX_429_RETRIES,
) -> httpx.Response:
    """The blocking twin of :func:`send_with_429_retry`, for the knowledge
    clients that read their vendors from a worker thread."""
    for attempt in range(max_retries + 1):
        resp = send()
        if resp.status_code != 429 or attempt == max_retries:
            return resp
        sleep(retry_after_seconds(resp))
    return resp


#: How much of a vendor's own answer rides along on an error. A refusal is normally
#: a short JSON object, but a proxy in front of the vendor can answer 5xx with a
#: whole HTML page, and this string lands on the job row as one line.
_VENDOR_WORDS = 500


def vendor_said(resp: httpx.Response) -> str:
    """The vendor's answer as one bounded line, for the error a refusal raises."""
    body = " ".join(resp.text.split())
    return body if len(body) <= _VENDOR_WORDS else f"{body[:_VENDOR_WORDS]}..."


def failure_cause(exc: Exception) -> str:
    """The httpx failure, kept for whoever has to diagnose it. A timeout carries no
    words of its own, so the class name is all there is to show."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


__all__ = [
    "MAX_429_RETRIES",
    "RETRY_AFTER_CAP_SECONDS",
    "RETRY_AFTER_DEFAULT_SECONDS",
    "failure_cause",
    "retry_after_seconds",
    "send_with_429_retry",
    "send_with_429_retry_sync",
    "vendor_said",
]
