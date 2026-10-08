"""The controls that reach a person's whole account, across their orgs.

"Sign out everywhere" and a password or email change end every session of the
person and keep the browser that asked signed in. That re-issue lands in the
org the request is in (never the home org by default), only when that org's
sign-in policy still admits the session it replaces, and it can never undo the
revocation before it.

While multi-org is on, a session an org's IdP started speaks for that org
alone: the org's admins run the IdP. It cannot end the person's sessions,
plant a second factor, or see the person's other orgs; only a sign-in as the
person (a password, Google, GitHub, or the home org's own IdP) can.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest
from alkera_core.auth import decode_session_token, revocation
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MembershipStatus, OrgSettings, User
from backend.auth.refusals import ACCOUNT_SIGN_IN_REQUIRED
from freezegun import freeze_time
from sqlalchemy import update
from tests.conftest import TwoOrg, app_client
from tests.test_cross_tenant_auth import REFRESH_COOKIE, _code
from tests.test_cross_tenant_identity import (
    Browser,
    _browser,
    _cli,
    _enforce,
    _refresh,
    _set_membership,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    revocation._cache.reset()
    yield
    revocation._cache.reset()


@pytest.fixture(autouse=True)
def _self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "self_hosted", True)


def _org_of_new_session(resp: httpx.Response) -> UUID | None:
    """The org of the session cookie the response set, or None when it set
    none (or cleared it)."""
    token = resp.cookies.get(settings.auth_cookie_name)
    if not token:
        return None
    return decode_session_token(token).org_team_id


def _cleared(resp: httpx.Response) -> bool:
    cookies = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    return any(
        c.startswith(f"{settings.auth_cookie_name}=") and "Max-Age=0" in c for c in cookies
    ) and any(c.startswith(f"{REFRESH_COOKIE}=") and "Max-Age=0" in c for c in cookies)


async def _post(path: str, browser: Browser, json: object | None = None) -> httpx.Response:
    async with app_client() as c:
        return await c.post(path, json=json, headers={"Cookie": browser.cookies})


async def _get(path: str, credential: Browser | str) -> httpx.Response:
    headers = (
        {"Cookie": credential.cookies}
        if isinstance(credential, Browser)
        else {"Authorization": f"Bearer {credential}"}
    )
    async with app_client() as c:
        return await c.get(path, headers=headers)


async def _family_revoked(browser: Browser) -> bool:
    resp = await _refresh(browser)
    return resp.status_code == 401 and _code(resp) == "session_revoked"


async def _disallow_google(org: UUID) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(OrgSettings, org)
        if row is None:
            db.add(OrgSettings(org_team_id=org, allow_login_google=False))
        else:
            row.allow_login_google = False
        await db.commit()


# --- the re-issue: in the request's org, under its policy ------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_sign_out_everywhere_from_b_keeps_a_session_in_b_never_in_enforcing_a(
    two_org_identity: TwoOrg,
) -> None:
    """Home org A enforces SSO; the person signed in to B with a password. The
    kept session is B's: nothing mints into A without A's IdP."""
    t = two_org_identity
    await _enforce(t.org_a)
    acting = await _browser(t.user.id, t.org_b)
    resp = await _post("/api/v1/auth/logout-all", acting)
    assert resp.status_code == 200, resp.text
    assert _org_of_new_session(resp) == t.org_b


@pytest.mark.usefixtures("multi_org")
async def test_a_password_change_in_b_keeps_a_session_in_b(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _enforce(t.org_a)
    acting = await _browser(t.user.id, t.org_b)
    async with app_client() as c:
        resp = await c.patch(
            f"/api/v1/users/{t.user.id}",
            json={"password": "a-new-password-98765!", "current_password": t.password},
            headers={"Cookie": acting.cookies},
        )
    assert resp.status_code == 200, resp.text
    assert _org_of_new_session(resp) == t.org_b


@pytest.mark.usefixtures("multi_org")
async def test_sign_out_everywhere_revokes_every_family_when_the_home_membership_is_gone(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    acting = await _browser(t.user.id, t.org_b)
    other = await _browser(t.user.id, t.org_b)
    await _set_membership(t.user.id, t.org_a, status=MembershipStatus.DEACTIVATED)
    resp = await _post("/api/v1/auth/logout-all", acting)
    assert resp.status_code == 200, resp.text
    assert await _family_revoked(other)
    assert await _family_revoked(acting)


async def test_sign_out_everywhere_signs_this_browser_out_when_its_org_no_longer_admits_it(
    two_org_identity: TwoOrg,
) -> None:
    """The org switched off the only way this session signed in: the re-issue
    is refused like a refresh would be, the browser is signed out, and every
    other session still ends."""
    t = two_org_identity
    acting = await _browser(t.user.id, t.org_a, method="google")
    other = await _browser(t.user.id, t.org_a)
    await _disallow_google(t.org_a)
    resp = await _post("/api/v1/auth/logout-all", acting)
    assert resp.status_code == 200, resp.text
    assert _org_of_new_session(resp) is None
    assert _cleared(resp)
    assert await _family_revoked(other)


async def test_sign_out_everywhere_keeps_this_browser_in_its_enforcing_org_by_default(
    two_org_identity: TwoOrg,
) -> None:
    """Flag off, an SSO-enforced org: the person who asked stays signed in, as
    before (their session was admitted, and re-issuing it proves nothing new
    or less)."""
    t = two_org_identity
    acting = await _browser(t.user.id, t.org_a, method="sso")
    await _enforce(t.org_a)
    resp = await _post("/api/v1/auth/logout-all", acting)
    assert resp.status_code == 200, resp.text
    assert _org_of_new_session(resp) == t.org_a


# --- account-wide controls from an org's IdP --------------------------------------------


ACCOUNT_WIDE_WRITES = [
    pytest.param("logout-all", id="sign-out-everywhere"),
    pytest.param("revoke-session", id="end-one-session"),
]


async def _account_wide_write(kind: str, acting: Browser, other: Browser) -> httpx.Response:
    if kind == "logout-all":
        return await _post("/api/v1/auth/logout-all", acting)
    async with app_client() as c:
        return await c.delete(
            f"/api/v1/auth/sessions/{other.family_id.hex}", headers={"Cookie": acting.cookies}
        )


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("kind", ACCOUNT_WIDE_WRITES)
@pytest.mark.parametrize(
    ("method", "org", "allowed"),
    [
        pytest.param("sso", "b", False, id="b-idp-session"),
        pytest.param("password", "b", True, id="password-session-in-b"),
        pytest.param("sso", "a", True, id="home-org-idp-session"),
    ],
)
async def test_only_a_sign_in_as_the_person_ends_their_sessions(
    two_org_identity: TwoOrg, kind: str, method: str, org: str, allowed: bool
) -> None:
    t = two_org_identity
    where = t.org_a if org == "a" else t.org_b
    acting = await _browser(t.user.id, where, method=method)
    other = await _browser(t.user.id, t.org_a)
    resp = await _account_wide_write(kind, acting, other)
    if allowed:
        assert resp.status_code == 200, resp.text
        assert await _family_revoked(other)
    else:
        assert resp.status_code == 403
        assert _code(resp) == "account_sign_in_required"
        assert (await _refresh(other)).status_code == 200


@pytest.mark.parametrize("kind", ACCOUNT_WIDE_WRITES)
async def test_an_org_idp_session_ends_sessions_as_before_while_multi_org_is_off(
    two_org_identity: TwoOrg, kind: str
) -> None:
    t = two_org_identity
    acting = await _browser(t.user.id, t.org_a, method="sso")
    other = await _browser(t.user.id, t.org_a)
    resp = await _account_wide_write(kind, acting, other)
    assert resp.status_code == 200, resp.text
    assert await _family_revoked(other)


async def _no_password(user_id: UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id).values(password_hash=None))
        await db.commit()


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize(
    ("method", "org", "has_password", "body", "expected"),
    [
        pytest.param("sso", "b", False, None, "account_sign_in_required", id="b-idp-no-password"),
        pytest.param("sso", "b", True, None, "current_password_required", id="b-idp-asks-password"),
        pytest.param("sso", "b", True, "right", None, id="b-idp-with-the-password"),
        pytest.param("password", "b", True, None, None, id="fresh-password-sign-in"),
        pytest.param("sso", "a", False, None, None, id="fresh-home-idp-sign-in"),
    ],
)
async def test_mfa_enrollment_needs_a_recent_sign_in_as_the_person(
    two_org_identity: TwoOrg,
    method: str,
    org: str,
    has_password: bool,
    body: str | None,
    expected: str | None,
) -> None:
    t = two_org_identity
    if not has_password:
        await _no_password(t.user.id)
    acting = await _browser(t.user.id, t.org_a if org == "a" else t.org_b, method=method)
    payload = {"current_password": t.password} if body == "right" else None
    resp = await _post("/api/v1/auth/mfa/enroll", acting, payload)
    if expected is None:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 403
        assert _code(resp) == expected


@pytest.mark.usefixtures("multi_org")
async def test_a_refresh_is_not_a_recent_sign_in_for_mfa_enrollment(
    two_org_identity: TwoOrg,
) -> None:
    """Every refresh mints a token moments old; the proof is when the person
    last signed in, so a session refreshed long after its sign-in must show
    the password."""
    t = two_org_identity
    with freeze_time("2026-10-04 12:00:00", real_asyncio=True) as frozen:
        session = await _browser(t.user.id, t.org_b)
        frozen.move_to("2026-10-04 12:14:00")
        fresh = await _post("/api/v1/auth/mfa/enroll", session)
        assert fresh.status_code == 200, fresh.text
        frozen.move_to("2026-10-04 12:40:00")
        refreshed = await _refresh(session)
        assert refreshed.status_code == 200, refreshed.text
        renewed = Browser(
            access=refreshed.cookies[settings.auth_cookie_name],
            refresh=refreshed.cookies[REFRESH_COOKIE],
            family_id=session.family_id,
        )
        stale = await _post("/api/v1/auth/mfa/enroll", renewed)
    assert stale.status_code == 403
    assert _code(stale) == "current_password_required"


async def test_a_fresh_token_still_admits_mfa_enrollment_while_multi_org_is_off(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _no_password(t.user.id)
    acting = await _browser(t.user.id, t.org_a, method="sso")
    resp = await _post("/api/v1/auth/mfa/enroll", acting)
    assert resp.status_code == 200, resp.text


# --- account-wide reads ----------------------------------------------------------------


def _org_ids(resp: httpx.Response) -> set[str]:
    return {m["org_team_id"] for m in resp.json()["memberships"]}


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize(
    ("method", "org", "sees_both"),
    [
        pytest.param("sso", "b", False, id="b-idp-session-sees-b-alone"),
        pytest.param("password", "b", True, id="password-session-sees-both"),
        pytest.param("sso", "a", True, id="home-idp-session-sees-both"),
    ],
)
async def test_the_orgs_list_shows_other_orgs_only_to_a_sign_in_as_the_person(
    two_org_identity: TwoOrg, method: str, org: str, sees_both: bool
) -> None:
    t = two_org_identity
    where = t.org_a if org == "a" else t.org_b
    acting = await _browser(t.user.id, where, method=method)
    resp = await _get("/api/v1/auth/memberships", acting)
    assert resp.status_code == 200, resp.text
    expected = {str(t.org_a), str(t.org_b)} if sees_both else {str(where)}
    assert _org_ids(resp) == expected


@pytest.mark.usefixtures("multi_org")
async def test_a_cli_token_ends_no_session_and_still_lists_the_orgs(
    two_org_identity: TwoOrg,
) -> None:
    """Nothing records how the session that approved a CLI token signed in:
    it may not end the person's sessions, and it keeps listing their orgs for
    the CLI and the editor's org switcher."""
    t = two_org_identity
    other = await _browser(t.user.id, t.org_a)
    token = await _cli(t.user.id, t.org_b)
    async with app_client() as c:
        refused = await c.post(
            "/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {token}"}
        )
    assert refused.status_code == 403
    assert _code(refused) == "account_sign_in_required"
    # Worded for a CLI, not for a browser that signed in through an org's IdP.
    assert refused.json()["error"]["message"] == ACCOUNT_SIGN_IN_REQUIRED.client
    assert (await _refresh(other)).status_code == 200
    resp = await _get("/api/v1/auth/memberships", token)
    assert resp.status_code == 200, resp.text
    assert _org_ids(resp) == {str(t.org_a), str(t.org_b)}


