"""Session token lifetime: signature enforcement, and sliding renewal.

Two external findings meet here.

*"Improper JWT signature verification"* claimed a signature-stripped token was
accepted. The endpoint it was demonstrated on — `GET /auth/oauth/providers` — is
PUBLIC by design (the signed-out login page renders its buttons from it), so it
never looked at the cookie at all. The claim is pinned both ways below: the
public endpoint stays public, and every mangled-token shape is refused on
endpoints that actually authenticate.

*"JWT with excessive time-to-live"* was real: production issued a 30-day
absolute token. The remedy is the standard trio -- a fixed absolute lifetime, a
sliding idle window, and server-side revocation -- with production values that
mean something. The second half of this file pins the part that bounds a stolen
cookie: `exp` is fixed at authentication and nothing extends it, because an
attacker replaying a cookie is indistinguishable from the user at every
activity-based check.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from alkera_core.auth import COOKIE_NAME
from alkera_core.auth.tokens import encode_session_token
from alkera_core.config import settings
from freezegun import freeze_time
from httpx import AsyncClient

# Endpoints that MUST authenticate. One per permission tier, because the report's
# real worry was "does this pattern exist elsewhere in the framework".
PROTECTED = [
    pytest.param("/api/v1/auth/me", id="identity"),
    pytest.param("/api/v1/teams", id="org-scoped"),
    pytest.param("/api/v1/dashboard", id="product"),
    pytest.param("/admin/v1/orgs", id="platform-staff"),
]


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return client.cookies[COOKIE_NAME]


def _strip_signature(token: str) -> str:
    header, payload, _ = token.split(".")
    return f"{header}.{payload}."


def _alg_none(token: str) -> str:
    """Re-encode the same claims under `alg: none` — the classic JWT bypass the
    report's remediation section names explicitly."""
    claims = jwt.decode(token, options={"verify_signature": False})
    return jwt.encode(claims, key="", algorithm="none")


def _wrong_secret(token: str) -> str:
    claims = jwt.decode(token, options={"verify_signature": False})
    return jwt.encode(claims, "not-the-signing-secret", algorithm="HS256")


def _reclaim(token: str, **changes: object) -> str:
    """The original header and signature over a REWRITTEN payload — the shape a
    signature check that runs but is not enforced would wave through."""
    header, _, signature = token.split(".")
    claims = jwt.decode(token, options={"verify_signature": False})
    claims.update(changes)
    forged = jwt.encode(claims, "x", algorithm="HS256").split(".")[1]
    return f"{header}.{forged}.{signature}"


def _extended_expiry(token: str) -> str:
    """A payload edited only to outlive its own expiry — the change an attacker
    holding a captured cookie actually wants."""
    claims = jwt.decode(token, options={"verify_signature": False})
    return _reclaim(token, exp=int(claims["exp"]) + 86_400 * 365)


