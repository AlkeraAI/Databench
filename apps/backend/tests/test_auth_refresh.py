"""The refresh-token family behind a browser session.

The access token lives thirty minutes and says `token_expired` when it lapses;
what keeps a browser signed in is the refresh cookie — opaque, hashed at rest,
scoped to its own route, rotated on every use, and ended server-side by logout,
a credential change, a ban, a privilege change, or the user themselves. Every
timeline here is driven across its boundary with a frozen clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import (
    COOKIE_NAME,
    decode_session_token,
    encode_cli_token,
    hash_lookup_token,
    register_token,
    rotate_refresh_token,
)
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuthRefreshToken, IdentitySecurityEvent, TeamRole, TokenType
from backend.api.routes.realtime import events as events_route
from backend.services.org import memberships as membership_service
from backend.services.realtime.filters import EntitlementRef, EntitlementSnapshot
from freezegun import freeze_time
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, make_member, mint_cli_token

REFRESH = "/api/v1/auth/refresh"
ME = "/api/v1/auth/me"
SESSIONS = "/api/v1/auth/sessions"
REFRESH_COOKIE = settings.auth_refresh_cookie_name


@pytest.fixture(autouse=True)
def _reset_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


@pytest.fixture(autouse=True)
def _thirty_minute_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned so a developer's `.env` cannot stretch the timelines below."""
    monkeypatch.setattr(settings, "auth_token_ttl_seconds", 30 * 60)
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 0)


def _start() -> datetime:
    # After the fixtures' real clock, so a frozen `iat` never predates the
    # account's token_epoch (a revoke-all lever, not an expiry).
    return datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)


def _set_cookies(resp: Response) -> dict[str, str]:
    """Every `Set-Cookie` on a response, keyed by cookie name (the full header)."""
    return {h.split("=", 1)[0]: h for h in resp.headers.get_list("set-cookie")}


def _value(header: str) -> str:
    return header.split(";", 1)[0].split("=", 1)[1]


class Pair:
    """The two cookies a sign-in hands a browser, held as raw values so each
    request below chooses exactly what it presents (a jar would silently drop
    an expired cookie, and the point is what the SERVER says to it)."""

    def __init__(self, resp: Response) -> None:
        cookies = _set_cookies(resp)
        self.access = _value(cookies[COOKIE_NAME])
        self.refresh = _value(cookies[REFRESH_COOKIE])
        self.raw = cookies

    @property
    def jti(self) -> str:
        jti = decode_session_token(self.access, allow_expired=True).jti
        assert jti is not None
        return jti


def _fresh() -> AsyncClient:
    return app_client()


async def _login(email: str, password: str) -> Pair:
    async with _fresh() as c:
        resp = await c.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        return Pair(resp)


async def _me(access: str) -> Response:
    async with _fresh() as c:
        return await c.get(ME, headers={"Cookie": f"{COOKIE_NAME}={access}"})


async def _refresh(refresh: str) -> Response:
    async with _fresh() as c:
        return await c.post(REFRESH, headers={"Cookie": f"{REFRESH_COOKIE}={refresh}"})


def _code(resp: Response) -> str | None:
    return resp.json().get("error", {}).get("code")


# ---------------------------------------------------------------------------
# The pair, and where the refresh cookie may travel
# ---------------------------------------------------------------------------


async def test_login_sets_both_cookies_and_scopes_the_refresh_cookie_to_its_route(
    org_admin: OrgWithAdmin,
) -> None:
    pair = await _login(org_admin.admin_email, org_admin.admin_password)
    refresh_header = pair.raw[REFRESH_COOKIE].lower()
    assert f"path={REFRESH}".lower() in refresh_header
    assert "httponly" in refresh_header
    assert "samesite=strict" in refresh_header
    assert "path=/;" in pair.raw[COOKIE_NAME].lower() or pair.raw[COOKIE_NAME].lower().endswith(
        "path=/"
    )
    assert len(pair.refresh) >= 43  # 32 random bytes, URL-safe base64


