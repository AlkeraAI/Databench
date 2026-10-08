"""What ``/health/ready`` answers the load balancer, as opposed to what it found.

The load balancer's health check is the only readiness signal ECS has, and it
drives two things at once: whether a task receives traffic, and whether ECS
stops the task and starts a replacement. A probe that follows the database
second by second therefore turns a shared outage into a fleet replacement. When
the database is slow for every task at once, every task fails the check, ECS
drains them all, and the replacements cannot pass the same check on the same
database, so the outage gains a cold start and an empty target group and fixes
nothing.

What a replacement can fix is a fault of the task's own. A task that has never
been ready (a new revision that cannot reach the database, a tenant-isolation
canary that fails, a store it cannot open) is refused at once, which is what
lets the deployment circuit breaker roll a broken image back. A task that WAS
ready and then loses a dependency every task shares keeps answering 200 — with
``status: "degraded"`` in the body, so a caller reading the body still sees the
truth — until the failure has lasted longer than the grace window. Past that
the task is reported not ready, because a failure that long may be the task's
alone and a replacement is the one thing left to try.

The grace applies only to a dependency that could not be asked. A dependency
that answered and said no — the tenant-isolation canary naming a problem — is
never graced: that is a fact about the task's trustworthiness, not a blip.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.schemas.system.health import ReadyStatus

log = get_logger("alkera.health")


@dataclass
class ReadinessLatch:
    """Whether a failed probe is still answered as ready.

    ``grace_seconds`` is read on every call, so a deployment that retunes it —
    or a test that pins it — takes effect without a new instance. The clock is
    monotonic and read at call time (``None`` means ``time.monotonic``), so a
    wall-clock step cannot open or close the window, and a test can move it.
    """

    grace_seconds: Callable[[], float]
    clock: Callable[[], float] | None = None
    _last_ready: float | None = field(default=None, init=False)

    def _now(self) -> float:
        return self.clock() if self.clock is not None else time.monotonic()

    def ready(self) -> None:
        """Record a probe that found every dependency answering."""
        self._last_ready = self._now()

    def still_ready(self) -> bool:
        """Whether a probe that could not reach a dependency keeps the task in
        rotation: only after a ready probe, and only inside the grace window
        measured from it. A grace of zero or less is the strict probe."""
        if self._last_ready is None:
            return False
        return self._now() - self._last_ready < self.grace_seconds()

    @property
    def has_been_ready(self) -> bool:
        return self._last_ready is not None

    def reset(self) -> None:
        """Forget every probe: the next failure is refused. For a test that
        needs a process that has never been ready."""
        self._last_ready = None


def _configured_grace() -> float:
    return float(settings.health_ready_grace_seconds)


#: The process's latch. One process serves one app (the backend or the
#: gateway), and readiness is a fact about the process, so one per process.
probe_latch = ReadinessLatch(grace_seconds=_configured_grace)


#: The name of the database check. The body's ``db`` field reports it, because
#: every caller reads that field to tell "the database answered" apart from
#: "something after the database failed".
DATABASE = "database"

#: What a database that could not be asked tells the caller. This endpoint
#: needs no credentials and the load balancer routes to it on Host alone, so it
#: answers the whole internet. A driver's own words name the database host and
#: port, the database user and the failing statement, so the reason goes to the
#: logs and the caller gets a constant.
DATABASE_UNREACHABLE = "database unreachable"

#: Deadline for resetting the failed probe's transaction. The reset talks to the
#: same database that just failed, so a blackholed server would otherwise hang
#: the endpoint on the very connection it declared dead.
RESET_TIMEOUT_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class Unready:
    """What one failed check found.

    ``detail`` is a constant the caller may read: no host, bucket, user or
    driver message. ``shared`` says which kind of failure it is. ``True`` is a
    dependency every task shares that could not be asked, graced inside the
    latch's window; ``False`` is a fact about this task (the dependency
    answered and said no), never graced.
    """

    detail: str
    shared: bool


#: One check: given the probe's own session, ``None`` when the dependency is
#: fine, else what is wrong with it.
CheckRun = Callable[[AsyncSession], Awaitable[Unready | None]]


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One dependency readiness asks about. ``name`` keys the registry and the
    log line a failure leaves."""

    name: str
    run: CheckRun


