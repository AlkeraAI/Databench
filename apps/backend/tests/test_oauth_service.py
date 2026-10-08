"""Service-level OAuth decision-tree tests.

The HTTP tests use the mock provider (which isn't org-gated). The per-org
`allow_login_<provider>` gate only applies to google/github, so we exercise it
here by calling the service directly with those provider keys.
"""

from __future__ import annotations

import secrets

import pytest
from alkera_core.models import OAuthIdentity
from backend.auth.oauth.profile import FederatedProfile
from backend.services.identity import oauth as oauth_service
from backend.services.identity import users as user_service
from backend.services.identity.oauth import LoginOutcome, OAuthLoginBlockedError, RegisterOutcome
from backend.services.org import settings as org_settings_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email, make_member

pytestmark = pytest.mark.asyncio


async def _link(session: AsyncSession, *, user_id, provider: str, subject: str) -> None:
    session.add(
        OAuthIdentity(
            user_id=user_id,
            provider=provider,
            subject=subject,
            email_verified=True,
        )
    )
    await session.commit()


async def test_disabled_provider_blocks_linked_login(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    subject = f"g-{secrets.token_hex(6)}"
    await _link(real_session, user_id=member.id, provider="google", subject=subject)
    await org_settings_service.update(real_session, org_admin.org_id, allow_login_google=False)
    await real_session.commit()

    profile = FederatedProfile(
        provider="google", subject=subject, email=member.email, email_verified=True
    )
    with pytest.raises(OAuthLoginBlockedError) as exc:
        await oauth_service.resolve(real_session, profile, invite_token=None)
    assert exc.value.reason == "provider_disabled"


async def test_enabled_provider_allows_linked_login(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    subject = f"h-{secrets.token_hex(6)}"
    await _link(real_session, user_id=member.id, provider="github", subject=subject)
    # github allowed by default.
    profile = FederatedProfile(
        provider="github", subject=subject, email=member.email, email_verified=True
    )
    outcome = await oauth_service.resolve(real_session, profile, invite_token=None)
    assert isinstance(outcome, LoginOutcome)
    assert outcome.user.id == member.id


async def test_disabled_provider_blocks_autolink(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await org_settings_service.update(real_session, org_admin.org_id, allow_login_github=False)
    await real_session.commit()
    # Verified email matches an existing user, but github is disabled for the org.
    profile = FederatedProfile(
        provider="github",
        subject=f"h-{secrets.token_hex(6)}",
        email=member.email,
        email_verified=True,
    )
    with pytest.raises(OAuthLoginBlockedError) as exc:
        await oauth_service.resolve(real_session, profile, invite_token=None)
    assert exc.value.reason == "provider_disabled"


async def test_unknown_email_yields_register_outcome(real_session: AsyncSession) -> None:
    from tests.conftest import _unique_email

    profile = FederatedProfile(
        provider="google",
        subject="brand-new",
        email=_unique_email("svc-new"),
        email_verified=True,
        first_name="New",
        last_name="Person",
    )
    outcome = await oauth_service.resolve(real_session, profile, invite_token=None)
    assert isinstance(outcome, RegisterOutcome)
    assert outcome.ticket


async def test_provider_allowed_defaults_true_without_row(real_session: AsyncSession) -> None:
    # An org id with no settings row reads as all-allowed.
    from uuid import uuid4

    assert await org_settings_service.provider_allowed(real_session, uuid4(), "google") is True


async def test_second_provider_account_for_same_user_is_clean_error(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # User already has ONE google account linked. Signing in with a SECOND
    # google account whose verified email matches must be a clean error, not a
    # 500 from the (provider, user_id) unique constraint.
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await _link(
        real_session, user_id=member.id, provider="google", subject=f"g-{secrets.token_hex(6)}"
    )

    profile = FederatedProfile(
        provider="google",
        subject=f"g-other-{secrets.token_hex(6)}",
        email=member.email,
        email_verified=True,
    )
    with pytest.raises(OAuthLoginBlockedError) as exc:
        await oauth_service.resolve(real_session, profile, invite_token=None)
    assert exc.value.reason == "already_linked"


async def _pending_invite(session: AsyncSession, *, org: OrgWithAdmin, email: str) -> str:
    """Insert a pending invitation row for `email` to the org root. Returns token."""
    from datetime import UTC, datetime, timedelta

    from alkera_core.auth import hash_lookup_token
    from alkera_core.models import Invitation, InvitationStatus, TeamRole

    token = secrets.token_urlsafe(24)
    session.add(
        Invitation(
            team_id=org.org_id,
            email=email.lower(),
            role=TeamRole.MEMBER,
            # The row stores only the hash; the raw token is what callers present.
            token=hash_lookup_token(token),
            status=InvitationStatus.PENDING,
            invited_by_id=org.admin_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await session.commit()
    return token


def _ticket(*, email: str, provider: str = "google", invite_token: str | None = None):  # type: ignore[no-untyped-def]
    from alkera_core.auth import decode_register_ticket, encode_register_ticket

    return decode_register_ticket(
        encode_register_ticket(
            provider=provider,
            subject=f"{provider}-{secrets.token_hex(6)}",
            email=email,
            email_verified=True,
            first_name="A",
            last_name="B",
            invite_token=invite_token,
        )
    )


async def test_oauth_register_invite_email_mismatch_rejected(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # A ticket for email A cannot consume an invite issued to email B.
    invitee = _unique_email("invitee")
    token = await _pending_invite(real_session, org=org_admin, email=invitee)
    attacker = _ticket(email=_unique_email("attacker"), invite_token=token)
    with pytest.raises(oauth_service.OAuthRegistrationError, match="does not match"):
        await oauth_service.complete_registration(
            real_session, attacker, first_name="A", last_name="B", org_name=None, invite_token=None
        )


async def test_oauth_register_via_invite_respects_provider_gate(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # The org forbids Google; an invited user completing OAuth-register with
    # Google must be blocked (the mock-based HTTP test can't exercise this).
    await org_settings_service.update(real_session, org_admin.org_id, allow_login_google=False)
    await real_session.commit()
    invitee = _unique_email("gated-invitee")
    token = await _pending_invite(real_session, org=org_admin, email=invitee)
    ticket = _ticket(email=invitee, provider="google", invite_token=token)
    with pytest.raises(OAuthLoginBlockedError) as exc:
        await oauth_service.complete_registration(
            real_session, ticket, first_name="A", last_name="B", org_name=None, invite_token=None
        )
    assert exc.value.reason == "provider_disabled"


async def test_create_user_stores_email_domain(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from tests.conftest import _unique_email

    email = _unique_email("Domain.Test").upper()  # exercise normalization
    user = await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=email,
        first_name="Dom",
        last_name="Ain",
        password=None,
    )
    await real_session.commit()
    assert user.email == email.lower()
    assert user.email_domain == "alkera.dev"


# --- account pre-hijack defense (claim of never-verified accounts) ----------


async def test_resolve_claims_unverified_account_and_evicts_squatter(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # An UNVERIFIED account with a password models a squat (e.g. a bare password
    # signup at the victim's address). A verified provider login with a NEW
    # (provider, subject) must CLAIM it: link the identity, wipe the password,
    # mark the email verified, and bump token_epoch so prior sessions die.
    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    assert member.email_verified_at is None
    assert member.password_hash is not None
    assert member.token_epoch.year == 1970  # never revoked → default epoch

    profile = FederatedProfile(
        provider="google",
        subject=f"g-{secrets.token_hex(6)}",
        email=member.email,
        email_verified=True,
    )
    outcome = await oauth_service.resolve(real_session, profile, invite_token=None)
    assert isinstance(outcome, LoginOutcome)
    assert outcome.user.id == member.id

    # Re-read from the DB (revoke_all_for_user is a bulk UPDATE that bypasses
    # the in-memory object).
    await real_session.refresh(member)
    assert member.password_hash is None  # squatter's password evicted
    assert member.email_verified_at is not None  # provider vouched → verified
    assert member.token_epoch.year > 1970  # revoke-all bumped the epoch

    identity = (
        await real_session.execute(
            select(OAuthIdentity).where(OAuthIdentity.subject == profile.subject)
        )
    ).scalar_one()
    assert identity.user_id == member.id


async def test_resolve_does_not_touch_verified_account_on_autolink(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # A legitimate, already-verified account must auto-link WITHOUT credential
    # or session disruption — the claim path is reserved for unverified accounts.
    member, _pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    pw_before = member.password_hash
    assert pw_before is not None
    assert member.token_epoch.year == 1970

    profile = FederatedProfile(
        provider="github",  # allowed by default
        subject=f"h-{secrets.token_hex(6)}",
        email=member.email,
        email_verified=True,
    )
    outcome = await oauth_service.resolve(real_session, profile, invite_token=None)
    assert isinstance(outcome, LoginOutcome)

    await real_session.refresh(member)
    assert member.password_hash == pw_before  # untouched
    assert member.token_epoch.year == 1970  # no revoke-all on a verified account


async def test_resolve_already_linked_refusal_leaves_unverified_account_intact(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # Ordering guard: the pre-hijack cleanup must run only AFTER a successful
    # link. An unverified account that already has google linked, hit by a
    # SECOND google account (same email, new subject), must be refused with
    # `already_linked` and left fully intact — no password wipe, no revoke-all,
    # no premature verification.
    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    member_id = member.id  # capture before the rollback below expires the object
    await _link(
        real_session, user_id=member_id, provider="google", subject=f"g-{secrets.token_hex(6)}"
    )

    profile = FederatedProfile(
        provider="google",
        subject=f"g-other-{secrets.token_hex(6)}",
        email=member.email,
        email_verified=True,
    )
    with pytest.raises(OAuthLoginBlockedError) as exc:
        await oauth_service.resolve(real_session, profile, invite_token=None)
    assert exc.value.reason == "already_linked"

    # The failed link left the session needing a rollback (mirrors get_db on a
    # refused login); re-read from a clean state to prove nothing persisted.
    await real_session.rollback()
    fresh = await user_service.get_by_id(real_session, member_id)
    assert fresh is not None
    assert fresh.password_hash is not None  # NOT wiped on a refusal
    assert fresh.email_verified_at is None  # NOT prematurely verified
    assert fresh.token_epoch.year == 1970  # NOT revoked


async def test_clear_password_wipes_hash_and_reset_token(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from datetime import UTC, datetime, timedelta

    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    member.password_reset_token = secrets.token_urlsafe(16)
    member.password_reset_expires_at = datetime.now(UTC) + timedelta(hours=1)
    await real_session.flush()
    assert member.password_hash is not None

    await user_service.clear_password(real_session, member)
    assert member.password_hash is None
    assert member.password_reset_token is None
    assert member.password_reset_expires_at is None
