"""Socket tickets: minted over an authenticated request, single-use across
sessions, refused at the handshake when expired, replayed, or bound to a
session that ended — and never read from a query string.

The mint route runs in process; the burn is exercised directly; the
handshake cases run against a real server on the test loop with a real
``websockets`` client offering the ticket in ``Sec-WebSocket-Protocol``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import websockets
from alkera_core.auth import (
    decode_session_token,
    decode_ws_ticket,
    encode_session_token,
    encode_ws_ticket,
    register_token,
    revoke_all_for_user,
)
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import TokenType, WsTicketUse
from alkera_core.schemas.realtime import (
    ENVELOPE_KINDS,
    WS_PATH,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
)
from backend.authz import decide_on_record
from backend.services.realtime import close_codes, tickets
from httpx import AsyncClient
from sqlalchemy import select, update
from tests.conftest import OrgWithAdmin, login, mint_cli_token
from websockets.exceptions import ConnectionClosed

MINT = "/api/v1/ws/tickets"
PROTOCOL = "/api/v1/ws/protocol"
COOKIE = "alkera_session"


@pytest.fixture(autouse=True)
def _reset_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


async def _mint(client: AsyncClient) -> dict[str, Any]:
    resp = await client.post(MINT)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _ws_uri(addr: str, path: str = WS_PATH) -> str:
    return f"ws://{addr}{path}"


#: How long a test waits for one frame or a close from the live server. A
#: five-second guard fired on a Windows shard under load for a handshake the
#: server refuses without any work; twenty seconds is the daemon pipe tests'
#: figure, well above the cost on any healthy host, and the suite-wide 90 s
#: limit still catches a hang.
_FRAME_BUDGET_S = 20.0


async def _close_code(ws: Any) -> int:
    """Read until the server closes; the code it closed with."""
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout=_FRAME_BUDGET_S)
    except ConnectionClosed as exc:
        assert exc.rcvd is not None, "closed without a close frame"
        return int(exc.rcvd.code)


def _subprotocols(ticket: str | None) -> list[str]:
    offered = [WS_SUBPROTOCOL]
    if ticket is not None:
        offered.append(f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}")
    return offered


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


async def test_mint_requires_a_session(client: AsyncClient) -> None:
    assert (await client.post(MINT)).status_code == 401


async def test_mint_returns_a_ticket_bound_to_the_calling_session(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    cookie = client.cookies.get(COOKIE)
    assert cookie is not None
    session = decode_session_token(cookie)
    body = await _mint(client)
    assert set(body) == {"ticket", "expires_in", "path"}
    assert body["expires_in"] == settings.realtime_ws_ticket_ttl_seconds == 30
    assert body["path"] == WS_PATH == "/api/v1/ws"
    ticket = decode_ws_ticket(body["ticket"])
    assert ticket.user_id == org_admin.admin_id
    assert ticket.org_id == org_admin.org_id
    assert ticket.session_jti == session.jti
    assert ticket.session_issued_at == session.issued_at
    assert ticket.session_expires_at == session.expires_at
    assert ticket.expires_at - ticket.issued_at == 30


async def test_mint_works_with_a_bearer_token_too(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    resp = await client.post(MINT, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert decode_ws_ticket(resp.json()["ticket"]).session_jti == decode_session_token(token).jti


async def test_mint_never_reads_a_query_string_credential(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    assert (await client.post(MINT, params={"token": token})).status_code == 401


async def test_mint_is_rate_limited_per_caller(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    for _ in range(30):
        assert (await client.post(MINT)).status_code == 200
    throttled = await client.post(MINT)
    assert throttled.status_code == 429
    assert throttled.headers["retry-after"]
    assert throttled.json()["error"]["code"] == "rate_limited"


async def test_each_mint_is_a_distinct_ticket(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = decode_ws_ticket((await _mint(client))["ticket"])
    b = decode_ws_ticket((await _mint(client))["ticket"])
    assert a.jti != b.jti
    assert a.session_jti == b.session_jti


async def test_a_mint_with_the_agent_headers_carries_the_agent_and_one_without_does_not(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The daemon mints its ticket the way it makes every call — the device
    JWT plus the agent assertion — and the socket it opens speaks as that
    agent. A person's mint (cookie or bare Bearer) carries no agent."""
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    machine = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
    as_agent = await client.post(
        MINT, headers={"Authorization": f"Bearer {token}", **agent_headers(machine)}
    )
    assert as_agent.status_code == 200, as_agent.text
    ticket = decode_ws_ticket(as_agent.json()["ticket"])
    assert ticket.agent_id == machine
    assert ticket.user_id == org_admin.admin_id, "the user behind the agent is the JWT's"

    bare = await client.post(MINT, headers={"Authorization": f"Bearer {token}"})
    assert decode_ws_ticket(bare.json()["ticket"]).agent_id is None

    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert decode_ws_ticket((await _mint(client))["ticket"]).agent_id is None