async def test_the_refresh_cookie_never_rides_an_ordinary_request(
    org_admin: OrgWithAdmin,
) -> None:
    """A browser honours the cookie's `Path`, so the long-lived credential is
    never in the bag on `/auth/me` (or on logout, which must end the family
    through the access token instead). The jar models exactly that."""
    async with _fresh() as c:
        resp = await c.post(
            "/api/v1/auth/login",
            json={"email": org_admin.admin_email, "password": org_admin.admin_password},
        )
        assert resp.status_code == 200
        jar = {cookie.name: cookie for cookie in c.cookies.jar}
        assert jar[REFRESH_COOKIE].path == REFRESH
        assert jar[COOKIE_NAME].path == "/"
        # The jar sends it to its route — the refresh succeeds from the jar alone —
        # and a request with no refresh cookie at all is refused, not guessed.
        assert (await c.post(REFRESH)).status_code == 200
    async with _fresh() as bare:
        refused = await bare.post(REFRESH)
        assert (refused.status_code, _code(refused)) == (401, "unauthorized")


# ---------------------------------------------------------------------------
# Rotation, expiry, reuse
# ---------------------------------------------------------------------------


async def test_an_expired_access_token_is_renewed_by_the_refresh_cookie(
    org_admin: OrgWithAdmin,
) -> None:
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        first = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=31))
        lapsed = await _me(first.access)
        assert (lapsed.status_code, _code(lapsed)) == (401, "token_expired")

        renewed = await _refresh(first.refresh)
        assert renewed.status_code == 200, renewed.text
        second = Pair(renewed)
        assert second.access != first.access and second.refresh != first.refresh
        assert (await _me(second.access)).status_code == 200
        expires = datetime.fromisoformat(renewed.json()["expires_at"])
        assert expires == start + timedelta(minutes=61)


async def _reuse_events(user_id: UUID) -> list[IdentitySecurityEvent]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(IdentitySecurityEvent).where(
                IdentitySecurityEvent.user_id == user_id,
                IdentitySecurityEvent.event == "auth.refresh_token_reused",
            )
        )
        return list(rows.scalars().all())


async def _live_rows(family_id: UUID) -> list[AuthRefreshToken]:
    """The family's current tokens: minted, never rotated, never revoked."""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(AuthRefreshToken).where(
                AuthRefreshToken.family_id == family_id,
                AuthRefreshToken.used_at.is_(None),
                AuthRefreshToken.revoked_at.is_(None),
            )
        )
        return list(rows.scalars().all())


async def _family_of(refresh: str) -> UUID:
    async with AsyncSessionLocal() as db:
        family = await db.scalar(
            select(AuthRefreshToken.family_id).where(
                AuthRefreshToken.token_hash == hash_lookup_token(refresh)
            )
        )
    assert family is not None
    return family


async def test_a_rotated_refresh_token_presented_again_ends_the_whole_family(
    org_admin: OrgWithAdmin,
) -> None:
    """Reuse detection, the property that makes rotation safe. A spent token
    coming back after the concurrent-tab grace means it was copied, so the
    legitimate successor is cut off with the thief: refresh token AND the live
    access token minted beside it. The answer names the reuse, and the
    identity's security log records it."""
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        first = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=10))
        second = Pair(await _refresh(first.refresh))
        assert (await _me(second.access)).status_code == 200

        frozen.move_to(start + timedelta(minutes=12))
        replay = await _refresh(first.refresh)
        assert (replay.status_code, _code(replay)) == (401, "refresh_token_reused")

        dead = await _refresh(second.refresh)
        assert (dead.status_code, _code(dead)) == (401, "session_revoked")
        cut = await _me(second.access)
        assert (cut.status_code, _code(cut)) == (401, "session_revoked")
    events = await _reuse_events(org_admin.admin_id)
    assert [e.detail for e in events] == [{"family_id": str(await _family_of(first.refresh))}]


