"""A transport that remembers a host it could not connect to.

The box's Files client speaks to two hosts: the API, and the content origin
its redirects name. When the origin cannot be connected to from this box — a
security group, a proxy bound to the wrong interface, a hostname that resolves
to loopback — every folder pull that needs a byte fails the same way, and each
one paid the whole connect budget to learn it, one after another, holding a
take slot the while. The first failure is remembered here for a window, and a
request to that host inside it is refused at once with the same kind of error
and a line that says when the host was last tried; the window over, the host
is asked again, and an answer of any kind clears the mark.

Only a failure to CONNECT is remembered. A host that answered — with a refusal,
a timeout on the read, a broken stream — is reachable, and that is its
caller's news to act on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

__all__ = ["OutageAwareTransport"]


@dataclass(frozen=True, slots=True)
class _Outage:
    failed_at: float
    until: float
    why: str


class OutageAwareTransport(httpx.BaseTransport):
    """Wrap ``inner``; refuse for ``window`` seconds a host it could not reach."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        window: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._window = window
        self._clock = clock
        self._down: dict[str, _Outage] = {}
        # The Files client is driven from worker threads, one folder apiece.
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        now = self._clock()
        with self._lock:
            outage = self._down.get(host)
            if outage is not None and now >= outage.until:
                del self._down[host]
                outage = None
        if outage is not None:
            raise httpx.ConnectError(
                f"{host} could not be reached {now - outage.failed_at:.0f} s ago "
                f"({outage.why}); it is not asked again for another "
                f"{outage.until - now:.0f} s",
                request=request,
            )
        try:
            response = self._inner.handle_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            with self._lock:
                self._down[host] = _Outage(
                    failed_at=now, until=now + self._window, why=type(exc).__name__
                )
            raise
        with self._lock:
            self._down.pop(host, None)
        return response

    def seconds_left(self, host: str) -> float | None:
        """How long ``host`` is still refused for, or ``None`` when it is not."""
        with self._lock:
            outage = self._down.get(host)
        if outage is None:
            return None
        left = outage.until - self._clock()
        return left if left > 0 else None

    def close(self) -> None:
        self._inner.close()