class TestSignatureEnforcement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", PROTECTED)
    async def test_no_credential_is_refused(self, client: AsyncClient, path: str) -> None:
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path} answered {resp.status_code}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", PROTECTED)
    @pytest.mark.parametrize(
        "mangle",
        [
            pytest.param(_strip_signature, id="signature-stripped"),
            pytest.param(_alg_none, id="alg-none"),
            pytest.param(_wrong_secret, id="signed-with-a-different-secret"),
            pytest.param(_extended_expiry, id="expiry-extended-signature-kept"),
            pytest.param(lambda _t: "not.a.jwt", id="garbage"),
            pytest.param(lambda _t: "", id="empty"),
        ],
    )
    async def test_mangled_cookie_is_refused(
        self, client: AsyncClient, platform_admin, path: str, mangle
    ) -> None:
        real = await _login(client, platform_admin.admin_email, platform_admin.admin_password)
        client.cookies.set(COOKIE_NAME, mangle(real))
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path} accepted a mangled token ({resp.status_code})"

    @pytest.mark.asyncio
    async def test_mangled_bearer_is_refused(self, client: AsyncClient, platform_admin) -> None:
        """The CLI's transport gets the same treatment as the browser's."""
        real = await _login(client, platform_admin.admin_email, platform_admin.admin_password)
        client.cookies.delete(COOKIE_NAME)
        resp = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {_strip_signature(real)}"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_a_forged_platform_role_does_not_grant_staff_access(
        self, client: AsyncClient, org_admin
    ) -> None:
        """The concrete escalation the finding warns about: an ordinary org admin
        rewrites `platform_role` into their own cookie and asks for the internal
        console. The claim is signed, so editing it invalidates the token — the
        answer is 401, not a staff-shaped 200 or even a 403."""
        real = await _login(client, org_admin.admin_email, org_admin.admin_password)
        assert (await client.get("/admin/v1/orgs")).status_code == 403

        client.cookies.set(COOKIE_NAME, _reclaim(real, platform_role="alkera_admin"))
        assert (await client.get("/admin/v1/orgs")).status_code == 401

    @pytest.mark.asyncio
    async def test_a_forged_subject_does_not_impersonate_another_account(
        self, client: AsyncClient, org_admin, platform_admin
    ) -> None:
        """Swapping `sub` to a staff account's id is the other half of the same
        forgery."""
        real = await _login(client, org_admin.admin_email, org_admin.admin_password)
        client.cookies.set(COOKIE_NAME, _reclaim(real, sub=platform_admin.admin_id.hex))
        assert (await client.get("/api/v1/auth/me")).status_code == 401

    @pytest.mark.asyncio
    async def test_oauth_providers_is_public_by_design(self, client: AsyncClient) -> None:
        """The endpoint the finding was demonstrated on. It lists which providers
        the PLATFORM has credentials for, so the signed-out login page can render
        its buttons — there is no session to check and no per-user data in the
        answer. Pinned so nobody "fixes" it into requiring auth and breaks the
        login page for every signed-out visitor."""
        resp = await client.get("/api/v1/auth/oauth/providers")
        assert resp.status_code == 200
        assert set(resp.json()) == {"providers"}

    @pytest.mark.asyncio
    async def test_a_mangled_cookie_does_not_grant_anything_on_the_public_endpoint(
        self, client: AsyncClient
    ) -> None:
        """The other half of the same point: the public endpoint IGNORES the
        cookie, it does not TRUST it. Its answer is identical with a garbage
        cookie attached and with none at all."""
        anonymous = (await client.get("/api/v1/auth/oauth/providers")).json()
        client.cookies.set(COOKIE_NAME, "not.a.jwt")
        assert (await client.get("/api/v1/auth/oauth/providers")).json() == anonymous