async def test_two_tabs_refreshing_within_the_grace_get_the_same_successor(
    org_admin: OrgWithAdmin,
) -> None:
    """A second presentation seconds after the first is the same browser's
    other tab, not a thief. It is handed the SAME successor, never a second
    one, so the family keeps exactly one current token; both tabs' access
    tokens work, and the successor rotates once more as usual."""
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        first = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=10))
        a = Pair(await _refresh(first.refresh))
        frozen.move_to(start + timedelta(minutes=10, seconds=5))
        second = await _refresh(first.refresh)
        assert second.status_code == 200, second.text
        b = Pair(second)
        assert b.refresh == a.refresh
        assert b.access != a.access
        assert [row.token_hash for row in await _live_rows(await _family_of(first.refresh))] == [
            hash_lookup_token(a.refresh)
        ]
        assert (await _me(a.access)).status_code == 200
        assert (await _me(b.access)).status_code == 200
        frozen.move_to(start + timedelta(minutes=20))
        assert (await _refresh(a.refresh)).status_code == 200


@pytest.mark.parametrize(
    ("after_rotation", "expected"),
    [
        pytest.param(timedelta(seconds=0), 200, id="same-instant"),
        pytest.param(timedelta(seconds=10), 200, id="at-the-grace"),
        pytest.param(timedelta(seconds=11), 401, id="one-second-past-the-grace"),
        pytest.param(timedelta(minutes=5), 401, id="minutes-later"),
    ],
)
async def test_the_grace_ends_at_its_boundary(
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    after_rotation: timedelta,
    expected: int,
) -> None:
    """Inside the grace the re-presentation gets the same successor; one second
    past it the same request is reuse and ends the family, the successor and
    its access token with it."""
    monkeypatch.setattr(settings, "auth_refresh_reuse_grace_seconds", 10)
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        first = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=10))
        a = Pair(await _refresh(first.refresh))
        frozen.move_to(start + timedelta(minutes=10) + after_rotation)
        again = await _refresh(first.refresh)
        assert again.status_code == expected, again.text
        if expected == 200:
            assert Pair(again).refresh == a.refresh
            assert (await _me(a.access)).status_code == 200
            return
        assert _code(again) == "refresh_token_reused"
        assert _code(await _refresh(a.refresh)) == "session_revoked"
        assert _code(await _me(a.access)) == "session_revoked"


async def test_a_replay_inside_the_grace_after_the_successor_moved_on_is_reuse(
    org_admin: OrgWithAdmin,
) -> None:
    """The grace hands back the successor only while it is the family's
    current token. Once the successor has itself been rotated, the parent
    coming back, however soon, can only be a copy."""
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        first = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=10))
        a = Pair(await _refresh(first.refresh))
        frozen.move_to(start + timedelta(minutes=10, seconds=1))
        b = Pair(await _refresh(a.refresh))
        frozen.move_to(start + timedelta(minutes=10, seconds=2))
        replay = await _refresh(first.refresh)
        assert (replay.status_code, _code(replay)) == (401, "refresh_token_reused")
        assert _code(await _refresh(b.refresh)) == "session_revoked"
        assert _code(await _me(b.access)) == "session_revoked"


async def test_ending_the_family_ends_the_access_token_of_every_tab(
    org_admin: OrgWithAdmin,
) -> None:
    """Both tabs hold an access token after a grace re-presentation; logging out
    from either ends the family and both tokens, and the successor with it."""
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        first = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=10))
        a = Pair(await _refresh(first.refresh))
        b = Pair(await _refresh(first.refresh))
        async with _fresh() as c:
            out = await c.post(
                "/api/v1/auth/logout", headers={"Cookie": f"{COOKIE_NAME}={a.access}"}
            )
        assert out.status_code == 200
        assert _code(await _me(a.access)) == "session_revoked"
        assert _code(await _me(b.access)) == "session_revoked"
        assert _code(await _refresh(a.refresh)) == "session_revoked"


