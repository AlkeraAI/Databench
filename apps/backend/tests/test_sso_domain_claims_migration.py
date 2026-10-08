"""Revision 0207: each listed SSO domain goes to one org, and the losers are told.

Driven by Alembic against a scratch copy: connections and accounts seeded on
the schema before the revision, the back-fill run, run again over what it
wrote, then taken down. The rules under test:

* a contested domain stays with one org (an enabled connection first, then the
  earliest) and is dropped for the others, never silently: each org's own audit
  trail says what happened, and neither names the other;
* a public mailbox domain or a value that is no domain stays with nobody;
* each connection's own list is rewritten to what it kept, so the previous
  release follows the same decision;
* an org left with no domain stops requiring SSO;
* accounts an org's IdP or SCIM created are marked, and only those.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = [pytest.mark.asyncio]

_PARENT = "0206"
_REVISION = "0207"
_T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _connection(
    session: AsyncSession,
    *,
    listed: str,
    enabled: bool = True,
    enforced: bool = False,
    created_days: int = 0,
) -> uuid.UUID:
    org = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, is_root) VALUES (:id, :name, true)"),
        {"id": org, "name": f"sso-claims-{org.hex[:8]}"},
    )
    await session.execute(
        text(
            "INSERT INTO sso_connections "
            "(id, org_team_id, enabled, enforced, allowed_domains, created_at) "
            "VALUES (:id, :org, :enabled, :enforced, :listed, :created)"
        ),
        {
            "id": uuid.uuid4(),
            "org": org,
            "enabled": enabled,
            "enforced": enforced,
            "listed": listed,
            "created": _T0 + timedelta(days=created_days),
        },
    )
    return org


async def _rows(session: AsyncSession, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in await session.execute(text(sql), params)]


async def _claims(session: AsyncSession, orgs: list[uuid.UUID]) -> list[tuple[Any, ...]]:
    return await _rows(
        session,
        "SELECT org_team_id, domain FROM sso_domain_claims "
        "WHERE org_team_id = ANY(:orgs) ORDER BY domain",
        orgs=orgs,
    )


async def _audit(session: AsyncSession, orgs: list[uuid.UUID]) -> list[tuple[Any, ...]]:
    return await _rows(
        session,
        "SELECT org_team_id, action, target, detail->>'reason', actor_id, entry_hash IS NOT NULL "
        "FROM org_audit_events WHERE org_team_id = ANY(:orgs) AND action LIKE 'sso.domain_%' "
        "ORDER BY org_team_id, target, action",
        orgs=orgs,
    )


async def _connection_state(session: AsyncSession, org: uuid.UUID) -> tuple[Any, ...]:
    (row,) = await _rows(
        session,
        "SELECT allowed_domains, enforced FROM sso_connections WHERE org_team_id = :org",
        org=org,
    )
    return row


async def test_one_org_keeps_a_contested_domain_and_both_trails_record_it() -> None:
    tag = uuid.uuid4().hex[:8]
    contested, sole = f"dup-{tag}.example", f"sole-{tag}.example"
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            # Listed it first, but never switched the connection on.
            drafted = await _connection(session, listed=contested, enabled=False, created_days=0)
            # The earliest ENABLED connection: this one keeps the domain.
            keeper = await _connection(
                session, listed=f"{contested}, {sole}", enforced=True, created_days=1
            )
            # Listed later, spelled differently, requires SSO on it alone.
            later = await _connection(
                session, listed=f" {contested.upper()}. ", enforced=True, created_days=2
            )
            # A mailbox domain and a value that is no domain; requires SSO.
            mailbox = await _connection(
                session, listed="gmail.com, not a domain", enforced=True, created_days=3
            )
            empty = await _connection(session, listed="", created_days=4)
            await session.commit()
        orgs = [drafted, keeper, later, mailbox, empty]

        await scratch.upgrade(_REVISION)

        async with scratch.session() as session:
            assert await _claims(session, orgs) == [(keeper, contested), (keeper, sole)]
            audit = await _audit(session, orgs)
            assert sorted(row[:4] for row in audit) == sorted(
                [
                    (drafted, "sso.domain_released", contested, "held_by_another_organization"),
                    (later, "sso.domain_released", contested, "held_by_another_organization"),
                    (keeper, "sso.domain_kept", contested, "also_listed_by_another_organization"),
                    (mailbox, "sso.domain_released", "gmail.com", "public_mailbox_domain"),
                    (mailbox, "sso.domain_released", "not a domain", "not_a_domain"),
                ]
            )
            # No actor (the platform decided during the upgrade), and every row
            # is on its org's hash chain.
            assert {row[4] for row in audit} == {None}
            assert {row[5] for row in audit} == {True}
            # Each list now says what the org kept.
            assert await _connection_state(session, keeper) == (f"{contested},{sole}", True)
            assert await _connection_state(session, later) == ("", False)
            assert await _connection_state(session, mailbox) == ("", False)
            assert await _connection_state(session, drafted) == ("", False)

        # A re-run over what it wrote changes nothing and audits nothing twice.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            assert await _claims(session, orgs) == [(keeper, contested), (keeper, sole)]
            assert len(await _audit(session, orgs)) == len(audit)

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert (
                await session.execute(text("SELECT to_regclass('sso_domain_claims')"))
            ).scalar_one() is None
            assert (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'users' AND column_name = 'provisioned_by'"
                    )
                )
            ).scalar_one() == 0
        await scratch.upgrade(_REVISION)


async def _user(session: AsyncSession, org: uuid.UUID, *, password: bool) -> uuid.UUID:
    user = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, org_team_id, email, password_hash) "
            "VALUES (:id, :org, :email, :pw)"
        ),
        {
            "id": user,
            "org": org,
            "email": f"u-{user.hex[:10]}@example.test",
            "pw": "x" if password else None,
        },
    )
    return user


async def test_accounts_an_org_made_are_marked_and_no_other() -> None:
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            org = await _connection(session, listed="")
            by_idp = await _user(session, org, password=False)
            by_scim = await _user(session, org, password=False)
            by_person = await _user(session, org, password=True)
            social_only = await _user(session, org, password=False)
            for user, provider in ((by_idp, f"sso:{org.hex}"), (social_only, "google")):
                await session.execute(
                    text(
                        "INSERT INTO oauth_identities (id, user_id, provider, subject) "
                        "VALUES (:id, :user, :provider, :subject)"
                    ),
                    {"id": uuid.uuid4(), "user": user, "provider": provider, "subject": user.hex},
                )
            # A person who set a password and was later federated by the IdP
            # made their own account.
            await session.execute(
                text(
                    "INSERT INTO oauth_identities (id, user_id, provider, subject) "
                    "VALUES (:id, :user, :provider, :subject)"
                ),
                {
                    "id": uuid.uuid4(),
                    "user": by_person,
                    "provider": f"sso:{org.hex}",
                    "subject": "p",
                },
            )
            await session.execute(
                text("UPDATE org_memberships SET scim_external_id = 'ext-1' WHERE user_id = :u"),
                {"u": by_scim},
            )
            await session.commit()

        await scratch.upgrade(_REVISION)

        async with scratch.session() as session:
            marks = dict(
                await _rows(
                    session,
                    "SELECT id, provisioned_by FROM users WHERE id = ANY(:ids)",
                    ids=[by_idp, by_scim, by_person, social_only],
                )
            )
            assert marks == {by_idp: "sso", by_scim: "scim", by_person: None, social_only: None}
            # The mark admits only the two values.
            with pytest.raises(Exception, match="ck_users_provisioned_by"):
                await session.execute(
                    text("UPDATE users SET provisioned_by = 'admin' WHERE id = :u"),
                    {"u": by_person},
                )
            await session.rollback()
