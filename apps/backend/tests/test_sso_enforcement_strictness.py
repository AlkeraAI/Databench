"""How hard an org that requires single sign-on holds its members.

By default (``sso_strict_enforcement_enabled`` off) an enforced org is held
exactly as before sign-ins were recorded per session: a password, Google or
GitHub sign-in of a governed person is refused, and a session the org admitted
stays signed in for its normal lifetime. Nothing forces a member back through
the IdP after a deploy or once a day, a device approval in the org the session
is in goes through, and turning enforcement on ends no CLI token or access key.

With the setting on, every refresh needs a sign-in through the org's IdP within
the connection's max age, SCIM governs, and turning enforcement on ends the
governed members' credentials. Each case runs both ways, so the default and the
strict feature are pinned side by side.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alkera_core.auth import revocation
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OrgMembership
from freezegun import freeze_time
from sqlalchemy import text, update
from tests.conftest import TwoOrg, app_client, hold_sso_domains
from tests.test_cross_tenant_auth import REFRESH_COOKIE, _code
from tests.test_cross_tenant_identity import (
    Browser,
    _all_standing,
    _approve,
    _browser,
    _ended,
    _enforce,
    _evaluate,
    _footprint,
    _grant,
    _kind,
    _pat_status,
    _point,
    _put_sso,
    _refresh,
    _status,
    _verify,
)

pytestmark = pytest.mark.asyncio

STRICTNESS = [
    pytest.param(False, id="default"),
    pytest.param(True, id="strict"),
]


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    revocation._cache.reset()
    yield
    revocation._cache.reset()


@pytest.fixture(autouse=True)
def _self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSO is an Enterprise surface; a self-hosted deployment has it."""
    monkeypatch.setattr(settings, "self_hosted", True)


