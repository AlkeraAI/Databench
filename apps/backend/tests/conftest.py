"""Test fixtures for the FastAPI backend.

Approach: tests use the real local Postgres (assumes `make infra-up` +
`make migrate`). Each test creates uniquely-suffixed entities (random
emails / team names) so concurrent test runs don't collide. The dev DB
accumulates rows over time — that's acceptable for a local-only test
DB. CI would use a fresh DB each run.

Fixtures:
    - real_session    : a raw AsyncSession for setup work (real commits)
    - app             : the FastAPI app, built from the installed extensions
    - client          : httpx.AsyncClient bound to the app
    - factory         : helpers (make_org, make_user, login_as_user)
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from alkera_core.auth import totp
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    OrgMembership,
    PlatformRole,
    TeamMembership,
    TeamRole,
    TokenType,
    User,
)
from alkera_test_support.plans import make_org_enterprise as make_org_enterprise
from backend.services.org import teams as team_service
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests._suite_app import create_app
from tests._wait_ceiling import wait_ceiling
from tests.chat_declarations import declared_chats  # noqa: F401 -- autouse fixture


@pytest.fixture(autouse=True)
def _captcha_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite key-less + deterministic regardless of a developer's
    ``.env.local``. A local ``TURNSTILE_SECRET_KEY`` (e.g. set for manual testing)
    would otherwise make ``/auth/login``, ``/signup`` and ``/password-reset``
    enforce captcha and 400 the token-less test helpers — exactly how it behaves in
    CI, where the key is unset. Force it off here (autouse runs first); the captcha
    tests re-enable it explicitly within their own fixtures/bodies, and a later
    ``monkeypatch.setattr`` on the shared instance wins."""
    monkeypatch.setattr(settings, "turnstile_secret_key", None)


#: The catalog the model gateway serves this suite by default: one real model,
#: so a chat created with no pick resolves onto it.
DEFAULT_GATEWAY_CATALOG: dict[str, object] = {
    "object": "list",
    "data": [
        {
            "id": "claude-opus-4.5",
            "object": "model",
            "display_name": "Claude Opus 4.5",
            "family": "claude",
            "wire": "anthropic",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
            "tier": "frontier",
            "context_window": 200000,
            "max_output_tokens": 64000,
        }
    ],
    "org_flags": {"web_search_enabled": True},
}


@pytest.fixture(autouse=True)
def _gateway_catalog_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat is never created without a model, so every create consults the
    gateway catalog — and the real gateway is never reachable from this suite.
    Serve ``DEFAULT_GATEWAY_CATALOG`` through the catalog reader's own HTTP
    seam (the client it builds when handed none), so a test that scripts the
    gateway itself (``client=`` a ``MockTransport``, or ``fetch_catalog``
    patched over) still wins."""
    import httpx
    from backend.services.chats import catalog as chat_catalog

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DEFAULT_GATEWAY_CATALOG)

    def default_client(**_: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(chat_catalog, "async_client", default_client)


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> None:
    """Clear every throttle bucket between tests.

    The limiters are module-level by design — a per-request bucket would throttle
    nothing. That same lifetime makes them bleed across tests: every request in
    the suite arrives from one client with one address, so a burst in one test
    would 429 the next one for reasons it has nothing to do with. Cleared here
    rather than switching throttling OFF in tests, so the real routes are still
    exercised with it on.
    """
    from backend.api.rate_limit import reset_all_limiters

    reset_all_limiters()


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limit_windows() -> None:
    """Forget the durable hourly counters between tests.

    The strict classes also count the hour in Postgres, keyed by the account
    and the caller address — and every request in the suite arrives from one
    address, so an hour of tests would exhaust that key's ceiling for reasons
    no single test has anything to do with."""
    from alkera_core.models import RateLimitWindow
    from sqlalchemy import delete

    async with AsyncSessionLocal() as session:
        await session.execute(delete(RateLimitWindow))
        await session.commit()


class ManualClock:
    """A clock that advances only when a test says so — both the monotonic
    seconds the buckets read and the wall clock the durable window floors."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.wall_start = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return self.wall_start + timedelta(seconds=self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def pin_clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    """Stop the rate-limit registry's clocks for one test.

    With the clock stopped a burst can only spend the tokens it started with,
    and the only way to earn one back is for the test to say so — a burst
    timed by the wall clock refills as fast as a slow loop drains it, which
    asks how loaded the machine is instead of what the bucket does.
    """
    from backend.api.rate_limit import REGISTRY

    clock = ManualClock()
    monkeypatch.setattr(REGISTRY, "clock", clock)
    monkeypatch.setattr(REGISTRY, "wall", clock.wall)
    REGISTRY.reset()
    return clock


@pytest.fixture
def wide_credential_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the sign-in ceiling for a test that drives the credential routes in a loop.

    A test that walks a refresh family across days does it under ``freeze_time``,
    which stops the monotonic clock the token bucket refills from: the burst it
    started with is all it will ever have, so the eleventh renewal is refused for
    being the eleventh rather than for anything the test is about. Widened by
    raising the numbers the class reads live from settings — the middleware, the
    class and the durable hourly counter all still run, so a throttle that stopped
    working would still be caught by the tests that are about throttling.
    """
    for name in ("credential", "credential_ip"):
        monkeypatch.setattr(settings, f"rate_limit_{name}_per_minute", 100_000)
        monkeypatch.setattr(settings, f"rate_limit_{name}_burst", 100_000)
        monkeypatch.setattr(settings, f"rate_limit_{name}_per_hour", 100_000)


