"""Presence: join / heartbeat / leave, the TTL roster, the sweeper, the
ephemeral lane's cap — at the service level with explicit clocks driven across
the TTL boundary — and one socket test proving a closed socket leaves every
channel it joined."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import websockets
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.schemas.realtime import WS_SUBPROTOCOL, WS_TICKET_SUBPROTOCOL_PREFIX
from backend.services.realtime import presence
from backend.services.realtime.channels import Channel, ChannelGrant
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_declarations import declare_chat
from tests.conftest import OrgWithAdmin, app_client, login, make_member

T0 = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _ttl() -> timedelta:
    return timedelta(seconds=settings.realtime_presence_ttl_seconds)


def _channel() -> Channel:
    return Channel(doc_type="chat", doc_id=f"sess-{uuid4().hex[:12]}")


def _declared_channel(org: OrgWithAdmin) -> Channel:
    """A chat declared for ``org`` — what a socket needs before it may
    subscribe. The presence rows themselves need no declaration; only the
    subscribe that puts a real socket on the channel does."""
    return Channel(
        doc_type="chat",
        doc_id=declare_chat(org.org_id, owner_user_id=org.admin_id).split(":", 2)[2],
    )


def _grant(channel: Channel, org_id: Any, *, team_id: Any = None) -> ChannelGrant:
    return ChannelGrant(
        channel=channel, org_id=org_id, can_write=True, owner_user_id=None, team_id=team_id
    )


async def _peers(channel: Channel, org_id: Any, now: datetime) -> list[str]:
    async with AsyncSessionLocal() as db:
        return [
            p.peer_id for p in await presence.roster(db, channel=channel, org_id=org_id, now=now)
        ]


async def test_join_heartbeat_leave_round_trip(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    channel = _channel()
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:a",
        user_id=org_admin.admin_id,
        org_id=org_admin.org_id,
        now=T0,
    )
    await real_session.commit()
    roster = await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=T0)
    assert [(p.peer_id, p.user_id) for p in roster] == [("p:a", str(org_admin.admin_id))]
    assert roster[0].last_seen_at == T0

    later = T0 + timedelta(seconds=10)
    assert await presence.heartbeat(real_session, channel=channel, peer_id="p:a", now=later)
    await real_session.commit()
    assert (
        await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=later)
    )[0].last_seen_at == later

    await presence.leave(real_session, channel=channel, peer_id="p:a")
    await real_session.commit()
    assert (
        await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=later)
        == []
    )
    assert await presence.count_for_channel(real_session, channel, org_admin.org_id) == 0


async def test_a_re_join_refreshes_rather_than_duplicates(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    channel = _channel()
    for stamp in (T0, T0 + timedelta(seconds=5)):
        await presence.join(
            real_session,
            channel=channel,
            peer_id="p:a",
            user_id=org_admin.admin_id,
            org_id=org_admin.org_id,
            now=stamp,
        )
        await real_session.commit()
    assert await presence.count_for_channel(real_session, channel, org_admin.org_id) == 1
    roster = await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=T0)
    assert roster[0].last_seen_at == T0 + timedelta(seconds=5)


async def test_the_roster_hides_peers_past_the_ttl_and_orders_by_join(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    channel = _channel()
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:old",
        user_id=org_admin.admin_id,
        org_id=org_admin.org_id,
        now=T0,
    )
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:new",
        user_id=member.id,
        org_id=org_admin.org_id,
        now=T0 + timedelta(seconds=30),
    )
    await real_session.commit()
    just_inside = T0 + _ttl() - timedelta(seconds=1)
    just_past = T0 + _ttl() + timedelta(seconds=1)
    org = org_admin.org_id
    assert await _peers(channel, org, T0 + timedelta(seconds=30)) == ["p:old", "p:new"]
    assert await _peers(channel, org, just_inside) == ["p:old", "p:new"]
    assert await _peers(channel, org, just_past) == ["p:new"], "the quiet peer drops out at the TTL"
    assert (
        await _peers(channel, org, T0 + timedelta(seconds=30) + _ttl() + timedelta(seconds=1)) == []
    )


async def test_the_roster_never_names_another_orgs_peer_on_the_same_channel(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_support: OrgWithAdmin
) -> None:
    """A channel name carries a client-chosen document id, so two tenants can
    sit on the same one. The roster a subscribe is answered with is the caller's
    org's alone: neither the other tenant's peer id nor its user id appears in
    it, and the row count each org sees is its own."""
    channel = _channel()
    for org, peer in ((org_admin, "p:mine"), (platform_support, "p:theirs")):
        await presence.join(
            real_session,
            channel=channel,
            peer_id=peer,
            user_id=org.admin_id,
            org_id=org.org_id,
            now=T0,
        )
    await real_session.commit()

    mine = await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=T0)
    theirs = await presence.roster(
        real_session, channel=channel, org_id=platform_support.org_id, now=T0
    )
    assert [(p.peer_id, p.user_id) for p in mine] == [("p:mine", str(org_admin.admin_id))]
    assert [(p.peer_id, p.user_id) for p in theirs] == [
        ("p:theirs", str(platform_support.admin_id))
    ]
    assert await presence.count_for_channel(real_session, channel, org_admin.org_id) == 1
    assert await presence.count_for_channel(real_session, channel, platform_support.org_id) == 1


async def test_a_heartbeat_keeps_a_peer_inside_the_ttl(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    channel = _channel()
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:a",
        user_id=org_admin.admin_id,
        org_id=org_admin.org_id,
        now=T0,
    )
    beat = T0 + _ttl() - timedelta(seconds=5)
    await presence.heartbeat(real_session, channel=channel, peer_id="p:a", now=beat)
    await real_session.commit()
    assert await _peers(channel, org_admin.org_id, T0 + _ttl() + timedelta(seconds=1)) == ["p:a"]
    assert await _peers(channel, org_admin.org_id, beat + _ttl() + timedelta(seconds=1)) == []


async def test_sweep_deletes_only_rows_past_the_ttl(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    stale, live = _channel(), _channel()
    await presence.join(
        real_session,
        channel=stale,
        peer_id="p:s",
        user_id=org_admin.admin_id,
        org_id=org_admin.org_id,
        now=T0,
    )
    await presence.join(
        real_session,
        channel=live,
        peer_id="p:l",
        user_id=org_admin.admin_id,
        org_id=org_admin.org_id,
        now=T0 + timedelta(seconds=40),
    )
    await real_session.commit()
    swept = await presence.sweep_expired(real_session, now=T0 + _ttl() + timedelta(seconds=1))
    await real_session.commit()
    assert swept >= 1
    assert await presence.count_for_channel(real_session, stale, org_admin.org_id) == 0
    assert await presence.count_for_channel(real_session, live, org_admin.org_id) == 1


async def test_a_heartbeat_after_a_sweep_reports_the_peer_gone(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    channel = _channel()
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:a",
        user_id=org_admin.admin_id,
        org_id=org_admin.org_id,
        now=T0,
    )
    await real_session.commit()
    await presence.sweep_expired(real_session, now=T0 + _ttl() + timedelta(seconds=1))
    await real_session.commit()
    assert await presence.heartbeat(real_session, channel=channel, peer_id="p:a") is False


async def test_ephemeral_events_carry_the_channel_and_the_grant_team(
    org_admin: OrgWithAdmin,
) -> None:
    channel = _channel()
    team = uuid4()
    event = presence.ephemeral_event(
        grant=_grant(channel, org_admin.org_id, team_id=team),
        type=presence.PRESENCE_EVENT_TYPE,
        body={"event": "join", "peer": {"peer_id": "p:a"}},
    )
    assert event.lane == "ephemeral"
    assert event.channel == channel.key == event.entity_id
    assert event.org_id == org_admin.org_id
    assert event.visibility == "org"
    assert event.payload["team_id"] == str(team)
    assert event.team_id == team
    assert event.id is None
    no_team = presence.ephemeral_event(
        grant=_grant(channel, org_admin.org_id), type=presence.CHUNK_EVENT_TYPE, body={}
    )
    assert "team_id" not in no_team.payload


async def test_an_ephemeral_payload_over_the_cap_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "realtime_ephemeral_max_bytes", 256)
    grant = _grant(_channel(), org_admin.org_id)
    small = presence.ephemeral_event(grant=grant, type=presence.CHUNK_EVENT_TYPE, body={"x": "y"})
    await presence.publish_ephemeral(real_session, small)
    big = presence.ephemeral_event(
        grant=grant, type=presence.CHUNK_EVENT_TYPE, body={"x": "y" * 300}
    )
    with pytest.raises(presence.EphemeralTooLargeError):
        await presence.publish_ephemeral(real_session, big)
    await real_session.rollback()


async def test_the_roster_frame_a_subscribe_answers_with_carries_only_the_callers_org(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
) -> None:
    """The leak as a client would see it: two orgs sit on one channel key, and
    the roster frame each socket is answered with names its own peer only —
    never the other tenant's peer id or user id."""
    channel = _declared_channel(org_admin)
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:squatter",
        user_id=platform_support.admin_id,
        org_id=platform_support.org_id,
    )
    await real_session.commit()

    mine = app_client()
    await login(mine, org_admin.admin_email, org_admin.admin_password)
    ticket = (await mine.post("/api/v1/ws/tickets")).json()["ticket"]
    async with websockets.connect(
        f"ws://{uvicorn_server}/api/v1/ws",
        subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
    ) as ws:
        assert json.loads(await ws.recv())["t"] == "welcome"
        await ws.send(json.dumps({"t": "subscribe", "channel": channel.key}))
        assert json.loads(await ws.recv())["t"] == "subscribed"
        roster_frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert roster_frame["t"] == "presence" and roster_frame["event"] == "roster"
        assert roster_frame["peers"] == [], "the other tenant's peer is not on this roster"
        await ws.send(json.dumps({"t": "presence.join", "channel": channel.key}))
        joined = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert joined["t"] == "presence" and joined["event"] == "join"
        assert [p["user_id"] for p in joined["peers"]] == [str(org_admin.admin_id)]
    await mine.aclose()


