"""One person, two orgs: the credential decides the org, on every door.

The adversarial suite for the tenancy foundation. Its subject is a person in
org A (their home) who also holds a membership in org B, each org with its own
admin (``two_org_identity``). Every case drives the real door — the API's
principal dependency, the refresh route, the device grant, the socket handshake
and its tick, the event stream's tick, the mint helper — against real Postgres.

The invariants, each pinned below:

* the request's org is the credential's org (A for ``token_a``, B for
  ``token_b``), echoed on the response;
* with multi-org off (production today), a credential for B is refused
  everywhere with ``session_org_revoked`` while the same person's A credential
  works — nobody can hold a working credential for a second org;
* deactivating, or bumping the epoch of, the A membership retires every A
  credential on its next use (open streams included) and leaves B untouched;
* a client's ``X-Alkera-Org`` assertion that disagrees with its credential is a
  409 before the route writes anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from alkera_core.auth import (
    SessionClaims,
    decode_session_token,
    encode_cli_token,
    encode_ws_ticket,
)
from alkera_core.auth.pat_token import mint_pat_token
from alkera_core.auth.tenancy import ORG_HEADER, MembershipRefused
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    AuthRefreshToken,
    AuthSessionOrgGrant,
    AuthToken,
    DeviceAuthorization,
    MembershipStatus,
    OrgAuditEvent,
    OrgMembership,
    PersonalAccessToken,
    User,
    UserPreference,
)
from alkera_core.schemas.realtime import WS_SUBPROTOCOL, WS_TICKET_SUBPROTOCOL_PREFIX
from backend.api.org_echo import OrgEchoMiddleware
from backend.api.routes.realtime import events as events_route
from backend.auth.dependencies import CurrentMember, CurrentOrg, CurrentPrincipal
from backend.auth.membership_tokens import mint_for_membership
from backend.auth.refusals import ORG_CHANGED
from backend.services.chats import catalog as chat_catalog
from backend.services.identity import device_authorization as device_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.realtime import session as socket_session
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.filters import EntitlementRef, load_entitlements
from backend.services.realtime.runtime import RealtimeRuntime
from backend.services.realtime.session import SocketSession
from fastapi import FastAPI, WebSocket
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from tests.conftest import TwoOrg, app_client

REVOKED = "session_org_revoked"
REFRESH_COOKIE = settings.auth_refresh_cookie_name


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _code(resp: httpx.Response) -> str | None:
    """The refusal's code: the app's envelope (``error``) or, on the probe app
    with no envelope handler, FastAPI's own (``detail``)."""
    body = resp.json()
    if not isinstance(body, dict):
        return None
    error = body.get("error") or body.get("detail")
    return error.get("code") if isinstance(error, dict) else None


def _claims(token: str) -> SessionClaims:
    return decode_session_token(token)


# --- a probe app: the principal dependencies, and nothing else -----------------


def _probe_app() -> FastAPI:
    """Three routes over the real dependencies and the real echo middleware:
    what a route sees as the request's org, member and principal."""
    app = FastAPI()
    app.add_middleware(OrgEchoMiddleware)

    @app.get("/org")
    async def org(org_id: CurrentOrg) -> dict[str, str]:
        return {"org": str(org_id)}

    @app.get("/member")
    async def member(m: CurrentMember) -> dict[str, str]:
        return {"user": str(m.user.id), "membership": str(m.membership.id), "org": str(m.org_id)}

    @app.get("/principal")
    async def principal(ctx: CurrentPrincipal) -> dict[str, str]:
        return {"org": str(ctx.org_id), "kind": ctx.acting_principal.kind.value}

    return app


async def _probe(path: str, headers: dict[str, str]) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=_probe_app()), base_url="http://p") as c:
        return await c.get(path, headers=headers)


async def _pat(user_id: UUID, org_id: UUID, membership: OrgMembership | None) -> str:
    """A personal access token row bound to ``membership`` (None: a row minted
    before tokens named a membership)."""
    raw, digest = mint_pat_token()
    async with AsyncSessionLocal() as db:
        db.add(
            PersonalAccessToken(
                org_team_id=org_id,
                user_id=user_id,
                token_hash=digest,
                label="probe",
                scopes=[],
                membership_id=membership.id if membership else None,
                membership_epoch=membership.credential_epoch if membership else None,
            )
        )
        await db.commit()
    return raw


