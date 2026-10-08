"""The origin guard on cookie-authenticated state changes.

The gap this closes is not cross-SITE — `SameSite=Lax` already withholds the
cookie there — it is cross-ORIGIN-but-same-site: another port on the same host
in dev, a sibling subdomain or the content origin in a single-domain install.
The browser sends the session cookie to every one of them, and before this
guard a form POST from such a page could revoke the victim's sessions.

Every case here drives the real app through the real middleware stack, with a
real session in the client's cookie jar, and asserts BOTH the status contract
and whether the victim's session actually survived — a 403 that still performed
the mutation would pass a status-only test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alkera_core.auth import COOKIE_NAME
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from backend.api import csrf
from backend.auth.dependencies import CurrentPrincipal
from backend.services.credentials import ci_tokens as ci_token_service
from backend.services.credentials import pats as pat_service
from backend.services.credentials import proxy_tokens as proxy_token_service
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, app_client, login, mint_cli_token

#: An origin that is cross-ORIGIN but SAME-SITE with a dev deployment on
#: localhost — the exact shape the browser repro used, and the one `SameSite`
#: does not stop.
SAME_SITE_ATTACKER = "http://localhost:29997"
CROSS_SITE_ATTACKER = "https://evil.example"

#: A mutation with no body and no path parameters, so a refusal can only come
#: from the guard and a success is unambiguous: it revokes every session the
#: caller had (the asking browser is handed a fresh one), so the session the
#: client signed in with stops answering `/auth/me`.
MUTATION = "/api/v1/auth/logout-all"

#: The session cookie each client signed in with, replayed to see whether the
#: victim's ORIGINAL session survived.
_LOGIN_COOKIE: dict[int, str] = {}


async def _signed_in(org: OrgWithAdmin, **kwargs: object) -> AsyncClient:
    client = app_client(**kwargs)
    await login(client, org.admin_email, org.admin_password)
    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie
    _LOGIN_COOKIE[id(client)] = cookie
    return client


async def _still_signed_in(client: AsyncClient) -> bool:
    async with app_client() as replay:
        resp = await replay.get(
            "/api/v1/auth/me", headers={"Cookie": f"{COOKIE_NAME}={_LOGIN_COOKIE[id(client)]}"}
        )
    return resp.status_code == 200


# --------------------------------------------------------------------------- #
# The guard itself
# --------------------------------------------------------------------------- #


async def test_same_origin_mutation_succeeds(org_admin: OrgWithAdmin) -> None:
    """The portal's own origin is what the deployment trusts, so the SPA's real
    request goes through untouched."""
    client = await _signed_in(org_admin)
    try:
        resp = await client.post(MUTATION)
        assert resp.status_code == 200
        assert not await _still_signed_in(client)
    finally:
        await client.aclose()


async def test_cross_origin_same_site_form_post_is_refused(org_admin: OrgWithAdmin) -> None:
    """The reported vulnerability: a page on another port of the same host
    carried the cookie and killed the victim's sessions."""
    client = await _signed_in(org_admin)
    try:
        resp = await client.post(
            MUTATION,
            headers={
                "Origin": SAME_SITE_ATTACKER,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="x=1",
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_origin_mismatch"
        # The mutation must not have run — a refusal that still logged the
        # victim out would be no fix at all.
        assert await _still_signed_in(client)
    finally:
        await client.aclose()


async def test_cross_site_origin_is_refused(org_admin: OrgWithAdmin) -> None:
    client = await _signed_in(org_admin)
    try:
        resp = await client.post(MUTATION, headers={"Origin": CROSS_SITE_ATTACKER})
        assert resp.status_code == 403
        assert await _still_signed_in(client)
    finally:
        await client.aclose()


async def test_missing_origin_and_referer_is_refused(org_admin: OrgWithAdmin) -> None:
    """Every browser in support sends `Origin` on a non-safe request, so a
    cookie-authed one without it cannot be shown to be same-origin. Fail closed."""
    client = await _signed_in(org_admin)
    try:
        client.headers.pop("Origin")
        resp = await client.post(MUTATION)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_origin_mismatch"
        assert await _still_signed_in(client)
    finally:
        await client.aclose()


async def test_referer_stands_in_for_a_missing_origin(org_admin: OrgWithAdmin) -> None:
    client = await _signed_in(org_admin)
    try:
        client.headers.pop("Origin")
        resp = await client.post(
            MUTATION, headers={"Referer": f"{settings.frontend_base_url}/settings/profile"}
        )
        assert resp.status_code == 200
        assert not await _still_signed_in(client)
    finally:
        await client.aclose()


async def test_foreign_referer_is_refused(org_admin: OrgWithAdmin) -> None:
    client = await _signed_in(org_admin)
    try:
        client.headers.pop("Origin")
        resp = await client.post(MUTATION, headers={"Referer": f"{SAME_SITE_ATTACKER}/a.html"})
        assert resp.status_code == 403
        assert await _still_signed_in(client)
    finally:
        await client.aclose()


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_an_allowed_origin_is_admitted_whatever_the_fetch_metadata_says(
    org_admin: OrgWithAdmin, site: str
) -> None:
    """A browser marks every request from another origin `same-site` or
    `cross-site`, and sets `Origin` itself where no page can forge it. An
    origin the deployment names is admitted on that name — refusing on the
    fetch metadata first would refuse every portal served from an origin other
    than the API's, which is what the allowlist is for."""
    client = await _signed_in(org_admin)
    try:
        resp = await client.post(
            MUTATION, headers={"Origin": settings.frontend_base_url, "Sec-Fetch-Site": site}
        )
        assert resp.status_code == 200
        assert not await _still_signed_in(client)
    finally:
        await client.aclose()


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(SAME_SITE_ATTACKER, id="same-site-attacker"),
        pytest.param(CROSS_SITE_ATTACKER, id="cross-site-attacker"),
        pytest.param("null", id="null"),
    ],
)
async def test_an_origin_off_the_list_is_refused_under_foreign_fetch_metadata(
    org_admin: OrgWithAdmin, site: str, origin: str
) -> None:
    client = await _signed_in(org_admin)
    try:
        resp = await client.post(MUTATION, headers={"Origin": origin, "Sec-Fetch-Site": site})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_origin_mismatch"
        assert await _still_signed_in(client)
    finally:
        await client.aclose()


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_foreign_fetch_metadata_without_an_origin_is_refused_even_with_a_trusted_referer(
    org_admin: OrgWithAdmin, site: str
) -> None:
    """With no `Origin`, the browser's own statement that the request came
    from elsewhere stands: a `Referer` is not asked to overrule it."""
    client = await _signed_in(org_admin)
    try:
        client.headers.pop("Origin")
        resp = await client.post(
            MUTATION,
            headers={
                "Sec-Fetch-Site": site,
                "Referer": f"{settings.frontend_base_url}/settings/profile",
            },
        )
        assert resp.status_code == 403
        assert await _still_signed_in(client)
    finally:
        await client.aclose()