@pytest.fixture(autouse=True)
def _reset_realtime_state() -> Iterator[None]:
    """Forget every realtime subscription and held connection slot between tests.

    The process-wide hub and the connection gates are module-level by design
    (a per-request counter caps nothing); that lifetime makes them bleed across
    tests. Cleared on both sides of each test so a stream a test left parked
    cannot 429 the next one.

    A served app (``uvicorn_server``) binds its own runtime with its own hub;
    tests that drive one inject events THROUGH THE DATABASE (``emit`` + commit),
    the way every producer in the product does, or publish to
    ``app.state.realtime.hub`` — everything runs on the one test loop.
    """
    from alkera_core.events import get_hub
    from backend.services.realtime.limits import ConnectionGate

    get_hub().reset_for_tests()
    ConnectionGate.reset_for_tests()
    yield
    get_hub().reset_for_tests()
    ConnectionGate.reset_for_tests()


@pytest_asyncio.fixture
async def realtime_app() -> AsyncIterator[FastAPI]:
    """The shared app with its lifespan RUNNING on the test loop: a real outbox
    listener and the hub the event stream fans out from. ``ASGITransport`` never
    runs a lifespan, so an in-process stream test enters it here; everything runs
    on the one test loop, so the test may publish straight to
    ``app.state.realtime.hub`` or commit through the outbox."""
    from backend.services.realtime.runtime import runtime_of

    async with fastapi_app.router.lifespan_context(fastapi_app):
        runtime = runtime_of(fastapi_app)
        assert runtime is not None and runtime.listener is not None
        # "Connected" means listening AND caught up to the head, so a row a test
        # commits after this point is a live event, never history.
        assert await runtime.listener.wait_connected(10.0), "the outbox listener did not connect"
        yield fastapi_app


def _unique_email(prefix: str = "test") -> str:
    return f"{prefix}-{secrets.token_hex(6)}@alkera.dev"


def _unique_org_name() -> str:
    return f"Test Org {secrets.token_hex(4)}"


@dataclass
class OrgWithAdmin:
    org_id: UUID
    admin_id: UUID
    admin_email: str
    admin_password: str


@pytest_asyncio.fixture
async def real_session(disposable_database: str) -> AsyncIterator[AsyncSession]:
    """A real AsyncSession for test setup. Its commits are REAL — nothing rolls them
    back — and the fixtures built on it clear whole tables, so it takes the root
    conftest's `disposable_database` to say out loud which database that is allowed
    to be."""
    async with AsyncSessionLocal() as session:
        yield session


async def _name_the_session_org(request: httpx.Request) -> None:
    """Name the org the request's browser session is in (``X-Alkera-Org``), as
    the portal's sender does on every request once its tab knows the org.

    A request that names an org itself keeps it (a test of a stale tab), and a
    request with no live session cookie, or with a credential of its own in
    ``Authorization`` (which the portal never sends), is sent as it is."""
    from http.cookies import SimpleCookie

    from alkera_core.auth import InvalidTokenError, decode_session_token
    from alkera_core.auth.tenancy import ORG_HEADER

    if ORG_HEADER in request.headers or "authorization" in request.headers:
        return
    jar: SimpleCookie = SimpleCookie()
    jar.load(request.headers.get("cookie", ""))
    morsel = jar.get(settings.auth_cookie_name)
    if morsel is None or not morsel.value:
        return
    try:
        claims = decode_session_token(morsel.value)
    except InvalidTokenError:
        return
    request.headers[ORG_HEADER] = str(claims.org_team_id)


def _browser_hooks(kwargs: dict[str, Any]) -> dict[str, list[Any]]:
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    if kwargs.pop("names_org", True):
        hooks["request"] = [_name_the_session_org, *hooks.get("request", [])]
    return hooks


def app_client(**kwargs: Any) -> AsyncClient:
    """An in-process client for the real app, shaped like the browser the SPA
    runs in.

    It carries an `Origin` because a browser sends one on every state-changing
    request and the origin guard (`backend.api.csrf`) refuses a cookie-authed
    one that does not. Reading it from `frontend_base_url` keeps the fixture
    honest about what the deployment under test actually trusts — a test that
    wants a foreign or absent origin passes its own `headers`.

    It also names the org its session is in on every request, as the portal
    does (a cookie-authed write that names none is refused); a test of a tab
    that names no org passes ``names_org=False``."""
    headers = {"Origin": settings.frontend_base_url, **(kwargs.pop("headers", None) or {})}
    event_hooks = _browser_hooks(kwargs)
    return AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url=kwargs.pop("base_url", "http://test"),
        headers=headers,
        event_hooks=event_hooks,
        **kwargs,
    )


