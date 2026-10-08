"""Session idle windows, and what does and does not count as activity on them.

The browser keeps its session by rotating the refresh cookie, which is scoped
to the refresh route and never reaches the event stream or the socket. What
the tests below pin:

- the person's own requests slide both idle windows (the access token's
  ``last_used_at`` and the family's ``idle_expires_at``);
- a connection merely held open does NOT: an open stream's keepalives, a
  socket's pings and the server's pushes go on in a tab nobody is at, so
  counting them would keep an unattended session alive to its absolute cap.
  A socket slides only on a tick after the person edited or moved a caret;
- no slide can move the family's ABSOLUTE expiry, which ends even a session
  that never stopped being active.

The timelines are driven across their boundaries with freezegun rather than
asserted at a fixed instant: a window is only proven by a clock that passes
through the moment it would have closed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from alkera_core.auth import (
    SessionClaims,
    TokenRevokedError,
    assert_token_active,
    encode_session_token,
    family_alive,
    mint_refresh_token,
    register_token,
    revoke_family,
    revoke_jti,
    slide_family_idle,
)
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuthRefreshToken, AuthToken, TokenType, User
from alkera_core.schemas.realtime.envelope import PresenceCursor
from alkera_core.schemas.realtime.frames import PingFrame, PresenceCursorFrame
from backend.api.routes.realtime import events as events_route
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.filters import EntitlementRef, load_entitlements
from backend.services.realtime.runtime import RealtimeRuntime
from backend.services.realtime.session import SocketSession
from fastapi import WebSocket
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin


@pytest.fixture(autouse=True)
def _reset_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


async def _session_with_family(org: OrgWithAdmin) -> SessionClaims:
    """A registered access token with the refresh family a browser login mints
    beside it — the shape every long-lived connection is opened with."""
    _, claims = encode_session_token(
        user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id, platform_role=None
    )
    async with AsyncSessionLocal() as s:
        await register_token(s, claims=claims, token_type=TokenType.SESSION)
        await mint_refresh_token(s, user_id=org.admin_id, access_jti=claims.jti)
        await s.commit()
    return claims


async def _family_row(jti: str) -> AuthRefreshToken:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(select(AuthRefreshToken).where(AuthRefreshToken.access_jti == jti))
        ).scalar_one()


async def _last_used_at(jti: str) -> datetime | None:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(select(AuthToken.last_used_at).where(AuthToken.jti == jti))
        ).scalar_one()


# ---------------------------------------------------------------------------
# The access token's idle window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_inside_the_window_carries_the_session_past_it(
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sixty-minute idle window, used at minute fifty: the request at minute
    fifty-nine is nine minutes since the last use, not fifty-nine, so it
    passes. The clock crosses the boundary the original mint set."""
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 3600)
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _session_with_family(org_admin)
        assert claims.jti is not None
        user = await real_session.get(User, org_admin.admin_id)
        assert user is not None

        frozen.move_to("2026-06-01 12:50:00")
        await assert_token_active(real_session, claims, user)
        await real_session.commit()
        assert await _last_used_at(claims.jti) == datetime(2026, 6, 1, 12, 50, tzinfo=UTC)

        frozen.move_to("2026-06-01 12:59:00")  # past mint + 60 min, inside use + 60 min
        await assert_token_active(real_session, claims, user)


