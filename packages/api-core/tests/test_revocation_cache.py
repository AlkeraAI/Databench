"""Unit tests for the in-process revocation cache.

A stub session answers the per-jti lookup so we can exercise the caching,
staleness, write-through and bounding logic deterministically, with no DB and
no real-time sleeps.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from alkera_core.auth.revocation import _CACHE_MAX_ENTRIES, _RevocationCache


class _StubResult:
    def __init__(self, hit: bool) -> None:
        self._hit = hit

    def first(self) -> tuple[str] | None:
        return ("row",) if self._hit else None


class _StubSession:
    """Stand-in for the AsyncSession.execute the per-jti lookup calls.

    Answers from a set of revoked jtis, and counts queries so a test can assert
    the cache actually elides them."""

    def __init__(self, revoked: set[str]) -> None:
        self.revoked = revoked
        self.calls = 0
        self.queried: list[str] = []

    async def execute(self, stmt: Any) -> _StubResult:
        self.calls += 1
        # The jti is the first bound literal in the WHERE clause.
        params = stmt.compile().params
        jti = next(v for v in params.values() if isinstance(v, str))
        self.queried.append(jti)
        return _StubResult(jti in self.revoked)


async def test_revoked_jti_is_reported_and_then_cached() -> None:
    cache = _RevocationCache()
    db = _StubSession({"a"})
    assert await cache.is_revoked(db, "a") is True  # type: ignore[arg-type]
    assert db.calls == 1
    assert await cache.is_revoked(db, "a") is True  # type: ignore[arg-type]
    assert db.calls == 1  # a revoke is one-way — never re-queried


async def test_live_jti_is_cached_within_the_ttl() -> None:
    cache = _RevocationCache()
    db = _StubSession(set())
    for _ in range(3):
        assert await cache.is_revoked(db, "a") is False  # type: ignore[arg-type]
    assert db.calls == 1


async def test_live_jti_is_rechecked_after_the_ttl() -> None:
    cache = _RevocationCache()
    db = _StubSession(set())
    assert await cache.is_revoked(db, "a") is False  # type: ignore[arg-type]
    assert db.calls == 1
    # Simulate the TTL elapsing; another worker revoked "a" meanwhile.
    cache._live["a"] = time.monotonic() - 3600
    db.revoked.add("a")
    assert await cache.is_revoked(db, "a") is True  # type: ignore[arg-type]
    assert db.calls == 2


async def test_lookup_is_scoped_to_the_asked_jti() -> None:
    """The cost of the check must not scale with how many tokens the whole
    deployment has revoked — the query names exactly one jti."""
    cache = _RevocationCache()
    db = _StubSession({"other-1", "other-2", "other-3"})
    assert await cache.is_revoked(db, "mine") is False  # type: ignore[arg-type]
    assert db.queried == ["mine"]


async def test_mark_revoked_is_immediate_write_through() -> None:
    cache = _RevocationCache()
    db = _StubSession(set())
    assert await cache.is_revoked(db, "x") is False  # type: ignore[arg-type]  # prime as live
    assert db.calls == 1
    cache.mark_revoked("x")
    # Visible at once, without waiting for (or triggering) another lookup.
    assert await cache.is_revoked(db, "x") is True  # type: ignore[arg-type]
    assert db.calls == 1


async def test_reset_forces_a_fresh_lookup() -> None:
    cache = _RevocationCache()
    db = _StubSession({"a"})
    await cache.is_revoked(db, "a")  # type: ignore[arg-type]
    assert db.calls == 1
    cache.reset()
    assert await cache.is_revoked(db, "a") is True  # type: ignore[arg-type]
    assert db.calls == 2


@pytest.mark.parametrize(
    ("populate", "attr"),
    [
        pytest.param("revoked", "_revoked", id="revoked-map"),
        pytest.param("live", "_live", id="live-map"),
    ],
)
async def test_cache_is_bounded(populate: str, attr: str) -> None:
    """Neither map may grow without bound: a free account can mint + revoke
    tokens all day, and this cache is shared by every request in the process."""
    cache = _RevocationCache()
    db = _StubSession(set())
    overflow = _CACHE_MAX_ENTRIES + 500
    for i in range(overflow):
        jti = f"j{i}"
        if populate == "revoked":
            cache.mark_revoked(jti)
        else:
            await cache.is_revoked(db, jti)  # type: ignore[arg-type]
    assert len(getattr(cache, attr)) == _CACHE_MAX_ENTRIES


async def test_eviction_falls_back_to_the_db_not_to_a_wrong_answer() -> None:
    """An evicted entry must be re-resolved against Postgres — the bound is only
    safe because the DB stays authoritative."""
    cache = _RevocationCache()
    db = _StubSession({"first"})
    assert await cache.is_revoked(db, "first") is True  # type: ignore[arg-type]
    for i in range(_CACHE_MAX_ENTRIES + 1):
        cache.mark_revoked(f"filler-{i}")
    assert "first" not in cache._revoked  # evicted as least-recently-used
    assert await cache.is_revoked(db, "first") is True  # type: ignore[arg-type]  # re-read