def served_client(address: str, **kwargs: Any) -> AsyncClient:
    """The same browser, over a real socket: a client for a `uvicorn_server`
    address (`host:port`, no scheme).

    It carries the same `Origin` as `app_client` for the same reason — the
    origin guard refuses a cookie-authed write that cannot name the origin it
    came from, and the address the server happens to be listening on is not
    the origin the deployment serves the portal from — and names its session's
    org the same way."""
    headers = {"Origin": settings.frontend_base_url, **(kwargs.pop("headers", None) or {})}
    event_hooks = _browser_hooks(kwargs)
    return AsyncClient(
        base_url=kwargs.pop("base_url", f"http://{address}"),
        headers=headers,
        timeout=kwargs.pop("timeout", 60.0),
        event_hooks=event_hooks,
        **kwargs,
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with app_client() as c:
        yield c


class TotpClock:
    """A pinned clock for the TOTP verifier, and only for it.

    An accepted TOTP is single-use, and clock-skew tolerance accepts a code
    across three 30-second steps, so a flow that authenticates more than twice
    runs out of usable codes. Anchoring to the wall clock instead would race the
    step boundary: a test that starts one step and finishes the next presents a
    code the verifier now considers too old.

    Only ``totp``'s view of time moves. The database, the event loop and every
    timeout keep real time, so nothing that polls to a deadline can spin.
    """

    def __init__(self) -> None:
        self._now = float(int(time.time() // 30) * 30)

    def time(self) -> float:
        return self._now

    @property
    def step(self) -> int:
        return int(self._now // 30)

    def code(self, secret: str) -> str:
        """The code an authenticator would show right now."""
        return totp._hotp(secret, self.step)

    def next_code(self, secret: str) -> str:
        """Advance one step and return that step's code -- what a user reading
        their authenticator again a minute later would type."""
        self._now += 30
        return self.code(secret)


@pytest.fixture
def totp_clock(monkeypatch: pytest.MonkeyPatch) -> TotpClock:
    clock = TotpClock()
    monkeypatch.setattr(totp, "time", clock)
    return clock


@pytest.fixture
def monkeypatch_email_send(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace `send_invitation_email` with a recording no-op.

    Returns the list of recorded calls; each entry is a dict with the
    invitation token, target email, team id, and inviter display name.
    """
    sent: list[dict] = []

    async def _capture(invitation, *, token, team, org_name, inviter_display_name):  # type: ignore[no-untyped-def]
        sent.append(
            {
                # The RAW token (what the recipient receives); the row stores only its hash.
                "invitation_token": token,
                "email": invitation.email,
                "team_id": str(team.id),
                "org_name": org_name,
                "inviter_display_name": inviter_display_name,
            }
        )

    # Patch the symbol where it's used (the route imports it directly).
    monkeypatch.setattr("backend.api.routes.org.invitations.send_invitation_email", _capture)
    monkeypatch.setattr("backend.services.identity.admin_create.send_invitation_email", _capture)
    return sent


@pytest.fixture
def monkeypatch_verification_send(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace `send_email_verification` with a recording no-op.

    Returns the list of recorded calls; each entry is a dict with the
    user id, target email, and the verification token.
    """
    sent: list[dict] = []

    async def _capture(user, *, token):  # type: ignore[no-untyped-def]
        sent.append({"user_id": str(user.id), "email": user.email, "token": token})
        return True

    monkeypatch.setattr("backend.api.routes.identity.auth.send_email_verification", _capture)
    monkeypatch.setattr("backend.api.routes.identity.users.send_email_verification", _capture)
    monkeypatch.setattr("backend.api.admin.orgs.send_email_verification", _capture)
    return sent


@pytest.fixture
def monkeypatch_password_reset_send(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace `send_password_reset` with a recording no-op."""
    sent: list[dict] = []

    async def _capture(user, *, token):  # type: ignore[no-untyped-def]
        sent.append({"user_id": str(user.id), "email": user.email, "token": token})

    monkeypatch.setattr("backend.api.routes.identity.auth.send_password_reset", _capture)
    monkeypatch.setattr("backend.services.identity.admin_create.send_password_reset", _capture)
    return sent


@pytest.fixture
def monkeypatch_welcome_send(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace `send_welcome_email` with a recording no-op.

    Patched inside `welcome_service` (the single chokepoint every verification path
    routes through) so the once-ever guard's column-stamping still runs while the
    actual SMTP send is captured. Returns the list of recorded calls.
    """
    sent: list[dict] = []

    async def _capture(user):  # type: ignore[no-untyped-def]
        sent.append({"user_id": str(user.id), "email": user.email})

    monkeypatch.setattr("backend.services.identity.welcome.send_welcome_email", _capture)
    return sent


@pytest_asyncio.fixture
async def org_admin(real_session: AsyncSession) -> OrgWithAdmin:
    """Create a fresh org with an Org Admin user. Returns ids + login creds."""
    return await make_org_admin(real_session)


async def make_org_admin(real_session: AsyncSession) -> OrgWithAdmin:
    """The ``org_admin`` fixture's org, for a caller that holds the session
    itself (a fixture of a wider scope than one test)."""
    email = _unique_email("admin")
    password = "admin-pass-12345"
    org, admin = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=email,
        admin_first_name="Test",
        admin_last_name="Admin",
        admin_password=password,
    )
    # A legitimate org admin has a verified email — `require_email_verified`
    # gates org-structure mutations (invites, memberships, settings, teams) on it,
    # so the default admin fixture must model a verified account.
    from datetime import UTC, datetime

    admin.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    return OrgWithAdmin(
        org_id=org.id,
        admin_id=admin.id,
        admin_email=email,
        admin_password=password,
    )


@pytest_asyncio.fixture
async def platform_support(real_session: AsyncSession) -> OrgWithAdmin:
    """Create a fresh org whose admin user also has platform_role=ALKERA_SUPPORT."""
    email = _unique_email("support")
    password = "support-pass-12345"
    org, admin = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=email,
        admin_first_name="Test",
        admin_last_name="Support",
        admin_password=password,
        admin_platform_role=PlatformRole.ALKERA_SUPPORT,
    )
    await real_session.commit()
    return OrgWithAdmin(
        org_id=org.id,
        admin_id=admin.id,
        admin_email=email,
        admin_password=password,
    )


@pytest_asyncio.fixture
async def platform_admin(real_session: AsyncSession) -> OrgWithAdmin:
    """Org admin with platform_role=ALKERA_ADMIN."""
    return await make_platform_admin(real_session)


async def make_platform_admin(real_session: AsyncSession) -> OrgWithAdmin:
    """The ``platform_admin`` fixture's org and admin, for a caller that holds
    the session itself."""
    email = _unique_email("padmin")
    password = "admin-pass-12345"
    org, admin = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=email,
        admin_first_name="Test",
        admin_last_name="Platform Admin",
        admin_password=password,
        admin_platform_role=PlatformRole.ALKERA_ADMIN,
    )
    await real_session.commit()
    return OrgWithAdmin(
        org_id=org.id,
        admin_id=admin.id,
        admin_email=email,
        admin_password=password,
    )


#: The route a browser signs in through — and the URL a minted session's cookies
#: are attributed to, so the client's jar stores them exactly as a real response
#: would have left them.
SIGN_IN_PATH = "/api/v1/auth/login"


def _sign_in_request(client: AsyncClient) -> Request:
    """The request a sign-in presents to ``issue_session``.

    It carries the client's own headers and the address ``ASGITransport`` gives
    every request it makes, because the session list shows the user agent and the
    coarse network prefix recorded here — so a session minted by
    :func:`login` describes the same client a routed one would.
    """
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": SIGN_IN_PATH,
            "raw_path": SIGN_IN_PATH.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(k.encode(), v.encode()) for k, v in client.headers.items()],
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }
    )