async def test_two_requests_racing_the_same_token_get_one_successor(
    org_admin: OrgWithAdmin,
) -> None:
    """The presented row is read under a lock, so a request that arrives while
    another is rotating the same token waits for that rotation to commit and
    is then handed its successor. Without the lock both would see the token
    unused and each mint a child: two live tokens in one family."""
    pair = await _login(org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as first_db, AsyncSessionLocal() as second_db:
        first, _ = await rotate_refresh_token(first_db, pair.refresh)
        racing = asyncio.create_task(rotate_refresh_token(second_db, pair.refresh))
        await asyncio.sleep(0.3)
        await first_db.commit()
        second, _ = await racing
        await second_db.commit()
    assert second.raw == first.raw
    live = await _live_rows(await _family_of(pair.refresh))
    assert [row.token_hash for row in live] == [hash_lookup_token(first.raw)]


async def test_strict_reuse_detection_when_the_grace_is_zero(
    org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_refresh_reuse_grace_seconds", 0)
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        first = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=10))
        assert (await _refresh(first.refresh)).status_code == 200
        frozen.move_to(start + timedelta(minutes=10, seconds=1))
        replay = await _refresh(first.refresh)
        assert (replay.status_code, _code(replay)) == (401, "refresh_token_reused")


async def test_a_family_nobody_comes_back_to_dies_of_neglect_after_seven_days(
    org_admin: OrgWithAdmin,
) -> None:
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        pair = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(days=6, hours=23))
        pair = Pair(await _refresh(pair.refresh))  # activity slides the idle window
        frozen.move_to(start + timedelta(days=13, hours=22))
        pair = Pair(await _refresh(pair.refresh))
        frozen.move_to(start + timedelta(days=20, hours=22, seconds=1))
        idle = await _refresh(pair.refresh)
        assert (idle.status_code, _code(idle)) == (401, "session_expired")


async def test_the_absolute_expiry_ends_the_family_at_thirty_days_whatever_the_activity(
    org_admin: OrgWithAdmin,
    wide_credential_window: None,
) -> None:
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        pair = await _login(org_admin.admin_email, org_admin.admin_password)
        for day in range(1, 30):
            frozen.move_to(start + timedelta(days=day))
            renewed = await _refresh(pair.refresh)
            assert renewed.status_code == 200, f"day {day}: {renewed.text}"
            pair = Pair(renewed)
        frozen.move_to(start + timedelta(days=29, hours=23, minutes=59))
        pair = Pair(await _refresh(pair.refresh))
        frozen.move_to(start + timedelta(days=30, seconds=1))
        over = await _refresh(pair.refresh)
        assert (over.status_code, _code(over)) == (401, "session_expired")


async def test_a_refusal_clears_both_cookies(org_admin: OrgWithAdmin) -> None:
    async with _fresh() as c:
        resp = await c.post(REFRESH, headers={"Cookie": f"{REFRESH_COOKIE}=not-a-token"})
    assert resp.status_code == 401
    cleared = _set_cookies(resp)
    assert {COOKIE_NAME, REFRESH_COOKIE} <= set(cleared)
    for header in cleared.values():
        assert "max-age=0" in header.lower() or "expires=" in header.lower()


# ---------------------------------------------------------------------------
# Every revocation path ends the family
# ---------------------------------------------------------------------------


async def test_logout_ends_the_family_even_with_an_expired_access_token(
    org_admin: OrgWithAdmin,
) -> None:
    """The refresh cookie never reaches the logout route; the (possibly expired)
    access token names the family, and signing out must end it."""
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        pair = await _login(org_admin.admin_email, org_admin.admin_password)
        frozen.move_to(start + timedelta(minutes=31))
        async with _fresh() as c:
            out = await c.post(
                "/api/v1/auth/logout", headers={"Cookie": f"{COOKIE_NAME}={pair.access}"}
            )
        assert out.status_code == 200
        assert {COOKIE_NAME, REFRESH_COOKIE} <= set(_set_cookies(out))
        dead = await _refresh(pair.refresh)
        assert (dead.status_code, _code(dead)) == (401, "session_revoked")


