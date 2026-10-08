"""Observability endpoints — unified under /api/v1/errors.

- POST /reports  — submit an opt-in crash report (stored + Sentry-forwarded).
- GET  /reports  — list the caller's own crash reports.
- POST /events   — passively report a client error (logged only, no DB row).

All authed (a valid session cookie or CLI/daemon Bearer token). Intentionally
NOT behind the email-verification gate so a broken/blocked client can still tell
us something went wrong.
"""

from __future__ import annotations

from alkera_core.logging import get_logger
from alkera_core.observability.context import get_trace_id, new_trace_id
from alkera_core.observability.events import EventName, emit_event
from alkera_core.observability.sentry import capture_message
from alkera_core.schemas.observability import (
    ClientErrorEvent,
    CrashReportCreate,
    CrashReportRead,
    CrashReportSummary,
    ErrorEventAck,
)
from fastapi import APIRouter, status

from backend.auth.dependencies import CurrentOrg, CurrentUser, DbSession
from backend.services.ops import crash_reports as crash_report_service

router = APIRouter(prefix="/api/v1/errors", tags=["errors"])
log = get_logger(__name__)


@router.post("/reports", response_model=CrashReportRead, status_code=status.HTTP_201_CREATED)
async def submit_crash_report(
    payload: CrashReportCreate, user: CurrentUser, db: DbSession, org_id: CurrentOrg
) -> CrashReportRead:
    report = await crash_report_service.create_crash_report(
        db, user=user, org_id=org_id, payload=payload
    )
    log.info(
        "crash_report.received",
        report_id=str(report.id),
        component=report.component,
        error_type=report.error_type,
    )
    # Forward to Sentry (no-op until the DSN is enabled). The structured context
    # is scrubbed by `before_send`.
    capture_message(
        f"crash report [{report.component}]: {report.message}",
        level="error",
        component=report.component,
        report_id=str(report.id),
    )
    emit_event(
        EventName.crash_report_submitted,
        user_id=user.id,
        org_id=org_id,
        component=report.component,
    )
    return CrashReportRead.model_validate(report)


@router.get("/reports", response_model=list[CrashReportSummary])
async def list_crash_reports(
    user: CurrentUser, db: DbSession, org_id: CurrentOrg
) -> list[CrashReportSummary]:
    rows = await crash_report_service.list_crash_reports(db, user=user, org_id=org_id)
    return [CrashReportSummary.model_validate(row) for row in rows]


@router.post("/events", response_model=ErrorEventAck)
async def report_client_error(
    payload: ClientErrorEvent, user: CurrentUser, org_id: CurrentOrg
) -> ErrorEventAck:
    """Passively-captured client error — logged (+ Sentry) so we're aware of it.

    No DB row; this is the high-volume, low-ceremony path for the web app and
    extension's global error handlers.
    """
    trace_id = get_trace_id() or new_trace_id()
    # The redaction processor scrubs message/url/stack/context before emit.
    log.error(
        "client_error.reported",
        component=payload.component,
        error_type=payload.error_type,
        message=payload.message,
        url=payload.url,
        stack=payload.stack,
        context=payload.context,
    )
    capture_message(
        f"client error [{payload.component}]: {payload.message}",
        level="error",
        component=payload.component,
    )
    emit_event(
        EventName.client_error_reported,
        user_id=user.id,
        org_id=org_id,
        component=payload.component,
    )
    return ErrorEventAck(received=True, trace_id=trace_id)
