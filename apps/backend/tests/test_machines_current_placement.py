"""``GET /machines/current`` answers what the next chat would be placed on.

The page reads it before a chat exists — ``/chat/new`` — and gates the
composer on it, while ``POST /chats`` places the chat by the placement rules.
When the read knew only an org's OWN box, every pool-served org was shown a dead
composer over a create that would have placed the chat at once. So the read is
answered by placement itself, and each case here runs through the real rules:
a dedicated box is the org's, a pool box the rules would pick reads ``pool``
(which shared box is not the tenant's to know), and an org nothing would serve —
a ``none`` pool box, a revoked or draining one, a dedicated box that is down
with no fallback — reads ``none``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.compute.provider import EC2
from alkera_core.models.compute import (
    DEDICATED_TENANCY,
    DRAINING,
    POOL_TENANCY,
    ComputeAllocation,
)
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import backfilled_org_machine, set_fallback
from tests.conftest import OrgWithAdmin, login

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

#: What stands between a new chat and its first turn when no machine can
#: serve the org, in the server's words.
NO_MACHINE = {
    "subject": "chat",
    "state": "unavailable",
    "label": "Can't run",
    "tone": "danger",
    "reason_code": "no_machine",
    "sentence": "No machine can serve your organization right now.",
    "since": None,
    "recheck_at": None,
    "action": None,
}
NOTHING = {
    "machine_id": None,
    "status": "none",
    "name": "",
    "reason": "",
    "last_heartbeat_at": None,
    "status_fact": NO_MACHINE,
}
#: A shared machine takes the chat: nothing stands in its way.
POOL = {**NOTHING, "status": "pool", "status_fact": None}


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The pool is global by design, so a pool box another test left live
    would serve this test's org. Every platform box is released first."""
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)))
        .values(state="released")
    )
    await real_session.commit()


async def _platform_box(
    session: AsyncSession,
    *,
    operator: OrgWithAdmin,
    name: str,
    tenancy: str = POOL_TENANCY,
    sandbox: str = "gvisor",
    state: str = "ready",
    fresh: bool = True,
    revoked: bool = False,
) -> ComputeAllocation:
    mt = await make_machine_type(session, provider=EC2)
    now = datetime.now(UTC)
    alloc = ComputeAllocation(
        user_id=operator.admin_id,
        org_team_id=operator.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        name=name,
        tenancy=tenancy,
        sandbox=sandbox,
        capacity=6,
        chats_served=0,
        state=state,
        provider_machine_id=f"pod-{name}",
        last_heartbeat_at=now if fresh else now - timedelta(hours=1),
    )
    session.add(alloc)
    await session.commit()
    await hold_with_credential(session, alloc, revoked=revoked)
    return alloc


