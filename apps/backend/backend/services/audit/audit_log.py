"""Persistence for the admin audit log.

`record` adds + flushes a row (the caller owns the commit boundary, per the
repo's session convention). `resolve_target` names what an action is done to
from its path parameters and body. `list_page` reads one newest-first page
(optionally filtered) plus the total count for the paginated admin view.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from alkera_core.models import AuditLog, ComputeAllocation, Team, User
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def record(
    db: AsyncSession,
    *,
    actor_id: UUID | None,
    actor_email: str,
    actor_platform_role: str | None,
    action: str,
    method: str,
    path: str,
    status_code: int,
    detail: dict[str, Any] | None,
    target: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        actor_email=actor_email,
        actor_platform_role=actor_platform_role,
        action=action,
        method=method,
        path=path,
        status_code=status_code,
        detail=detail,
        target=target[:512] if target else None,
    )
    db.add(entry)
    await db.flush()
    return entry


async def _org_name(db: AsyncSession, org_id: UUID) -> str | None:
    name = (await db.execute(select(Team.name).where(Team.id == org_id))).scalar_one_or_none()
    return name or None


async def _user_email(db: AsyncSession, user_id: UUID) -> str | None:
    return (await db.execute(select(User.email).where(User.id == user_id))).scalar_one_or_none()


async def _machine_name(db: AsyncSession, machine_id: UUID) -> str | None:
    name = (
        await db.execute(select(ComputeAllocation.name).where(ComputeAllocation.id == machine_id))
    ).scalar_one_or_none()
    return name or None


#: The identifiers a staff action can be about, in the order they name it: a
#: route under an org names the org even when it also carries a user or machine.
#: A new kind of target is one more entry here.
TARGET_RESOLVERS: tuple[tuple[str, Callable[[AsyncSession, UUID], Awaitable[str | None]]], ...] = (
    ("org_id", _org_name),
    ("user_id", _user_email),
    ("machine_id", _machine_name),
)


def _as_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def resolve_target(
    db: AsyncSession, *, path_params: Mapping[str, Any], body: object
) -> str | None:
    """The name of what an action is done to: the first known identifier in the
    path, else in a JSON object body, resolved to a name. An id that resolves
    to nothing is kept as the raw id, so the row still says which one."""
    sources: list[Mapping[str, Any]] = [path_params]
    if isinstance(body, Mapping):
        sources.append(body)
    for source in sources:
        for key, resolve in TARGET_RESOLVERS:
            if key not in source:
                continue
            ident = _as_uuid(source[key])
            if ident is None:
                continue
            return await resolve(db, ident) or str(ident)
    return None


@dataclass(frozen=True, slots=True)
class AuditLogFilters:
    """Optional narrowing of the platform log: an exact action, the actor's
    email, and inclusive time bounds."""

    action: str | None = None
    actor_email: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None

    def clauses(self) -> list[ColumnElement[bool]]:
        out: list[ColumnElement[bool]] = []
        if self.action:
            out.append(AuditLog.action == self.action)
        if self.actor_email:
            out.append(AuditLog.actor_email == self.actor_email.strip().lower())
        if self.created_after is not None:
            out.append(AuditLog.created_at >= self.created_after)
        if self.created_before is not None:
            out.append(AuditLog.created_at <= self.created_before)
        return out


async def list_page(
    db: AsyncSession,
    *,
    offset: int,
    limit: int,
    filters: AuditLogFilters | None = None,
) -> tuple[Sequence[AuditLog], int]:
    """One newest-first page of entries plus the total row count, both under
    the same filters."""
    where = (filters or AuditLogFilters()).clauses()
    total = (
        await db.execute(select(func.count()).select_from(AuditLog).where(*where))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(*where)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return rows, int(total)


async def recorded_actions(db: AsyncSession) -> list[str]:
    """Every action the log holds, alphabetically — the filter's choices."""
    rows = await db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action))
    return list(rows.scalars().all())
