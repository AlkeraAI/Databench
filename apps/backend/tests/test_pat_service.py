"""Personal-access-token lifecycle against real Postgres: mint stores only the
digest, resolution honours revocation, expiry, pepper rotation, owner state and
tenancy, and revoke/list stay inside the caller's org."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alkera_core.auth.ci_token import mint_ci_token
from alkera_core.auth.pat_token import PAT_TOKEN_PREFIX
from alkera_core.auth.tenancy import MembershipRefused
from alkera_core.auth.token_hash import hash_lookup_token
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import PersonalAccessToken, Team, User
from backend.services.credentials import pats as pat_service
from backend.services.identity import users as user_service
from backend.services.org import teams as team_service
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _org(session: AsyncSession) -> tuple[Team, User]:
    org, admin = await team_service.create_org_with_admin(
        session,
        org_name=f"PAT Org {secrets.token_hex(4)}",
        admin_email=f"pat-admin-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Pat",
        admin_last_name="Admin",
        admin_password="admin-pass-12345",
    )
    await session.commit()
    return org, admin


async def _user(session: AsyncSession, org_id: UUID) -> User:
    user = await user_service.create_user(
        session,
        org_team_id=org_id,
        email=f"pat-user-{secrets.token_hex(6)}@alkera.dev",
        first_name="Pat",
        last_name="User",
        password="user-pass-12345",
    )
    await session.commit()
    return user


async def _row(session: AsyncSession, token_id: UUID) -> PersonalAccessToken:
    return (
        await session.execute(select(PersonalAccessToken).where(PersonalAccessToken.id == token_id))
    ).scalar_one()


# --------------------------------------------------------------------------- #
# mint
# --------------------------------------------------------------------------- #


async def test_mint_returns_the_raw_secret_and_stores_only_its_digest() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        token, raw = await pat_service.mint(
            session, org_id=org.id, user_id=admin.id, label="laptop", scopes=["kb:write"]
        )
        await session.commit()
        assert raw.startswith(PAT_TOKEN_PREFIX)
        assert token.token_hash == hash_lookup_token(raw)
        assert token.token_hash != raw
        assert token.org_team_id == org.id
        assert token.user_id == admin.id
        assert token.label == "laptop"
        assert token.scopes == ["kb:write"]
        assert token.expires_at is None
        assert token.revoked_at is None
        assert token.last_used_at is None
        # Nothing in the row equals or contains the raw secret.
        stored = await _row(session, token.id)
        for value in (stored.token_hash, stored.label, *stored.scopes):
            assert raw not in str(value)


async def test_mint_copies_the_scopes_rather_than_aliasing_the_callers_list() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        scopes = ["a"]
        token, _raw = await pat_service.mint(
            session, org_id=org.id, user_id=admin.id, label=None, scopes=scopes
        )
        scopes.append("b")
        assert token.scopes == ["a"]


async def test_mint_with_a_future_expiry_stores_it() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        later = datetime.now(UTC) + timedelta(days=30)
        token, _raw = await pat_service.mint(
            session, org_id=org.id, user_id=admin.id, label=None, expires_at=later
        )
        assert token.expires_at == later


async def test_mint_refuses_a_user_from_another_org() -> None:
    async with AsyncSessionLocal() as session:
        org_a, _admin_a = await _org(session)
        _org_b, admin_b = await _org(session)
        with pytest.raises(ValueError, match="member of the org"):
            await pat_service.mint(session, org_id=org_a.id, user_id=admin_b.id, label=None)


async def test_mint_refuses_an_unknown_user() -> None:
    async with AsyncSessionLocal() as session:
        org, _admin = await _org(session)
        with pytest.raises(ValueError, match="member of the org"):
            await pat_service.mint(session, org_id=org.id, user_id=uuid4(), label=None)


async def test_mint_refuses_a_deactivated_user() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        admin.is_active = False
        await session.commit()
        with pytest.raises(ValueError, match="deactivated"):
            await pat_service.mint(session, org_id=org.id, user_id=admin.id, label=None)


async def test_mint_refuses_an_expiry_already_in_the_past() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        with pytest.raises(ValueError, match="already expired"):
            await pat_service.mint(
                session,
                org_id=org.id,
                user_id=admin.id,
                label=None,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )


async def test_a_refused_mint_adds_no_row() -> None:
    async with AsyncSessionLocal() as session:
        org_a, _admin_a = await _org(session)
        _org_b, admin_b = await _org(session)
        admin_b_id = admin_b.id
        with pytest.raises(ValueError):
            await pat_service.mint(session, org_id=org_a.id, user_id=admin_b_id, label=None)
        await session.rollback()
        assert await pat_service.list_for_user(session, user_id=admin_b_id) == []


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #


async def test_resolve_returns_the_token_and_its_owner() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        token, raw = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label="x")
        await session.commit()
    async with AsyncSessionLocal() as session:
        resolved = await pat_service.resolve_active(session, raw)
        assert resolved is not None
        assert resolved.token.id == token.id
        assert resolved.owner.id == admin.id
        assert resolved.owner.home_org_team_id == org.id


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("not-a-token", id="garbage"),
        pytest.param(PAT_TOKEN_PREFIX + secrets.token_urlsafe(48), id="unminted-pat-shape"),
        pytest.param(mint_ci_token()[0], id="a-ci-token"),
    ],
)
async def test_resolve_of_an_unknown_secret_is_none(raw: str) -> None:
    async with AsyncSessionLocal() as session:
        assert await pat_service.resolve_active(session, raw) is None


async def test_resolve_survives_a_pepper_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token minted under yesterday's pepper still resolves once that pepper
    is listed as previous; without the listing it is gone."""
    old_pepper = settings.effective_token_hash_pepper
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        _token, raw = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label=None)
        await session.commit()

    monkeypatch.setattr(settings, "token_hash_pepper", "rotated-pepper-" + secrets.token_hex(8))
    monkeypatch.setattr(settings, "token_hash_pepper_previous", "")
    async with AsyncSessionLocal() as session:
        assert await pat_service.resolve_active(session, raw) is None

    monkeypatch.setattr(settings, "token_hash_pepper_previous", old_pepper)
    async with AsyncSessionLocal() as session:
        resolved = await pat_service.resolve_active(session, raw)
        assert resolved is not None and resolved.owner.id == admin.id