async def test_a_half_formed_agent_assertion_mints_no_ticket(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The same rule as every REST call: an agent header without its pair is a
    400, never a ticket for "just the user"."""
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {token}", "X-Alkera-Actor": "agent"}
    resp = await client.post(MINT, headers=headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_agent_headers"


# ---------------------------------------------------------------------------
# Burning
# ---------------------------------------------------------------------------


async def test_burn_is_single_use() -> None:
    jti = f"t-{time.time_ns()}"
    assert await tickets.burn(jti) is True
    assert await tickets.burn(jti) is False


async def test_concurrent_burns_of_one_ticket_admit_exactly_one() -> None:
    jti = f"race-{time.time_ns()}"
    results = await asyncio.gather(*(tickets.burn(jti) for _ in range(6)))
    assert results.count(True) == 1
    assert results.count(False) == 5


async def _age(jti: str, days: int) -> None:
    """Backdate a consumed-ticket row, as the clock would."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(WsTicketUse)
            .where(WsTicketUse.jti == jti)
            .values(consumed_at=datetime.now(UTC) - timedelta(days=days))
        )
        await db.commit()


async def _recorded(*jtis: str) -> set[str]:
    async with AsyncSessionLocal() as db:
        return set(
            (
                await db.execute(select(WsTicketUse.jti).where(WsTicketUse.jti.in_(list(jtis))))
            ).scalars()
        )


async def test_a_burn_does_no_housekeeping_and_the_sweep_purges_past_the_window() -> None:
    """Every handshake burns, so a burn must be one insert and nothing else —
    not a delete across the whole table. Rows past the replay window are the
    sweeper's to remove, and the ones it leaves still refuse a replay."""
    stale = f"stale-{time.time_ns()}"
    fresh = f"fresh-{time.time_ns()}"
    assert await tickets.burn(stale)
    assert await tickets.burn(fresh)
    await _age(stale, days=2)

    assert await tickets.burn(f"trigger-{time.time_ns()}")
    assert await _recorded(stale, fresh) == {stale, fresh}, "a burn purges nothing"

    async with AsyncSessionLocal() as db:
        purged = await tickets.purge_consumed(db)
        await db.commit()
    assert purged >= 1
    assert await _recorded(stale, fresh) == {fresh}, "the stale use goes, the fresh one stays"
    assert await tickets.burn(fresh) is False, "single use still holds for what the sweep kept"
    # A purged jti could in principle be burned again — but its ticket expired
    # a day ago, so nothing presents it.
    assert await tickets.burn(stale) is True