@pytest.mark.parametrize("site", ["same-origin", "none"])
async def test_fetch_metadata_that_agrees_passes(org_admin: OrgWithAdmin, site: str) -> None:
    client = await _signed_in(org_admin)
    try:
        resp = await client.post(MUTATION, headers={"Sec-Fetch-Site": site})
        assert resp.status_code == 200
    finally:
        await client.aclose()


async def test_safe_method_from_a_foreign_origin_is_untouched(org_admin: OrgWithAdmin) -> None:
    """A GET changes nothing, and CORS — not this guard — decides whether the
    foreign page may read the answer."""
    client = await _signed_in(org_admin)
    try:
        resp = await client.get("/api/v1/auth/me", headers={"Origin": CROSS_SITE_ATTACKER})
        assert resp.status_code == 200
    finally:
        await client.aclose()


async def test_bearer_credentials_are_unaffected(org_admin: OrgWithAdmin) -> None:
    """The CLI, the daemon and the VS Code webview authenticate with a bearer
    token, which no page can make a browser attach — so nothing to forge, and
    nothing to check. A cookie-less client with a hostile Origin still works."""
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", "Origin": CROSS_SITE_ATTACKER},
    ) as cli:
        assert (await cli.get("/api/v1/auth/me")).status_code == 200
        assert (await cli.post(MUTATION)).status_code == 200