async def login(client: AsyncClient, email: str, password: str) -> AsyncClient:
    """Put an authenticated session on ``client``, for a test that needs a signed-in
    caller rather than a signed-in *route*.

    The credential is verified with the product's own ``verify_password`` against
    the stored hash, and a refused or deactivated account is refused here too, so
    the helper still means "these are this user's working credentials". The session
    itself comes from ``issue_session`` — the one function every route that signs a
    browser in calls — so the access token, its registered ``auth_tokens`` row, the
    refresh family and both cookies are exactly what ``POST /auth/login`` leaves
    behind, and everything that reads or revokes a session downstream sees the same
    thing.

    What it does NOT run is the rest of that route: the throttle bookkeeping, the
    lockout and SSO gates, the second factor, the forensic IP stamp and the
    ``auth.login`` audit entry. Those are the route's own behaviour and are proven
    by the tests that drive it — so a test whose subject is any of them calls
    :func:`login_via_route` (or posts to the route itself) instead of this.
    """
    from backend.auth.password import verify_password
    from backend.auth.session_issue import issue_session
    from backend.services.identity import users as user_service

    async with AsyncSessionLocal() as session:
        user, banned = await user_service.get_by_email_with_ban(session, email)
        assert user is not None and not banned, f"no account to sign in as: {email!r}"
        assert user.is_active, f"account is deactivated: {email!r}"
        assert verify_password(password, user.password_hash or ""), f"wrong password for {email!r}"
        carrier = Response()
        await issue_session(
            session,
            user,
            request=_sign_in_request(client),
            response=carrier,
            method="password",
        )
        await session.commit()

    # Hand the cookies to the client the way httpx takes them off a real response,
    # so the jar holds the same entries with the same attributes.
    client.cookies.extract_cookies(
        httpx.Response(
            200,
            headers=list(carrier.raw_headers),
            request=httpx.Request("POST", f"{client.base_url}{SIGN_IN_PATH}"),
        )
    )
    return client


