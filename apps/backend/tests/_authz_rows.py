"""Read back what ``backend.authz.enforce()`` recorded: the decision rows on the
outbox and the org audit rows beside them, for the exemplar route tests."""

from __future__ import annotations

import secrets
from uuid import UUID

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgAuditEvent
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import app_client, login, make_member

AUTHZ_TYPE = "authz.decision"


async def decisions(org_id: UUID, *, entity: str | None = None) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        stmt = select(EventOutbox).where(
            EventOutbox.org_id == org_id, EventOutbox.type == AUTHZ_TYPE
        )
        if entity is not None:
            stmt = stmt.where(EventOutbox.entity == entity)
        rows = await session.execute(stmt.order_by(EventOutbox.id))
        return list(rows.scalars().all())


async def audit_rows(org_id: UUID, action: str) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(OrgAuditEvent)
            .where(OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.action == action)
            .order_by(OrgAuditEvent.created_at)
        )
        return list(rows.scalars().all())


def effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]


def chain(row: EventOutbox) -> list[tuple[str, str]]:
    return [(link["kind"], link["id"]) for link in row.actor["chain"]]


async def other_org_member(client: AsyncClient) -> tuple[UUID, AsyncClient]:
    """A fresh org and a logged-in member of its root."""
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other Org {secrets.token_hex(4)}",
            admin_email=f"other-admin-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
        member, pw = await make_member(session, org_id=org.id, verified=True)
    outsider = app_client()
    await login(outsider, member.email, pw or "")
    return org.id, outsider
