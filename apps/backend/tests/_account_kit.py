"""Shared readers for the account lifecycle tests: the decision rows the
account policy leaves. The mail capture and the archive store are fixtures in
``conftest.py`` (``account_mail``, ``account_archives``)."""

from __future__ import annotations

from uuid import UUID

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from sqlalchemy import select

AUTHZ_TYPE = "authz.decision"


async def decisions(org_id: UUID, *, entity: str = "account") -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == AUTHZ_TYPE,
                EventOutbox.entity == entity,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]
