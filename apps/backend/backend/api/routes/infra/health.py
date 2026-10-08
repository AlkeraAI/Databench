"""Health endpoints — liveness is cheap; readiness pings the DB; info exposes build."""

from __future__ import annotations

import asyncio
from typing import Annotated

from alkera_core.config import settings
from alkera_core.db.session import get_health_db, report_pool_level
from alkera_core.deployment_health import probe_files_store
from alkera_core.entitlements import byok_active
from alkera_core.logging import get_logger
from alkera_core.readiness import (
    ReadinessCheck,
    ReadinessChecks,
    Unready,
    database_check,
    probe,
    probe_latch,
    reset_failed_session,
)
from alkera_core.schemas.system.health import InfoResponse, LiveStatus, ReadyStatus
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.files.store import store_factory
from backend.services.infra import row_security_canary

log = get_logger("alkera.health")

router = APIRouter(prefix="/health", tags=["health"])

#: What a failed store probe tells the caller. Named separately from the
#: database's so an orchestrator's logs say which dependency pulled the task out
#: of rotation, while still carrying no endpoint, bucket or credential.
_STORE_DEGRADED_DETAIL = "files object store unreachable"


@router.get("/live", response_model=LiveStatus)
async def live() -> LiveStatus:
    """Liveness — the process is up. Always 200."""
    return LiveStatus(status="ok")


async def _files_store_ready() -> bool:
    """Whether the object store answered — and True when this process holds no
    handle to ask with.

    Files can be switched on before its store settings are complete (no bucket,
    a provider whose credentials are missing), and building the factory then
    raises. Letting that escape answers an orchestrator with a 500 and pulls an
    instance whose entire non-Files surface is healthy, over a dependency this
    process was never handed. Readiness does not judge what it cannot ask: the
    misconfiguration is named in the log, and the deployment-health check is
    where an operator reads it as a failure.
    """
    try:
        store = store_factory().admin()
    except Exception as exc:
        log.warning(
            "health.ready.files_store_unconfigured",
            error=str(exc) or type(exc).__name__,
            error_type=type(exc).__name__,
        )
        return True
    return await probe_files_store(store)


async def _row_security_problems(db: AsyncSession) -> list[str]:
    """What the tenant-isolation canary says, bounded by the readiness
    deadline. A canary that could not answer is a failure, not a pass: an
    unread fact is not a trusted one."""
    try:
        return await asyncio.wait_for(
            row_security_canary.check(db), timeout=settings.health_ready_timeout_seconds
        )
    except Exception as exc:
        await reset_failed_session(db)
        log.error(
            "row_security.canary.unanswered",
            error=str(exc) or type(exc).__name__,
            error_type=type(exc).__name__,
        )
        return [f"the canary could not run: {type(exc).__name__}"]


async def _row_security(db: AsyncSession) -> Unready | None:
    """The database answers, but tenant isolation on it cannot be trusted, so
    this task must not serve tenants. The problems themselves are in the log.
    Never graced: this is a fact about the task, not a dependency's blip."""
    if await _row_security_problems(db):
        return Unready(row_security_canary.FAILED_DETAIL, shared=False)
    return None


async def _files_store(db: AsyncSession) -> Unready | None:
    """Readiness is deliberately all-or-nothing here, and the split it enforces
    is: an instance out of rotation stops taking *new* work — every content
    upload and download — which is what "fail fast" means for a store that
    cannot serve bytes. What keeps working meanwhile is everything that never
    touches the store: the whole metadata surface (listing, rename, move,
    trash, permissions) and reads of inline bytes, which live in Postgres.
    Those are served by the instances still in rotation, and by this one for as
    long as the load balancer keeps sending it traffic — nothing here refuses
    them. The store is shared by every task, so a task that has been ready
    rides out the grace window before it is reported not ready."""
    if not settings.files_enabled or await _files_store_ready():
        return None
    log.warning("health.ready.files_store_unavailable", graced=probe_latch.still_ready())
    return Unready(_STORE_DEGRADED_DETAIL, shared=True)


#: What the backend's readiness asks, in order: the database, the
#: tenant-isolation canary that reads through it, and the Files object store.
READINESS = ReadinessChecks()
READINESS.register(database_check)
READINESS.register(ReadinessCheck("row_security", _row_security))
READINESS.register(ReadinessCheck("files_store", _files_store))


@router.get("/ready", response_model=ReadyStatus)
async def ready(
    db: AsyncSession = Depends(get_health_db, scope="function"),
    strict: Annotated[bool, Query()] = False,
) -> JSONResponse:
    """Readiness — we can talk to the database. 503 if not, once past the grace
    window a task that has been ready is given for a dependency every task
    shares; the body reads ``degraded`` from the first failed probe.

    Asked on a connection of the probe's own, so the answer is about the
    database and not about how busy the tenants' pool is: a task whose pool is
    drained is still in rotation, shedding with retryable 503s, rather than
    pulled by the load balancer so its neighbours drain in turn. The pool's
    level is reported from here as well, because a fully stuck pool has no
    checkout left to report it.

    ``?strict=1`` answers 503 the moment any dependency is unreachable, with
    no grace window: for operators and checks that read the truth now, never
    for a load balancer.
    """
    report_pool_level()
    return (await probe(READINESS, db, byok=byok_active(), strict=strict)).response()


@router.get("/info", response_model=InfoResponse)
async def info() -> InfoResponse:
    """Build introspection — surfaces version, build id, and runtime env so
    ops can verify which image is live without needing credentials.
    """
    return InfoResponse(
        app="alkera-backend",
        version=settings.app_version,
        build_id=settings.build_id,
        env=settings.app_env,
    )
