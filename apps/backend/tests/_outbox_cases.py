"""The rig the outbox emit-point tables run on.

A table is a list of ``Case`` objects, one per producer: ``prepare`` seeds
(committed on its own, before the head is read) and ``mutate`` performs the
announced mutation and returns the rows it must have left. ``assert_rows`` and
``assert_rolled_back`` are the two checks every table runs on every case. Any
domain with emit points writes its own table against this rig.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, read_after
from alkera_core.models import User
from backend.services.org import teams as team_service
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, make_member

Row = tuple[str, str, str, int, str, dict[str, Any], dict[str, Any]]
"""What one outbox row is compared as: type, entity, entity_id, version, visibility,
payload, actor."""


def row(
    type: EventType,
    entity: str,
    entity_id: str,
    *,
    actor: dict[str, Any],
    version: int = 0,
    visibility: str = "org",
    payload: dict[str, Any] | None = None,
) -> Row:
    return (type.value, entity, entity_id, version, visibility, payload or {}, actor)


async def rows_after(org_id: UUID, after: int) -> list[Row]:
    async with AsyncSessionLocal() as db:
        rows = await read_after(db, after_id=after, org_id=org_id, limit=100)
    return [
        (r.type, r.entity, r.entity_id, r.version, r.visibility, dict(r.payload), dict(r.actor))
        for r in rows
    ]


async def head_id(org_id: UUID) -> int:
    async with AsyncSessionLocal() as db:
        rows = await read_after(db, after_id=0, org_id=org_id, limit=1000)
    return max((r.id for r in rows), default=0)


async def get_user(db: AsyncSession, user_id: UUID) -> User:
    user = await db.get(User, user_id)
    assert user is not None
    return user


@dataclass
class World:
    org: OrgWithAdmin
    admin_id: UUID
    admin_email: str
    admin_ctx: dict[str, Any]
    member_id: UUID
    member_email: str
    subteam_id: UUID
    monkeypatch: pytest.MonkeyPatch
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def org_id(self) -> UUID:
        return self.org.org_id


async def build_world(
    org_admin: OrgWithAdmin, real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> World:
    """An org with its admin, one verified member and one subteam, committed."""
    admin = await get_user(real_session, org_admin.admin_id)
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    subteam = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name=f"sub-{secrets.token_hex(3)}"
    )
    await real_session.commit()
    ctx = ActingContext.for_user(user_id=admin.id, org_id=admin.home_org_team_id, email=admin.email)
    return World(
        org=org_admin,
        admin_id=admin.id,
        admin_email=admin.email,
        admin_ctx=ctx.audit_dict(),
        member_id=member.id,
        member_email=member.email,
        subteam_id=subteam.id,
        monkeypatch=monkeypatch,
    )


class Case:
    """One producer: ``prepare`` seeds (committed on its own, before the head is
    read); ``mutate`` performs the announced mutation in the session it is given
    and returns the rows it must have left, in id order."""

    id: str
    unordered: bool = False

    async def prepare(self, db: AsyncSession, w: World) -> None:
        return None

    async def mutate(self, db: AsyncSession, w: World) -> list[Row]:
        raise NotImplementedError


def by_caller(w: World) -> dict[str, Any]:
    return w.admin_ctx


def by_nobody(w: World) -> None:
    return None


def _comparable(rows: list[Row], *, unordered: bool) -> list[Row]:
    return sorted(rows, key=lambda r: (r[0], r[2])) if unordered else rows


async def _prepared_head(case: Case, w: World) -> int:
    async with AsyncSessionLocal() as db:
        await case.prepare(db, w)
        await db.commit()
    return await head_id(w.org_id)


async def assert_rows(case: Case, w: World) -> None:
    """The committed mutation leaves exactly the rows the case names."""
    head = await _prepared_head(case, w)
    async with AsyncSessionLocal() as db:
        expected = await case.mutate(db, w)
        await db.commit()
    actual = await rows_after(w.org_id, head)
    assert _comparable(actual, unordered=case.unordered) == _comparable(
        expected, unordered=case.unordered
    )


async def assert_rolled_back(case: Case, w: World) -> None:
    """The same mutation rolled back leaves none: the rows ride its transaction."""
    head = await _prepared_head(case, w)
    async with AsyncSessionLocal() as db:
        expected = await case.mutate(db, w)
        # Flushed, so visible to the mutating transaction itself ...
        visible = await read_after(db, after_id=head, org_id=w.org_id, limit=100)
        assert len(visible) == len(expected)
        await db.rollback()
    # ... and gone with it.
    assert await rows_after(w.org_id, head) == []