@pytest.mark.asyncio
async def test_issued_session_lifetime_is_bounded(
    client: AsyncClient, org_admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding in one assertion: a browser access token issued today must
    not outlive an hour. 30 days was the value first reported and 7 days the
    retest still called excessive; the ceiling below is what production is
    allowed to configure up to (`_validate_production`). Pinned against the
    shipped default, not whatever a developer's `.env` says."""
    monkeypatch.setattr(
        settings,
        "auth_token_ttl_seconds",
        type(settings).model_fields["auth_token_ttl_seconds"].default,
    )
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    claims = jwt.decode(client.cookies[COOKIE_NAME], options={"verify_signature": False})
    lifetime = timedelta(seconds=claims["exp"] - claims["iat"])
    assert lifetime <= timedelta(hours=1), f"session token lives {lifetime}"
    assert datetime.fromtimestamp(claims["exp"], tz=UTC) > datetime.now(UTC)


def _code(resp) -> str | None:
    return resp.json().get("error", {}).get("code")


@pytest.mark.asyncio
async def test_the_access_token_lapses_at_its_ttl_and_says_so(
    client: AsyncClient, org_admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driven across the boundary: the same cookie authenticates a minute before
    its `exp` and is refused a second after it — with the one 401 code that
    invites a refresh, so a client never mistakes an expired token for a
    revoked session (or the reverse)."""
    monkeypatch.setattr(settings, "auth_token_ttl_seconds", 30 * 60)
    # After the fixtures' real clock, so the frozen `iat` never predates the
    # account's own token_epoch (a revoke-all lever, not an expiry).
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    with freeze_time(start, real_asyncio=True) as frozen:
        token = await _login(client, org_admin.admin_email, org_admin.admin_password)
        # Sent as a raw header: the client's jar would drop the cookie at its
        # max-age, and the point is what the SERVER says to a token past `exp`.
        replay = {"Cookie": f"{COOKIE_NAME}={token}"}
        frozen.move_to(start + timedelta(minutes=29))
        assert (await client.get("/api/v1/auth/me", headers=replay)).status_code == 200
        frozen.move_to(start + timedelta(minutes=30, seconds=1))
        resp = await client.get("/api/v1/auth/me", headers=replay)
        assert resp.status_code == 401
        assert _code(resp) == "token_expired"
        assert resp.cookies.get(COOKIE_NAME) is None


@pytest.mark.asyncio
async def test_the_401_codes_are_distinct(client: AsyncClient, org_admin) -> None:
    """A refresh is only ever the answer to `token_expired`. A revoked session and
    a tampered token are each refused under their own code, so a client holding
    a refresh credential cannot be talked into renewing either."""
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    live = client.cookies[COOKIE_NAME]
    assert (await client.post("/api/v1/auth/logout")).status_code == 200
    client.cookies.set(COOKIE_NAME, live)
    revoked = await client.get("/api/v1/auth/me")
    assert (revoked.status_code, _code(revoked)) == (401, "session_revoked")

    client.cookies.set(COOKIE_NAME, _wrong_secret(live))
    forged = await client.get("/api/v1/auth/me")
    assert (forged.status_code, _code(forged)) == (401, "unauthorized")

    client.cookies.delete(COOKIE_NAME)
    missing = await client.get("/api/v1/auth/me")
    assert (missing.status_code, _code(missing)) == (401, "unauthorized")


@pytest.mark.asyncio
async def test_using_the_session_never_extends_its_absolute_expiry(
    client: AsyncClient, org_admin
) -> None:
    """The property the absolute bound rests on, and the one worth guarding: use
    does not buy time.

    A control that re-issues or extends a session on activity cannot bound a
    stolen cookie, because an attacker replaying it is indistinguishable from
    the user at exactly the moment the decision is made -- they renew it by
    making requests, as the real user does. So `exp` is fixed at authentication
    and no request path may move it. The idle window may slide (that ends
    ABANDONED sessions); this may not.
    """
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    original = client.cookies[COOKIE_NAME]
    first = jwt.decode(original, options={"verify_signature": False})

    for _ in range(3):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.cookies.get(COOKIE_NAME) is None, "a request re-issued the session cookie"

    assert client.cookies[COOKIE_NAME] == original
    assert jwt.decode(original, options={"verify_signature": False})["exp"] == first["exp"]


@pytest.mark.asyncio
async def test_an_expired_session_is_refused(client: AsyncClient, org_admin) -> None:
    """Past `exp` the session is over, with nothing that can revive it."""
    expired, _ = encode_session_token(
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_team_id=org_admin.org_id,
        platform_role=None,
        now=int(time.time() - settings.auth_token_ttl_seconds - 60),
    )
    client.cookies.set(COOKIE_NAME, expired)
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.cookies.get(COOKIE_NAME) is None


@pytest.mark.asyncio
async def test_logout_ends_the_session_server_side(client: AsyncClient, org_admin) -> None:
    """Revocation is the lever that does not wait for `exp`: a replayed
    post-logout cookie is refused by the server, not merely forgotten by the
    browser."""
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    token = client.cookies[COOKIE_NAME]
    assert (await client.post("/api/v1/auth/logout")).status_code == 200

    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)
    assert (await client.get("/api/v1/auth/me")).status_code == 401