async def test_a_closed_socket_leaves_every_channel_it_joined(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ticket = (await client.post("/api/v1/ws/tickets")).json()["ticket"]
    a, b = _declared_channel(org_admin), _declared_channel(org_admin)
    async with websockets.connect(
        f"ws://{uvicorn_server}/api/v1/ws",
        subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
    ) as ws:
        assert json.loads(await ws.recv())["t"] == "welcome"
        for channel in (a, b):
            await ws.send(json.dumps({"t": "subscribe", "channel": channel.key}))
            assert json.loads(await ws.recv())["t"] == "subscribed"
            assert json.loads(await ws.recv())["t"] == "presence"  # the roster
            await ws.send(json.dumps({"t": "presence.join", "channel": channel.key}))
            joined = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert joined["t"] == "presence" and joined["event"] == "join"
        async with AsyncSessionLocal() as db:
            assert await presence.count_for_channel(db, a, org_admin.org_id) == 1
            assert await presence.count_for_channel(db, b, org_admin.org_id) == 1
    # The socket is closed: both rows are gone within a moment.
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        async with AsyncSessionLocal() as db:
            left = await presence.count_for_channel(
                db, a, org_admin.org_id
            ) + await presence.count_for_channel(db, b, org_admin.org_id)
        if left == 0:
            break
        assert asyncio.get_running_loop().time() < deadline, "presence rows outlived the socket"
        await asyncio.sleep(0.05)


async def _frame(ws: Any, *, within: float = 5.0) -> dict[str, Any]:
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=within)))


