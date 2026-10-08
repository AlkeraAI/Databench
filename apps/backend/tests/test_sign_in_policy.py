"""The sign-in policy: whether a sign-in may enter an org.

``alkera_core.auth.sign_in_policy.evaluate`` is the one decision every sign-in
path acts on (the password route, the Google/GitHub callbacks, SSO). These
cases pin the rules for one org against real rows — the org's SSO connection,
its login toggles and the person's membership — and that the password route
and the OAuth resolver answer exactly as the policy does. The cases with two
orgs live in ``test_cross_tenant_identity.py``.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from alkera_core.auth import sign_in_policy
from alkera_core.auth.sign_in_policy import Allowed, StepUp
from alkera_core.models import (
    OAuthIdentity,
    OrgMembership,
    OrgSettings,
    PlatformRole,
    SsoConnection,
    User,
)
from backend.auth.oauth import FederatedProfile
from backend.services.identity import oauth as oauth_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, hold_sso_domains

DOMAIN = "governed.example"


async def _person(
    session: AsyncSession,
    org: OrgWithAdmin,
    *,
    domain: str = DOMAIN,
    sso_exempt: bool = False,
    platform_role: PlatformRole | None = None,
) -> tuple[User, str]:
    from backend.services.identity import users as user_service

    password = f"pw-{secrets.token_hex(8)}-Aa1!"
    user = await user_service.create_user(
        session,
        org_team_id=org.org_id,
        email=f"p-{secrets.token_hex(5)}@{domain}",
        first_name="Pat",
        last_name="Person",
        password=password,
    )
    user.platform_role = platform_role
    membership = await session.scalar(
        select(OrgMembership).where(
            OrgMembership.user_id == user.id, OrgMembership.org_team_id == org.org_id
        )
    )
    assert membership is not None
    membership.sso_exempt = sso_exempt
    await session.commit()
    return user, password


async def _sso(
    session: AsyncSession, org: OrgWithAdmin, *, enabled: bool = True, enforced: bool = True
) -> None:
    session.add(
        SsoConnection(
            org_team_id=org.org_id,
            enabled=enabled,
            enforced=enforced,
            protocol="oidc",
        )
    )
    await session.commit()
    await hold_sso_domains(org.org_id, f"{DOMAIN}, other.example")


async def _toggles(session: AsyncSession, org: OrgWithAdmin, **values: bool) -> None:
    row = await session.get(OrgSettings, org.org_id)
    if row is None:
        row = OrgSettings(org_team_id=org.org_id)
        session.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    await session.commit()


async def _evaluate(session: AsyncSession, user: User, method: str) -> Allowed | StepUp:
    return await sign_in_policy.evaluate(
        session, user=user, org_team_id=user.home_org_team_id, family_id=None, method=method
    )


CASES = [
    pytest.param({"sso": True}, {}, "password", "sso_required", id="enforced-sso-refuses-password"),
    pytest.param({"sso": True}, {}, "google", "sso_required", id="enforced-sso-refuses-google"),
    pytest.param({"sso": True}, {}, "sso", None, id="enforced-sso-admits-its-own-sso"),
    pytest.param({"sso": True, "exempt": True}, {}, "password", None, id="sso-exempt"),
    pytest.param({"sso": True, "staff": True}, {}, "password", None, id="platform-staff"),
    pytest.param(
        {"sso": True, "domain": "elsewhere.example"}, {}, "password", None, id="ungoverned-domain"
    ),
    pytest.param(
        {"sso": True, "domain": "elsewhere.example", "linked": True},
        {},
        "password",
        "sso_required",
        id="governed-by-a-linked-identity",
    ),
    pytest.param({"sso": True, "enforced": False}, {}, "password", None, id="sso-not-enforced"),
    pytest.param({"sso": True, "enabled": False}, {}, "password", None, id="sso-disabled"),
    pytest.param(
        {},
        {"allow_login_google": False},
        "google",
        "login_method_not_allowed",
        id="google-disallowed",
    ),
    pytest.param(
        {},
        {"allow_login_github": False},
        "github",
        "login_method_not_allowed",
        id="github-disallowed",
    ),
    pytest.param({}, {"allow_login_google": False}, "github", None, id="google-off-leaves-github"),
    pytest.param(
        {},
        {"allow_login_google": False, "allow_login_github": False},
        "password",
        None,
        id="password-has-no-toggle",
    ),
    pytest.param(
        {"sso": True},
        {"allow_login_google": False},
        "google",
        "sso_required",
        id="sso-is-decided-before-the-toggle",
    ),
    pytest.param(
        {"sso": True, "domain": "elsewhere.example"},
        {"allow_login_google": False},
        "google",
        "login_method_not_allowed",
        id="an-ungoverned-person-meets-the-toggle",
    ),
    pytest.param({}, {}, "mock", None, id="an-untoggled-provider"),
]


@pytest.mark.parametrize(("person", "toggles", "method", "expected"), CASES)
async def test_the_policy(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    person: dict[str, Any],
    toggles: dict[str, bool],
    method: str,
    expected: str | None,
) -> None:
    user, _ = await _person(
        real_session,
        org_admin,
        domain=person.get("domain", DOMAIN),
        sso_exempt=person.get("exempt", False),
        platform_role=PlatformRole.ALKERA_SUPPORT if person.get("staff") else None,
    )
    if person.get("sso"):
        await _sso(
            real_session,
            org_admin,
            enabled=person.get("enabled", True),
            enforced=person.get("enforced", True),
        )
    if person.get("linked"):
        real_session.add(
            OAuthIdentity(
                user_id=user.id,
                provider=sign_in_policy.sso_provider_key(org_admin.org_id),
                subject=f"sub-{secrets.token_hex(4)}",
                email_at_link=user.email,
                email_verified=True,
            )
        )
        await real_session.commit()
    if toggles:
        await _toggles(real_session, org_admin, **toggles)
    outcome = await _evaluate(real_session, user, method)
    if expected is None:
        assert outcome == Allowed()
    else:
        assert isinstance(outcome, StepUp)
        assert outcome.kind == expected
        if expected == "sso_required":
            assert outcome.login_url == sign_in_policy.sso_login_url(
                org_admin.org_id, protocol="oidc"
            )


async def test_the_password_route_answers_as_the_policy_does(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    governed, governed_pw = await _person(real_session, org_admin)
    exempt, exempt_pw = await _person(real_session, org_admin, sso_exempt=True)
    await _sso(real_session, org_admin)
    refused = await client.post(
        "/api/v1/auth/login", json={"email": governed.email, "password": governed_pw}
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "sso_required"
    admitted = await client.post(
        "/api/v1/auth/login", json={"email": exempt.email, "password": exempt_pw}
    )
    assert admitted.status_code == 200, admitted.text


@pytest.mark.parametrize(
    ("toggles", "sso", "reason"),
    [
        pytest.param({"allow_login_google": False}, False, "provider_disabled", id="toggle"),
        pytest.param({}, True, "sso_required", id="enforced-sso"),
    ],
)
async def test_the_oauth_resolver_answers_as_the_policy_does(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    toggles: dict[str, bool],
    sso: bool,
    reason: str,
) -> None:
    user, _ = await _person(real_session, org_admin)
    if toggles:
        await _toggles(real_session, org_admin, **toggles)
    if sso:
        await _sso(real_session, org_admin)
    profile = FederatedProfile(
        provider="google",
        subject=f"g-{secrets.token_hex(4)}",
        email=user.email,
        email_verified=True,
        first_name="Pat",
        last_name="Person",
        raw={},
    )
    with pytest.raises(oauth_service.OAuthLoginBlockedError) as excinfo:
        await oauth_service.resolve(real_session, profile, invite_token=None)
    assert excinfo.value.reason == reason
    await real_session.rollback()