def _strict(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(settings, "sso_strict_enforcement_enabled", on)


async def _forget_grants(family_id: object) -> None:
    """Make a family look like one started before sign-ins were recorded."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM auth_session_org_grants WHERE family_id = :f"), {"f": family_id}
        )
        await db.commit()


def _renewed(resp: httpx.Response, family: Browser) -> Browser:
    """The browser after a refresh: the rotated pair, the same family."""
    return Browser(
        access=resp.cookies[settings.auth_cookie_name],
        refresh=resp.cookies[REFRESH_COOKIE],
        family_id=family.family_id,
    )


# --- refresh ----------------------------------------------------------------------------


@pytest.mark.parametrize("strict", STRICTNESS)
async def test_a_family_from_before_the_deploy_keeps_refreshing_in_an_enforced_org(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    """The first refresh after the deploy, for a session that has no grant rows
    (every session started before it), is not a step-up by default."""
    _strict(monkeypatch, strict)
    t = two_org_identity
    session = await _browser(t.user.id, t.org_a)
    await _forget_grants(session.family_id)
    await _point(session.family_id, None)
    await _enforce(t.org_a)
    resp = await _refresh(session)
    if strict:
        assert resp.status_code == 401
        assert _code(resp) == "sso_required"
    else:
        assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("strict", STRICTNESS)
async def test_turning_enforcement_on_does_not_sign_out_a_password_session(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    _strict(monkeypatch, strict)
    t = two_org_identity
    session = await _browser(t.user.id, t.org_a, method="password")
    await _enforce(t.org_a)
    resp = await _refresh(session)
    assert (resp.status_code, _code(resp) if resp.status_code != 200 else None) == (
        (401, "sso_required") if strict else (200, None)
    )


@pytest.mark.parametrize("strict", STRICTNESS)
async def test_crossing_the_max_age_and_a_day_forces_no_sso_by_default(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    """An SSO session refreshes past the connection's max age, and past the
    24 hours a connection defaults to, without a round trip to the IdP; only
    strict enforcement sends it back."""
    _strict(monkeypatch, strict)
    t = two_org_identity
    await _enforce(t.org_a, max_age=86_400)
    with freeze_time("2026-10-04 12:00:00", real_asyncio=True) as frozen:
        session = await _browser(t.user.id, t.org_a, method="sso")
        frozen.move_to("2026-10-05 11:59:00")
        within = await _refresh(session)
        assert within.status_code == 200, within.text
        current = _renewed(within, session)
        frozen.move_to("2026-10-05 12:00:01")
        crossed = await _refresh(current)
        if strict:
            assert crossed.status_code == 401
            assert _code(crossed) == "sso_required"
            return
        assert crossed.status_code == 200, crossed.text
        current = _renewed(crossed, session)
        frozen.move_to("2026-10-06 12:30:00")
        next_day = await _refresh(current)
    assert next_day.status_code == 200, next_day.text


# --- sign-in methods: the same answer as before, whatever the setting ----------------------


@pytest.mark.parametrize("strict", STRICTNESS)
@pytest.mark.parametrize(
    ("method", "expected"),
    [
        pytest.param("password", "sso_required", id="password-refused"),
        pytest.param("google", "sso_required", id="google-refused"),
        pytest.param("github", "sso_required", id="github-refused"),
        pytest.param("sso", None, id="the-orgs-own-sso-admitted"),
    ],
)
async def test_a_fresh_sign_in_meets_enforcement_the_same_way_either_way(
    two_org_identity: TwoOrg,
    monkeypatch: pytest.MonkeyPatch,
    strict: bool,
    method: str,
    expected: str | None,
) -> None:
    """A governed member's sign-in into an enforced org: only the org's IdP
    enters, as before (password and the social providers were refused then,
    too)."""
    _strict(monkeypatch, strict)
    t = two_org_identity
    await _enforce(t.org_a)
    assert _kind(await _evaluate(t.user.id, t.org_a, None, method)) == expected


async def _scim_provisioned_outside_the_domains(t: TwoOrg) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(OrgMembership)
            .where(OrgMembership.user_id == t.user.id, OrgMembership.org_team_id == t.org_a)
            .values(scim_external_id="okta-1")
        )
        await db.commit()


@pytest.mark.parametrize("strict", STRICTNESS)
async def test_scim_alone_governs_only_under_strict_enforcement(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    _strict(monkeypatch, strict)
    t = two_org_identity
    await _enforce(t.org_a, domains="elsewhere.example")
    await _scim_provisioned_outside_the_domains(t)
    expected = "sso_required" if strict else None
    assert _kind(await _evaluate(t.user.id, t.org_a, None, "password")) == expected


# --- device approval --------------------------------------------------------------------


@pytest.mark.parametrize("strict", STRICTNESS)
async def test_device_approval_in_the_sessions_own_org_goes_through_by_default(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    _strict(monkeypatch, strict)
    t = two_org_identity
    session = await _browser(t.user.id, t.org_a)
    await _enforce(t.org_a, max_age=3600)
    await _grant(session.family_id, t.org_a, "sso", at=datetime.now(UTC) - timedelta(hours=30))
    resp = await _approve(session)
    if strict:
        assert resp.status_code == 403
        assert _code(resp) == "sso_required"
    else:
        assert resp.status_code == 200, resp.text


# --- turning enforcement on -------------------------------------------------------------


@pytest.mark.parametrize("strict", STRICTNESS)
async def test_turning_enforcement_on_ends_cli_tokens_and_access_keys_only_when_strict(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    _strict(monkeypatch, strict)
    t = two_org_identity
    await _verify(t.admin_a.id)
    admin = await _browser(t.admin_a.id, t.org_a)
    assert (await _put_sso(admin, enforced=False)).status_code == 200
    await hold_sso_domains(t.org_a, "alkera.dev")
    await _grant(admin.family_id, t.org_a, "sso")
    held = await _footprint(t, "a")
    resp = await _put_sso(admin, enforced=True)
    assert resp.status_code == 200, resp.text
    if strict:
        assert _ended(await _status(held.cli))
        assert await _pat_status(held.pat) == 401
    else:
        await _all_standing(t, "a", held)


# --- multi-org on: entering another org still takes its IdP -----------------------------


@pytest.mark.usefixtures("multi_org")
async def test_a_password_session_in_b_cannot_switch_into_enforcing_a_by_default(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _enforce(t.org_a)
    session = await _browser(t.user.id, t.org_b)
    async with app_client() as c:
        resp = await c.post(
            "/api/v1/auth/refresh/org",
            json={"org_team_id": str(t.org_a)},
            headers={
                "Cookie": f"{REFRESH_COOKIE}={session.refresh}",
                "X-Requested-With": "alkera",
            },
        )
    assert resp.status_code == 409, resp.text
    assert _code(resp) == "sso_required"
    assert f"/sso/{t.org_a}/login" in resp.json()["error"]["details"]["login_url"]


@pytest.mark.usefixtures("multi_org")
async def test_an_sso_grant_of_any_age_lets_a_session_enter_the_org_by_default(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _enforce(t.org_a, max_age=3600)
    session = await _browser(t.user.id, t.org_b)
    await _grant(session.family_id, t.org_a, "sso", at=datetime.now(UTC) - timedelta(days=3))
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) is None


@pytest.mark.usefixtures("multi_org")
async def test_a_session_that_forgot_its_org_lands_in_enforcing_home_only_through_its_idp(
    two_org_identity: TwoOrg,
) -> None:
    """A family whose org was taken away names none; its next refresh lands in
    the home org, which, enforcing SSO, it never entered: a step-up, not a
    way around the IdP."""
    t = two_org_identity
    await _enforce(t.org_a)
    session = await _browser(t.user.id, t.org_b)
    await _point(session.family_id, None)
    resp = await _refresh(session)
    assert resp.status_code == 401
    assert _code(resp) == "sso_required"
