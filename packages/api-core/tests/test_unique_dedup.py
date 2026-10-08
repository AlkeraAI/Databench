"""Regression tests for migration 0016 + the Invitation partial-unique-index.

Two invariants this guards:

1. **Model declares the partial unique index.** `uq_invitations_pending_email_team`
   was created in migration 0003 but the model never declared it — so a
   `make migrate-create` would autogenerate a migration that DROPS a
   business-critical index. The metadata test below fails the moment that
   declaration is removed (it's what keeps `make migrate-check` green).
2. **Uniqueness survives the constraint drop.** Migration 0016 drops the
   redundant `users_email_key` / `invitations_token_key` unique constraints; the
   standalone unique indexes (`ix_users_email`, `ix_invitations_token`) must keep
   enforcing uniqueness. The DB tests assert duplicates still raise.

The DB tests run against the real migrated dev DB (like `test_audit_log_model`).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Invitation, InvitationStatus, Team, User
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def test_invitation_declares_pending_partial_unique_index() -> None:
    """The model carries the partial unique index from migration 0003.

    Fails if the `__table_args__` declaration is dropped — which is exactly what
    would let autogenerate propose dropping the index in the live DB.
    """
    idx = next(
        (i for i in Invitation.__table__.indexes if i.name == "uq_invitations_pending_email_team"),
        None,
    )
    assert idx is not None, "partial unique index missing from the Invitation model"
    assert idx.unique is True
    assert idx.dialect_options["postgresql"]["where"] is not None


async def _make_org_id(session: AsyncSession) -> uuid.UUID:
    """Create a fresh root team and return its id as a plain value, so callers
    never re-read an ORM attribute that a commit/rollback has expired (which
    would trigger a sync lazy-load → MissingGreenlet under the async engine)."""
    team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
    session.add(team)
    await session.flush()
    return team.id


async def test_user_email_unique_after_constraint_drop() -> None:
    """`ix_users_email` still enforces email uniqueness after 0016 dropped the
    redundant `users_email_key` constraint."""
    email = f"dup-{secrets.token_hex(6)}@alkera.dev"
    async with AsyncSessionLocal() as session:
        team_id = await _make_org_id(session)
        session.add(User(home_org_team_id=team_id, email=email, first_name="A", last_name="One"))
        await session.commit()

        session.add(User(home_org_team_id=team_id, email=email, first_name="B", last_name="Two"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_invitation_token_unique_after_constraint_drop() -> None:
    """`ix_invitations_token` still enforces token uniqueness after 0016 dropped
    the redundant `invitations_token_key` constraint."""
    token = secrets.token_urlsafe(24)
    exp = datetime.now(UTC) + timedelta(days=7)
    async with AsyncSessionLocal() as session:
        team_id = await _make_org_id(session)
        session.add(
            Invitation(
                team_id=team_id,
                email=f"a-{secrets.token_hex(4)}@x.dev",
                token=token,
                expires_at=exp,
            )
        )
        await session.commit()

        session.add(
            Invitation(
                team_id=team_id,
                email=f"b-{secrets.token_hex(4)}@x.dev",
                token=token,
                expires_at=exp,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_pending_invitation_partial_unique_index() -> None:
    """At most one PENDING invite per (email, team); a non-pending duplicate for
    the same pair is allowed — proving the partial `WHERE status = 'pending'`."""
    email = f"invitee-{secrets.token_hex(6)}@x.dev"
    exp = datetime.now(UTC) + timedelta(days=7)
    async with AsyncSessionLocal() as session:
        team_id = await _make_org_id(session)
        session.add(
            Invitation(
                team_id=team_id,
                email=email,
                token=secrets.token_urlsafe(24),
                status=InvitationStatus.PENDING,
                expires_at=exp,
            )
        )
        await session.commit()

        # Second PENDING for the same (email, team) → blocked by the partial index.
        session.add(
            Invitation(
                team_id=team_id,
                email=email,
                token=secrets.token_urlsafe(24),
                status=InvitationStatus.PENDING,
                expires_at=exp,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        # A REVOKED invite for the same (email, team) is allowed (outside the WHERE).
        session.add(
            Invitation(
                team_id=team_id,
                email=email,
                token=secrets.token_urlsafe(24),
                status=InvitationStatus.REVOKED,
                expires_at=exp,
            )
        )
        await session.commit()
