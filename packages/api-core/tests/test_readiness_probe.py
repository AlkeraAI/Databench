"""The shared readiness probe: which checks run, what the body says, and which
failures the grace window covers.

The checks here are plain functions, so what is pinned is the probe's own
rules; the real database check runs through both apps' route tests.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from alkera_core.readiness import (
    DATABASE,
    ReadinessCheck,
    ReadinessChecks,
    ReadinessLatch,
    Unready,
    probe,
)
from sqlalchemy.ext.asyncio import AsyncSession

#: The probe hands the session to each check and never touches it itself.
NO_SESSION = cast(AsyncSession, object())


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _check(name: str, found: Unready | None, ran: list[str]) -> ReadinessCheck:
    async def run(db: Any) -> Unready | None:
        ran.append(name)
        return found

    return ReadinessCheck(name, run)


def _latch(clock: _Clock, grace: float = 900.0) -> ReadinessLatch:
    return ReadinessLatch(grace_seconds=lambda: grace, clock=clock)


async def test_every_check_passing_is_ready_and_marks_the_latch() -> None:
    ran: list[str] = []
    latch = _latch(_Clock())
    checks = [_check(DATABASE, None, ran), _check("store", None, ran)]

    answer = await probe(checks, NO_SESSION, byok=True, latch=latch)

    assert answer.status_code == 200
    assert answer.body.model_dump() == {"status": "ok", "db": "ok", "detail": None, "byok": True}
    assert ran == [DATABASE, "store"]
    assert latch.has_been_ready


async def test_the_first_failure_stops_the_probe_and_names_only_itself() -> None:
    ran: list[str] = []
    latch = _latch(_Clock())
    checks = [
        _check(DATABASE, None, ran),
        _check("store", Unready("store unreachable", shared=True), ran),
        _check("later", None, ran),
    ]

    answer = await probe(checks, NO_SESSION, byok=True, latch=latch)

    assert answer.status_code == 503, "a task never ready is refused at once"
    assert answer.body.model_dump() == {
        "status": "degraded",
        "db": "ok",
        "detail": "store unreachable",
        "byok": False,
    }
    assert ran == [DATABASE, "store"]
    assert not latch.has_been_ready


async def test_a_failed_database_does_not_claim_the_database_is_fine() -> None:
    answer = await probe(
        [_check(DATABASE, Unready("database unreachable", shared=True), [])],
        NO_SESSION,
        byok=False,
        latch=_latch(_Clock()),
    )
    assert answer.body.db is None


@pytest.mark.parametrize(
    ("shared", "after", "expected"),
    [
        pytest.param(True, 899.0, 200, id="shared-inside-the-window-is-graced"),
        pytest.param(True, 900.0, 503, id="shared-past-the-window-is-refused"),
        pytest.param(False, 1.0, 503, id="a-fact-about-the-task-is-never-graced"),
    ],
)
async def test_the_grace_window_covers_only_a_shared_dependency(
    shared: bool, after: float, expected: int
) -> None:
    clock = _Clock()
    latch = _latch(clock)
    assert (
        await probe([_check("dep", None, [])], NO_SESSION, byok=False, latch=latch)
    ).status_code == 200

    clock.now += after
    answer = await probe(
        [_check("dep", Unready("down", shared=shared), [])], NO_SESSION, byok=False, latch=latch
    )

    assert answer.status_code == expected
    assert answer.body.status == "degraded"


def test_a_check_name_registers_once() -> None:
    checks = ReadinessChecks()
    checks.register(_check("store", None, []))
    with pytest.raises(ValueError, match="already registered"):
        checks.register(_check("store", None, []))
    assert [c.name for c in checks] == ["store"]


async def test_a_registered_check_runs_in_registration_order() -> None:
    ran: list[str] = []
    checks = ReadinessChecks()
    for name in (DATABASE, "canary", "kernels"):
        checks.register(_check(name, None, ran))

    await probe(checks, NO_SESSION, byok=False, latch=_latch(_Clock()))

    assert ran == [DATABASE, "canary", "kernels"]


async def test_strict_drops_the_grace_and_nothing_else() -> None:
    clock = _Clock()
    latch = _latch(clock)
    await probe([_check("dep", None, [])], NO_SESSION, byok=False, latch=latch)
    clock.now += 1.0
    failing = [_check("dep", Unready("down", shared=True), [])]

    lenient = await probe(failing, NO_SESSION, byok=False, latch=latch)
    strict = await probe(failing, NO_SESSION, byok=False, latch=latch, strict=True)
    passing = await probe(
        [_check("dep", None, [])], NO_SESSION, byok=False, strict=True, latch=latch
    )

    assert (lenient.status_code, strict.status_code, passing.status_code) == (200, 503, 200)
    assert strict.body == lenient.body