async def test_the_realtime_sweeper_is_what_purges_consumed_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, not just the function: the runtime's sweeper reaches the
    purge on its own timer, with no handshake involved."""
    from backend.app_factory import process_app

    fastapi_app = process_app()
    from backend.services.realtime import runtime as realtime_runtime

    stale = f"swept-{time.time_ns()}"
    assert await tickets.burn(stale)
    await _age(stale, days=2)

    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    monkeypatch.setattr(realtime_runtime, "SWEEP_INTERVAL_SECONDS", 0.05)
    runtime = await realtime_runtime.start(fastapi_app, decide=decide_on_record)
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while await _recorded(stale):
            assert asyncio.get_running_loop().time() < deadline, "the sweeper never purged"
            await asyncio.sleep(0.05)
    finally:
        await realtime_runtime.stop(fastapi_app, runtime)


# ---------------------------------------------------------------------------
# The handshake
# ---------------------------------------------------------------------------


async def test_a_header_carried_ticket_opens_a_socket_and_the_server_selects_v1(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ticket = (await _mint(client))["ticket"]
    async with websockets.connect(
        _ws_uri(uvicorn_server), subprotocols=_subprotocols(ticket)
    ) as ws:
        assert ws.subprotocol == WS_SUBPROTOCOL
        welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=_FRAME_BUDGET_S))
        assert welcome["t"] == "welcome"
        assert welcome["peer_id"].startswith("p:")
        assert welcome["instance"]
        assert welcome["server_time"]


async def test_the_same_ticket_cannot_open_a_second_socket(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ticket = (await _mint(client))["ticket"]
    async with websockets.connect(
        _ws_uri(uvicorn_server), subprotocols=_subprotocols(ticket)
    ) as first:
        assert json.loads(await first.recv())["t"] == "welcome"
        async with websockets.connect(
            _ws_uri(uvicorn_server), subprotocols=_subprotocols(ticket)
        ) as replay:
            assert await _close_code(replay) == close_codes.UNAUTHORIZED
        # The first socket is unaffected by the refused replay.
        await first.send(json.dumps({"t": "ping"}))
        assert (
            json.loads(await asyncio.wait_for(first.recv(), timeout=_FRAME_BUDGET_S))["t"] == "pong"
        )


@pytest.mark.parametrize(
    "carrier",
    [
        pytest.param("ticket-query", id="ticket-in-the-query-string"),
        pytest.param("token-query", id="session-token-in-the-query-string"),
        pytest.param("none", id="no-credential-at-all"),
        pytest.param("cookie", id="session-cookie-is-not-a-ticket"),
        pytest.param("bearer", id="bearer-token-is-not-a-ticket"),
    ],
)
async def test_only_a_subprotocol_ticket_authenticates_a_socket(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin, carrier: str
) -> None:
    """A credential in a URL lands in every access log on the path; a cookie
    or bearer would make the socket a cross-site hijacking target. The ticket
    in the ``Sec-WebSocket-Protocol`` header is the one credential accepted."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ticket = (await _mint(client))["ticket"]
    cookie = client.cookies.get(COOKIE)
    assert cookie is not None
    bearer = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    path = WS_PATH
    headers: dict[str, str] = {}
    if carrier == "ticket-query":
        path = f"{WS_PATH}?ticket={ticket}"
    elif carrier == "token-query":
        path = f"{WS_PATH}?token={cookie}"
    elif carrier == "cookie":
        headers["Cookie"] = f"{COOKIE}={cookie}"
    elif carrier == "bearer":
        headers["Authorization"] = f"Bearer {bearer}"
    async with websockets.connect(
        _ws_uri(uvicorn_server, path), subprotocols=[WS_SUBPROTOCOL], additional_headers=headers
    ) as ws:
        assert await _close_code(ws) == close_codes.UNAUTHORIZED
    # And the ticket itself is still unused: nothing above consumed it.
    assert await tickets.burn(decode_ws_ticket(ticket).jti) is True