async def signed_in_through_sso(client: AsyncClient, org_id: UUID) -> None:
    """Record that the browser session on ``client`` just signed in through
    ``org_id``'s IdP, exactly as the SSO callback's step-up does: a fresh
    ``sso`` grant for the org on the session's family. For a test that needs an
    admin who may turn SSO enforcement on, without driving an IdP."""
    from datetime import UTC, datetime

    from alkera_core.auth import decode_session_token
    from alkera_core.auth.refresh import family_of_access_token
    from backend.auth.session_issue import record_grant

    access = client.cookies.get(settings.auth_cookie_name)
    assert access, "the client has no browser session to step up"
    jti = decode_session_token(access).jti
    assert jti is not None
    async with AsyncSessionLocal() as session:
        family = await family_of_access_token(session, jti)
        assert family is not None, "the session names no refresh family"
        await record_grant(
            session,
            family_id=family.family_id,
            org_team_id=org_id,
            method="sso",
            at=datetime.now(UTC),
        )
        await session.commit()


async def hold_sso_domains(org_id: UUID, domains: str) -> None:
    """Assign ``org_id`` the SSO email domains ``domains`` (comma-separated),
    through the same owner function the staff route calls.

    The suite shares one database and reuses a few domains across orgs that
    never meet, so an org that holds one of these domains from an earlier test
    lets it go first, as if staff had moved it. A test about two orgs
    contending for a domain drives the staff route instead."""
    from alkera_core.auth import sso_domains
    from alkera_core.models import SsoDomainClaim
    from sqlalchemy import delete

    wanted = sso_domains.parse_domains(domains.split(","))
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(SsoDomainClaim).where(
                SsoDomainClaim.domain.in_(wanted), SsoDomainClaim.org_team_id != org_id
            )
        )
        await sso_domains.assign(session, org_id, wanted, assigned_by_id=None)
        await session.commit()


async def login_via_route(client: AsyncClient, email: str, password: str) -> AsyncClient:
    """Sign in through the real ``POST /auth/login``.

    For a test whose subject is the route itself — the throttle, the lockout and
    SSO gates, the second factor, the IP stamp, the audit entry — rather than one
    that only needs a signed-in caller.
    """
    resp = await client.post(SIGN_IN_PATH, json={"email": email, "password": password})
    assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
    return client


async def mint_cli_token(
    *,
    user_id: UUID,
    email: str,
    org_team_id: UUID,
    platform_role: PlatformRole | None = None,
) -> str:
    """Mint + register a long-lived CLI Bearer token directly, the way the device
    token endpoint does on approval. Used by tests that need a Bearer credential
    now that the old `/auth/cli-tokens` endpoint is gone."""
    from alkera_core.auth import encode_cli_token, register_token
    from alkera_core.models import TokenType

    token, claims = encode_cli_token(
        user_id=user_id,
        email=email,
        org_team_id=org_team_id,
        platform_role=platform_role,
    )
    async with AsyncSessionLocal() as session:
        await register_token(session, claims=claims, token_type=TokenType.CLI)
        await session.commit()
    return token


async def make_member(
    session: AsyncSession,
    *,
    org_id: UUID,
    role: TeamRole = TeamRole.MEMBER,
    email: str | None = None,
    first_name: str = "Member",
    last_name: str = "User",
    password: str | None = None,
    verified: bool = False,
) -> tuple[User, str | None]:
    """Create a regular user in `org_id` with optional team membership of
    the org root.

    `verified=True` stamps `email_verified_at` so the user models a legitimate,
    email-verified account (the OAuth pre-hijack defense only fires on
    *unverified* accounts, so tests must pin this explicitly)."""
    from datetime import UTC, datetime

    from backend.services.identity import users as user_service

    chosen_email = email or _unique_email("member")
    chosen_password = password or f"member-pass-{uuid4().hex[:8]}"
    user = await user_service.create_user(
        session,
        org_team_id=org_id,
        email=chosen_email,
        first_name=first_name,
        last_name=last_name,
        password=chosen_password,
    )
    if verified:
        user.email_verified_at = datetime.now(UTC)
    membership = TeamMembership(user_id=user.id, team_id=org_id, role=role)
    session.add(membership)
    await session.commit()
    return user, chosen_password


@pytest.fixture
def multi_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-org on for this test alone. Never set globally: every other test
    runs with the single-org guard that production runs with."""
    monkeypatch.setattr(settings, "multi_org_enabled", True)


@pytest.fixture
def strict_sso(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict single sign-on enforcement on for this test alone (a refresh
    needs a sign-in through the org's IdP within its max age, SCIM governs,
    enforcing ends credentials). Every other test runs with it off, as a
    deployment does by default."""
    monkeypatch.setattr(settings, "sso_strict_enforcement_enabled", True)