async def test_an_anonymous_request_is_not_the_guards_business(client: AsyncClient) -> None:
    """No cookie, nothing ambient — the route's own 401/422 must be what the
    caller sees, not a CSRF refusal that would hide it."""
    resp = await client.post(
        MUTATION, headers={"Origin": CROSS_SITE_ATTACKER}, cookies={"unrelated": "x"}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize(
    ("setting", "site"),
    [
        pytest.param("auth_csrf_trusted_origins", "same-site", id="trusted-origins-same-site"),
        pytest.param("auth_csrf_trusted_origins", "cross-site", id="trusted-origins-cross-site"),
        pytest.param("api_cors_origins", "same-site", id="cors-origins-same-site"),
        pytest.param("api_cors_origins", "cross-site", id="cors-origins-cross-site"),
    ],
)
async def test_a_configured_extra_origin_is_admitted(
    org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch, setting: str, site: str
) -> None:
    """The seam a self-hosted layout extends: a second portal hostname is
    registered in settings, not compiled in. Sent the way a browser sends it —
    `Sec-Fetch-Site` included, because a browser on another origin always says
    so — so the seam is proven for the requests it exists to admit."""
    headers = {"Origin": SAME_SITE_ATTACKER, "Sec-Fetch-Site": site}
    client = await _signed_in(org_admin)
    try:
        assert (await client.post(MUTATION, headers=headers)).status_code == 403
        assert await _still_signed_in(client)
        monkeypatch.setattr(settings, setting, SAME_SITE_ATTACKER)
        assert (await client.post(MUTATION, headers=headers)).status_code == 200
        assert not await _still_signed_in(client)
    finally:
        await client.aclose()


async def test_the_refresh_cookie_alone_is_guarded(org_admin: OrgWithAdmin) -> None:
    """The refresh cookie mints a new session on its own, so forging a refresh
    is the same win as forging anything the access cookie carries."""
    client = await _signed_in(org_admin)
    try:
        del client.cookies[settings.auth_cookie_name]
        resp = await client.post("/api/v1/auth/refresh", headers={"Origin": SAME_SITE_ATTACKER})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_origin_mismatch"
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# The exemption allowlist
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        pytest.param("/api/v1/webhooks/stripe", {}, id="stripe-webhook"),
        pytest.param("/api/v1/auth/device/code", {"client_id": "alkera-cli"}, id="device-code"),
        pytest.param(
            "/api/v1/auth/device/token",
            {"device_code": "nope", "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
            id="device-token",
        ),
        pytest.param("/api/v1/gate/snapshots", {}, id="gate-ingest"),
    ],
)
async def test_exempt_machine_routes_still_answer_their_own_refusal(
    org_admin: OrgWithAdmin, path: str, payload: dict[str, str]
) -> None:
    """Each exempt surface must reach its own auth/validation, not the guard —
    so a cookie-carrying request with a hostile Origin gets that route's answer
    and never a 403 `csrf_origin_mismatch`."""
    client = await _signed_in(org_admin)
    try:
        resp = await client.post(path, json=payload, headers={"Origin": CROSS_SITE_ATTACKER})
        assert "csrf_origin_mismatch" not in resp.text, (path, resp.status_code, resp.text)
    finally:
        await client.aclose()


_PROBE_PREFIX = "/api/v1/_test/csrf"
_probe_router = APIRouter(prefix=_PROBE_PREFIX)


@_probe_router.post("/mutate")
async def _mutate_as_principal(ctx: CurrentPrincipal) -> dict[str, str]:
    return {"principal": ctx.acting_principal.kind, "org_id": str(ctx.org_id)}


@pytest.fixture
def _probe_route() -> Iterator[None]:
    """A state-changing route any credential the API accepts may reach, so the
    guard's verdict can be read per credential shape without finding a real
    route each of them is authorized on."""
    before = len(fastapi_app.router.routes)
    fastapi_app.include_router(_probe_router)
    try:
        yield
    finally:
        del fastapi_app.router.routes[before:]


async def _mint(shape: str, org: OrgWithAdmin) -> str:
    if shape == "bearer-jwt":
        return await mint_cli_token(
            user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id
        )
    async with AsyncSessionLocal() as session:
        if shape == "personal-access-token":
            _, raw = await pat_service.mint(
                session, org_id=org.org_id, user_id=org.admin_id, label="pat"
            )
        elif shape == "ci-token":
            _, raw = await ci_token_service.mint(
                session, org_id=org.org_id, created_by_id=org.admin_id, label="ci"
            )
        elif shape == "proxy-token":
            _, raw = await proxy_token_service.mint(
                session, org_id=org.org_id, created_by_id=org.admin_id, label="proxy"
            )
        else:  # pragma: no cover - a typo in the parametrization
            raise AssertionError(shape)
        await session.commit()
        return raw


@pytest.mark.usefixtures("_probe_route")
@pytest.mark.parametrize(
    ("shape", "principal"),
    [
        pytest.param("bearer-jwt", "user", id="bearer-jwt"),
        pytest.param("personal-access-token", "pat", id="personal-access-token"),
        pytest.param("ci-token", "service", id="ci-token"),
        pytest.param("proxy-token", "service", id="proxy-token"),
    ],
)
async def test_a_non_ambient_credential_needs_no_origin_at_all(
    org_admin: OrgWithAdmin, shape: str, principal: str
) -> None:
    """The CLI, the daemon, the cloud mirror, a CI job and curl authenticate
    with a credential a page cannot make a browser attach, and none of them
    sends `Origin` or `Referer` on a write. The guard must not ask them to:
    each shape reaches the route and is resolved to its own principal with no
    browser evidence at all — the request the product actually sends."""
    raw = await _mint(shape, org_admin)
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw}"},
    ) as machine:
        assert "origin" not in machine.headers and "referer" not in machine.headers
        resp = await machine.post(f"{_PROBE_PREFIX}/mutate")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"principal": principal, "org_id": str(org_admin.org_id)}