@pytest.mark.asyncio
async def test_silence_past_the_window_ends_the_session(
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same window: nothing touched it, so minute
    sixty-one is over."""
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 3600)
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _session_with_family(org_admin)
        user = await real_session.get(User, org_admin.admin_id)
        assert user is not None

        frozen.move_to("2026-06-01 13:01:00")
        with pytest.raises(TokenRevokedError, match="idle timeout"):
            await assert_token_active(real_session, claims, user)


# ---------------------------------------------------------------------------
# The family's idle window — the one a long-lived connection has to move
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_connection_slides_the_family_idle_window(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The slide the re-auth tick performs: the family's idle expiry moves to
    now + the idle window. Without it the only thing that ever moves it is a
    refresh rotation, which an open stream never performs."""
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _session_with_family(org_admin)
        assert claims.jti is not None
        before = (await _family_row(claims.jti)).idle_expires_at

        frozen.move_to("2026-06-01 18:00:00")
        assert await slide_family_idle(real_session, claims) is True
        await real_session.commit()

    after = (await _family_row(claims.jti)).idle_expires_at
    assert after == datetime(2026, 6, 1, 18, tzinfo=UTC) + timedelta(
        seconds=settings.auth_refresh_idle_seconds
    )
    assert after > before


@pytest.mark.asyncio
async def test_a_slide_is_throttled_so_a_keepalive_is_not_a_write(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A stream re-checks every fifteen seconds. Moving the row each time would
    be a write per tick per open connection, so a slide that would move the
    window by less than half a minute is skipped."""
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _session_with_family(org_admin)
        frozen.move_to("2026-06-01 12:00:15")
        assert await slide_family_idle(real_session, claims) is False
        frozen.move_to("2026-06-01 12:01:00")
        assert await slide_family_idle(real_session, claims) is True


async def _work_every_day_until(
    frozen: object, db: AsyncSession, claims: SessionClaims, until: datetime
) -> None:
    """A connection held every day from the session's start to ``until`` — the
    shape of somebody who uses the app daily and is never idle long enough for
    the idle window to bite."""
    moment = datetime(2026, 6, 1, 12, tzinfo=UTC)
    while moment < until:
        moment += timedelta(days=1)
        frozen.move_to(min(moment, until))  # type: ignore[attr-defined]
        await slide_family_idle(db, claims)
    await db.commit()


@pytest.mark.asyncio
async def test_no_slide_moves_the_absolute_expiry(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The bound activity may not move. Worked every day right up to the
    ceiling, the idle window is clamped AT the ceiling rather than carried
    past it — so the session still ends there however live the connection."""
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _session_with_family(org_admin)
        assert claims.jti is not None
        absolute = (await _family_row(claims.jti)).absolute_expires_at

        await _work_every_day_until(frozen, real_session, claims, absolute - timedelta(hours=1))
        row = await _family_row(claims.jti)
        assert row.absolute_expires_at == absolute
        assert row.idle_expires_at == absolute, "the idle window is clamped, not extended"


@pytest.mark.asyncio
async def test_the_absolute_cap_ends_a_session_that_never_stopped_being_active(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The whole point of the cap: a month of daily use keeps the family alive
    the entire way — the thing sliding is FOR — and it is still dead the moment
    the ceiling passes."""
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _session_with_family(org_admin)
        assert claims.jti is not None
        absolute = (await _family_row(claims.jti)).absolute_expires_at

        await _work_every_day_until(frozen, real_session, claims, absolute - timedelta(minutes=1))
        assert await family_alive(real_session, claims) is True, (
            "daily use must not run into the seven-day idle window"
        )

        frozen.move_to(absolute + timedelta(seconds=1))
        assert await family_alive(real_session, claims) is False
        assert await slide_family_idle(real_session, claims) is False, (
            "a slide must never revive a family the ceiling has ended"
        )


@pytest.mark.asyncio
async def test_a_family_left_idle_past_its_window_is_not_revived_by_a_slide(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The other bound: come back after the idle window and the connection
    cannot write the session back to life either."""
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _session_with_family(org_admin)
        frozen.move_to(
            datetime(2026, 6, 1, 12, tzinfo=UTC)
            + timedelta(seconds=settings.auth_refresh_idle_seconds + 60)
        )
        assert await family_alive(real_session, claims) is False
        assert await slide_family_idle(real_session, claims) is False


@pytest.mark.asyncio
async def test_a_revoked_family_is_not_revived_by_a_slide(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Signing out, a ban or a reuse detection ends the family. A connection
    still ticking must not write it back to life."""
    claims = await _session_with_family(org_admin)
    assert claims.jti is not None
    row = await _family_row(claims.jti)
    await revoke_family(real_session, row.family_id, reason="user")
    await real_session.commit()

    assert await slide_family_idle(real_session, claims) is False
    assert await family_alive(real_session, claims) is False


@pytest.mark.asyncio
async def test_a_revoked_jti_stays_revoked_after_a_slide(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Sliding is activity, not authorization: the revocation check still
    refuses the token, and a slide performed before it does not launder it."""
    claims = await _session_with_family(org_admin)
    assert claims.jti is not None
    user = await real_session.get(User, org_admin.admin_id)
    assert user is not None
    await assert_token_active(real_session, claims, user)

    await revoke_jti(real_session, claims.jti)
    await real_session.commit()

    await slide_family_idle(real_session, claims)
    with pytest.raises(TokenRevokedError, match="token revoked"):
        await assert_token_active(real_session, claims, user)


@pytest.mark.asyncio
async def test_a_token_with_no_family_slides_nothing(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A CLI bearer token has no family to slide. The helper must answer that
    with "nothing moved", not by inventing a row or raising."""
    _, claims = encode_session_token(
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_team_id=org_admin.org_id,
        platform_role=None,
    )
    assert await slide_family_idle(real_session, claims) is False


# ---------------------------------------------------------------------------
# A connection held open is not activity; what the person does is
# ---------------------------------------------------------------------------


async def _session_token_with_family(org: OrgWithAdmin) -> tuple[str, SessionClaims]:
    """``_session_with_family``, keeping the token a request presents."""
    token, claims = encode_session_token(
        user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id, platform_role=None
    )
    async with AsyncSessionLocal() as s:
        await register_token(s, claims=claims, token_type=TokenType.SESSION)
        await mint_refresh_token(s, user_id=org.admin_id, access_jti=claims.jti)
        await s.commit()
    return token, claims


async def _entitlements(user_id: object) -> EntitlementRef:
    async with AsyncSessionLocal() as s:
        user = await s.get(User, user_id)
        assert user is not None
        return EntitlementRef(await load_entitlements(s, user, org_id=user.home_org_team_id))


class _Socket:
    """The socket's far end: what the server sends it is dropped."""

    async def send_text(self, _text: str) -> None:
        return None


async def _socket_for(org: OrgWithAdmin, claims: SessionClaims) -> SocketSession:
    async with AsyncSessionLocal() as s:
        user = await s.get(User, org.admin_id)
        assert user is not None
        ref = EntitlementRef(await load_entitlements(s, user, org_id=user.home_org_team_id))
    return SocketSession(
        websocket=cast(WebSocket, _Socket()),
        user=user,
        claims=claims,
        peer_id="peer-activity",
        runtime=cast(RealtimeRuntime, None),
        registry=cast(DocRegistry, None),
        ref=ref,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested", [pytest.param(False, id="keepalives-only"), pytest.param(True, id="a-request")]
)
async def test_an_open_stream_does_not_hold_the_access_idle_window_open(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    requested: bool,
) -> None:
    """A sixty-minute idle window and a stream ticking all the way through it.
    With nothing but the stream, minute sixty-one is over: the keepalives read
    the window and never move it. One real request at minute fifty is what
    carries the session past the minute the mint set. The access token lives
    two hours here, so its own expiry is not what ends it."""
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 3600)
    monkeypatch.setattr(settings, "auth_token_ttl_seconds", 7200)
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        token, claims = await _session_token_with_family(org_admin)
        ref = await _entitlements(org_admin.admin_id)
        for minute in (10, 20, 30, 40, 50):
            frozen.move_to(f"2026-06-01 12:{minute}:00")
            assert await events_route._recheck(claims, org_admin.admin_id, ref) is True
        if requested:
            me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert me.status_code == 200

        frozen.move_to("2026-06-01 13:01:00")
        assert await events_route._recheck(claims, org_admin.admin_id, ref) is requested


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acted", [pytest.param(False, id="pings-only"), pytest.param(True, id="the-person-typed")]
)
async def test_an_open_socket_does_not_hold_the_session_family_open(
    org_admin: OrgWithAdmin, acted: bool
) -> None:
    """The refresh family's seven-day idle window, with a socket held through
    all of it. A socket that only pings and ticks ends with the window — an
    unattended tab does not keep its session to the absolute cap — while one
    whose person moved a caret every day is still signed in past it."""
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        _, claims = await _session_token_with_family(org_admin)
        socket = await _socket_for(org_admin, claims)
        start = datetime(2026, 6, 1, 12, tzinfo=UTC)
        idle = timedelta(seconds=settings.auth_refresh_idle_seconds)
        day = timedelta(days=1)
        moment = start
        while moment + day < start + idle:
            moment += day
            frozen.move_to(moment)
            await socket._dispatch(PingFrame())
            if acted:
                await socket._dispatch(
                    PresenceCursorFrame(
                        channel="chat:activity", cursor=PresenceCursor(offset=0, anchor=0)
                    )
                )
            assert await socket._recheck() is True

        frozen.move_to(start + idle + timedelta(minutes=1))
        assert await socket._recheck() is acted