async def _set_membership(membership_id: UUID, **values: Any) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(OrgMembership).where(OrgMembership.id == membership_id).values(**values)
        )
        await db.commit()


async def _deactivate(membership_id: UUID) -> None:
    """Deactivate a membership on ``org_memberships`` alone: the identity row is
    not touched, so nothing but the membership check can refuse."""
    await _set_membership(membership_id, status=MembershipStatus.DEACTIVATED)


async def _bump(membership_id: UUID) -> None:
    async with AsyncSessionLocal() as db:
        await org_membership_service.bump_epoch(db, membership_id)
        await db.commit()


async def _end(how: str, membership_id: UUID) -> None:
    await (_deactivate if how == "deactivate" else _bump)(membership_id)


ENDINGS = [pytest.param("deactivate", id="deactivated"), pytest.param("bump", id="epoch-bumped")]


# --- the credential is the source of the org (multi-org on) --------------------


@pytest.mark.usefixtures("multi_org")
async def test_the_request_org_is_the_credentials_org_and_is_echoed(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    for token, org, membership in (
        (t.token_a, t.org_a, t.membership_a),
        (t.token_b, t.org_b, t.membership_b),
    ):
        resp = await _probe("/org", _bearer(token))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"org": str(org)}
        assert resp.headers[ORG_HEADER] == str(org)
        member = await _probe("/member", _bearer(token))
        assert member.json() == {
            "user": str(t.user.id),
            "membership": str(membership.id),
            "org": str(org),
        }