@dataclass
class TwoOrg:
    """An identity in two orgs, each org with its own admin.

    ``user`` is a member of org A (its home) and of org B. ``token_a`` and
    ``token_b`` are registered CLI bearer tokens minted through the mint helper
    for each membership, so each names its org, its membership and the
    membership's credential epoch."""

    user: User
    org_a: UUID
    org_b: UUID
    admin_a: User
    admin_b: User
    membership_a: OrgMembership
    membership_b: OrgMembership
    token_a: str
    token_b: str
    password: str


@pytest_asyncio.fixture
async def two_org_identity(real_session: AsyncSession) -> TwoOrg:
    """The adversarial factory: one person, two orgs (see
    :func:`make_two_org_identity`)."""
    return await make_two_org_identity(real_session)


async def make_two_org_identity(real_session: AsyncSession) -> TwoOrg:
    """The adversarial factory: one person, two orgs.

    Built through the normal services: each org with its admin, the person a
    verified member of A (their home), then an active membership of B and a
    seat on B's root team through ``membership_service.add_member`` (the team
    row needs the org membership first). The tokens are minted with multi-org
    on for the mint only, so the test decides whether the guard is on."""
    from alkera_core.auth import register_token
    from backend.auth.membership_tokens import mint_for_membership
    from backend.services.org import memberships as membership_service
    from backend.services.org import org_memberships as org_membership_service

    org_a, admin_a = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("admin-a"),
        admin_first_name="Admin",
        admin_last_name="A",
        admin_password="admin-pass-12345",
    )
    org_b, admin_b = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("admin-b"),
        admin_first_name="Admin",
        admin_last_name="B",
        admin_password="admin-pass-12345",
    )
    await real_session.commit()
    user, password = await make_member(
        real_session, org_id=org_a.id, email=_unique_email("two-org"), verified=True
    )
    assert password is not None
    membership_b = await org_membership_service.create(
        real_session, user_id=user.id, org_team_id=org_b.id
    )
    await membership_service.add_member(real_session, team_id=org_b.id, user_id=user.id)
    await real_session.commit()
    membership_a = await org_membership_service.get(
        real_session, user_id=user.id, org_team_id=org_a.id
    )
    assert membership_a is not None

    tokens: dict[UUID, str] = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "multi_org_enabled", True)
        for org_id in (org_a.id, org_b.id):
            token, claims = await mint_for_membership(real_session, user, org_id, kind="cli")
            await register_token(real_session, claims=claims, token_type=TokenType.CLI)
            tokens[org_id] = token
    await real_session.commit()
    return TwoOrg(
        user=user,
        org_a=org_a.id,
        org_b=org_b.id,
        admin_a=admin_a,
        admin_b=admin_b,
        membership_a=membership_a,
        membership_b=membership_b,
        token_a=tokens[org_a.id],
        token_b=tokens[org_b.id],
        password=password,
    )


# ---------------------------------------------------------------------------
# A real uvicorn server, on the test's own event loop.
#
# The socket and event-stream tests need real TCP: httpx's ASGITransport
# collects a whole body before returning (no live stream), and FastAPI's
# TestClient drives WebSockets through a portal that fights the pytest-asyncio
# loop. Running uvicorn IN A THREAD is not the answer either: the app, the
# test and the fixtures all share ONE SQLAlchemy engine, and its driver adapter
# holds a loop-bound asyncio lock, so a second event loop touching the same
# engine stalls forever on its first connect (this was reproduced). So the
# server runs as a task on the test loop: real sockets, real h11 streaming,
# real disconnects, one loop, no cross-loop state anywhere.
#
# ``lifespan="on"``: the realtime runtime (outbox listener + hub) starts with
# the server, and a genuine lifespan failure fails the server loudly instead
# of being logged as "unsupported".
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServedApp:
    """A live server: where to reach it, and the handles a test needs to stop
    it deliberately and watch it go."""

    addr: str
    server: uvicorn.Server
    task: asyncio.Task[None]