async def _current(client: AsyncClient, org: OrgWithAdmin) -> dict[str, Any]:
    await login(client, org.admin_email, org.admin_password)
    resp = await client.get("/api/v1/machines/current")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_a_pool_served_org_reads_pool_and_its_chat_is_placed_there(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """The read and the create agree: ``pool`` before, a placed chat after."""
    box = await _platform_box(real_session, operator=platform_admin, name="pool-ready")
    assert await _current(client, org_admin) == POOL

    created = await client.post("/api/v1/chats", json={"title": None})
    assert created.status_code == 201, created.text
    assert created.json()["machine_id"] == str(box.id)


@pytest.mark.parametrize(
    "box",
    [
        pytest.param({"sandbox": "none"}, id="none-sandbox-pool-box-never-serves-a-tenant"),
        pytest.param({"revoked": True}, id="revoked-pool-box"),
        pytest.param({"state": DRAINING}, id="draining-pool-box"),
    ],
)
async def test_an_org_nothing_would_serve_reads_none(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    box: dict[str, Any],
) -> None:
    await _platform_box(real_session, operator=platform_admin, name="pool-unusable", **box)
    assert await _current(client, org_admin) == NOTHING


async def test_an_org_with_no_compute_anywhere_reads_none(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    assert await _current(client, org_admin) == NOTHING


async def test_a_dedicated_org_reads_its_own_box(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """The dedicated box is the org's: named, with its reachability — and it
    wins over a pool box that also stands."""
    await _platform_box(real_session, operator=platform_admin, name="pool-ready")
    dedicated = await _platform_box(
        real_session, operator=platform_admin, name="dedicated-a", tenancy=DEDICATED_TENANCY
    )
    await backfilled_org_machine(real_session, org_id=org_admin.org_id, box=dedicated)

    body = await _current(client, org_admin)
    assert body["machine_id"] == str(dedicated.id)
    assert body["status"] == "ready"
    assert body["name"] == "dedicated-a"


async def test_a_dedicated_org_whose_box_sleeps_reads_it_asleep(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """A sleeping dedicated box is still the org's box: the page reads it by
    name as ``asleep`` — composable, since the first send wakes it — however
    long ago it last beat, and not as a dead box or as nothing."""
    dedicated = await _platform_box(
        real_session,
        operator=platform_admin,
        name="dedicated-asleep",
        tenancy=DEDICATED_TENANCY,
        state="asleep",
        fresh=False,
    )
    await backfilled_org_machine(real_session, org_id=org_admin.org_id, box=dedicated)

    body = await _current(client, org_admin)
    assert (body["machine_id"], body["status"], body["name"]) == (
        str(dedicated.id),
        "asleep",
        "dedicated-asleep",
    )


async def test_a_pool_machine_whose_box_was_released_holds_the_org_only_without_fallback(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """An org pool machine whose provider machine was released serves nothing
    until the reconcile replaces it: an org that opted out of the shared pool
    waits for its own machine, and one that did not runs on the pool."""
    await _platform_box(real_session, operator=platform_admin, name="pool-ready")
    dead = await _platform_box(
        real_session,
        operator=platform_admin,
        name="dedicated-dead",
        tenancy=DEDICATED_TENANCY,
        state="released",
    )
    await backfilled_org_machine(real_session, org_id=org_admin.org_id, box=dead, fallback=False)
    assert await _current(client, org_admin) == NOTHING
    await set_fallback(real_session, org_id=org_admin.org_id, fallback=True)
    assert await _current(client, org_admin) == POOL


@pytest.mark.parametrize(
    ("fallback", "expected"),
    [
        pytest.param(True, POOL, id="fallback-runs-on-the-pool"),
        pytest.param(False, NOTHING, id="no-fallback-waits-for-its-box"),
    ],
)
async def test_a_dedicated_org_whose_box_is_down_follows_its_fallback(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fallback: bool,
    expected: dict[str, Any],
) -> None:
    await _platform_box(real_session, operator=platform_admin, name="pool-ready")
    dedicated = await _platform_box(
        real_session,
        operator=platform_admin,
        name="dedicated-down",
        tenancy=DEDICATED_TENANCY,
        fresh=False,
    )
    await backfilled_org_machine(
        real_session, org_id=org_admin.org_id, box=dedicated, fallback=fallback
    )
    assert await _current(client, org_admin) == expected


async def _workspace(session: AsyncSession, org: OrgWithAdmin) -> Any:
    from alkera_core.models import User
    from backend.services.workspaces import workspace_service

    owner = await session.get(User, org.admin_id)
    assert owner is not None
    workspace, _ = await workspace_service.create_project(
        session, owner=owner, org_id=org.org_id, title="Pinned", client_id=None
    )
    await session.commit()
    return workspace


@pytest.mark.parametrize(
    ("look", "status"),
    [
        pytest.param("running", "ready", id="pinned-to-a-running-machine-reads-it-ready"),
        pytest.param("stopped", "asleep", id="pinned-to-a-stopped-machine-reads-a-send-wakes-it"),
    ],
)
async def test_a_chat_of_a_pinned_workspace_reads_its_pinned_machine(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    look: str,
    status: str,
) -> None:
    """The org's next chat has nowhere to run, but a chat of a workspace pinned
    to an org machine runs there, so the composer for it is open."""
    from tests._org_machine_helpers import make_org_machine, pin_workspace

    machine, alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look=look
    )
    workspace = await _workspace(real_session, org_admin)
    await pin_workspace(real_session, workspace.id, machine.id)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    unpinned = await client.get("/api/v1/machines/current")
    pinned = await client.get(
        "/api/v1/machines/current", params={"workspace_id": str(workspace.id)}
    )
    assert unpinned.json()["status"] == "none"
    assert pinned.status_code == 200, pinned.text
    assert (pinned.json()["status"], pinned.json()["machine_id"]) == (status, str(alloc.id))


async def test_a_workspace_the_caller_cannot_read_is_not_found(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    other = await _workspace(real_session, platform_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    for raw in (str(other.id), "00000000-0000-4000-8000-000000000000"):
        resp = await client.get("/api/v1/machines/current", params={"workspace_id": raw})
        assert resp.status_code == 404, resp.text