@pytest.mark.usefixtures("multi_org")
async def test_a_real_route_answers_in_the_credentials_org(
    client: AsyncClient, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    for token, org in ((t.token_a, t.org_a), (t.token_b, t.org_b)):
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        assert resp.headers[ORG_HEADER] == str(org)


# --- the single-org guard (multi-org off) --------------------------------------


async def _door_current_user(t: TwoOrg, which: str) -> tuple[int, str | None]:
    async with app_client() as c:
        resp = await c.get("/api/v1/auth/me", headers=_bearer(_token(t, which)))
    return resp.status_code, _code(resp) if resp.status_code != 200 else None


async def _door_current_principal(t: TwoOrg, which: str) -> tuple[int, str | None]:
    resp = await _probe("/principal", _bearer(_token(t, which)))
    return resp.status_code, _code(resp) if resp.status_code != 200 else None


async def _door_pat(t: TwoOrg, which: str) -> tuple[int, str | None]:
    org, membership = _org(t, which), _membership(t, which)
    raw = await _pat(t.user.id, org, membership)
    resp = await _probe("/principal", _bearer(raw))
    return resp.status_code, _code(resp) if resp.status_code != 200 else None


async def _door_ws_ticket_mint(t: TwoOrg, which: str) -> tuple[int, str | None]:
    async with app_client() as c:
        resp = await c.post("/api/v1/ws/tickets", headers=_bearer(_token(t, which)))
    return resp.status_code, _code(resp) if resp.status_code != 200 else None


class _FakeHandshake:
    """What ``admit`` reads off a WebSocket handshake: its headers and the
    subprotocols it offered."""

    def __init__(self, ticket: str) -> None:
        self.headers: dict[str, str] = {}
        self.scope = {"subprotocols": [WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"]}


async def _door_ws_admission(t: TwoOrg, which: str) -> tuple[int, str | None]:
    claims = _claims(_token(t, which))
    ticket = encode_ws_ticket(user_id=t.user.id, org_id=claims.org_team_id, session=claims)
    outcome = await socket_session.admit(cast(WebSocket, _FakeHandshake(ticket)))
    if isinstance(outcome, socket_session.Refusal):
        return 401, outcome.reason
    return 200, None


async def _door_sse_connect(t: TwoOrg, which: str) -> tuple[int, str | None]:
    async with app_client() as c:
        resp = await c.get("/api/v1/events", headers=_bearer(_token(t, which)))
    # 503 is the stream itself (no realtime runtime in this process): the door
    # admitted the credential and the route body ran.
    if resp.status_code == 503:
        return 200, None
    return resp.status_code, _code(resp)


async def _door_sse_recheck(t: TwoOrg, which: str) -> tuple[int, str | None]:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        claims = _claims(_token(t, which))
        ref = EntitlementRef(await load_entitlements(db, user, org_id=claims.org_team_id))
    alive = await events_route._recheck(claims, t.user.id, ref)
    return (200, None) if alive else (401, REVOKED)


async def _door_refresh(t: TwoOrg, which: str) -> tuple[int, str | None]:
    raw, family_id = await _family(t, org=None)
    await _point_family(family_id, _org(t, which))
    async with app_client() as c:
        resp = await c.post("/api/v1/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={raw}"})
    return resp.status_code, _code(resp) if resp.status_code != 200 else None


async def _door_device(t: TwoOrg, which: str) -> tuple[int, str | None]:
    status, body = await _redeem_device_grant(t.user.id, _org(t, which))
    if status == 200:
        return 200, None
    return status, body.get("error_description")


async def _door_mint(t: TwoOrg, which: str) -> tuple[int, str | None]:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        try:
            await mint_for_membership(db, user, _org(t, which), kind="session")
        except MembershipRefused as exc:
            return 401, exc.code
    return 200, None


def _token(t: TwoOrg, which: str) -> str:
    return t.token_a if which == "a" else t.token_b


def _org(t: TwoOrg, which: str) -> UUID:
    return t.org_a if which == "a" else t.org_b


def _membership(t: TwoOrg, which: str) -> OrgMembership:
    return t.membership_a if which == "a" else t.membership_b


DOORS = [
    pytest.param(_door_current_user, id="current_user"),
    pytest.param(_door_current_principal, id="current_principal"),
    pytest.param(_door_pat, id="personal-access-token"),
    pytest.param(_door_ws_ticket_mint, id="ws-ticket-mint"),
    pytest.param(_door_ws_admission, id="ws-admission"),
    pytest.param(_door_sse_connect, id="sse-connect"),
    pytest.param(_door_sse_recheck, id="sse-recheck"),
    pytest.param(_door_refresh, id="refresh"),
    pytest.param(_door_device, id="device-redemption"),
    pytest.param(_door_mint, id="mint_for_membership"),
]


@pytest.mark.parametrize("door", DOORS)
async def test_with_multi_org_off_the_second_org_is_refused_on_every_door(
    two_org_identity: TwoOrg, door: Any
) -> None:
    """Production today: the person holds a real, active B membership and a
    B-bound credential, and every door still refuses it, because B is not their
    home org. Their A credential goes through the same door."""
    assert settings.multi_org_enabled is False
    status, code = await door(two_org_identity, "b")
    assert status in (400, 401), (status, code)
    if door is not _door_ws_admission:
        assert code == REVOKED
    assert await door(two_org_identity, "a") == (200, None)


# --- membership lifecycle (multi-org on) ---------------------------------------


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("how", ENDINGS)
async def test_ending_one_membership_refuses_its_credentials_and_spares_the_other_org(
    two_org_identity: TwoOrg, how: str
) -> None:
    t = two_org_identity
    for which in ("a", "b"):
        for door in (_door_current_user, _door_pat, _door_ws_admission, _door_sse_recheck):
            assert await door(t, which) == (200, None), (door.__name__, which)

    await _end(how, t.membership_a.id)

    assert await _door_current_user(t, "a") == (401, REVOKED)
    assert (await _door_ws_admission(t, "a"))[0] == 401
    assert await _door_sse_recheck(t, "a") == (401, REVOKED)
    # B: requests, tokens, streams all untouched.
    for door in (_door_current_user, _door_ws_admission, _door_sse_recheck):
        assert await door(t, "b") == (200, None), door.__name__
    assert await _door_pat(t, "b") == (200, None)


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("how", ENDINGS)
async def test_an_open_socket_closes_within_one_tick_when_its_membership_ends(
    two_org_identity: TwoOrg, how: str
) -> None:
    """Neither token is revoked: only the membership moved. The A socket's tick
    refuses, the B socket's goes on."""
    t = two_org_identity
    socket_a = await _socket(t, _claims(t.token_a))
    socket_b = await _socket(t, _claims(t.token_b))
    assert await socket_a._recheck() is True
    assert await socket_b._recheck() is True

    await _end(how, t.membership_a.id)

    assert await socket_a._recheck() is False
    assert await socket_b._recheck() is True


@pytest.mark.usefixtures("multi_org")
async def test_a_pat_is_refused_once_its_membership_epoch_moves(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    bound = await _pat(t.user.id, t.org_a, t.membership_a)
    legacy = await _pat(t.user.id, t.org_a, None)
    for raw in (bound, legacy):
        assert (await _probe("/principal", _bearer(raw))).status_code == 200
    await _bump(t.membership_a.id)
    for raw in (bound, legacy):
        resp = await _probe("/principal", _bearer(raw))
        assert resp.status_code == 401
        assert _code(resp) == REVOKED


async def test_a_legacy_token_stands_at_epoch_zero_and_falls_at_the_first_bump(
    client: AsyncClient, two_org_identity: TwoOrg
) -> None:
    """A token minted before memberships existed names none; it is held to
    epoch 0, which is what the back-fill gave every membership."""
    t = two_org_identity
    legacy, claims = encode_cli_token(
        user_id=t.user.id, email=t.user.email, org_team_id=t.org_a, platform_role=None
    )
    assert claims.membership_id is None
    assert "mid" not in jwt.decode(legacy, options={"verify_signature": False})
    assert (await client.get("/api/v1/auth/me", headers=_bearer(legacy))).status_code == 200
    await _bump(t.membership_a.id)
    resp = await client.get("/api/v1/auth/me", headers=_bearer(legacy))
    assert resp.status_code == 401
    assert _code(resp) == REVOKED


async def test_a_token_naming_another_membership_is_refused(
    client: AsyncClient, two_org_identity: TwoOrg
) -> None:
    """A forged token that pairs org A with membership B is not A's."""
    t = two_org_identity
    forged, _ = encode_cli_token(
        user_id=t.user.id,
        email=t.user.email,
        org_team_id=t.org_a,
        platform_role=None,
        membership_id=t.membership_b.id,
        membership_epoch=0,
    )
    resp = await client.get("/api/v1/auth/me", headers=_bearer(forged))
    assert resp.status_code == 401
    assert _code(resp) == REVOKED


async def test_a_token_for_an_org_with_no_membership_is_refused(
    client: AsyncClient, two_org_identity: TwoOrg, multi_org: None
) -> None:
    t = two_org_identity
    # B is an org the person is in (its token works with multi-org on); a
    # third org they are not in is refused.
    third, _ = encode_cli_token(
        user_id=t.user.id, email=t.user.email, org_team_id=uuid4(), platform_role=None
    )
    assert (await client.get("/api/v1/auth/me", headers=_bearer(t.token_b))).status_code == 200
    resp = await client.get("/api/v1/auth/me", headers=_bearer(third))
    assert resp.status_code == 401
    assert _code(resp) == REVOKED


# --- the mint helper -----------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("kind", ["session", "cli", "hop"])
async def test_the_mint_helper_stamps_the_org_and_membership(
    two_org_identity: TwoOrg, kind: str
) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        await _bump(t.membership_b.id)
        token, claims = await mint_for_membership(db, user, t.org_b, kind=kind)  # type: ignore[arg-type]
        await db.commit()
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["org_team_id"] == t.org_b.hex
    assert payload["mid"] == t.membership_b.id.hex
    bumped = org_membership_service.FIRST_MEMBERSHIP_EPOCH + 1
    assert payload["mep"] == bumped
    assert (claims.membership_id, claims.membership_epoch) == (t.membership_b.id, bumped)


async def test_the_mint_helper_refuses_a_non_member_org_always(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        for enabled in (False, True):
            monkeypatch.setattr(settings, "multi_org_enabled", enabled)
            with pytest.raises(MembershipRefused):
                await mint_for_membership(db, user, uuid4(), kind="cli")
        # A deactivated membership is not a membership to mint for.
        await _deactivate(t.membership_b.id)
        with pytest.raises(MembershipRefused):
            await mint_for_membership(db, user, t.org_b, kind="cli")


@pytest.mark.usefixtures("multi_org")
async def test_last_active_moves_for_session_and_cli_and_not_for_a_hop(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    with freeze_time("2026-10-01 09:00:00", real_asyncio=True) as frozen:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, t.user.id)
            assert user is not None
            seen: dict[str, datetime | None] = {}
            for kind, minute in (("session", "09:05"), ("hop", "09:10"), ("cli", "09:15")):
                frozen.move_to(f"2026-10-01 {minute}:00")
                await mint_for_membership(db, user, t.org_b, kind=kind)  # type: ignore[arg-type]
                await db.commit()
                row = await org_membership_service.get(db, user_id=user.id, org_team_id=t.org_b)
                assert row is not None
                await db.refresh(row)
                seen[kind] = row.last_active_at
    assert seen["session"] == datetime(2026, 10, 1, 9, 5, tzinfo=UTC)
    assert seen["hop"] == seen["session"], "a hop is not activity"
    assert seen["cli"] == datetime(2026, 10, 1, 9, 15, tzinfo=UTC)


async def test_the_hop_token_is_never_registered_and_names_the_request_org(
    two_org_identity: TwoOrg, multi_org: None
) -> None:
    t = two_org_identity
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["authorization"].removeprefix("Bearer "))
        return httpx.Response(200, json={"data": []})

    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await chat_catalog.fetch_catalog(db, user, t.org_b, client=client)
        await client.aclose()
        hop = _claims(sent[0])
        assert (hop.org_team_id, hop.membership_id) == (t.org_b, t.membership_b.id)
        registered = await db.scalar(
            select(func.count()).select_from(AuthToken).where(AuthToken.jti == hop.jti)
        )
    assert registered == 0


async def test_every_browser_mint_site_stamps_the_membership(
    two_org_identity: TwoOrg,
) -> None:
    """The login route and the refresh route both mint through the helper: the
    cookie they set names the org, the membership and its epoch, and the
    registry row records them."""
    t = two_org_identity
    async with app_client() as c:
        login = await c.post(
            "/api/v1/auth/login", json={"email": t.user.email, "password": t.password}
        )
        assert login.status_code == 200, login.text
        access = login.cookies[settings.auth_cookie_name]
        refresh = login.cookies[REFRESH_COOKIE]
    async with app_client() as c:
        rotated = await c.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={refresh}"}
        )
        assert rotated.status_code == 200, rotated.text
        renewed = rotated.cookies[settings.auth_cookie_name]
    for token in (access, renewed):
        claims = _claims(token)
        assert (claims.org_team_id, claims.membership_id, claims.membership_epoch) == (
            t.org_a,
            t.membership_a.id,
            0,
        )
        async with AsyncSessionLocal() as db:
            row = await db.scalar(select(AuthToken).where(AuthToken.jti == claims.jti))
        assert row is not None
        assert (row.org_team_id, row.membership_id) == (t.org_a, t.membership_a.id)
    # The login wrote the identity-level password grant for its family.
    async with AsyncSessionLocal() as db:
        family = await db.scalar(
            select(AuthRefreshToken.family_id).where(
                AuthRefreshToken.access_jti == _claims(access).jti
            )
        )
        grants = (
            await db.execute(
                select(AuthSessionOrgGrant.org_team_id, AuthSessionOrgGrant.method).where(
                    AuthSessionOrgGrant.family_id == family
                )
            )
        ).all()
    assert [tuple(g) for g in grants] == [(None, "password")]


# --- refresh -----------------------------------------------------------------


async def _family(t: TwoOrg, *, org: UUID | None) -> tuple[str, UUID]:
    """A browser login for the person, through ``issue_session``. Returns the
    raw refresh token and the family id."""
    from backend.auth.session_issue import issue_session
    from fastapi import Request, Response

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "query_string": b"",
        }
    )
    carrier = Response()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        claims = await issue_session(
            db, user, request=request, response=carrier, method="password", org_team_id=org
        )
        await db.commit()
        family = await db.scalar(
            select(AuthRefreshToken.family_id).where(AuthRefreshToken.access_jti == claims.jti)
        )
    assert family is not None
    return _refresh_from(carrier), family


