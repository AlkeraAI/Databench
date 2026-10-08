"""Crash-report persistence.

Thin service over the `CrashReport` ORM row. Re-scrubs every free-text field
server-side (defense-in-depth on top of the client's redaction) before the row
is written, then lets the request's session-dependency drive the commit.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.models import CrashReport, User
from alkera_core.observability.redaction import scrub_mapping, scrub_path, scrub_text
from alkera_core.schemas.observability import CrashReportCreate
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


def _scrub(text: str | None) -> str | None:
    if text is None:
        return None
    return scrub_path(scrub_text(text))


async def create_crash_report(
    db: AsyncSession, *, user: User, org_id: UUID, payload: CrashReportCreate
) -> CrashReport:
    """File ``user``'s report under ``org_id``, the org of the request that
    sent it."""
    report = CrashReport(
        user_id=user.id,
        org_team_id=org_id,
        component=payload.component,
        error_type=payload.error_type,
        message=_scrub(payload.message) or payload.message,
        stacktrace=_scrub(payload.stacktrace),
        context=scrub_mapping(payload.context) if payload.context else None,
        comment=_scrub(payload.comment),
        app_version=payload.app_version,
        platform=payload.platform,
        logs=_scrub(payload.logs),
        occurred_at=payload.occurred_at,
    )
    db.add(report)
    await db.flush()
    await db.refresh(report)
    return report


async def list_crash_reports(
    db: AsyncSession, *, user: User, org_id: UUID, limit: int = 50
) -> Sequence[CrashReport]:
    """The caller's own crash reports filed in ``org_id``, newest first."""
    rows = await db.execute(
        select(CrashReport)
        .where(CrashReport.user_id == user.id, CrashReport.org_team_id == org_id)
        .order_by(CrashReport.created_at.desc())
        .limit(limit)
    )
    return rows.scalars().all()


# --------------------------------------------------------------------------- #
# Admin triage (platform staff) — list / detail / mark-read / delete.
# --------------------------------------------------------------------------- #


async def list_all_reports(
    db: AsyncSession,
    *,
    component: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[Sequence[CrashReport], int, int]:
    """Cross-tenant crash reports for the admin dashboard, newest first.

    Returns ``(rows, total_matching, unread_total)``. `user` and `read_by` are
    eager-loaded so the route can surface reporter and triage attribution.
    """
    filters = []
    if component is not None:
        filters.append(CrashReport.component == component)
    if unread_only:
        filters.append(CrashReport.read_at.is_(None))

    rows_q = (
        select(CrashReport)
        .options(selectinload(CrashReport.user), selectinload(CrashReport.read_by))
        .where(*filters)
        .order_by(CrashReport.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(rows_q)).scalars().all()

    total = (
        await db.execute(select(func.count()).select_from(CrashReport).where(*filters))
    ).scalar_one()
    unread = (
        await db.execute(
            select(func.count()).select_from(CrashReport).where(CrashReport.read_at.is_(None))
        )
    ).scalar_one()
    return rows, total, unread


async def get_report(db: AsyncSession, report_id: UUID) -> CrashReport | None:
    row = await db.execute(
        select(CrashReport)
        .options(selectinload(CrashReport.user), selectinload(CrashReport.read_by))
        .where(CrashReport.id == report_id)
    )
    return row.scalar_one_or_none()


async def set_read(
    db: AsyncSession, report: CrashReport, *, admin_id: UUID, read: bool
) -> CrashReport:
    """Mark a report read (recording the acting admin + timestamp) or unread."""
    if read:
        report.read_at = datetime.now(tz=UTC)
        report.read_by_user_id = admin_id
    else:
        report.read_at = None
        report.read_by_user_id = None
    await db.flush()
    await db.refresh(report, attribute_names=["read_by"])
    return report


async def delete_report(db: AsyncSession, report: CrashReport) -> None:
    await db.delete(report)
    await db.flush()
