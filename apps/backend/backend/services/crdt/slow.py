"""How the lane answers sandbox work that outlived its budget: busy, never a verdict.

A timeout says the host was slow (a loaded machine, a worker paying a cold
cost), not that the bytes were bad, so it may never refuse, poison or count
against whoever sent them. What it may do is cost time: the same work timing
out again is told to wait longer each time, and until that wait has passed
it is answered busy without reaching a worker, so one slow request cannot
have the slot's worker killed over and over (every kill drops the cache of
every document on the slot).

Work is keyed by what makes it the same work: an update's digest, a load's
document position, otherwise the document and the operation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from alkera_core.logging import get_logger

from backend.services.crdt.errors import CrdtError
from backend.services.crdt.sandbox.pool import SandboxTimeoutError

log = get_logger(__name__)

_T = TypeVar("_T")

#: The first wait after a timeout, doubled for each further one.
FIRST_WAIT_MS = 1000
#: The longest wait a timeout asks for.
MAX_WAIT_MS = 30_000
#: How long after its wait ends a timed-out key is remembered: its next
#: timeout within that time waits longer than its last.
REMEMBER_SECONDS = 600.0
#: The refusal reason of work that outlived its budget, by the sandbox
#: operation (``sandbox_timeout`` for any other).
TIMEOUT_REASONS: Mapping[str, str] = {"validate": "validator_timeout", "load": "load_timeout"}


_BUSY = "the sandbox is busy; send again shortly"


@dataclass(slots=True)
class _Slow:
    timeouts: int
    until: float


@dataclass
class SlowWork:
    """Timeouts by the work that had them. ``clock`` is in seconds."""

    clock: Callable[[], float]
    first_wait_ms: int = FIRST_WAIT_MS
    max_wait_ms: int = MAX_WAIT_MS
    remember_seconds: float = REMEMBER_SECONDS
    _seen: dict[Hashable, _Slow] = field(default_factory=dict, init=False)

    async def send(
        self,
        key: Hashable,
        request: Callable[[], Awaitable[_T]],
        *,
        op: str,
        doc: str,
        budget: float,
        background: bool = False,
    ) -> _T:
        """``request``'s answer. Work still waiting out an earlier timeout is
        answered ``crdt_busy`` without being sent; work that outlives its
        budget (:class:`SandboxTimeoutError`) is answered ``crdt_busy`` with
        a longer wait than its last. Neither is ever a refusal.

        ``background`` work (a pass that holds nothing and nobody waits on)
        is sent even while ``key`` waits, and its timeout tells no one to
        wait longer; answered in time, it ends the wait for everyone."""
        reason = TIMEOUT_REASONS.get(op, "sandbox_timeout")
        waiting = 0 if background else self.wait_ms(key)
        if waiting:
            raise CrdtError("crdt_busy", _BUSY, reason=reason, retry_after_ms=waiting)
        try:
            answer = await request()
        except SandboxTimeoutError as exc:
            if background:
                raise CrdtError("crdt_busy", _BUSY, reason=reason, retry_after_ms=0) from exc
            wait = self.timed_out(key)
            log.warning(
                "crdt.sandbox.request_timed_out", doc=doc, op=op, budget=budget, retry_after_ms=wait
            )
            raise CrdtError("crdt_busy", _BUSY, reason=reason, retry_after_ms=wait) from exc
        self.answered(key)
        return answer

    def wait_ms(self, key: Hashable) -> int:
        """How much longer ``key`` must wait before it is tried again; 0 when
        it may be tried now."""
        now = self.clock()
        self._forget(now)
        slow = self._seen.get(key)
        if slow is None or slow.until <= now:
            return 0
        return max(1, int((slow.until - now) * 1000))

    def timed_out(self, key: Hashable) -> int:
        """Record a timeout of ``key``; the wait it is told, in ms."""
        now = self.clock()
        self._forget(now)
        slow = self._seen.get(key)
        timeouts = 1 if slow is None else slow.timeouts + 1
        wait: int = min(self.max_wait_ms, self.first_wait_ms * (1 << min(timeouts - 1, 16)))
        self._seen[key] = _Slow(timeouts=timeouts, until=now + wait / 1000)
        return wait

    def answered(self, key: Hashable) -> None:
        """``key`` was answered in time: its next timeout starts over."""
        self._seen.pop(key, None)

    def _forget(self, now: float) -> None:
        stale = [k for k, s in self._seen.items() if s.until + self.remember_seconds <= now]
        for key in stale:
            del self._seen[key]


__all__ = ["FIRST_WAIT_MS", "MAX_WAIT_MS", "REMEMBER_SECONDS", "TIMEOUT_REASONS", "SlowWork"]
