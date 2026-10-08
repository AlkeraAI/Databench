"""Behavior contracts for one authenticated read-cache lane."""

from __future__ import annotations

import asyncio
import gc

import pytest
from alkera_cli.account.auth_read_cache import ReadThroughCache

_KEY = ("https://api.test", "token")


@pytest.mark.asyncio
async def test_cache_coalesces_reads_and_shields_the_shared_flight() -> None:
    """One cancelled waiter cannot cancel the fetch shared by another waiter."""
    cache = ReadThroughCache[str](cooldown_seconds=30.0)
    release = asyncio.Event()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "answer"

    cancelled = asyncio.create_task(cache.read(_KEY, fetch, ttl_seconds=60.0))
    surviving = asyncio.create_task(cache.read(_KEY, fetch, ttl_seconds=60.0))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()

    assert await surviving == "answer"
    assert await cache.read(_KEY, fetch, ttl_seconds=60.0) == "answer"
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_cools_failures_and_serves_stale_until_retry() -> None:
    """Cold failures return None; warm failures retain the last successful value."""
    now = [0.0]
    cache = ReadThroughCache[str](cooldown_seconds=30.0, clock=lambda: now[0])
    attempts: list[str] = []

    async def fail() -> str:
        attempts.append("fail")
        raise RuntimeError("offline")

    assert await cache.read(_KEY, fail, ttl_seconds=60.0) is None
    assert await cache.read(_KEY, fail, ttl_seconds=60.0) is None
    assert attempts == ["fail"]

    now[0] = 30.0

    async def recover() -> str:
        attempts.append("recover")
        return "known"

    assert await cache.read(_KEY, recover, ttl_seconds=60.0) == "known"
    now[0] = 92.0
    assert await cache.read(_KEY, fail, ttl_seconds=60.0) == "known"
    assert await cache.read(_KEY, fail, ttl_seconds=60.0) == "known"
    assert attempts == ["fail", "recover", "fail"]


@pytest.mark.asyncio
async def test_fetch_cancellation_cools_the_key_then_retries_at_expiry() -> None:
    """Fetch cancellation degrades like failure without cancelling its reader."""
    now = [0.0]
    cache = ReadThroughCache[str](cooldown_seconds=30.0, clock=lambda: now[0])
    attempts = 0

    async def cancel_fetch() -> str:
        nonlocal attempts
        attempts += 1
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        raise AssertionError("a cancelled fetch cannot resume")

    assert await cache.read(_KEY, cancel_fetch, ttl_seconds=60.0) is None
    assert await cache.read(_KEY, cancel_fetch, ttl_seconds=60.0) is None
    assert attempts == 1

    now[0] = 30.0

    async def recover() -> str:
        nonlocal attempts
        attempts += 1
        return "recovered"

    assert await cache.read(_KEY, recover, ttl_seconds=60.0) == "recovered"
    assert attempts == 2

    assert await cache.read(_KEY, cancel_fetch, ttl_seconds=0.0) == "recovered"
    assert await cache.read(_KEY, cancel_fetch, ttl_seconds=0.0) == "recovered"
    assert attempts == 3

    now[0] = 60.0
    assert await cache.read(_KEY, recover, ttl_seconds=0.0) == "recovered"
    assert attempts == 4


@pytest.mark.asyncio
async def test_simultaneous_reader_and_fetch_cancellation_propagates() -> None:
    """A cancelled fetch never hides cancellation requested on its reader."""
    cache = ReadThroughCache[str](cooldown_seconds=30.0)
    holder: list[asyncio.Task[str | None]] = []

    async def cancel_both() -> str:
        holder[0].cancel()
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        raise AssertionError("cancelled tasks cannot resume")

    reader = asyncio.create_task(cache.read(_KEY, cancel_both, ttl_seconds=60.0))
    holder.append(reader)

    with pytest.raises(asyncio.CancelledError):
        await reader


@pytest.mark.asyncio
async def test_detached_cancelled_flight_cannot_cool_its_successor() -> None:
    """Cancellation from a cleared generation cannot mutate the replacement lane."""
    cache = ReadThroughCache[str](cooldown_seconds=30.0)
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    fresh_calls = 0

    async def old_fetch() -> str:
        old_started.set()
        await release_old.wait()
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        raise AssertionError("cancelled tasks cannot resume")

    old_waiter = asyncio.create_task(cache.read(_KEY, old_fetch, ttl_seconds=60.0))
    await old_started.wait()
    cache.clear()

    async def fresh_fetch() -> str:
        nonlocal fresh_calls
        fresh_calls += 1
        return "fresh"

    assert await cache.read(_KEY, fresh_fetch, ttl_seconds=60.0) == "fresh"
    release_old.set()
    assert await old_waiter is None
    assert await cache.read(_KEY, fresh_fetch, ttl_seconds=0.0) == "fresh"
    assert fresh_calls == 2


@pytest.mark.asyncio
async def test_cancelled_sole_waiter_does_not_leak_a_later_fetch_failure() -> None:
    """A shielded orphan flight retrieves its exception after its only waiter leaves."""
    cache = ReadThroughCache[str](cooldown_seconds=30.0)
    started = asyncio.Event()
    release = asyncio.Event()
    leaked: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def record_ours(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        # The loop is the worker's for the whole session, and gc.collect() below
        # also finalizes whatever an earlier test on this worker left behind
        # (a harness adapter's pump task, once). Only this test's own flight is
        # the claim: a context about some other task is another test's leak.
        task = context.get("task") or context.get("future")
        code = getattr(getattr(task, "get_coro", lambda: None)(), "cr_code", None)
        if task is None or code is None or code.co_name in {"fail_later", "read", "_fetch"}:
            leaked.append(context)

    loop.set_exception_handler(record_ours)

    async def fail_later() -> str:
        started.set()
        await release.wait()
        raise RuntimeError("orphaned failure")

    try:
        reader = asyncio.create_task(cache.read(_KEY, fail_later, ttl_seconds=60.0))
        await started.wait()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert leaked == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_clear_detaches_an_old_success_from_a_fresh_successor() -> None:
    """A result started before clear answers its waiter but never overwrites the new lane."""
    cache = ReadThroughCache[str](cooldown_seconds=30.0)
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    fresh_calls = 0

    async def old_fetch() -> str:
        old_started.set()
        await release_old.wait()
        return "old"

    old_waiter = asyncio.create_task(cache.read(_KEY, old_fetch, ttl_seconds=60.0))
    await old_started.wait()
    cache.clear()

    async def fresh_fetch() -> str:
        nonlocal fresh_calls
        fresh_calls += 1
        return "fresh"

    assert await cache.read(_KEY, fresh_fetch, ttl_seconds=60.0) == "fresh"
    release_old.set()
    assert await old_waiter == "old"
    assert await cache.read(_KEY, fresh_fetch, ttl_seconds=60.0) == "fresh"
    assert fresh_calls == 1