async def test_a_peer_the_sweep_removes_is_announced_as_a_leave(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
) -> None:
    """A process that died without a graceful leave left its peer on every
    other open tab's roster, caret and all, until they reloaded: the sweep
    deleted the row and told nobody. Whatever removes presence announces it,
    and only to the org whose row it was."""
    channel = _declared_channel(org_admin)
    now = datetime.now(UTC)
    # Heard from just inside the TTL: on the roster a reconnecting tab is sent.
    lapsing = now - _ttl() + timedelta(seconds=5)
    for peer_id, user_id, org_id in (
        ("p:ghost", org_admin.admin_id, org_admin.org_id),
        ("p:other-tenant", platform_support.admin_id, platform_support.org_id),
    ):
        await presence.join(
            real_session,
            channel=channel,
            peer_id=peer_id,
            user_id=user_id,
            org_id=org_id,
            now=lapsing,
        )
    await real_session.commit()

    mine = app_client()
    await login(mine, org_admin.admin_email, org_admin.admin_password)
    ticket = (await mine.post("/api/v1/ws/tickets")).json()["ticket"]
    async with websockets.connect(
        f"ws://{uvicorn_server}/api/v1/ws",
        subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
    ) as ws:
        assert (await _frame(ws))["t"] == "welcome"
        await ws.send(json.dumps({"t": "subscribe", "channel": channel.key}))
        assert (await _frame(ws))["t"] == "subscribed"
        roster_frame = await _frame(ws)
        assert [p["peer_id"] for p in roster_frame["peers"]] == ["p:ghost"]

        async with AsyncSessionLocal() as db:
            swept = await presence.sweep_expired(db, now=now + timedelta(seconds=10))
            await db.commit()
        assert swept >= 2

        leave = await _frame(ws)
        assert leave["t"] == "presence" and leave["event"] == "leave"
        assert [p["peer_id"] for p in leave["peers"]] == ["p:ghost"]
        assert leave["channel"] == channel.key
        # The other tenant's row went too, and its leave never reached this org.
        with pytest.raises(TimeoutError):
            await _frame(ws, within=1.0)
    await mine.aclose()


