"""Shared helpers for the placement tests that drive a machine's life through
the real routes: a provisioned platform box with a scripted node behind it,
an org's dedicated assignment, and the reads a tenant and an operator make —
the chat page, the chat list, ``machines/current``, the console. None of
these runs a fleet-wide pass; a module that does names the serial lane itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from alkera_core.compute.nodes import NodeLaunch
from alkera_core.compute.provider import RUNNING
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgComputeAssignment, WorkspaceObject
from alkera_core.models.compute import (
    DEDICATED_TENANCY,
    ComputeAllocation,
    ComputeAllocationEvent,
)
from alkera_test_support.compute.fake_nodes import FakeNode, FakeNodeProvider
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import backfilled_org_machine
from tests.conftest import OrgWithAdmin, app_client, login

MACHINES = "/admin/v1/machines"
CHATS = "/api/v1/chats"


async def platform_box(
    session: AsyncSession,
    fake: FakeNodeProvider,
    *,
    operator: OrgWithAdmin,
    name: str,
    tenancy: str = DEDICATED_TENANCY,
    state: str = "ready",
    fresh: bool = True,
    chats_served: int = 0,
    capacity: int = 6,
) -> ComputeAllocation:
    """A platform box the plane provisioned, held by a live credential, with a
    running machine ``i-<name>`` at ``fake``."""
    mt = await make_machine_type(session, provider="ec2")
    now = datetime.now(UTC)
    alloc = ComputeAllocation(
        user_id=operator.admin_id,
        org_team_id=operator.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        name=name,
        tenancy=tenancy,
        sandbox="gvisor",
        capacity=capacity,
        chats_served=chats_served,
        state=state,
        provider_machine_id=f"i-{name}",
        created_at=now,
        ready_at=now,
        state_changed_at=now,
        last_metered_at=now,
        last_heartbeat_at=now if fresh else None,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=1000,
    )
    session.add(alloc)
    await session.commit()
    await hold_with_credential(session, alloc)
    fake.nodes[f"i-{name}"] = FakeNode(
        launch=NodeLaunch(
            allocation_id=alloc.id, name=name, type_code="m6i.large", storage_gb=0, script=""
        ),
        phase=RUNNING,
        stopped=state == "asleep",
    )
    return alloc


async def assign(
    session: AsyncSession, *, org: OrgWithAdmin, box: ComputeAllocation, fallback: bool = False
) -> None:
    """The org's machine in its org pool, as the migration made every
    dedicated-box assignment (the org on the plan the pool needs). The
    assignment row stays beside it, as the migration leaves it, for the
    release path that still reads it."""
    session.add(
        OrgComputeAssignment(org_team_id=org.org_id, machine_id=box.id, fallback_to_pool=fallback)
    )
    await session.commit()
    await backfilled_org_machine(session, org_id=org.org_id, box=box, fallback=fallback)


async def open_chat(org: OrgWithAdmin, title: str) -> dict[str, Any]:
    async with app_client() as browser:
        await login(browser, org.admin_email, org.admin_password)
        created = await browser.post(CHATS, json={"title": title})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


async def read_chat(org: OrgWithAdmin, chat_id: str) -> dict[str, Any]:
    async with app_client() as browser:
        await login(browser, org.admin_email, org.admin_password)
        reread = await browser.get(f"{CHATS}/{chat_id}")
    assert reread.status_code == 200, reread.text
    body: dict[str, Any] = reread.json()
    return body


async def listed_chat(org: OrgWithAdmin, chat_id: str) -> dict[str, Any]:
    async with app_client() as browser:
        await login(browser, org.admin_email, org.admin_password)
        listed = await browser.get(CHATS)
    assert listed.status_code == 200, listed.text
    row: dict[str, Any] = next(item for item in listed.json()["items"] if item["id"] == chat_id)
    return row


async def send_message(org: OrgWithAdmin, chat_id: str, text: str) -> tuple[int, dict[str, Any]]:
    """``POST /chats/{id}/messages`` as the org admin; the status and body."""
    async with app_client() as browser:
        await login(browser, org.admin_email, org.admin_password)
        sent = await browser.post(
            f"{CHATS}/{chat_id}/messages", json={"text": text, "client_id": uuid4().hex}
        )
    body: dict[str, Any] = sent.json() if sent.content else {}
    return sent.status_code, body


async def current_machine(org: OrgWithAdmin) -> dict[str, Any]:
    async with app_client() as browser:
        await login(browser, org.admin_email, org.admin_password)
        resp = await browser.get("/api/v1/machines/current")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def spec_of(chat_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        return dict(chat.spec)


async def chat_frames(chat_id: str) -> int:
    """How many ``chat.updated`` frames name the chat."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox).where(
                EventOutbox.type == "chat.updated", EventOutbox.entity_id == chat_id
            )
        )
        return len(rows.scalars().all())