async def test_logout_everywhere_ends_every_family(org_admin: OrgWithAdmin) -> None:
    here = await _login(org_admin.admin_email, org_admin.admin_password)
    there = await _login(org_admin.admin_email, org_admin.admin_password)
    async with _fresh() as c:
        resp = await c.post(
            "/api/v1/auth/logout-all", headers={"Cookie": f"{COOKIE_NAME}={here.access}"}
        )
    assert resp.status_code == 200
    for pair in (here, there):
        assert _code(await _refresh(pair.refresh)) == "session_revoked"


async def test_logout_everywhere_keeps_the_browser_that_asked(org_admin: OrgWithAdmin) -> None:
    """ "You stay signed in on this device": the asking tab gets a fresh session
    past the new epoch; every other browser, CLI token and the tab's own old
    pair are refused afterwards."""
    acting = await _login(org_admin.admin_email, org_admin.admin_password)
    other = await _login(org_admin.admin_email, org_admin.admin_password)
    cli_token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    async with _fresh() as c:
        resp = await c.post(
            "/api/v1/auth/logout-all", headers={"Cookie": f"{COOKIE_NAME}={acting.access}"}
        )
    assert resp.status_code == 200, resp.text
    kept = Pair(resp)
    assert kept.access != acting.access
    assert (await _me(kept.access)).status_code == 200
    assert (await _refresh(kept.refresh)).status_code == 200

    assert _code(await _me(other.access)) == "session_revoked"
    assert _code(await _refresh(other.refresh)) == "session_revoked"
    assert _code(await _me(acting.access)) == "session_revoked"
    assert _code(await _refresh(acting.refresh)) == "session_revoked"
    async with _fresh() as c:
        cli = await c.get(ME, headers={"Authorization": f"Bearer {cli_token}"})
    assert cli.status_code == 401