def _refresh_from(carrier: Any) -> str:
    for key, value in carrier.raw_headers:
        text = value.decode()
        if key == b"set-cookie" and text.startswith(f"{REFRESH_COOKIE}="):
            return text.split(";")[0].split("=", 1)[1]
    raise AssertionError("no refresh cookie was set")


async def _point_family(family_id: UUID, org: UUID | None) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.family_id == family_id)
            .values(active_org_team_id=org)
        )
        await db.commit()


async def _rotate(raw: str) -> httpx.Response:
    async with app_client() as c:
        return await c.post("/api/v1/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={raw}"})


@pytest.mark.parametrize(
    "point",
    [pytest.param("a", id="active-org-a"), pytest.param(None, id="legacy-null-family")],
)
async def test_a_family_rotates_into_its_active_org(
    two_org_identity: TwoOrg, point: str | None
) -> None:
    t = two_org_identity
    raw, family = await _family(t, org=None)
    await _point_family(family, t.org_a if point else None)
    resp = await _rotate(raw)
    assert resp.status_code == 200, resp.text
    claims = _claims(resp.cookies[settings.auth_cookie_name])
    assert (claims.org_team_id, claims.membership_id) == (t.org_a, t.membership_a.id)


async def test_a_family_whose_membership_ended_is_refused_and_survives(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    raw, family = await _family(t, org=None)
    await _deactivate(t.membership_a.id)
    resp = await _rotate(raw)
    assert resp.status_code == 401
    assert _code(resp) == REVOKED
    async with AsyncSessionLocal() as db:
        rows = (
            (await db.execute(select(AuthRefreshToken).where(AuthRefreshToken.family_id == family)))
            .scalars()
            .all()
        )
        access_revoked = await db.scalar(
            select(AuthToken.revoked_at).where(AuthToken.jti == rows[0].access_jti)
        )
    assert rows, "the family is the identity's and must survive"
    assert all(row.revoked_at is None for row in rows)
    assert all(row.active_org_team_id is None for row in rows)
    assert access_revoked is not None, "the access token minted beside it is revoked"


# --- device grants --------------------------------------------------------------


async def _redeem_device_grant(user_id: UUID, org: UUID | None) -> tuple[int, dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        raw, row = await device_service.create_device_code(db, client_id="alkera-cli", scope=None)
        user = await db.get(User, user_id)
        assert user is not None
        await device_service.approve(db, row, user=user, org_team_id=cast(UUID, org))
        await db.commit()
    async with app_client() as c:
        resp = await c.post(
            "/api/v1/auth/device/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": raw,
                "client_id": "alkera-cli",
            },
        )
    return resp.status_code, resp.json()


async def test_device_approval_records_the_session_org_and_redemption_mints_for_it(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        raw, row = await device_service.create_device_code(db, client_id="alkera-cli", scope=None)
        await db.commit()
    async with app_client() as c:
        login = await c.post(
            "/api/v1/auth/login", json={"email": t.user.email, "password": t.password}
        )
        assert login.status_code == 200
        approved = await c.post("/api/v1/auth/device/approve", json={"user_code": row.user_code})
        assert approved.status_code == 200, approved.text
    async with AsyncSessionLocal() as db:
        stored = await db.get(DeviceAuthorization, row.id)
        assert stored is not None
        assert stored.org_team_id == t.org_a
    async with app_client() as c:
        resp = await c.post(
            "/api/v1/auth/device/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": raw,
                "client_id": "alkera-cli",
            },
        )
    assert resp.status_code == 200, resp.text
    claims = _claims(resp.json()["access_token"])
    assert (claims.org_team_id, claims.membership_id) == (t.org_a, t.membership_a.id)
    async with AsyncSessionLocal() as db:
        audit = await db.scalar(
            select(OrgAuditEvent)
            .where(OrgAuditEvent.actor_id == t.user.id, OrgAuditEvent.action == "auth.device_login")
            .order_by(OrgAuditEvent.created_at.desc())
        )
    assert audit is not None
    assert audit.org_team_id == t.org_a


@pytest.mark.usefixtures("multi_org")
async def test_a_grant_stamped_with_the_second_org_mints_for_it(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    status, body = await _redeem_device_grant(t.user.id, t.org_b)
    assert status == 200, body
    claims = _claims(body["access_token"])
    assert (claims.org_team_id, claims.membership_id) == (t.org_b, t.membership_b.id)


async def test_a_grant_approved_before_orgs_were_recorded_mints_for_home(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    status, body = await _redeem_device_grant(t.user.id, None)
    assert status == 200, body
    assert _claims(body["access_token"]).org_team_id == t.org_a


# --- the echo and the assertion guard ------------------------------------------


async def test_every_credential_kind_echoes_its_org(
    two_org_identity: TwoOrg, real_session: Any
) -> None:
    from backend.services.credentials import ci_tokens as ci_token_service
    from tests.files._boxes import credential_box

    t = two_org_identity
    _ci_row, ci_raw = await ci_token_service.mint(
        real_session, org_id=t.org_a, created_by_id=t.admin_a.id, label="ci"
    )
    await real_session.commit()
    machine_raw, _machine = await credential_box(real_session, org_id=t.org_b, user_id=t.admin_b.id)
    pat = await _pat(t.user.id, t.org_a, t.membership_a)
    for headers, org, kind in (
        (_bearer(t.token_a), t.org_a, "user"),
        (_bearer(pat), t.org_a, "personal_access_token"),
        (_bearer(ci_raw), t.org_a, "service"),
        (_bearer(machine_raw), t.org_b, "machine"),
    ):
        resp = await _probe("/principal", headers)
        assert resp.status_code == 200, (kind, resp.text)
        assert resp.headers[ORG_HEADER] == str(org), kind


async def test_an_unauthenticated_response_names_no_org(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert ORG_HEADER not in resp.headers


async def _preference_rows(user_id: UUID) -> int:
    async with AsyncSessionLocal() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(UserPreference)
                .where(UserPreference.user_id == user_id)
            )
            or 0
        )


@pytest.mark.parametrize(
    "asserted",
    [
        pytest.param("org-b", id="the-other-org"),
        pytest.param("not-a-uuid", id="unreadable"),
        pytest.param(str(uuid4()), id="an-unknown-org"),
    ],
)
async def test_a_stale_org_assertion_is_a_409_before_the_route_writes(
    client: AsyncClient, two_org_identity: TwoOrg, asserted: str
) -> None:
    t = two_org_identity
    header = str(t.org_b) if asserted == "org-b" else asserted
    before = await _preference_rows(t.user.id)
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"preferences": {"reduce_motion": True}},
        headers={**_bearer(t.token_a), ORG_HEADER: header},
    )
    assert resp.status_code == 409, resp.text
    assert _code(resp) == "org_changed"
    # A Bearer token is no browser tab: it is told what is wrong with its
    # request, not that another window switched organizations.
    assert resp.json()["error"]["message"] == ORG_CHANGED.client
    # The answer still says which org the credential is in.
    assert resp.headers[ORG_HEADER] == str(t.org_a)
    assert await _preference_rows(t.user.id) == before