class ReadinessChecks:
    """The checks one app's ``/health/ready`` runs, in the order it runs them.

    An app builds one and registers what it depends on (the database first,
    because every later check reads through it); a component that adds a
    dependency (a kernel pool, an object store) registers its own check rather
    than editing the probe. A name registers once.
    """

    def __init__(self) -> None:
        self._checks: dict[str, ReadinessCheck] = {}

    def register(self, check: ReadinessCheck) -> ReadinessCheck:
        if check.name in self._checks:
            raise ValueError(f"readiness check {check.name!r} is already registered")
        self._checks[check.name] = check
        return check

    def __iter__(self) -> Iterator[ReadinessCheck]:
        return iter(tuple(self._checks.values()))

    def __len__(self) -> int:
        return len(self._checks)


async def reset_failed_session(db: AsyncSession) -> None:
    """Return a failed probe's session to a committable state, or the caller's
    commit-on-exit raises over it and turns the 503 into an unhandled 500. Both
    steps are bounded. Past the deadline the connection is dropped best-effort
    rather than awaited."""
    bound = RESET_TIMEOUT_SECONDS
    try:
        await asyncio.wait_for(db.rollback(), timeout=bound)
    except Exception:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(db.invalidate(), timeout=bound)


def _configured_deadline() -> float:
    return float(settings.health_ready_timeout_seconds)


async def _ping_database(db: AsyncSession) -> Unready | None:
    """``SELECT 1`` inside the readiness deadline, read off the settings on
    every probe so a deployment that widens it needs no new image and a test can
    pin it. The backend and the gateway share it, so one slow database cannot
    make the two disagree about whether the deployment is healthy."""
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=_configured_deadline())
    except Exception as exc:
        await reset_failed_session(db)
        log.warning(
            "health.ready.failed",
            error=str(exc) or type(exc).__name__,
            error_type=type(exc).__name__,
            graced=probe_latch.still_ready(),
            exc_info=exc,
        )
        return Unready(DATABASE_UNREACHABLE, shared=True)
    return None


#: The database check every app registers first.
database_check = ReadinessCheck(DATABASE, _ping_database)


@dataclass(frozen=True, slots=True)
class Readiness:
    """The probe's answer: the status code the load balancer acts on and the
    body a reader acts on."""

    status_code: int
    body: ReadyStatus

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status_code, content=self.body.model_dump())


async def probe(
    checks: Sequence[ReadinessCheck] | ReadinessChecks,
    db: AsyncSession,
    *,
    byok: bool,
    strict: bool = False,
    latch: ReadinessLatch | None = None,
) -> Readiness:
    """Run ``checks`` in order on the probe's own session, stopping at the first
    that fails.

    Every check passing records the task as ready on the latch and answers 200
    ``ok``. A failure always reads ``degraded`` in the body; its status code is
    503, except for a shared dependency on a task that has been ready, which
    answers 200 while the latch's grace window lasts (see the module docstring).
    ``db`` in the body is ``ok`` once the database check has passed, so a
    reader can tell the database apart from what failed after it.

    ``strict`` drops the grace: any failure is a 503 at once. It is for an
    operator or a test asking whether every dependency answers right now; a
    load balancer keeps the default, because a shared outage must not replace
    the fleet.
    """
    held = latch if latch is not None else probe_latch
    passed: set[str] = set()
    for check in checks:
        found = await check.run(db)
        if found is None:
            passed.add(check.name)
            continue
        graced = found.shared and not strict and held.still_ready()
        code = status.HTTP_200_OK if graced else status.HTTP_503_SERVICE_UNAVAILABLE
        body = ReadyStatus(
            status="degraded", db="ok" if DATABASE in passed else None, detail=found.detail
        )
        return Readiness(code, body)
    held.ready()
    db_state = "ok" if DATABASE in passed else None
    return Readiness(status.HTTP_200_OK, ReadyStatus(status="ok", db=db_state, byok=byok))


__all__ = [
    "DATABASE",
    "DATABASE_UNREACHABLE",
    "RESET_TIMEOUT_SECONDS",
    "CheckRun",
    "Readiness",
    "ReadinessCheck",
    "ReadinessChecks",
    "ReadinessLatch",
    "Unready",
    "database_check",
    "probe",
    "probe_latch",
    "reset_failed_session",
]
