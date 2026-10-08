"""A transport that puts a whole-request deadline on every call.

``httpx``'s timeouts are per operation: a connect, one socket write, one
socket read. A server that answers one byte every hundred seconds never trips a
two-minute read timeout, and a call that retries on its own multiplies whatever
it waited. A box whose folder pull waited like that held its take slot, and the
chat behind it, for as long as the server cared to dribble.

Every request through :class:`DeadlineTransport` either finishes — status,
headers and body — or fails with :class:`httpx.TimeoutException` inside a bound
fixed when it was sent. The operations are capped so that waiting for a
connection, connecting, one write and the read of the headers cannot together
outlive the budget (half of it for the read, a fifth each to connect and to
write, a tenth to wait for a pooled connection), and the body is cut off at
the deadline between chunks; the worst case is the deadline plus one capped
read.

A transfer is paced rather than cut off: a body that is large (said by its
``Content-Length``, either way) earns time in proportion to its size at a floor
rate, so a large file on a slow but moving link finishes. The per-operation
caps do not grow with it, so a transfer that stops moving still fails on the
first read or write that waits past its cap.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Final

import httpx

__all__ = ["MIN_TRANSFER_BYTES_PER_SECOND", "DeadlineTransport"]

#: The slowest a transfer may move and still be given time for its size.
MIN_TRANSFER_BYTES_PER_SECOND: Final = 64 * 1024

#: Each operation's share of the budget; together they make the whole of it.
_SHARES: Final = {"pool": 1 / 10, "connect": 1 / 5, "write": 1 / 5, "read": 1 / 2}


def _length(headers: httpx.Headers) -> int:
    try:
        return max(int(headers.get("content-length", "0")), 0)
    except ValueError:
        return 0


class _DeadlineStream(httpx.SyncByteStream):
    """A response body that refuses to be read past its deadline."""

    def __init__(
        self,
        inner: httpx.SyncByteStream,
        *,
        deadline: float,
        request: httpx.Request,
        clock: Callable[[], float],
    ) -> None:
        self._inner = inner
        self._deadline = deadline
        self._request = request
        self._clock = clock

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            if self._clock() > self._deadline:
                raise httpx.ReadTimeout(
                    f"{self._request.method} {self._request.url.path} passed its whole-request "
                    "deadline while its body was still arriving",
                    request=self._request,
                )
            yield chunk

    def close(self) -> None:
        self._inner.close()


class DeadlineTransport(httpx.BaseTransport):
    """Wrap ``inner``; no request outlives ``total`` seconds (plus its pacing)."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        total: float,
        min_rate: float = MIN_TRANSFER_BYTES_PER_SECOND,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if total <= 0:
            raise ValueError("a whole-request deadline must be positive")
        self._inner = inner
        self._total = total
        self._min_rate = min_rate
        self._clock = clock

    def _budget(self, sent: int) -> float:
        return self._total + sent / self._min_rate

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        started = self._clock()
        budget = self._budget(_length(request.headers))
        timeouts = dict(request.extensions.get("timeout") or {})
        for operation, share in _SHARES.items():
            cap = self._total * share
            configured = timeouts.get(operation)
            timeouts[operation] = cap if configured is None else min(configured, cap)
        request.extensions = {**request.extensions, "timeout": timeouts}
        response = self._inner.handle_request(request)
        # The body earns its own time once its size is known: a large download
        # is paced, never cut off at the budget of a JSON answer.
        deadline = started + budget + _length(response.headers) / self._min_rate
        if self._clock() > deadline:
            response.close()
            raise httpx.ReadTimeout(
                f"{request.method} {request.url.path} answered after its whole-request deadline",
                request=request,
            )
        stream = response.stream
        if isinstance(stream, httpx.SyncByteStream):
            response.stream = _DeadlineStream(
                stream, deadline=deadline, request=request, clock=self._clock
            )
        return response

    def close(self) -> None:
        self._inner.close()
