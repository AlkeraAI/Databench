"""A seat per membership: the revision, its back-fills and its refusing downgrade.

``alembic check`` sees the new column and index. It does not see the back-fill
that names every existing seat's org, the identity grant rows derived from
existing subscriptions, or a downgrade that must refuse to merge two seats. So
this module drives the revision on a scratch copy against seeded rows: down,
up, down, up again, then the refusal with two seats for one person, and the
downgrade that succeeds once they are gone.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0187_seats_per_membership.py"
_PARENT = "0186"
_SIGNED_UP = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _org(session: AsyncSession) -> uuid.UUID:
    root = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, is_root) VALUES (:id, :name, true)"),
        {"id": root, "name": f"org-{root.hex[:8]}"},
    )
    return root


async def _user(session: AsyncSession, org: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)"),
        {"id": user_id, "org": org, "email": f"mig-{secrets.token_hex(6)}@alkera.dev"},
    )
    return user_id


async def _old_seat(session: AsyncSession, user_id: uuid.UUID, *, subscribed: bool) -> uuid.UUID:
    """A seat as the previous schema writes it: owned by a person, no org."""
    seat = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO billing_accounts (id, scope, owner_user_id) VALUES (:id, 'user', :owner)"
        ),
        {"id": seat, "owner": user_id},
    )
    if subscribed:
        await session.execute(
            text(
                "INSERT INTO billing_subscriptions (id, billing_account_id, created_at) "
                "VALUES (:id, :acct, :at)"
            ),
            {"id": uuid.uuid4(), "acct": seat, "at": _SIGNED_UP},
        )
    return seat


async def _pool(session: AsyncSession, org: uuid.UUID) -> uuid.UUID:
    pool = uuid.uuid4()
    await session.execute(
        text("INSERT INTO billing_accounts (id, scope, owner_team_id) VALUES (:id, 'org', :team)"),
        {"id": pool, "team": org},
    )
    return pool


async def _index_exists(session: AsyncSession, name: str) -> bool:
    return bool(await _scalar(session, "SELECT to_regclass(:n) IS NOT NULL", n=name))


async def test_the_revision_names_every_seats_org_and_round_trips() -> None:
    assert _MIGRATION.is_file()
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            org = await _org(session)
            subscribed = await _user(session, org)
            bare = await _user(session, org)
            seat = await _old_seat(session, subscribed, subscribed=True)
            bare_seat = await _old_seat(session, bare, subscribed=False)
            pool = await _pool(session, org)
            await session.commit()

        await scratch.upgrade()
        async with scratch.session() as session:
            orgs = {
                row.id: row.org_team_id
                for row in await session.execute(
                    text("SELECT id, org_team_id FROM billing_accounts WHERE id = ANY(:ids)"),
                    {"ids": [seat, bare_seat, pool]},
                )
            }
            assert orgs == {seat: org, bare_seat: org, pool: None}
            grants = (
                (
                    await session.execute(
                        text(
                            "SELECT user_id, free_grant_org_team_id, free_granted_at "
                            "FROM identity_billing_grants WHERE user_id = ANY(:ids)"
                        ),
                        {"ids": [subscribed, bare]},
                    )
                )
                .mappings()
                .all()
            )
            # Only the subscribed seat was placed on Free; the bare seat never was.
            assert [dict(g) for g in grants] == [
                {
                    "user_id": subscribed,
                    "free_grant_org_team_id": org,
                    "free_granted_at": _SIGNED_UP,
                }
            ]
            assert await _index_exists(session, "uq_billing_accounts_owner_user_org")
            assert not await _index_exists(session, "uq_billing_accounts_owner_user_id")
            assert await _scalar(session, "SELECT to_regclass('identity_org_creations')")

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert await _index_exists(session, "uq_billing_accounts_owner_user_id")
            assert await _scalar(session, "SELECT to_regclass('identity_billing_grants')") is None
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'billing_accounts' AND column_name = 'org_team_id'",
                )
                == 0
            )
            # The seats themselves survive the trip.
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM billing_accounts WHERE id = ANY(:ids)",
                    ids=[seat, bare_seat, pool],
                )
                == 3
            )

        await scratch.upgrade()
        async with scratch.session() as session:
            assert (
                await _scalar(
                    session, "SELECT org_team_id FROM billing_accounts WHERE id = :id", id=seat
                )
                == org
            )


async def test_the_downgrade_refuses_to_merge_two_seats_and_runs_once_they_are_one() -> None:
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            home = await _org(session)
            other = await _org(session)
            person = await _user(session, home)
            await session.execute(
                text(
                    "INSERT INTO org_memberships (id, user_id, org_team_id) "
                    "VALUES (gen_random_uuid(), :u, :o)"
                ),
                {"u": person, "o": other},
            )
            for org in (home, other):
                await session.execute(
                    text(
                        "INSERT INTO billing_accounts (id, scope, owner_user_id, org_team_id) "
                        "VALUES (gen_random_uuid(), 'user', :u, :o)"
                    ),
                    {"u": person, "o": org},
                )
            await session.commit()

        with pytest.raises(Exception, match="seats in more than one org"):
            await scratch.downgrade(_PARENT)
        assert scratch.revision() == "0187"
        async with scratch.session() as session:
            # Nothing was changed: both seats and the new column are still there.
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM billing_accounts WHERE owner_user_id = :u",
                    u=person,
                )
                == 2
            )
            await session.execute(
                text("DELETE FROM billing_accounts WHERE owner_user_id = :u AND org_team_id = :o"),
                {"u": person, "o": other},
            )
            await session.commit()

        await scratch.downgrade(_PARENT)
        assert scratch.revision() == _PARENT
        await scratch.upgrade()
        async with scratch.session() as session:
            assert (
                await _scalar(
                    session,
                    "SELECT org_team_id FROM billing_accounts WHERE owner_user_id = :u",
                    u=person,
                )
                == home
            )