async def test_a_revoked_token_does_not_resolve() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        token, raw = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label=None)
        await session.commit()
        assert await pat_service.revoke(session, org_id=org.id, token_id=token.id) is True
        await session.commit()
        assert await pat_service.resolve_active(session, raw) is None


async def test_a_token_stops_resolving_the_instant_it_expires() -> None:
    """Driven ACROSS the expiry with a frozen clock: valid one second before,
    gone at the boundary and after."""
    with freeze_time("2026-09-05T12:00:00Z", real_asyncio=True) as frozen:
        expires = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
        async with AsyncSessionLocal() as session:
            org, admin = await _org(session)
            _token, raw = await pat_service.mint(
                session, org_id=org.id, user_id=admin.id, label=None, expires_at=expires
            )
            await session.commit()
        async with AsyncSessionLocal() as session:
            assert await pat_service.resolve_active(session, raw) is not None
            frozen.move_to("2026-09-05T12:59:59Z")
            assert await pat_service.resolve_active(session, raw) is not None
            frozen.move_to("2026-09-05T13:00:00Z")
            assert await pat_service.resolve_active(session, raw) is None
            frozen.move_to("2026-09-06T13:00:00Z")
            assert await pat_service.resolve_active(session, raw) is None


async def test_resolve_honours_an_explicit_now() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        expires = datetime.now(UTC) + timedelta(hours=1)
        _token, raw = await pat_service.mint(
            session, org_id=org.id, user_id=admin.id, label=None, expires_at=expires
        )
        await session.commit()
        assert await pat_service.resolve_active(session, raw, now=expires) is None
        assert (
            await pat_service.resolve_active(session, raw, now=expires - timedelta(seconds=1))
            is not None
        )


async def test_a_deactivated_owner_takes_their_tokens_down_with_them() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        _token, raw = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label=None)
        admin.is_active = False
        await session.commit()
        assert await pat_service.resolve_active(session, raw) is None


async def test_a_token_whose_org_no_longer_matches_its_owner_does_not_resolve() -> None:
    """Edited directly: the mint refuses this shape. A token whose org is one
    its owner holds no membership in is refused as a membership refusal (the
    door answers ``session_org_revoked``), never trusted with that tenancy."""
    async with AsyncSessionLocal() as session:
        org_a, admin_a = await _org(session)
        org_b, _admin_b = await _org(session)
        token, raw = await pat_service.mint(
            session, org_id=org_a.id, user_id=admin_a.id, label=None
        )
        await session.commit()
        row = await _row(session, token.id)
        row.org_team_id = org_b.id
        await session.commit()
        with pytest.raises(MembershipRefused):
            await pat_service.resolve_active(session, raw)


async def test_resolve_never_stamps_last_used_at_itself() -> None:
    """Stamping is the caller's job (in its own committed session); the
    resolver is a pure read so a probe cannot be mistaken for use."""
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        token, raw = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label=None)
        await session.commit()
        assert await pat_service.resolve_active(session, raw) is not None
        await session.commit()
        assert (await _row(session, token.id)).last_used_at is None


# --------------------------------------------------------------------------- #
# revoke / list
# --------------------------------------------------------------------------- #


async def test_revoke_is_idempotent_and_keeps_the_first_timestamp() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        token, _raw = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label=None)
        await session.commit()
        assert await pat_service.revoke(session, org_id=org.id, token_id=token.id) is True
        await session.commit()
        first = (await _row(session, token.id)).revoked_at
        assert first is not None
        assert await pat_service.revoke(session, org_id=org.id, token_id=token.id) is True
        await session.commit()
        assert (await _row(session, token.id)).revoked_at == first


async def test_revoke_from_another_org_is_indistinguishable_from_not_found() -> None:
    async with AsyncSessionLocal() as session:
        org_a, admin_a = await _org(session)
        org_b, _admin_b = await _org(session)
        token, raw = await pat_service.mint(
            session, org_id=org_a.id, user_id=admin_a.id, label=None
        )
        await session.commit()
        assert await pat_service.revoke(session, org_id=org_b.id, token_id=token.id) is False
        assert await pat_service.revoke(session, org_id=org_b.id, token_id=uuid4()) is False
        await session.commit()
        # Still live: the foreign attempt changed nothing.
        assert await pat_service.resolve_active(session, raw) is not None


async def test_list_for_user_is_newest_first_and_hides_revoked_unless_asked() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        other = await _user(session, org.id)
        first, _ = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label="first")
        await session.commit()
        second, _ = await pat_service.mint(session, org_id=org.id, user_id=admin.id, label="second")
        await session.commit()
        _theirs, _ = await pat_service.mint(session, org_id=org.id, user_id=other.id, label="other")
        await session.commit()
        await pat_service.revoke(session, org_id=org.id, token_id=first.id)
        await session.commit()

        live = await pat_service.list_for_user(session, user_id=admin.id)
        assert [t.id for t in live] == [second.id]
        everything = await pat_service.list_for_user(
            session, user_id=admin.id, include_revoked=True
        )
        assert [t.label for t in everything] == ["second", "first"]
        assert all(t.user_id == admin.id for t in everything)
