"""Burst bounds on the two routes an ordinary member's daemon WRITES to.

Both append rows to tables shared by every tenant, and both were reachable at
whatever rate a scripted loop could manage: the agent-audit ingest lets a member
bury a genuine denial entry under noise the admin's view and chain verification
must scan, and the connection-inventory report is partitioned by a key the
client mints itself, so a fresh key each time only ever appends.

Both are the daemon's flushes, so they live in the ``machine`` class: keyed by
the machine credential when the daemon sends one, by the member otherwise. The
cases drive the class on a clock only the test moves — a burst timed by the
wall clock refills as fast as a slow loop drains it, and a hunt for a 429 the
refill rate is quietly paying for is a test that can only fail slowly.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from httpx import AsyncClient, Response
from tests.conftest import ManualClock, OrgWithAdmin, login, make_member, make_org_enterprise

pytestmark = pytest.mark.asyncio

_AGENT_INGEST = "/api/v1/org/audit-events/agent"
_INVENTORY = "/api/v1/me/connection-inventory"

#: A ceiling small enough to exhaust in a handful of requests, so proving the
#: refusal costs four round trips rather than the shipped hundreds. The shipped
#: numbers are proven against the limiter itself.
_PINNED = 3

#: A hard per-request deadline. Nothing here talks to the network, so a request
#: that has not answered within this is wedged; failing the case beats parking a
#: worker on it for an hour.
_REQUEST_DEADLINE_S = 30.0

Send = Callable[[], Awaitable[Response]]


@pytest.fixture
def pinned_machine_class(pin_clock: ManualClock, monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    monkeypatch.setattr(settings, "rate_limit_machine_per_minute", _PINNED)
    monkeypatch.setattr(settings, "rate_limit_machine_burst", _PINNED)
    return pin_clock


async def _member_of(org_id: UUID) -> tuple[str, str]:
    async with AsyncSessionLocal() as session:
        member, password = await make_member(session, org_id=org_id, verified=True)
        return member.email, password or ""


def _post_agent_batch(client: AsyncClient) -> Send:
    def _send() -> Awaitable[Response]:
        return client.post(
            _AGENT_INGEST,
            json={
                "events": [
                    {
                        "action": "agent.session_started",
                        "session_id": f"sess-{secrets.token_hex(4)}",
                        "occurred_at": "2026-07-19T10:00:00+00:00",
                        "target": "scratch-project",
                        "detail": {},
                    }
                ]
            },
        )

    return _send


def _put_inventory_report(client: AsyncClient) -> Send:
    def _send() -> Awaitable[Response]:
        # A fresh workspace key every time is exactly the abuse shape: the
        # server-side replace is scoped to the key, so a new one deletes nothing
        # and the report is pure append.
        return client.put(_INVENTORY, json={"workspace": secrets.token_hex(8), "connections": []})

    return _send


async def _statuses(send: Send, count: int) -> list[int]:
    out: list[int] = []
    for _ in range(count):
        resp = await asyncio.wait_for(send(), _REQUEST_DEADLINE_S)
        out.append(resp.status_code)
    return out


async def test_a_member_cannot_flood_the_agent_audit_ingest(
    client: AsyncClient, org_admin: OrgWithAdmin, pinned_machine_class: ManualClock
) -> None:
    await make_org_enterprise(org_admin.org_id)  # the audit log is Enterprise-gated
    email, password = await _member_of(org_admin.org_id)
    await login(client, email, password)
    send = _post_agent_batch(client)

    assert await _statuses(send, _PINNED) == [201] * _PINNED
    refused = await asyncio.wait_for(send(), _REQUEST_DEADLINE_S)
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "rate_limited"
    # The client spools a refused batch and retries it, so it has to be told how
    # long to wait — a trail that keeps bouncing is an audit gap.
    assert int(refused.headers["retry-after"]) >= 1


async def test_a_flood_is_paced_at_the_refill_rate_it_earns(
    client: AsyncClient, org_admin: OrgWithAdmin, pinned_machine_class: ManualClock
) -> None:
    """Past the ceiling the caller is paced, not banned — and paced at exactly
    the rate the clock has moved. One refill buys one batch and no more."""
    await make_org_enterprise(org_admin.org_id)
    email, password = await _member_of(org_admin.org_id)
    await login(client, email, password)
    send = _post_agent_batch(client)
    refill_seconds = 60.0 / _PINNED

    await _statuses(send, _PINNED)
    assert await _statuses(send, 1) == [429]
    pinned_machine_class.advance(refill_seconds * 0.9)
    assert await _statuses(send, 1) == [429]
    pinned_machine_class.advance(refill_seconds * 0.1)
    assert await _statuses(send, 2) == [201, 429]


async def test_a_member_cannot_flood_the_connection_inventory_with_fresh_keys(
    client: AsyncClient, org_admin: OrgWithAdmin, pinned_machine_class: ManualClock
) -> None:
    email, password = await _member_of(org_admin.org_id)
    await login(client, email, password)
    send = _put_inventory_report(client)

    assert await _statuses(send, _PINNED) == [204] * _PINNED
    assert await _statuses(send, 1) == [429]
    pinned_machine_class.advance(60.0 / _PINNED)
    assert await _statuses(send, 2) == [204, 429]


async def test_the_reporting_cadence_is_not_throttled(
    client: AsyncClient, org_admin: OrgWithAdmin, pin_clock: ManualClock
) -> None:
    """A guard against sizing the bound so tightly it breaks the product: a
    client uploads one workspace every fifteen minutes, so a handful of reports
    in a row sail through on the shipped ceiling with the clock stopped."""
    del pin_clock
    email, password = await _member_of(org_admin.org_id)
    await login(client, email, password)
    send = _put_inventory_report(client)
    assert await _statuses(send, 10) == [204] * 10