async def test_logout_everywhere_from_a_bearer_token_keeps_nothing(
    org_admin: OrgWithAdmin,
) -> None:
    """A CLI caller has no browser session to keep: no fresh cookie is minted,
    the cookies are cleared, and its own token is refused afterwards."""
    browser = await _login(org_admin.admin_email, org_admin.admin_password)
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    async with _fresh() as c:
        resp = await c.post("/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        assert (await c.get(ME, headers={"Authorization": f"Bearer {token}"})).status_code == 401
    assert "Max-Age=0" in _set_cookies(resp)[COOKIE_NAME]
    assert _code(await _refresh(browser.refresh)) == "session_revoked"


async def test_a_password_change_ends_every_other_family_but_keeps_the_acting_tab(
    org_admin: OrgWithAdmin,
) -> None:
    acting = await _login(org_admin.admin_email, org_admin.admin_password)
    other = await _login(org_admin.admin_email, org_admin.admin_password)
    async with _fresh() as c:
        resp = await c.patch(
            f"/api/v1/users/{org_admin.admin_id}",
            json={"password": "a-new-password-98765", "current_password": org_admin.admin_password},
            headers={"Cookie": f"{COOKIE_NAME}={acting.access}"},
        )
    assert resp.status_code == 200, resp.text
    kept = Pair(resp)
    assert (await _me(kept.access)).status_code == 200
    assert (await _refresh(kept.refresh)).status_code == 200
    assert _code(await _refresh(other.refresh)) == "session_revoked"
    assert _code(await _me(other.access)) == "session_revoked"
    assert _code(await _refresh(acting.refresh)) == "session_revoked"


async def test_a_ban_ends_the_family(
    platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    pair = await _login(member.email, password)
    staff = await _login(platform_admin.admin_email, platform_admin.admin_password)
    async with _fresh() as c:
        banned = await c.post(
            "/admin/v1/bans/users",
            json={"user_id": str(member.id)},
            headers={"Cookie": f"{COOKIE_NAME}={staff.access}"},
        )
    assert banned.status_code == 201, banned.text
    assert (await _refresh(pair.refresh)).status_code == 401
    assert (await _me(pair.access)).status_code == 401


async def test_a_platform_role_change_ends_the_family(
    platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    pair = await _login(member.email, password)
    staff = await _login(platform_admin.admin_email, platform_admin.admin_password)
    async with _fresh() as c:
        granted = await c.patch(
            f"/admin/v1/users/{member.id}/platform_role",
            json={"platform_role": "alkera_support"},
            headers={"Cookie": f"{COOKIE_NAME}={staff.access}"},
        )
    assert granted.status_code == 200, granted.text
    assert _code(await _refresh(pair.refresh)) == "session_revoked"
    assert _code(await _me(pair.access)) == "session_revoked"
    # The member signs back in and holds the new role from the first request.
    again = await _login(member.email, password)
    assert decode_session_token(again.access).platform_role is not None


async def test_a_team_role_change_ends_the_access_tokens_minted_under_the_old_role(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A role change is the org's decision: it ends every credential the member
    holds in the org (the membership's epoch moves), so the next request is
    decided under the new role. The login session is the identity's and
    survives: its next refresh mints a token at the new epoch."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    pair = await _login(member.email, password)
    membership = await membership_service.get(
        real_session, team_id=org_admin.org_id, user_id=member.id
    )
    assert membership is not None
    await membership_service.change_role(real_session, membership, TeamRole.ADMIN)
    await real_session.commit()
    assert (await _me(pair.access)).status_code == 401
    renewed = await _refresh(pair.refresh)
    assert renewed.status_code == 200, renewed.text
    assert (await _me(renewed.cookies[COOKIE_NAME])).status_code == 200


async def test_a_role_set_to_what_it_already_is_ends_nothing(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    pair = await _login(member.email, password)
    membership = await membership_service.get(
        real_session, team_id=org_admin.org_id, user_id=member.id
    )
    assert membership is not None
    await membership_service.change_role(real_session, membership, TeamRole.MEMBER)
    await real_session.commit()
    assert (await _refresh(pair.refresh)).status_code == 200


# ---------------------------------------------------------------------------
# The sessions list
# ---------------------------------------------------------------------------


async def test_the_sessions_list_shows_families_and_a_user_can_end_one(
    org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    here = await _login(org_admin.admin_email, org_admin.admin_password)
    there = await _login(org_admin.admin_email, org_admin.admin_password)
    headers = {"Cookie": f"{COOKIE_NAME}={here.access}"}
    async with _fresh() as c:
        listed = await c.get(SESSIONS, headers=headers)
        assert listed.status_code == 200
        sessions = listed.json()["sessions"]
        assert len(sessions) == 2
        assert all(s["token_type"] == "session" for s in sessions)
        current = [s for s in sessions if s["current"]]
        assert len(current) == 1
        other = next(s for s in sessions if not s["current"])
        # Ending the other family kills its refresh token AND its access token.
        assert (await c.delete(f"{SESSIONS}/{other['jti']}", headers=headers)).status_code == 200
        assert _code(await _refresh(there.refresh)) == "session_revoked"
        assert _code(await _me(there.access)) == "session_revoked"
        assert len((await c.get(SESSIONS, headers=headers)).json()["sessions"]) == 1
        # Unknown, and someone else's, are the same 404.
        assert (await c.delete(f"{SESSIONS}/{uuid4().hex}", headers=headers)).status_code == 404
        foreign = await _login(platform_admin.admin_email, platform_admin.admin_password)
        theirs = (
            await c.get(SESSIONS, headers={"Cookie": f"{COOKIE_NAME}={foreign.access}"})
        ).json()["sessions"][0]["jti"]
        assert (await c.delete(f"{SESSIONS}/{theirs}", headers=headers)).status_code == 404
        assert (await _refresh(foreign.refresh)).status_code == 200


async def test_a_cli_token_still_lists_and_revokes_by_its_jti(org_admin: OrgWithAdmin) -> None:
    pair = await _login(org_admin.admin_email, org_admin.admin_password)
    token, claims = encode_cli_token(
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_team_id=org_admin.org_id,
        platform_role=None,
    )
    async with AsyncSessionLocal() as db:
        await register_token(db, claims=claims, token_type=TokenType.CLI)
        await db.commit()
    headers = {"Cookie": f"{COOKIE_NAME}={pair.access}"}
    async with _fresh() as c:
        sessions = (await c.get(SESSIONS, headers=headers)).json()["sessions"]
        cli = [s for s in sessions if s["token_type"] == "cli"]
        assert [s["jti"] for s in cli] == [claims.jti]
        assert (await c.delete(f"{SESSIONS}/{claims.jti}", headers=headers)).status_code == 200
        bearer = await c.get(ME, headers={"Authorization": f"Bearer {token}"})
        assert (bearer.status_code, _code(bearer)) == (401, "session_revoked")


async def test_me_reports_when_the_access_token_lapses(org_admin: OrgWithAdmin) -> None:
    start = _start()
    with freeze_time(start, real_asyncio=True):
        pair = await _login(org_admin.admin_email, org_admin.admin_password)
        me = await _me(pair.access)
    assert datetime.fromisoformat(me.json()["session_expires_at"]) == start + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# A long-lived connection follows the family, not the access token's exp
# ---------------------------------------------------------------------------


def _ref() -> EntitlementRef:
    return EntitlementRef(
        EntitlementSnapshot(org_id=None, team_ids=frozenset(), org_admin=False, platform=False)
    )


async def test_the_stream_recheck_outlives_the_access_token_while_the_family_is_alive(
    org_admin: OrgWithAdmin,
) -> None:
    """An open event stream re-checks its session every keepalive. Past the
    access token's `exp` the family is what answers: alive keeps the stream,
    a revoked family closes it on the next tick, and a token with no family
    (a CLI bearer) still ends at its own `exp`."""
    start = _start()
    with freeze_time(start, real_asyncio=True) as frozen:
        pair = await _login(org_admin.admin_email, org_admin.admin_password)
        claims = decode_session_token(pair.access)
        frozen.move_to(start + timedelta(minutes=45))
        assert await events_route._recheck(claims, org_admin.admin_id, _ref()) is True

        async with _fresh() as c:
            await c.post("/api/v1/auth/logout", headers={"Cookie": f"{COOKIE_NAME}={pair.access}"})
        assert await events_route._recheck(claims, org_admin.admin_id, _ref()) is False

        _cli, cli_claims = encode_cli_token(
            user_id=org_admin.admin_id,
            email=org_admin.admin_email,
            org_team_id=org_admin.org_id,
            platform_role=None,
            now=int(start.timestamp()) - settings.auth_cli_token_ttl_seconds - 1,
        )
        async with AsyncSessionLocal() as db:
            await register_token(db, claims=cli_claims, token_type=TokenType.CLI)
            await db.commit()
        assert await events_route._recheck(cli_claims, org_admin.admin_id, _ref()) is False


async def test_a_family_ended_from_the_sessions_list_closes_the_stream(
    org_admin: OrgWithAdmin,
) -> None:
    pair = await _login(org_admin.admin_email, org_admin.admin_password)
    other = await _login(org_admin.admin_email, org_admin.admin_password)
    claims = decode_session_token(other.access)
    assert await events_route._recheck(claims, org_admin.admin_id, _ref()) is True
    headers = {"Cookie": f"{COOKIE_NAME}={pair.access}"}
    async with _fresh() as c:
        family = next(
            s["jti"]
            for s in (await c.get(SESSIONS, headers=headers)).json()["sessions"]
            if not s["current"]
        )
        assert (await c.delete(f"{SESSIONS}/{family}", headers=headers)).status_code == 200
    assert await events_route._recheck(claims, org_admin.admin_id, _ref()) is False