@pytest.mark.usefixtures("_probe_route")
async def test_the_same_route_refuses_the_cookie_with_no_origin(org_admin: OrgWithAdmin) -> None:
    """The control for the case above: on the very route every machine
    credential just reached, the ambient cookie alone with no browser evidence
    is refused — so the exemptions are about the credential, not the route."""
    client = await _signed_in(org_admin, headers={"Origin": ""})
    try:
        del client.headers["Origin"]
        resp = await client.post(f"{_PROBE_PREFIX}/mutate")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_origin_mismatch"
    finally:
        await client.aclose()


def test_every_exempt_prefix_names_a_live_route() -> None:
    """A rename that orphans an exemption leaves a machine surface guarded (a
    silent outage) or, worse, leaves a cookie surface exempt under the old
    spelling. Pin the allowlist to the routing table."""
    paths = {getattr(r, "path", "") for r in fastapi_app.routes}
    paths |= {"/c"}  # the mounted content app, which carries no `path` route
    for prefix in csrf.EXEMPT_PREFIXES:
        assert any(p == prefix or p.startswith(f"{prefix}/") for p in paths), prefix


def test_the_cookie_surfaces_sharing_an_exempt_prefix_are_not_exempt() -> None:
    """`/api/v1/gate` and `/api/v1/auth/device` each mix machine routes with
    org-admin cookie routes. The allowlist is spelled per surface so the cookie
    halves stay guarded; this is the test that says so out loud."""
    for guarded in (
        "/api/v1/auth/device/approve",
        "/api/v1/auth/device/deny",
        "/api/v1/gate/tokens",
        "/api/v1/gate/waivers",
        "/api/v1/gate/github/claim",
        "/api/v1/slack/link",
        "/api/v1/org/sso",
    ):
        assert not csrf.is_exempt(guarded), guarded


def test_an_exemption_does_not_extend_past_a_segment_boundary() -> None:
    assert csrf.is_exempt("/api/v1/webhooks")
    assert csrf.is_exempt("/api/v1/webhooks/stripe")
    assert not csrf.is_exempt("/api/v1/webhooks-admin/purge")


# --------------------------------------------------------------------------- #
# Origin normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("https://App.Example", "https://app.example", id="host-case-folds"),
        pytest.param("https://app.example:443", "https://app.example", id="default-https-port"),
        pytest.param("http://app.example:80", "http://app.example", id="default-http-port"),
        pytest.param("http://localhost:5173", "http://localhost:5173", id="explicit-port-kept"),
        pytest.param("http://app.example/", "http://app.example", id="trailing-slash"),
        pytest.param("null", None, id="sandboxed-null"),
        pytest.param("", None, id="empty"),
        pytest.param("file:///etc/passwd", None, id="non-http-scheme"),
        pytest.param("app.example", None, id="schemeless"),
        pytest.param("http://app.example:notaport", None, id="malformed-port"),
    ],
)
def test_origin_normalization(raw: str, expected: str | None) -> None:
    assert csrf.normalize_origin(raw) == expected


def test_a_default_port_spelling_in_settings_matches_the_bare_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that writes the default port into its settings and a browser
    that omits it are naming the same origin."""
    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example:443")
    assert "https://app.example" in csrf.trusted_origins()


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param("alkera_session=abc", True, id="access-cookie"),
        pytest.param("alkera_refresh=abc", True, id="refresh-cookie"),
        pytest.param("theme=dark; alkera_session=abc", True, id="among-others"),
        pytest.param(" alkera_session =abc", True, id="whitespace-padded"),
        pytest.param("theme=dark", False, id="unrelated-cookie"),
        pytest.param("not_alkera_session=abc", False, id="suffix-lookalike"),
        pytest.param("", False, id="empty"),
    ],
)
def test_session_cookie_detection(header: str, expected: bool) -> None:
    assert csrf.carries_session_cookie(header) is expected


def test_the_apis_own_origin_is_trusted_for_the_pages_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Slack link page is served by the API and posts back to it, so its
    confirm form carries the API's origin. On a hosted deployment that origin is
    not the app's, and the guard refused the form; the API's own origin is
    same-origin with the route by definition."""
    from alkera_core.config import settings
    from backend.api.csrf import trusted_origins

    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example.test")
    monkeypatch.setattr(settings, "api_public_base_url", "https://api.example.test")
    origins = trusted_origins()
    assert "https://api.example.test" in origins
    assert "https://app.example.test" in origins
    assert CROSS_SITE_ATTACKER not in origins