@contextlib.asynccontextmanager
async def serve_controlled(
    application: FastAPI | None = None, *, graceful_timeout: float = 5
) -> AsyncIterator[ServedApp]:
    """Serve ``application`` (default: the shared app) on a random loopback port
    as a task on the current loop; yield the server itself; stop it — lifespan
    shutdown included — on exit.

    Each instance gets its OWN application object when asked (the two-instance
    tests pass ``create_app()``), so each lifespan binds its own runtime; two
    servers sharing one app object would clobber each other's state.

    ``graceful_timeout`` is the deadline the shipped launch lines pass. Without
    one uvicorn waits forever for open connections, so a stream a test forgot to
    close would hold the teardown hostage.

    The socket is bound here and handed to uvicorn still open: picking a port,
    closing it and letting uvicorn bind it again leaves a window in which
    anything else on the box can take it, and under a full-suite run something
    does.

    A served backend is not ready when uvicorn says so, only when the realtime
    runtime its lifespan started can deliver: the socket route refuses with
    ``UNAVAILABLE`` on a replica whose outbox listener has not connected, and
    waits only two seconds for one. On a loaded box the listener's first
    catch-up read takes longer than that, so the wait belongs here — once, in
    the fixture — instead of in every test that opens a socket.
    """
    target = application or fastapi_app
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        target,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=graceful_timeout,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name=f"uvicorn-test-server:{port}")
    try:
        # ``server.started`` is the boundary; the ceiling is only a safety net,
        # so it is a share of the run's own per-test budget rather than a
        # number that would quietly become a claim about the host — a fixture
        # refusing to set up because a loaded box took six seconds to bind a
        # loopback port says nothing about the app it was going to serve.
        startup = wait_ceiling()
        deadline = asyncio.get_running_loop().time() + startup
        while not server.started:
            if task.done():
                task.result()  # surfaces the startup failure
                raise RuntimeError("uvicorn server exited before it started")
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"uvicorn server did not start within {startup:.0f}s")
            await asyncio.sleep(0.02)
        await _await_realtime_ready(target)
        yield ServedApp(addr=f"127.0.0.1:{port}", server=server, task=task)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=15.0)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(OSError):
            sock.close()


#: How long a served backend may take to reach "this replica can deliver".
#: Paid only when the listener is genuinely slow, and the alternative is the
#: socket route's two-second refusal landing in a test as an unexplained
#: ``4503`` — so it is deliberately generous, and derived rather than picked:
#: a share of the run's own per-test budget, which is the one knob that already
#: knows how long this box is being given (``_wait_ceiling``).
REALTIME_READY_SECONDS = wait_ceiling()


async def _await_realtime_ready(application: FastAPI) -> None:
    """Block until ``application``'s realtime runtime can serve a socket.

    An application without one — the fake backends and the content origin the
    suite also serves through this helper — has nothing to wait for.
    """
    from backend.services.realtime.runtime import runtime_of

    runtime = runtime_of(application)
    if runtime is None or runtime.listener is None:
        return
    if not await runtime.listener.wait_connected(REALTIME_READY_SECONDS):
        raise RuntimeError(
            f"the served app's outbox listener did not connect within "
            f"{REALTIME_READY_SECONDS}s; every socket would be refused as unavailable"
        )


def fresh_app() -> FastAPI:
    """A new instance of the product app, for a test that serves two at once
    or restarts one. Built here, the one test module that knows the product's
    composition root, so a test of the open app need not import it."""
    return create_app()


@contextlib.asynccontextmanager
async def serve_app(application: FastAPI | None = None) -> AsyncIterator[str]:
    """``serve_controlled`` for the tests that only need the address."""
    async with serve_controlled(application) as served:
        yield served.addr


@pytest_asyncio.fixture
async def uvicorn_server() -> AsyncIterator[str]:
    """A fresh server per test; yields `host:port` (no scheme)."""
    async with serve_app() as addr:
        yield addr


@pytest_asyncio.fixture
async def uvicorn_server_pair() -> AsyncIterator[tuple[str, str]]:
    """Two independent server instances on one database — one is the shared
    app, the other a fresh ``create_app()`` — for cross-instance delivery
    tests. Yields ``(host:port, host:port)``."""
    async with serve_app() as a, serve_app(create_app()) as b:
        yield a, b


# ---------------------------------------------------------------------------
# The compute fleet is one fleet, and this database is shared
# ---------------------------------------------------------------------------

COMPUTE_FLEET_GROUP = "compute-fleet"
"""The xdist group every module that RUNS a fleet-wide compute pass declares::

    pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group(COMPUTE_FLEET_GROUP)]

``meter_and_cutoff``, ``reconcile_pods`` and ``sweep_reachability`` act on EVERY
meterable allocation in the database, because in production there is one fleet
and it is theirs to sweep. Two such passes inside one database, each under its
own frozen clock, read each other's rows: one reaps them (a row silent against a
clock two hours ahead is a dead box), or merely holds them — ``FOR UPDATE SKIP
LOCKED`` makes a row another pass has locked invisible to the pass that owns it,
so a test watches its own terminate do nothing. Both shapes were observed.

The group is the narrow half of the fix, and it is narrow deliberately. Passes
overlap only when two modules share a database, and under ``-n`` they do not:
every xdist worker provisions and is redirected at a clone of its own (see the
repo-root ``conftest.py``), and the session is refused outright if anything
subverts that binding. When those passes were observed, that redirect was broken
— a revision script built the settings singleton against the base database and
every worker followed it — so the whole suite was in fact sharing one database.
What remains true regardless is that a worker runs its own modules one after
another in ONE database, so a box a module leaves running is swept by a later
module's pass on that worker; that is what ``retired_compute_rows`` is for, and
it is keyed on the rows a module creates rather than on this group.

So: a module that RUNS a fleet-wide pass names this group. A module that only
creates live allocations declares ``pytest.mark.compute_rows``, keeps the
default one-module-one-worker group, and is handed out in parallel with
everything else. That matters because the shared group is a serial lane by
construction — it held 741 tests across 32 modules, of which ``test_chats_api``
alone was 63% of the cost, and one worker had to run all of it while the other
twenty-three finished and idled.
"""

