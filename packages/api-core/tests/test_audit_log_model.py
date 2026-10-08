"""AuditLog ORM round-trip — JSONB detail survives, actor FK is SET NULL."""

from __future__ import annotations

import secrets

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuditLog, Team, User
from sqlalchemy import select


async def test_audit_log_round_trip() -> None:
    async with AsyncSessionLocal() as session:
        team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
        session.add(team)
        await session.flush()
        actor = User(
            home_org_team_id=team.id,
            email=f"actor-{secrets.token_hex(6)}@alkera.dev",
            first_name="Aud",
            last_name="Itor",
        )
        session.add(actor)
        await session.flush()

        entry = AuditLog(
            actor_id=actor.id,
            actor_email=actor.email,
            actor_platform_role="alkera_admin",
            action="grant_credits",
            method="POST",
            path="/admin/v1/billing/grants",
            status_code=201,
            detail={"body": {"scope": "user", "amount_usd": "5.00"}, "path_params": {}},
        )
        session.add(entry)
        await session.commit()
        entry_id = entry.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(AuditLog).where(AuditLog.id == entry_id))
        ).scalar_one()
        assert loaded.actor_email.startswith("actor-")
        assert loaded.actor_platform_role == "alkera_admin"
        assert loaded.action == "grant_credits"
        assert loaded.status_code == 201
        assert loaded.detail == {"body": {"scope": "user", "amount_usd": "5.00"}, "path_params": {}}
        assert loaded.created_at is not None


async def test_audit_log_accepts_null_actor() -> None:
    """actor_id is nullable so the trail survives a later user deletion."""
    async with AsyncSessionLocal() as session:
        entry = AuditLog(
            actor_id=None,
            actor_email="ghost@alkera.dev",
            actor_platform_role=None,
            action="delete_org",
            method="DELETE",
            path="/admin/v1/orgs/00000000-0000-0000-0000-000000000000",
            status_code=204,
            detail=None,
        )
        session.add(entry)
        await session.commit()
        entry_id = entry.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(AuditLog).where(AuditLog.id == entry_id))
        ).scalar_one()
        assert loaded.actor_id is None
        assert loaded.detail is None