async def test_a_socket_must_offer_the_v1_subprotocol(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ticket = (await _mint(client))["ticket"]
    async with websockets.connect(
        _ws_uri(uvicorn_server), subprotocols=[f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"]
    ) as ws:
        assert await _close_code(ws) == close_codes.UNAUTHORIZED


async def test_two_tickets_in_one_handshake_are_refused(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = (await _mint(client))["ticket"]
    b = (await _mint(client))["ticket"]
    async with websockets.connect(
        _ws_uri(uvicorn_server),
        subprotocols=[
            WS_SUBPROTOCOL,
            f"{WS_TICKET_SUBPROTOCOL_PREFIX}{a}",
            f"{WS_TICKET_SUBPROTOCOL_PREFIX}{b}",
        ],
    ) as ws:
        assert await _close_code(ws) == close_codes.UNAUTHORIZED


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("expired", id="expired-ticket"),
        pytest.param("garbage", id="not-a-jwt"),
        pytest.param("session-jwt", id="a-session-token-in-the-ticket-slot"),
        pytest.param("tampered", id="tampered-signature"),
    ],
)
async def test_a_bad_ticket_is_refused_at_the_handshake(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin, shape: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    cookie = client.cookies.get(COOKIE)
    assert cookie is not None
    session = decode_session_token(cookie)
    if shape == "expired":
        raw = encode_ws_ticket(
            user_id=org_admin.admin_id,
            org_id=org_admin.org_id,
            session=session,
            now=int(time.time()) - 120,
        )
    elif shape == "garbage":
        raw = "not.a.ticket"
    elif shape == "session-jwt":
        raw = cookie
    else:
        good = (await _mint(client))["ticket"]
        head, body, sig = good.split(".")
        raw = f"{head}.{body}.{sig[:-2]}zz"
    async with websockets.connect(_ws_uri(uvicorn_server), subprotocols=_subprotocols(raw)) as ws:
        assert await _close_code(ws) == close_codes.UNAUTHORIZED


async def test_a_ticket_minted_from_a_revoked_session_is_refused(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The ticket is bound to its session: a logout everywhere between mint
    and connect must close the door even though the ticket itself is valid."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ticket = (await _mint(client))["ticket"]
    async with AsyncSessionLocal() as db:
        await revoke_all_for_user(db, org_admin.admin_id)
        await db.commit()
    async with websockets.connect(
        _ws_uri(uvicorn_server), subprotocols=_subprotocols(ticket)
    ) as ws:
        assert await _close_code(ws) == close_codes.UNAUTHORIZED


async def test_a_ticket_whose_session_expired_is_refused(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    """A ticket outlives nothing: a session that expired one second after
    minting the ticket cannot open a socket with it."""
    issued = int(time.time()) - settings.auth_token_ttl_seconds - 5
    _token, claims = encode_session_token(
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_team_id=org_admin.org_id,
        platform_role=None,
        now=issued,
    )
    async with AsyncSessionLocal() as db:
        await register_token(db, claims=claims, token_type=TokenType.SESSION)
        await db.commit()
    assert claims.expires_at < time.time()
    raw = encode_ws_ticket(user_id=org_admin.admin_id, org_id=org_admin.org_id, session=claims)
    async with websockets.connect(_ws_uri(uvicorn_server), subprotocols=_subprotocols(raw)) as ws:
        assert await _close_code(ws) == close_codes.UNAUTHORIZED


# ---------------------------------------------------------------------------
# The protocol descriptor
# ---------------------------------------------------------------------------


async def test_the_protocol_descriptor_pins_the_vocabulary(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    assert (await client.get(PROTOCOL)).status_code == 401
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(PROTOCOL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == "2.1.0"
    assert body["envelope_kinds"] == list(ENVELOPE_KINDS)
    assert body["doc_types"] == [
        "chat",
        "artifact",
        "chat_draft",
        "file",
        "notebook",
        "chat_workspace",
    ]
    assert body["op_intents"] == ["append", "set_meta", "user_message", "chunk", "set_fields"]
    assert body["close_codes"] == close_codes.CLOSE_CODES
    assert body["path"] == "/api/v1/ws"
    assert body["subprotocol"] == "alkera-v1"
    assert body["ticket_subprotocol_prefix"] == "alkera-ticket."
    assert body["channel_pattern"] == (
        r"^doc:(chat_workspace|chat_draft|artifact|notebook|chat|file):([A-Za-z0-9._:-]{1,255})$"
    )