# ---------------------------------------------------------------------------
# Naming the people on a roster
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "last", "email", "expected"),
    [
        pytest.param("Dana", "Okafor", "dana@x.test", "Dana Okafor", id="both-names"),
        pytest.param("Dana", "", "dana@x.test", "Dana", id="first-name-only"),
        pytest.param("", "Okafor", "dana@x.test", "Okafor", id="last-name-only"),
        pytest.param("", "", "dana.o@x.test", "dana.o", id="profile-not-completed-yet"),
        pytest.param(None, None, None, "", id="no-user-row-at-all"),
        pytest.param("  ", "  ", "dana@x.test", "dana", id="whitespace-is-not-a-name"),
        pytest.param("Dana", "Okafor", None, "Dana Okafor", id="no-email-needed"),
    ],
)
def test_the_name_a_roster_shows(
    first: str | None, last: str | None, email: str | None, expected: str
) -> None:
    """Signup takes an email and a password; the name is collected afterwards,
    so a chat opened in between must still say who is in it. What it must never
    do is fall back on the user id — a sliced UUID identifies nobody and puts a
    raw identifier in front of everyone else in the chat."""
    assert presence.display_name_of(first, last, email) == expected


async def test_the_roster_names_the_people_on_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The name is joined from ``users`` rather than stamped onto the presence
    row, so a person who renames themselves does not have to re-join every chat
    they are reading for the roster to catch up."""
    from alkera_core.models import User

    channel = _channel()
    member, _ = await make_member(
        real_session,
        org_id=org_admin.org_id,
        verified=True,
        first_name="Dana",
        last_name="Okafor",
    )
    await real_session.commit()
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:a",
        user_id=member.id,
        org_id=org_admin.org_id,
        now=T0,
    )
    await real_session.commit()
    roster = await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=T0)
    assert [p.display_name for p in roster] == ["Dana Okafor"]

    renamed = await real_session.get(User, member.id)
    assert renamed is not None
    renamed.first_name = "Danielle"
    await real_session.commit()
    roster = await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=T0)
    assert [p.display_name for p in roster] == ["Danielle Okafor"], "no re-join required"


async def test_the_roster_carries_the_address_a_surface_colours_a_person_by(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A colleague's colour — their face in a presence stack, their caret in a
    shared draft — is a hash of WHO THEY ARE, and the only stable name for that
    which every deployment agrees on is the login address. The roster therefore
    resolves it alongside the display name; without it a surface can only key
    off the user id, which is per deployment, or off the peer id, which changes
    every tab."""
    channel = _channel()
    member, _ = await make_member(
        real_session,
        org_id=org_admin.org_id,
        verified=True,
        first_name="Dana",
        last_name="Okafor",
    )
    await real_session.commit()
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:a",
        user_id=member.id,
        org_id=org_admin.org_id,
        now=T0,
    )
    await real_session.commit()
    roster = await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=T0)
    assert [p.email for p in roster] == [member.email]
    # And it is the person's own address, not something derived from the id.
    assert member.email not in {p.user_id for p in roster}


async def test_a_reader_who_has_not_named_themselves_is_still_on_the_roster(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Signup takes an email and a password; the name comes later. Until then
    the roster still has to carry them — the count of who is reading is what a
    second person joining a chat is told, and it must not go down because
    somebody skipped a profile page."""
    channel = _channel()
    nameless, _ = await make_member(
        real_session, org_id=org_admin.org_id, verified=True, first_name="", last_name=""
    )
    await real_session.commit()
    await presence.join(
        real_session,
        channel=channel,
        peer_id="p:new",
        user_id=nameless.id,
        org_id=org_admin.org_id,
        now=T0,
    )
    await real_session.commit()
    roster = await presence.roster(real_session, channel=channel, org_id=org_admin.org_id, now=T0)
    assert [(p.peer_id, p.display_name) for p in roster] == [
        ("p:new", nameless.email.split("@", 1)[0])
    ]