@pytest.mark.parametrize(
    "asserted", [pytest.param(True, id="matching"), pytest.param(False, id="absent")]
)
async def test_a_matching_or_absent_assertion_changes_nothing(
    client: AsyncClient, two_org_identity: TwoOrg, asserted: bool
) -> None:
    t = two_org_identity
    headers = {**_bearer(t.token_a), **({ORG_HEADER: str(t.org_a)} if asserted else {})}
    resp = await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"reduce_motion": True}}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert await _preference_rows(t.user.id) == 1


async def test_an_assertion_is_held_on_every_credential_kind(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    pat = await _pat(t.user.id, t.org_a, t.membership_a)
    resp = await _probe("/principal", {**_bearer(pat), ORG_HEADER: str(t.org_b)})
    assert resp.status_code == 409


async def test_authenticated_json_is_never_cached(
    client: AsyncClient, two_org_identity: TwoOrg
) -> None:
    resp = await client.get("/api/v1/auth/me", headers=_bearer(two_org_identity.token_a))
    assert resp.status_code == 200
    assert "no-store" in resp.headers["cache-control"]


# --- helpers for the socket ---------------------------------------------------


class _SocketEnd:
    async def send_text(self, _text: str) -> None:
        return None


async def _socket(t: TwoOrg, claims: SessionClaims) -> SocketSession:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        ref = EntitlementRef(await load_entitlements(db, user, org_id=claims.org_team_id))
    return SocketSession(
        websocket=cast(WebSocket, _SocketEnd()),
        user=user,
        claims=claims,
        peer_id="peer-two-org",
        runtime=cast(RealtimeRuntime, None),
        registry=cast(DocRegistry, None),
        ref=ref,
    )