async def test_the_orgs_list_is_unchanged_while_multi_org_is_off(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    resp = await _get("/api/v1/auth/memberships", t.token_a)
    assert resp.status_code == 200, resp.text
    assert str(t.org_a) in _org_ids(resp)


@pytest.mark.usefixtures("multi_org")
async def test_the_sessions_list_from_b_idp_shows_only_bs_browser_sessions(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    in_a = await _browser(t.user.id, t.org_a)
    acting = await _browser(t.user.id, t.org_b, method="sso")
    narrowed = await _get("/api/v1/auth/sessions", acting)
    assert narrowed.status_code == 200, narrowed.text
    listed = {s["jti"] for s in narrowed.json()["sessions"]}
    assert acting.family_id.hex in listed
    assert in_a.family_id.hex not in listed

    own = await _browser(t.user.id, t.org_b)
    full = await _get("/api/v1/auth/sessions", own)
    assert in_a.family_id.hex in {s["jti"] for s in full.json()["sessions"]}


async def _security_events_naming_a(t: TwoOrg) -> None:
    from backend.services.audit import identity_security

    async with AsyncSessionLocal() as db:
        await identity_security.record(
            db, user_id=t.user.id, event="auth.mfa_enabled", org_team_id=t.org_a
        )
        await identity_security.record(
            db,
            user_id=t.user.id,
            event="auth.org_joined",
            org_team_id=t.org_b,
            detail={"joined_org_team_id": str(t.org_a)},
        )
        await identity_security.record(
            db,
            user_id=t.user.id,
            event="auth.sso_linked",
            org_team_id=t.org_b,
            detail={"linked_org_team_id": str(t.org_b), "joined": True},
        )
        await db.commit()


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize(
    ("method", "redacted"),
    [
        pytest.param("sso", True, id="b-idp-session"),
        pytest.param("password", False, id="password-session"),
    ],
)
async def test_the_security_log_read_from_b_idp_names_no_other_org(
    two_org_identity: TwoOrg, method: str, redacted: bool
) -> None:
    t = two_org_identity
    await _security_events_naming_a(t)
    acting = await _browser(t.user.id, t.org_b, method=method)
    resp = await _get("/api/v1/me/security-events", acting)
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The sign-ins that minted these sessions are on the log too; the three
    # seeded rows are the ones that name another org.
    events = [e for e in resp.json()["events"] if e["event"] != "auth.signed_in"]
    assert len(events) == 3
    if redacted:
        assert str(t.org_a) not in body
        linked = next(e for e in events if e["event"] == "auth.sso_linked")
        assert linked["org_team_id"] == str(t.org_b)
        assert linked["detail"] == {"linked_org_team_id": str(t.org_b), "joined": True}
    else:
        assert str(t.org_a) in body


async def test_the_security_log_keeps_every_org_while_multi_org_is_off(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _security_events_naming_a(t)
    acting = await _browser(t.user.id, t.org_a, method="sso")
    resp = await _get("/api/v1/me/security-events", acting)
    assert resp.status_code == 200, resp.text
    assert str(t.org_b) in resp.text


@pytest.mark.usefixtures("multi_org")
async def test_a_session_from_before_sign_ins_were_recorded_stands_for_the_account(
    two_org_identity: TwoOrg,
) -> None:
    """Such a session could only have been the person signing in to their home
    org, so it keeps every account-wide control."""
    from sqlalchemy import text

    t = two_org_identity
    acting = await _browser(t.user.id, t.org_a)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM auth_session_org_grants WHERE family_id = :f"),
            {"f": acting.family_id},
        )
        await db.commit()
    resp = await _get("/api/v1/auth/memberships", acting)
    assert resp.status_code == 200, resp.text
    assert _org_ids(resp) == {str(t.org_a), str(t.org_b)}


@pytest.mark.usefixtures("multi_org")
async def test_a_b_idp_session_may_still_end_itself(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    acting = await _browser(t.user.id, t.org_b, method="sso")
    async with app_client() as c:
        resp = await c.delete(
            f"/api/v1/auth/sessions/{acting.family_id.hex}", headers={"Cookie": acting.cookies}
        )
    assert resp.status_code == 200, resp.text
    assert await _family_revoked(acting)
