"""The cross-cutting job hardening: the overlap lock + the transient-error
classification the activity interceptor retries on.

The advisory-lock tests hit real Postgres on purpose — the whole point of the lock
is its cross-connection exclusivity, which an in-memory fake could never prove.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from alkera_core.db.locking import advisory_key
from structlog.testing import capture_logs
from worker.tasks._hardening import JobOverranError, advisory_lock, run_locked


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("compute_reaper", 1305480218, id="compute_reaper"),
        pytest.param("free_cycles", 1894936416, id="free_cycles"),
    ],
)
def test_a_job_claim_key_matches_the_shipped_derivation(name: str, expected: int) -> None:
    """A rolling deploy runs old and new workers together: a job's key moving
    would let an old and a new run of the same sweep overlap."""
    assert advisory_key("worker-job", name).value == expected


@pytest.mark.asyncio
async def test_advisory_lock_is_exclusive_across_connections() -> None:
    """A second acquirer (a different connection) is refused while the first holds it,
    and the name is acquirable again once released."""
    async with advisory_lock("test_exclusive") as first:
        assert first is True
        async with advisory_lock("test_exclusive") as second:
            assert second is False  # held by `first`
    async with advisory_lock("test_exclusive") as third:
        assert third is True  # released on exit


@pytest.mark.asyncio
async def test_advisory_lock_distinct_names_do_not_block() -> None:
    async with advisory_lock("test_name_a") as a, advisory_lock("test_name_b") as b:
        assert a is True
        assert b is True  # different keys → no contention


@pytest.mark.asyncio
async def test_run_locked_runs_body_then_skips_a_concurrent_run() -> None:
    ran: list[int] = []

    async def body() -> int:
        ran.append(1)
        return 42

    # While the lock is held, run_locked skips entirely (body never runs).
    async with advisory_lock("test_run_locked") as got:
        assert got is True
        skipped = await run_locked("test_run_locked", body)
    assert skipped is None
    assert ran == []

    # Once released, it runs and returns the body's result.
    result = await run_locked("test_run_locked", body)
    assert result == 42
    assert ran == [1]


class _ProviderTransientError(Exception):
    """A client's 5xx / rate-limit error, as a distribution's API client raises it."""


class _ProviderRefusedError(Exception):
    """The same client's 4xx error: a retry cannot clear it."""


def test_a_registered_error_class_is_retried_and_an_unregistered_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A distribution adds the transient errors of the APIs its jobs call by
    registering them in ``RETRYABLE_ERRORS``. Until it registers one, that
    error is poison; once registered, it and its subclasses are retried, and a
    sibling the distribution did not register stays poison."""
    from alkera_core.extensions import ExtensionPoint
    from worker.tasks import _hardening

    class _ProviderOverloadedError(_ProviderTransientError):
        pass

    # Fresh points, so the one the installed composition froze is untouched. A
    # point freezes when it is read, so each composition gets its own.
    unregistered: ExtensionPoint[type[Exception]] = ExtensionPoint("test.none_registered")
    monkeypatch.setattr(_hardening, "RETRYABLE_ERRORS", unregistered)
    assert _hardening.is_transient_error(_ProviderTransientError("502")) is False

    point: ExtensionPoint[type[Exception]] = ExtensionPoint("test.retryable_errors")
    point.register(_ProviderTransientError)
    monkeypatch.setattr(_hardening, "RETRYABLE_ERRORS", point)
    assert _hardening.is_transient_error(_ProviderTransientError("502")) is True
    assert _hardening.is_transient_error(_ProviderOverloadedError("503")) is True
    assert _hardening.is_transient_error(_ProviderRefusedError("404")) is False
    # The platform's own classes stay transient beside the registered ones.
    assert _hardening.is_transient_error(httpx.ConnectError("refused")) is True


def test_transient_classification_covers_network_errors() -> None:
    """A network-level failure is retried for free; the drain does not burn its
    dead-letter budget on a connection that dropped."""
    import httpx
    from worker.tasks._hardening import is_transient_error

    assert is_transient_error(httpx.ConnectError("connection refused"))


@pytest.mark.asyncio
async def test_a_run_past_its_budget_is_stopped_logged_and_lets_go_of_its_claim() -> None:
    """A run that hangs (a database waiting on its disk, a provider that never
    answers) is stopped at its budget and says so, and the next run of the
    same job is not turned away by a claim nothing is using any more."""
    started = asyncio.Event()

    async def hangs() -> int:
        started.set()
        await asyncio.Event().wait()
        return 1

    with capture_logs() as logs, pytest.raises(JobOverranError):
        await run_locked("test_overran", hangs, budget=timedelta(milliseconds=200))
    assert started.is_set()
    assert [entry["task"] for entry in logs if entry["event"] == "task.overran"] == ["test_overran"]
    async with advisory_lock("test_overran") as again:
        assert again is True


@pytest.mark.asyncio
async def test_a_run_inside_its_budget_returns_its_result() -> None:
    async def quick() -> int:
        return 7

    assert await run_locked("test_in_budget", quick, budget=timedelta(seconds=5)) == 7
