"""The in-memory lease cache both credential lanes share.

One shape: a module-level dict keyed
``"<caller-identity-digest>:<record_id>[@<generation>]"`` maps to a leased value
and its serve-until epoch. The identity digest keeps a logout or an account
switch on the same machine from serving the previous identity's lease; the
optional generation keeps a rotation from serving the retired secret. Each lane
owns its dict (tests isolate them per-module) and its
:class:`LeasePolicy`; the check → fetch → store flow and the
invalidate-by-record matching live here, once.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

#: One lock for every lane's dict — lease traffic is a handful of fetches a
#: minute, and one lock keeps the shape one thing.
_lock = threading.Lock()

_LeaseValue = TypeVar("_LeaseValue")


@dataclass(frozen=True)
class LeaseEntry(Generic[_LeaseValue]):
    """One leased value and the epoch second through which it may be served."""

    value: _LeaseValue
    serve_until: float


_epochs: dict[int, int] = {}
"""Per-cache invalidation counter, keyed by the dict's identity. A fetch reads
it before releasing the lock and refuses to store against a bumped value."""


@dataclass(frozen=True)
class LeasePolicy:
    """How long a lane serves a lease from memory.

    ``reuse_seconds`` caps reuse well under the lease's own window, so
    staleness after a server-side revoke is minutes, not the full lease.
    ``expiry_skew_seconds`` refuses to serve this close to the reported
    expiry, so a credential never dies mid-query."""

    reuse_seconds: float
    expiry_skew_seconds: float


def _lease_expiry(value: object) -> float | None:
    """Parse an optional ISO lease expiry without rejecting an older server."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def lease_through_cache(
    cache: dict[str, LeaseEntry[_LeaseValue]],
    key: str,
    fetch: Callable[[], tuple[_LeaseValue, float | None]],
    policy: LeasePolicy,
) -> _LeaseValue:
    """Return a fresh cached value or fetch and cache it for the allowed window."""
    now = time.time()
    with _lock:
        hit = cache.get(key)
        if hit is not None and now < hit.serve_until:
            return hit.value
        epoch = _epochs.get(id(cache), 0)
    value, expires_at = fetch()
    serve_until = now + policy.reuse_seconds
    if expires_at is not None:
        serve_until = min(serve_until, expires_at - policy.expiry_skew_seconds)
    if serve_until > now:
        with _lock:
            # An invalidation during the fetch above wins: storing now would
            # put a just-removed credential back in memory for the whole reuse
            # window, which is the one thing invalidation promises it cannot do.
            if _epochs.get(id(cache), 0) == epoch:
                cache[key] = LeaseEntry(value, serve_until)
    return value


def invalidate_record(cache: dict[str, LeaseEntry[_LeaseValue]], record_id: str) -> None:
    """Drop every cached lease for this record, across identities and
    generations — a cached credential must not outlive the thing it belongs
    to. Matches both key grammars: ``…:<id>`` and ``…:<id>@<generation>``."""
    plain = f":{record_id}"
    generation = f":{record_id}@"
    with _lock:
        _epochs[id(cache)] = _epochs.get(id(cache), 0) + 1
        for key in [k for k in cache if k.endswith(plain) or generation in k]:
            cache.pop(key, None)


__all__ = ["LeaseEntry", "LeasePolicy", "invalidate_record", "lease_through_cache"]