COMPUTE_ROWS_MARK = "compute_rows"
"""What a module declares when its tests bring live ``ComputeAllocation`` rows
onto the plane but run no fleet-wide pass::

    pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

It buys the cleanup below and nothing else — no group, no serialization."""


def _fleet_group(node: pytest.Item) -> bool:
    """Whether this test declared the shared compute-fleet group."""
    marker = node.get_closest_marker("xdist_group")
    if marker is None:
        return False
    name = marker.kwargs.get("name") if marker.kwargs else None
    if name is None and marker.args:
        name = marker.args[0]
    return name == COMPUTE_FLEET_GROUP


def _touches_the_fleet(node: pytest.Item) -> bool:
    """Whether this test puts a live allocation on the plane, either way round."""
    return _fleet_group(node) or node.get_closest_marker(COMPUTE_ROWS_MARK) is not None


async def _retire(*, keep: set[UUID] | None) -> None:
    """Take every live allocation off the plane, except the ones named in
    ``keep``. ``released`` is what the production release path leaves behind:
    out of the metered states, out of placement, invisible to the next pass."""
    from alkera_core.models.compute import COMPUTE_METERED_STATES, ComputeAllocation

    async with AsyncSessionLocal() as session:
        stmt = update(ComputeAllocation).where(ComputeAllocation.state.in_(COMPUTE_METERED_STATES))
        if keep:
            stmt = stmt.where(ComputeAllocation.id.not_in(keep))
        await session.execute(
            stmt.values(
                state="released",
                released_at=datetime.now(UTC),
                terminated_reason="test_teardown",
            )
        )
        await session.commit()


#: Whether this worker has already cleared what an earlier RUN left running.
_FLEET_SWEPT = False


@pytest_asyncio.fixture(autouse=True)
async def retired_compute_rows(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Leave no live machine behind, for every test that puts one on the plane.

    This is the half of the fix that does not depend on grouping, and it is why
    a module can leave the shared group: a worker runs its modules one after
    another in ONE database, so a box left running by an earlier module is swept
    by a later module's fleet-wide pass — the same failure the group prevents,
    one step removed — and these modules commit real rows with no rollback. So
    every allocation a test brought onto the plane goes off it when the test
    ends, whether the test named the group or only ``compute_rows``. Only rows
    that appeared DURING the test are touched: a row that was already there
    belongs to something outside it.

    The FIRST such test on a worker also clears what an earlier RUN left behind,
    because this database outlives the run. A leftover box's heartbeat is a real
    timestamp from whenever that run happened, and a test that freezes the clock
    into the past reads it as a beat from the future: the fleet is "heard", the
    silence watch stops holding, and a box the test expects held is reaped — or
    one it expects reaped is held. It is the one condition a fleet test cannot
    set for itself, since it is about every OTHER row in the database.
    """
    if not _touches_the_fleet(request.node):
        yield
        return

    from alkera_core.models.compute import ComputeAllocation
    from sqlalchemy import select

    global _FLEET_SWEPT
    if not _FLEET_SWEPT:
        await _retire(keep=None)
        _FLEET_SWEPT = True

    async with AsyncSessionLocal() as session:
        before = set((await session.execute(select(ComputeAllocation.id))).scalars().all())
    try:
        yield
    finally:
        await _retire(keep=before)


# --------------------------------------------------------------------------- #
# Account lifecycle: the lifecycle emails, and the export archive store
# --------------------------------------------------------------------------- #


@pytest.fixture
def account_mail(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every lifecycle email sent during the test, as ``{to, subject, text, event}``."""
    sent: list[dict[str, Any]] = []

    async def capture(message: Any, *, log_event: str, to: str, **_: object) -> bool:
        body = message.get_body(preferencelist=("plain",))
        sent.append(
            {
                "to": to,
                "subject": str(message["Subject"]),
                "text": body.get_content() if body is not None else "",
                "event": log_event,
            }
        )
        return True

    monkeypatch.setattr("alkera_core.email.account._send", capture)
    return sent


@pytest.fixture
def account_archives(tmp_path: Path) -> Iterator[Any]:
    """The export archive store, on disk under ``tmp_path``."""
    from alkera_core.account import archive_store
    from alkera_core.files.store.filesystem import FilesystemStore

    store = FilesystemStore(tmp_path / "archives", clock=lambda: datetime.now(UTC))
    archive_store.set_archive_store(store)
    try:
        yield store
    finally:
        archive_store.set_archive_store(None)
