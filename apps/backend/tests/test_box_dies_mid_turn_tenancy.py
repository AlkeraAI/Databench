"""A platform box dies: the reader's next message moves the chat or is refused.

The org-owned box has its own cases in ``test_chat_machine_binding_seam.py``.
A pool or dedicated box belongs to the operator org, not to the chat's, so the
send path reaches it through placement by tenancy — a query that reads the
operator's rows by the chat's org would call a dead pool box "no machine" and
accept a message nothing will answer. Each case drives the real message route:
the box's heartbeat is aged past the liveness window, the reader sends, and
the chat either lands on a box of the same tenancy that is answering or the
reader is told, once, with the web refusal sentence, and nothing is recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.compute.provider import EC2
from alkera_core.config import settings
from alkera_core.models.compute import DEDICATED_TENANCY, POOL_TENANCY, ComputeAllocation
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import backfilled_org_machine
from tests.conftest import OrgWithAdmin, app_client, login

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

REFUSAL = (
    "This chat's workspace machine stopped answering. "
    "Start it again to resume the chat, then send the message."
)


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The pool is global: a pool box another test left live would serve this
    test's org, so every platform box is released first."""
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)))
        .values(state="released")
    )
    await real_session.commit()


async def _box(session: AsyncSession, *, operator: OrgWithAdmin, name: str, tenancy: str) -> str:
    machine_type = await make_machine_type(session, provider=EC2)
    alloc = ComputeAllocation(
        user_id=operator.admin_id,
        org_team_id=operator.org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        name=name,
        tenancy=tenancy,
        sandbox="gvisor",
        capacity=6,
        chats_served=0,
        state="ready",
        provider_machine_id=f"i-{name}",
        last_heartbeat_at=datetime.now(UTC),
    )
    session.add(alloc)
    await session.commit()
    await hold_with_credential(session, alloc)
    return str(alloc.id)


async def _dies(session: AsyncSession, machine_id: str) -> None:
    """The daemon stops beating; the row still says ``ready``, as a killed one's does."""
    silent_since = datetime.now(UTC) - timedelta(
        seconds=settings.compute_heartbeat_ready_seconds + 60
    )
    await session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.id == machine_id)
        .values(last_heartbeat_at=silent_since)
    )
    await session.commit()


async def _assign(
    session: AsyncSession, *, org: OrgWithAdmin, machine_id: str, fallback: bool
) -> None:
    box = await session.get(ComputeAllocation, UUID(machine_id))
    assert box is not None
    await backfilled_org_machine(session, org_id=org.org_id, box=box, fallback=fallback)


async def _open_chat(browser: AsyncClient, org: OrgWithAdmin, expected_box: str) -> str:
    await login(browser, org.admin_email, org.admin_password)
    created = await browser.post("/api/v1/chats", json={"title": "mid-turn"})
    assert created.status_code == 201, created.text
    assert created.json()["machine_id"] == expected_box
    return str(created.json()["id"])


async def _send(browser: AsyncClient, chat_id: str, client_id: str) -> tuple[int, dict]:
    sent = await browser.post(
        f"/api/v1/chats/{chat_id}/messages", json={"text": "still there?", "client_id": client_id}
    )
    return sent.status_code, sent.json()


async def _assert_refused_once(browser: AsyncClient, chat_id: str, dead: str) -> None:
    before = (await browser.get(f"/api/v1/chats/{chat_id}")).json()
    assert before["machine_status"] == "unreachable"
    status, body = await _send(browser, chat_id, "refused")
    assert status == 409, body
    assert body["error"]["code"] == "machine_unreachable"
    assert body["error"]["message"] == REFUSAL
    assert body["error"]["details"]["machineId"] == dead
    after = (await browser.get(f"/api/v1/chats/{chat_id}")).json()
    # Nothing recorded that nothing will answer, and the chat keeps its box.
    assert after["last_seq"] == before["last_seq"]
    assert after["machine_id"] == dead


async def test_a_pool_chat_whose_box_died_moves_to_the_pool_box_still_answering(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    first = await _box(real_session, operator=platform_admin, name="pool-1", tenancy=POOL_TENANCY)
    async with app_client(base_url="http://testserver") as browser:
        chat_id = await _open_chat(browser, org_admin, first)
        await _dies(real_session, first)
        second = await _box(
            real_session, operator=platform_admin, name="pool-2", tenancy=POOL_TENANCY
        )
        status, body = await _send(browser, chat_id, "moved")
        assert status == 201, body
        chat = (await browser.get(f"/api/v1/chats/{chat_id}")).json()
    assert chat["machine_id"] == second
    assert chat["machine_status"] == "ready"


async def test_a_pool_chat_with_no_pool_box_left_is_refused_with_the_sentence(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    only = await _box(real_session, operator=platform_admin, name="pool-1", tenancy=POOL_TENANCY)
    async with app_client(base_url="http://testserver") as browser:
        chat_id = await _open_chat(browser, org_admin, only)
        await _dies(real_session, only)
        await _assert_refused_once(browser, chat_id, only)


async def test_a_dedicated_chat_waits_for_its_own_box_and_is_refused_not_pooled(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """No fallback: an enterprise chat is never moved onto a shared host, even
    when one is answering; the reader is told instead."""
    theirs = await _box(
        real_session, operator=platform_admin, name="dedicated", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=org_admin, machine_id=theirs, fallback=False)
    await _box(real_session, operator=platform_admin, name="pool-1", tenancy=POOL_TENANCY)
    async with app_client(base_url="http://testserver") as browser:
        chat_id = await _open_chat(browser, org_admin, theirs)
        await _dies(real_session, theirs)
        await _assert_refused_once(browser, chat_id, theirs)


async def test_a_dedicated_chat_that_may_fall_back_moves_to_the_pool(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    theirs = await _box(
        real_session, operator=platform_admin, name="dedicated", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=org_admin, machine_id=theirs, fallback=True)
    pool = await _box(real_session, operator=platform_admin, name="pool-1", tenancy=POOL_TENANCY)
    async with app_client(base_url="http://testserver") as browser:
        chat_id = await _open_chat(browser, org_admin, theirs)
        await _dies(real_session, theirs)
        status, body = await _send(browser, chat_id, "fell-back")
        assert status == 201, body
        chat = (await browser.get(f"/api/v1/chats/{chat_id}")).json()
    assert chat["machine_id"] == pool
