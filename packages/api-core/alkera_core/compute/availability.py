"""Whether a machine size can be started right now, per provider.

The provision picker offers only what can actually start, so the machine-type
listing carries a live answer per size:

- ``available`` — offered, and within what the account may still run;
- ``limited`` — offered, but the account's quota (EC2) or the provider's stock
  (RunPod ``LOW``) makes a start uncertain;
- ``unavailable`` — not offered in the region / zones, or out of stock;
- ``unknown`` — the provider could not be asked (no key, an error, a timeout).

Each provider answers for all its sizes in one call. Answers are cached for
:data:`CACHE_SECONDS` per process, and a provider that does not answer within
:data:`TIMEOUT_SECONDS` yields ``unknown`` with the reason, so the listing is
never held up by a slow provider.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

AvailabilityStatus = Literal["available", "limited", "unavailable", "unknown"]

CACHE_SECONDS = 60.0
TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class Quota:
    vcpu_limit: int
    vcpu_in_use: int

    @property
    def vcpu_available(self) -> int:
        return max(0, self.vcpu_limit - self.vcpu_in_use)


@dataclass(frozen=True)
class Availability:
    status: AvailabilityStatus
    detail: str
    checked_at: datetime
    quota: Quota | None = None


@dataclass(frozen=True)
class SizeQuery:
    code: str
    vcpu: int


AvailabilityProbe = Callable[[list[SizeQuery]], Awaitable[dict[str, Availability]]]


@dataclass
class _Entry:
    expires: float
    answers: dict[str, Availability]


@dataclass
class AvailabilityCache:
    """Per-process cache of each provider kind's last answer."""

    ttl: float = CACHE_SECONDS
    timeout: float = TIMEOUT_SECONDS
    clock: Callable[[], float] = time.monotonic
    _entries: dict[str, _Entry] = field(default_factory=dict)

    async def lookup(
        self, kind: str, sizes: list[SizeQuery], probe: AvailabilityProbe | None
    ) -> dict[str, Availability]:
        entry = self._entries.get(kind)
        now = self.clock()
        if (
            entry is not None
            and entry.expires > now
            and all(s.code in entry.answers for s in sizes)
        ):
            return entry.answers
        if probe is None:
            return {s.code: unknown("This provider reports no availability") for s in sizes}
        try:
            answers = await asyncio.wait_for(probe(sizes), timeout=self.timeout)
        except TimeoutError:
            return {
                s.code: unknown(f"The provider did not answer within {self.timeout:g} s")
                for s in sizes
            }
        except Exception as exc:  # any provider failure reads as "could not ask"
            return {s.code: unknown(f"The provider could not be asked: {exc}") for s in sizes}
        for s in sizes:
            answers.setdefault(s.code, unknown("The provider did not mention this size"))
        self._entries[kind] = _Entry(expires=now + self.ttl, answers=answers)
        return answers

    def clear(self) -> None:
        self._entries.clear()


def unknown(detail: str) -> Availability:
    return Availability(status="unknown", detail=detail, checked_at=datetime.now(UTC))


def from_quota(*, offered: bool, vcpu: int, quota: Quota | None) -> Availability:
    """The EC2 rule: not offered is unavailable; more vCPUs than the quota has
    left is limited; otherwise available."""
    moment = datetime.now(UTC)
    if not offered:
        return Availability("unavailable", "not offered in this region's zones", moment, quota)
    if quota is not None and vcpu > quota.vcpu_available:
        return Availability(
            "limited",
            f"needs {vcpu} vCPUs; {quota.vcpu_available} of {quota.vcpu_limit} left",
            moment,
            quota,
        )
    return Availability("available", "", moment, quota)


def from_stock(stock: str) -> Availability:
    """The RunPod rule over the catalog's stock word."""
    moment = datetime.now(UTC)
    word = (stock or "").upper()
    if word in ("HIGH", "MEDIUM"):
        return Availability("available", f"stock {word.lower()}", moment)
    if word == "LOW":
        return Availability("limited", "stock low", moment)
    if word == "NONE":
        return Availability("unavailable", "out of stock", moment)
    return Availability("unknown", "the provider reports no stock for this size", moment)


CACHE = AvailabilityCache()
"""The process-wide cache the listing reads through."""

__all__ = [
    "CACHE",
    "CACHE_SECONDS",
    "TIMEOUT_SECONDS",
    "Availability",
    "AvailabilityCache",
    "AvailabilityProbe",
    "AvailabilityStatus",
    "Quota",
    "SizeQuery",
    "from_quota",
    "from_stock",
    "unknown",
]