async def assignment_of(org: OrgWithAdmin) -> OrgComputeAssignment | None:
    async with AsyncSessionLocal() as session:
        return await session.get(OrgComputeAssignment, org.org_id)


async def machine_row(machine_id: UUID) -> ComputeAllocation:
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, machine_id)
        assert alloc is not None
        return alloc


async def machine_edges(machine_id: UUID) -> list[ComputeAllocationEvent]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ComputeAllocationEvent)
            .where(ComputeAllocationEvent.allocation_id == machine_id)
            .order_by(ComputeAllocationEvent.at)
        )
        return list(rows.scalars().all())


async def console_rows(client: AsyncClient, admin: OrgWithAdmin) -> dict[str, dict[str, Any]]:
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.get(MACHINES, params={"include_gone": "true"})
    assert resp.status_code == 200, resp.text
    return {row["id"]: row for row in resp.json()["items"]}


async def console_detail(
    client: AsyncClient, admin: OrgWithAdmin, machine_id: UUID
) -> dict[str, Any]:
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.get(f"{MACHINES}/{machine_id}")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def lifecycle_action(
    client: AsyncClient,
    admin: OrgWithAdmin,
    machine_id: UUID,
    action: str,
    *,
    json: dict[str, Any] | None = None,
    expect: int = 200,
) -> dict[str, Any]:
    """``POST /admin/v1/machines/{id}/<action>`` as a platform admin."""
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.post(f"{MACHINES}/{machine_id}/{action}", json=json)
    assert resp.status_code == expect, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def beat(machine_id: UUID, *, operator: OrgWithAdmin) -> None:
    """One heartbeat from the box, the way the route delivers it: in a session
    of its own, reading the row fresh — a session that watched another route
    move a box would otherwise place against its stale copy of it."""
    from alkera_core.authz import ActingContext
    from backend.services.compute import machines as machine_service

    ctx = ActingContext.for_user(
        user_id=operator.admin_id, org_id=operator.org_id, email=operator.admin_email
    )
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, machine_id)
        assert alloc is not None
        await machine_service.heartbeat(db, alloc, ctx=ctx)


async def legacy_chat(org: OrgWithAdmin, machine_id: UUID, *, status: str) -> str:
    """A chat exactly as a binding left it: bound to the machine with the word
    recorded at the time, never restated by anything since."""
    async with AsyncSessionLocal() as session:
        chat = WorkspaceObject(
            org_team_id=org.org_id,
            logical_id=f"placement-{uuid4().hex[:10]}",
            namespace="workspace",
            type="chat",
            title="left behind",
            version=1,
            status="ready",
            spec={"machine_id": str(machine_id), "machine_status": status, "last_seq": 0},
            owner_user_id=org.admin_id,
            visibility_scope="private",
        )
        session.add(chat)
        await session.commit()
        return str(chat.id)


__all__ = [
    "CHATS",
    "MACHINES",
    "assign",
    "assignment_of",
    "beat",
    "chat_frames",
    "console_detail",
    "console_rows",
    "current_machine",
    "legacy_chat",
    "lifecycle_action",
    "listed_chat",
    "machine_edges",
    "machine_row",
    "open_chat",
    "platform_box",
    "read_chat",
    "send_message",
    "spec_of",
]
