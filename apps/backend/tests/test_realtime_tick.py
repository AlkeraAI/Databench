"""A socket's periodic tick and the database: a busy database skips a beat
rather than closing every socket at once, a socket whose standing cannot be
checked for long fails closed, and sockets reconnected together do not all
expire together."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.schemas.realtime import WS_SUBPROTOCOL, WS_TICKET_SUBPROTOCOL_PREFIX
from backend.services.realtime.tick import (
    BEAT_SKIP_LIMIT,
    BeatFaults,
    session_deadline_seconds,
)
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from tests.conftest import OrgWithAdmin, app_client, login


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("exc", "tolerated"),
    [
        pytest.param(_PgError("55P03"), True, id="a-held-lock"),
        pytest.param(_PgError("57014"), True, id="a-statement-timeout"),
        pytest.param(_PgError("57P03"), True, id="the-database-is-starting"),
        pytest.param(_PgError("08006"), True, id="a-dropped-connection"),
        pytest.param(PoolTimeoutError("pool"), True, id="a-full-pool"),
        pytest.param(_PgError("23505"), False, id="a-constraint-is-a-bug"),
        pytest.param(ValueError("bad"), False, id="a-defect"),
    ],
)
def test_contention_skips_a_beat_and_anything_else_closes(
    exc: BaseException, tolerated: bool
) -> None:
    assert BeatFaults().tolerate(exc) is tolerated


def test_contention_that_outlasts_its_beats_closes_and_a_landed_beat_resets() -> None:
    faults = BeatFaults()
    busy = _PgError("55P03")
    assert all(faults.tolerate(busy) for _ in range(BEAT_SKIP_LIMIT))
    assert faults.tolerate(busy) is False, "a socket nobody could recheck stayed open"

    faults.landed()
    assert faults.tolerate(busy) is True


@pytest.mark.parametrize(
    ("draw", "expected"),
    [
        pytest.param(0.0, 3000.0, id="at-most-the-ceiling"),
        pytest.param(0.5, 2850.0, id="half-way"),
        pytest.param(1.0, 2700.0, id="at-least-ninety-percent"),
    ],
)
def test_the_session_deadline_is_spread_below_its_ceiling(draw: float, expected: float) -> None:
    assert session_deadline_seconds(3000.0, lambda: draw) == pytest.approx(expected)


def test_two_sockets_admitted_together_do_not_share_a_deadline() -> None:
    deadlines = {session_deadline_seconds(3000.0) for _ in range(50)}
    assert len(deadlines) > 1
    assert all(2700.0 <= d <= 3000.0 for d in deadlines)


async def _frame(ws: Any, *, within: float) -> dict[str, Any]:
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=within)))


async def test_a_busy_database_on_the_recheck_does_not_close_the_socket(
    uvicorn_server: str, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recheck reads the user's row every tick. While another transaction
    holds the users table, that read waits out ``lock_timeout`` and fails with
    ``55P03``; the socket skips the beat and is still open once the lock goes,
    where it used to close with a server reset and send every tab reconnecting
    into the same queue."""
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 1)
    client = app_client()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    ticket = (await client.post("/api/v1/ws/tickets")).json()["ticket"]
    async with websockets.connect(
        f"ws://{uvicorn_server}/api/v1/ws",
        subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
    ) as ws:
        assert (await _frame(ws, within=5.0))["t"] == "welcome"
        async with AsyncSessionLocal() as holder:
            await holder.execute(text("LOCK TABLE users IN ACCESS EXCLUSIVE MODE"))
            # A tick starts within a second and waits out the five second
            # lock_timeout on the held table: at least one beat fails on it.
            await asyncio.sleep(7.5)
            await holder.rollback()
        await asyncio.sleep(2.0)  # a tick lands once the lock is gone
        await ws.send(json.dumps({"t": "ping"}))
        assert (await _frame(ws, within=5.0))["t"] == "pong", "the socket closed on a busy recheck"
    await client.aclose()
