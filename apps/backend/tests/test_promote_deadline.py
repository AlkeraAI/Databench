"""A promote the machine never answers gets a terminal state.

Promoting creates the object first and asks the machine for the rows second, so
"Saving — the workspace is uploading this result." is a state a reader meets
legitimately. It stops being legitimate the moment the machine has gone: nothing
delivers the payload and nothing refuses it, so the object waited in
``pending_upload`` with no rows, no reason and a permanent Saving pill on the
list — ninety minutes, in the rehearsal, and counting.

Driven through the REAL routes on both sides of the sweep: the promote that
creates the object, the sweep that ends its wait, the read the object page
makes, and the retry the reader presses on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.models import EventOutbox, WorkspaceObject
from alkera_core.objects import PROMOTE_UNANSWERED_REASON, expire_stalled_promotes
from httpx import AsyncClient
from sqlalchemy import select, update
from tests.conftest import OrgWithAdmin, login, mint_cli_token

pytestmark = pytest.mark.asyncio


async def _chat(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json={"title": "Ops"})
    assert response.status_code == 201, response.text
    return response.json()


async def _promoted_result(client: AsyncClient, chat_id: str, event_id: str) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/chats/{chat_id}/promote", json={"event_id": event_id, "title": "Rows"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _agent_headers(org: OrgWithAdmin) -> dict[str, str]:
    token = await mint_cli_token(
        user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id
    )
    return {"Authorization": f"Bearer {token}", **agent_headers("sess-01J7Q3M8")}


async def _changed(org_id: UUID) -> int:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == EventType.WORKSPACE_OBJECT_CHANGED.value,
            )
        )
        return len(list(rows.scalars().all()))


async def _relays(org_id: UUID, chat_id: str) -> list[dict[str, Any]]:
    """Every relay body the cloud put on this chat's document lane."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == EventType.DOC_OP.value,
                EventOutbox.entity_id == f"doc:chat:{chat_id}",
            )
            .order_by(EventOutbox.id)
        )
        out: list[dict[str, Any]] = []
        for row in rows.scalars().all():
            for event in row.payload["envelope"]["payload"]["events"]:
                out.append(dict(event))
        return out


async def _age(object_id: str, seconds: float) -> None:
    """Put the wait `seconds` in the past. ``updated_at`` is the row's own cursor
    and a promote does not touch the row again while it waits, so it IS the
    moment the wait began."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.id == UUID(object_id))
            .values(updated_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        await session.commit()


async def _sweep() -> int:
    async with AsyncSessionLocal() as session:
        return await expire_stalled_promotes(session)


async def _stored(object_id: str) -> WorkspaceObject:
    async with AsyncSessionLocal() as session:
        obj = await session.get(WorkspaceObject, UUID(object_id))
        assert obj is not None
        return obj


async def test_a_promote_past_its_deadline_fails_with_a_reason_the_page_can_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], f"ev-stalled-{uuid4().hex[:8]}")
    assert result["status"] == "pending_upload"
    assert result["spec"]["failure_reason"] is None
    before = await _changed(org_admin.org_id)

    await _age(result["id"], settings.objects_promote_deadline_seconds + 60)
    assert await _sweep() >= 1

    fresh = await client.get(f"/api/v1/objects/{result['id']}")
    assert fresh.status_code == 200, fresh.text
    body = fresh.json()
    assert body["status"] == "failed"
    assert body["spec"]["failure_reason"] == PROMOTE_UNANSWERED_REASON
    # The reader's browser is told to re-read it, exactly as for any change.
    assert await _changed(org_admin.org_id) > before
    # …and the promoter recorded at create time survives the failure.
    assert body["spec"]["receipt"]["principal_chain"]["promoted_by"]


async def test_a_promote_still_inside_its_deadline_is_left_waiting(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A deadline, not a policy against waiting: a slow machine may answer."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], f"ev-slow-{uuid4().hex[:8]}")

    await _age(result["id"], max(settings.objects_promote_deadline_seconds - 30, 1))
    await _sweep()

    fresh = (await client.get(f"/api/v1/objects/{result['id']}")).json()
    assert fresh["status"] == "pending_upload"
    assert fresh["spec"]["failure_reason"] is None


async def test_the_machines_own_refusal_outranks_the_deadlines_generic_one(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A machine that IS alive and cannot find the result says so in its own
    words. The deadline is for the machine that says nothing at all."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], f"ev-refused-{uuid4().hex[:8]}")
    reason = "no tool result with event id prt_07ad57d1c0016yL2P6MozuMrVf"
    refused = await client.post(
        f"/api/v1/objects/{result['id']}/payload/failed",
        json={"reason": reason},
        headers=await _agent_headers(org_admin),
    )
    assert refused.status_code == 200, refused.text

    await _age(result["id"], settings.objects_promote_deadline_seconds * 10)
    await _sweep()

    fresh = (await client.get(f"/api/v1/objects/{result['id']}")).json()
    assert fresh["spec"]["failure_reason"] == reason


async def test_the_sweep_runs_again_without_failing_anything_twice(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], f"ev-twice-{uuid4().hex[:8]}")
    await _age(result["id"], settings.objects_promote_deadline_seconds + 60)
    await _sweep()
    version = (await _stored(result["id"])).version
    after_first = await _changed(org_admin.org_id)

    await _sweep()

    assert (await _stored(result["id"])).version == version
    assert await _changed(org_admin.org_id) == after_first


async def test_a_failed_promote_can_be_asked_for_again_and_the_machine_is_told(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """ "Save again" on the object's own page. Pressing Save on the card instead
    would land on this very row — the create is idempotent on the chat and the
    event — and relay nothing."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    event_id = f"ev-retry-{uuid4().hex[:8]}"
    result = await _promoted_result(client, chat["id"], event_id)
    await _age(result["id"], settings.objects_promote_deadline_seconds + 60)
    await _sweep()
    before = len(await _relays(org_admin.org_id, chat["id"]))

    retried = await client.post(f"/api/v1/objects/{result['id']}/promote/retry")

    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body["status"] == "pending_upload"
    # A fresh "Saving…" under the last failure would be two states at once.
    assert body["spec"]["failure_reason"] is None
    relays = await _relays(org_admin.org_id, chat["id"])
    assert len(relays) == before + 1
    assert relays[-1]["object_id"] == result["id"]
    assert relays[-1]["event_id"] == event_id


async def test_a_result_that_is_not_failed_is_not_asked_for_again(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """One still waiting has an outstanding relay; one that is ready has rows."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    result = await _promoted_result(client, chat["id"], f"ev-waiting-{uuid4().hex[:8]}")

    response = await client.post(f"/api/v1/objects/{result['id']}/promote/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "promote_not_failed"
