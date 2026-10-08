"""The org's workspace machine through the real routes and the placement seam.

The daemon on the box registers with the customer's user JWT plus the agent
assertion headers: register is idempotent by pod id, goes through the same
admission as any start (a refused registration is a visible 402/429), pins the
grant's rate and the catalog's cost, and is born ``starting``. Heartbeats
derive ``ready`` and announce only a transition; the sweep announces the
transition a heartbeat cannot make (the machine going quiet). ``/machines/
current`` and ``resolve_machine_for`` agree on which machine serves the org,
and a foreign org's machine is never bound or visible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import ActingContext, agent_headers
from alkera_core.compute.machines import machine_state, machine_status, sweep_reachability
from alkera_core.compute.meter import meter_and_cutoff
from alkera_core.compute.provider import ComputeProviderError
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from alkera_core.models.compute import ComputeAllocation
from backend.services.compute import placement
from backend.services.compute.placement import MachineBinding, resolve_machine_for
from backend.services.org import teams as team_service
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs
from tests._compute_helpers import FakeProvider, make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, login, make_member, mint_cli_token

pytestmark = pytest.mark.xdist_group("compute-fleet")

T0 = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


async def _daemon_headers(org: OrgWithAdmin, session_id: str = "sess-box") -> dict[str, str]:
    """What the daemon sends: the user's Bearer JWT plus the agent assertion."""
    jwt = await mint_cli_token(user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id)
    return {"Authorization": f"Bearer {jwt}", **agent_headers(session_id)}


async def _catalog_and_grant(
    real_session: AsyncSession, org: OrgWithAdmin, **grant_kw: Any
) -> tuple[Any, Any]:
    mt = await make_machine_type(real_session)
    grant = await make_grant(
        real_session, org_team_id=org.org_id, machine_type_id=mt.id, **grant_kw
    )
    return mt, grant


def _register_body(mt: Any, pod: str = "pod-demo-1", name: str = "demo-box") -> dict[str, str]:
    return {
        "provider": "runpod",
        "provider_pod_id": pod,
        "name": name,
        "machine_type_code": mt.provider_type_id,
    }


async def _machine_frames(entity_id: str) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.type == "compute_machine.changed", EventOutbox.entity_id == entity_id
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def _decisions(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "compute_machine",
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


async def test_register_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/machines/register", json={})
    assert resp.status_code == 401


async def test_the_daemon_registers_the_box_as_an_agent_for_the_user(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt, grant = await _catalog_and_grant(real_session, org_admin, rate_per_minute_nanos=0)
    headers = await _daemon_headers(org_admin)
    resp = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "demo-box"
    assert body["provider"] == "runpod" and body["provider_pod_id"] == "pod-demo-1"
    assert body["lifecycle"] == "workspace"
    assert body["status"] == "starting"
    assert body["last_heartbeat_at"] is None
    assert body["machine_type"]["id"] == str(mt.id)
    assert body["machine_type"]["rate_per_minute_nanos"] == 0
    assert not [k for k in body if "cost" in k]
    row = (
        await real_session.execute(
            select(ComputeAllocation).where(ComputeAllocation.id == UUID(body["id"]))
        )
    ).scalar_one()
    assert (row.lifecycle, row.state, row.name) == ("workspace", "ready", "demo-box")
    assert row.user_id == org_admin.admin_id and row.org_team_id == org_admin.org_id
    assert row.grant_id == grant.id
    assert row.price_per_minute_nanos == 0
    assert row.true_cost_per_minute_nanos == mt.provider_price_per_minute_nanos
    assert row.last_metered_at is not None and row.ready_at is not None
    assert row.max_lease_minutes is None
    assert row.last_reported_status == "starting"
    (frame,) = await _machine_frames(body["id"])
    assert frame.payload == {"status": "starting", "reason": None}
    assert frame.org_id == org_admin.org_id
    # The acting chain says an agent acted for the admin.
    assert frame.actor["acting"]["kind"] == "agent"
    assert [link["kind"] for link in frame.actor["chain"]] == ["user", "agent"]
    (decision,) = await _decisions(org_admin.org_id)
    assert (decision.payload["effect"], decision.payload["reason"]) == ("allow", "admin_controls")
    assert decision.payload["attrs"]["lifecycle"] == "workspace"
    assert decision.actor["acting"]["kind"] == "agent"


async def test_register_is_idempotent_by_pod_id(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt, _ = await _catalog_and_grant(real_session, org_admin, ceiling=1)
    headers = await _daemon_headers(org_admin)
    first = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    again = await client.post(
        "/api/v1/machines/register", json=_register_body(mt, name="renamed"), headers=headers
    )
    assert first.status_code == 201 and again.status_code == 200, (first.text, again.text)
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["name"] == "demo-box"  # the existing row is returned unchanged
    assert len(await _machine_frames(first.json()["id"])) == 1  # announced once
    # A ceiling of one is not consumed twice by the same pod.
    rows = (
        (
            await real_session.execute(
                select(ComputeAllocation).where(ComputeAllocation.org_team_id == org_admin.org_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_a_second_pod_over_the_ceiling_is_a_visible_429(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt, _ = await _catalog_and_grant(real_session, org_admin, ceiling=1)
    headers = await _daemon_headers(org_admin)
    assert (
        await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    ).status_code == 201
    resp = await client.post(
        "/api/v1/machines/register", json=_register_body(mt, pod="pod-demo-2"), headers=headers
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "compute_limit_reached"
    frames = await _machine_frames(str(org_admin.org_id))
    assert [f.payload for f in frames] == [{"status": "refused", "reason": "compute_limit_reached"}]


async def test_register_without_a_grant_is_a_visible_429(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session)
    headers = await _daemon_headers(org_admin)
    resp = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "no_compute_grant"


async def test_register_a_priced_type_without_credit_is_a_visible_402(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt, _ = await _catalog_and_grant(real_session, org_admin, rate_per_minute_nanos=10**15)
    headers = await _daemon_headers(org_admin)
    resp = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert resp.status_code == 402, resp.text
    assert resp.json()["error"]["code"] == "insufficient_credit"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"provider_pod_id": "p", "name": "n", "machine_type_code": "nope"}, id="unknown-code"
        ),
        pytest.param(
            {"provider": "ec2", "provider_pod_id": "p", "name": "n", "machine_type_code": "SET"},
            id="other-provider",
        ),
    ],
)
async def test_register_an_unknown_machine_type_is_404(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin, body: dict[str, str]
) -> None:
    mt = await make_machine_type(real_session)
    if body["machine_type_code"] == "SET":
        body["machine_type_code"] = mt.provider_type_id
    headers = await _daemon_headers(org_admin)
    resp = await client.post("/api/v1/machines/register", json=body, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Machine type not found"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"provider_pod_id": "", "name": "n", "machine_type_code": "c"}, id="empty-pod"
        ),
        pytest.param(
            {"provider_pod_id": "p", "name": "", "machine_type_code": "c"}, id="empty-name"
        ),
        pytest.param(
            {"provider_pod_id": "p", "name": "x" * 129, "machine_type_code": "c"}, id="long-name"
        ),
        pytest.param({"name": "n", "machine_type_code": "c"}, id="missing-pod"),
    ],
)
async def test_register_validates_its_body(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict[str, str]
) -> None:
    headers = await _daemon_headers(org_admin)
    resp = await client.post("/api/v1/machines/register", json=body, headers=headers)
    assert resp.status_code == 422


async def test_a_plain_cookie_session_may_register_too(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The principal dependency accepts the user directly as well as an agent for
    them; the chain then has one link."""
    mt, _ = await _catalog_and_grant(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/machines/register", json=_register_body(mt))
    assert resp.status_code == 201, resp.text
    (frame,) = await _machine_frames(resp.json()["id"])
    assert [link["kind"] for link in frame.actor["chain"]] == ["user"]


async def test_an_unverified_member_cannot_register(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt, _ = await _catalog_and_grant(real_session, org_admin)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=False)
    await login(client, member.email, password or "")
    resp = await client.post("/api/v1/machines/register", json=_register_body(mt))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_verification_required"


# --------------------------------------------------------------------------- #
# heartbeat + current + the sweep
# --------------------------------------------------------------------------- #


async def _registered(
    client: AsyncClient, real_session: AsyncSession, org: OrgWithAdmin
) -> tuple[str, dict[str, str]]:
    mt, _ = await _catalog_and_grant(real_session, org)
    headers = await _daemon_headers(org)
    resp = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert resp.status_code == 201, resp.text
    machine_id: str = resp.json()["id"]
    return machine_id, headers


async def test_current_is_none_for_an_org_without_a_machine(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/machines/current")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "machine_id": None,
        "status": "none",
        "name": "",
        "reason": "",
        "last_heartbeat_at": None,
        "status_fact": {
            "subject": "chat",
            "state": "unavailable",
            "label": "Can't run",
            "tone": "danger",
            "reason_code": "no_machine",
            "sentence": "No machine can serve your organization right now.",
            "since": None,
            "recheck_at": None,
            "action": None,
        },
    }


async def test_heartbeats_drive_starting_to_ready_and_announce_the_transition_once(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id, headers = await _registered(client, real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    current = (await client.get("/api/v1/machines/current")).json()
    assert current["machine_id"] == machine_id and current["status"] == "starting"
    assert current["name"] == "demo-box"

    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
    assert beat.status_code == 204, beat.text
    beat_again = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
    assert beat_again.status_code == 204

    current = (await client.get("/api/v1/machines/current")).json()
    assert current["status"] == "ready"
    assert current["last_heartbeat_at"] is not None
    frames = await _machine_frames(machine_id)
    assert [f.payload["status"] for f in frames] == ["starting", "ready"]  # two beats, one frame
    assert frames[1].actor["acting"]["kind"] == "agent"


async def test_the_banner_state_carries_the_machines_own_account_of_not_being_ready(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A machine that is not ready says so, and the reader is entitled to a
    reason -- but the one the provider recorded is an API message with a URL
    and a machine id in it, written for whoever operates the box. The banner is
    read by the person waiting on an answer, so it carries a sentence and the
    provider's own words stay on the allocation. A ready machine has nothing to
    explain."""
    machine_id, headers = await _registered(client, real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    raw = "400 Bad Request from https://api.runpod.io/v2/pods/pod-9f31: no capacity"
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.error = raw
        await db.commit()

    current = (await client.get("/api/v1/machines/current")).json()
    assert current["status"] == "starting"
    assert current["reason"] == "The workspace could not be started."
    assert "runpod" not in current["reason"] and "pod-9f31" not in current["reason"]

    # The operator's copy is untouched: the words are still on the row.
    async with AsyncSessionLocal() as db:
        kept = await db.get(ComputeAllocation, UUID(machine_id))
        assert kept is not None and kept.error == raw

    assert (
        await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
    ).status_code == 204
    ready = (await client.get("/api/v1/machines/current")).json()
    assert ready["status"] == "ready" and ready["reason"] == ""


async def test_a_machine_that_stops_heartbeating_reads_unreachable(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    window = settings.compute_heartbeat_ready_seconds
    # Register and log in INSIDE the freeze: both mint a JWT, and a token issued
    # at the real clock has an `iat` in the future once T0 is in the past, which
    # the heartbeat rejects with a 401.
    with freeze_time(T0, real_asyncio=True) as frozen:
        machine_id, headers = await _registered(client, real_session, org_admin)
        await login(client, org_admin.admin_email, org_admin.admin_password)
        assert (
            await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
        ).status_code == 204
        frozen.move_to(T0 + timedelta(seconds=window - 1))
        assert (await client.get("/api/v1/machines/current")).json()["status"] == "ready"
        frozen.move_to(T0 + timedelta(seconds=window))
        assert (await client.get("/api/v1/machines/current")).json()["status"] == "unreachable"
        # Nothing announced the silence yet — that is the sweep's job.
        assert [f.payload["status"] for f in await _machine_frames(machine_id)] == [
            "starting",
            "ready",
        ]
        async with AsyncSessionLocal() as session:
            changed = await sweep_reachability(session, now=T0 + timedelta(seconds=window))
        assert changed >= 1
        frames = await _machine_frames(machine_id)
        assert [f.payload["status"] for f in frames] == ["starting", "ready", "unreachable"]
        assert frames[-1].actor["acting"]["kind"] == "service"
        # A second sweep announces nothing new.
        async with AsyncSessionLocal() as session:
            await sweep_reachability(session, now=T0 + timedelta(seconds=window + 5))
        assert len(await _machine_frames(machine_id)) == 3
        # The daemon comes back: ready again, announced by the heartbeat itself.
        frozen.move_to(T0 + timedelta(seconds=window + 10))
        assert (
            await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
        ).status_code == 204
        assert (await client.get("/api/v1/machines/current")).json()["status"] == "ready"
        assert [f.payload["status"] for f in await _machine_frames(machine_id)][-1] == "ready"


async def test_the_sweep_ignores_session_machines_and_released_rows(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session)
    session_row = ComputeAllocation(
        user_id=org_admin.admin_id,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        lifecycle="session",
        state="ready",
        provider_machine_id="pod-s",
    )
    released = ComputeAllocation(
        user_id=org_admin.admin_id,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        state="released",
        provider_machine_id="pod-r",
        last_reported_status="ready",
    )
    real_session.add_all([session_row, released])
    await real_session.commit()
    async with AsyncSessionLocal() as session:
        await sweep_reachability(session, now=T0)
    assert await _machine_frames(str(session_row.id)) == []
    assert await _machine_frames(str(released.id)) == []


@pytest.mark.parametrize(
    ("state", "drain_kind", "last_reported", "gap"),
    [
        pytest.param("ready", None, "ready", True, id="a-ready-box"),
        pytest.param("draining", None, "draining", True, id="a-draining-box"),
        pytest.param("draining", "restart", "restarting", True, id="a-restarting-box"),
        pytest.param("ready", None, "place_again", True, id="a-box-asked-to-place-again"),
        pytest.param("ready", None, "starting", False, id="a-box-that-never-came-up"),
        pytest.param("ready", None, None, False, id="a-box-never-announced"),
        pytest.param("ready", None, "unreachable", False, id="already-announced-quiet"),
    ],
)
async def test_a_serving_box_that_goes_quiet_is_logged_as_a_heartbeat_gap(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    state: str,
    drain_kind: str | None,
    last_reported: str | None,
    gap: bool,
) -> None:
    """The figure the heartbeat-gap alarm counts: a box that was serving
    (ready, draining, restarting) and missed heartbeats past the ready window,
    said once, when the sweep announces it unreachable, with how long it has
    been quiet. A box that never came up, or one already announced quiet, is
    not a gap."""
    window = settings.compute_heartbeat_ready_seconds
    mt = await make_machine_type(real_session)
    box = ComputeAllocation(
        user_id=org_admin.admin_id,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        state=state,
        drain_kind=drain_kind,
        provider_machine_id=f"pod-gap-{uuid4().hex[:8]}",
        last_heartbeat_at=T0,
        last_reported_status=last_reported,
    )
    real_session.add(box)
    await real_session.commit()
    with capture_logs() as logs:
        async with AsyncSessionLocal() as session:
            await sweep_reachability(session, now=T0 + timedelta(seconds=window + 42))
    gaps = [
        e
        for e in logs
        if e["event"] == "compute.machine.heartbeat_gap" and e["allocation_id"] == str(box.id)
    ]
    if not gap:
        assert gaps == []
        return
    (said,) = gaps
    assert said["log_level"] == "warning"
    assert (said["org_id"], said["previous"]) == (str(org_admin.org_id), last_reported)
    assert said["silent_seconds"] == window + 42


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(f"/api/v1/machines/{uuid4()}/heartbeat", id="unknown"),
        pytest.param("/api/v1/machines/not-a-uuid/heartbeat", id="malformed"),
    ],
)
async def test_heartbeat_for_a_missing_machine_is_404(
    client: AsyncClient, org_admin: OrgWithAdmin, path: str
) -> None:
    headers = await _daemon_headers(org_admin)
    resp = await client.post(path, headers=headers)
    assert resp.status_code == 404


async def test_another_orgs_machine_is_invisible_to_heartbeat_current_and_placement(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id, _ = await _registered(client, real_session, org_admin)
    other, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {uuid4().hex[:6]}",
        admin_email=f"other-{uuid4().hex[:8]}@alkera.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="pw-1234567890",
    )
    other_admin.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    outsider = OrgWithAdmin(
        org_id=other.id,
        admin_id=other_admin.id,
        admin_email=other_admin.email,
        admin_password="pw-1234567890",
    )
    headers = await _daemon_headers(outsider)
    resp = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Machine not found"
    await login(client, outsider.admin_email, outsider.admin_password)
    assert (await client.get("/api/v1/machines/current")).json()["status"] == "none"
    other_ctx = ActingContext.for_user(
        user_id=other_admin.id, org_id=other.id, email=other_admin.email
    )
    assert (
        await resolve_machine_for(real_session, ctx=other_ctx, org_team_id=other.id, purpose="chat")
        is None
    )
    # Naming the victim's org as the team resolves nothing either.
    assert (
        await resolve_machine_for(
            real_session, ctx=other_ctx, org_team_id=org_admin.org_id, purpose="chat"
        )
        is None
    )


async def test_a_member_who_did_not_register_the_box_cannot_heartbeat_it(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id, _ = await _registered(client, real_session, org_admin)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await login(client, member.email, password or "")
    resp = await client.post(f"/api/v1/machines/{machine_id}/heartbeat")
    assert resp.status_code == 404  # not theirs: indistinguishable from missing
    # ...but they see the org's machine, as any member does.
    assert (await client.get("/api/v1/machines/current")).json()["machine_id"] == machine_id


# --------------------------------------------------------------------------- #
# placement — the single chat-to-machine binding
# --------------------------------------------------------------------------- #


async def test_resolve_machine_for_binds_the_orgs_machine_with_its_status(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    ctx = ActingContext.for_user(
        user_id=org_admin.admin_id, org_id=org_admin.org_id, email=org_admin.admin_email
    )
    assert (
        await resolve_machine_for(
            real_session, ctx=ctx, org_team_id=org_admin.org_id, purpose="chat"
        )
        is None
    )
    machine_id, headers = await _registered(client, real_session, org_admin)
    binding = await resolve_machine_for(
        real_session, ctx=ctx, org_team_id=org_admin.org_id, purpose="chat"
    )
    assert binding == MachineBinding(
        machine_id=UUID(machine_id), status="starting", name="demo-box"
    )
    assert (
        await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
    ).status_code == 204
    binding = await resolve_machine_for(
        real_session, ctx=ctx, org_team_id=org_admin.org_id, purpose="chat"
    )
    assert binding is not None and binding.status == "ready"
    # A team below the root binds the org's machine too (one machine per org today).
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name=f"team-{uuid4().hex[:4]}"
    )
    from_team = await resolve_machine_for(
        real_session, ctx=ctx, org_team_id=team.id, purpose="chat"
    )
    assert from_team is not None and from_team.machine_id == UUID(machine_id)
    # An agent acting for the user binds the same machine.
    agent = ActingContext.for_agent(
        user_id=org_admin.admin_id,
        org_id=org_admin.org_id,
        email=org_admin.admin_email,
        session_id="sess-1",
    )
    as_agent = await resolve_machine_for(
        real_session, ctx=agent, org_team_id=org_admin.org_id, purpose="chat"
    )
    assert as_agent is not None and as_agent.machine_id == UUID(machine_id)


async def test_the_longest_standing_answering_machine_is_the_current_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The resolver's shape admits several boxes per org: the ones answering
    now are preferred over the ones that have gone quiet, the oldest of those
    is bound, and a released row never is.

    The tie-break is deliberately the allocation's age rather than whose beat
    landed last — a box cannot take the org's next chat by beating faster than
    the box that has been serving it.
    """
    mt = await make_machine_type(real_session)
    now = datetime.now(UTC)

    def row(
        name: str, beat: datetime | None, born: datetime, state: str = "ready"
    ) -> ComputeAllocation:
        return ComputeAllocation(
            user_id=org_admin.admin_id,
            org_team_id=org_admin.org_id,
            machine_type_id=mt.id,
            lifecycle="workspace",
            name=name,
            state=state,
            provider_machine_id=f"pod-{name}",
            created_at=born,
            last_heartbeat_at=beat,
        )

    quiet = row("quiet", None, now - timedelta(days=4))
    silent = row("silent", now - timedelta(minutes=10), now - timedelta(days=3))
    established = row("established", now - timedelta(seconds=20), now - timedelta(days=2))
    newest = row("newest", now, now)
    gone = row("gone", now, now - timedelta(days=5), state="released")
    real_session.add_all([quiet, silent, established, newest, gone])
    await real_session.commit()
    ctx = ActingContext.for_user(
        user_id=org_admin.admin_id, org_id=org_admin.org_id, email=org_admin.admin_email
    )
    binding = await resolve_machine_for(
        real_session, ctx=ctx, org_team_id=org_admin.org_id, purpose="chat"
    )
    # `newest` beat a moment ago and `established` twenty seconds ago; both are
    # answering, so the older allocation wins.
    assert binding is not None and binding.machine_id == established.id
    assert machine_state(newest, now=now) == "ready"
    assert machine_state(silent, now=now) == "unreachable"
    assert machine_state(quiet, now=now) == "starting"
    assert machine_state(gone, now=now) == "none"
    assert machine_status(newest, now=now + timedelta(minutes=5)) == "unreachable"


def test_the_placement_signature_is_the_contract() -> None:
    import inspect

    params = inspect.signature(placement.resolve_machine_for).parameters
    assert list(params) == [
        "db",
        "ctx",
        "org_team_id",
        "purpose",
        "prefer",
        "permission_mode",
        "capability",
        "owner_user_id",
        "workspace_pin",
    ]
    assert all(
        params[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in (
            "ctx",
            "org_team_id",
            "purpose",
            "prefer",
            "permission_mode",
            "capability",
            "owner_user_id",
            "workspace_pin",
        )
    )
    # A pin and its owner are optional: a caller that names neither places
    # a chat by the org's rules.
    assert params["workspace_pin"].default is None
    assert params["owner_user_id"].default is None
    # The box a chat already sits on is a hint, never a requirement.
    assert params["prefer"].default is None
    # A chat's stance is optional at the seam; a caller that omits it is treated
    # as writable, so a non-gVisor pool box is never handed a chat by default.
    assert params["permission_mode"].default is None
    # What a chat needs of a box beyond running a chat is a preference too.
    assert params["capability"].default is None
    # Only a caller placing a person's own chat names its owner; nobody else's
    # chat can reach a personal box by omission.
    assert params["owner_user_id"].default is None
    assert {f.name for f in MachineBinding.__dataclass_fields__.values()} == {
        "machine_id",
        "status",
        "name",
        "pinned",
        "wake",
    }


# --------------------------------------------------------------------------- #
# re-register: the box that came back
# --------------------------------------------------------------------------- #


async def _kill_row(machine_id: str, *, state: str = "released") -> None:
    """What the reaper does to a box it decided is gone: the row leaves the live
    set, the way the meter's stop and an explicit release both leave it."""
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.state = state
        alloc.released_at = datetime.now(UTC)
        alloc.terminated_reason = "provider_gone"
        await db.commit()


async def _org_machines(org_id: UUID) -> list[ComputeAllocation]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(ComputeAllocation)
            .where(ComputeAllocation.org_team_id == org_id)
            .order_by(ComputeAllocation.created_at)
        )
        return list(rows.scalars().all())


async def test_a_box_that_was_reaped_takes_its_own_row_back_when_it_re_registers(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The pod id is the box's identity, and a box only registers again because
    the row it had stopped answering for it (the reaper decided its pod was
    gone, an operator released it). Opening a SECOND row instead of taking the
    first one back is what strands every chat bound to the old id: no daemon
    serves a chat bound to a machine it did not register as, so the reader's
    message sits unanswered under a banner that says there is no workspace.
    """
    mt, _ = await _catalog_and_grant(real_session, org_admin, ceiling=1)
    headers = await _daemon_headers(org_admin)
    first = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert first.status_code == 201, first.text
    machine_id = first.json()["id"]
    await _kill_row(machine_id)

    again = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)

    assert again.status_code == 200, again.text
    assert again.json()["id"] == machine_id
    assert again.json()["status"] == "starting"
    assert [str(row.id) for row in await _org_machines(org_admin.org_id)] == [machine_id]
    async with AsyncSessionLocal() as db:
        row = await db.get(ComputeAllocation, UUID(machine_id))
        assert row is not None
        assert row.state == "ready"
        assert row.released_at is None and row.terminated_reason == ""


async def test_the_revived_row_is_metered_from_its_return_not_from_the_first_boot(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The dead stretch between the reap and the return is not compute anyone
    used, and the meter bills from ``last_metered_at``: leaving it at the first
    registration would charge the org for every minute the box was gone."""
    mt, _ = await _catalog_and_grant(real_session, org_admin, ceiling=1)
    headers = await _daemon_headers(org_admin)
    first = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert first.status_code == 201, first.text
    machine_id = first.json()["id"]
    booted_at = datetime.now(UTC)
    await _kill_row(machine_id)

    # Three hours later, the box is back. The credential and the grant are read
    # at the frozen moment, so it moves forward rather than starting in the past.
    returned_at = booted_at + timedelta(hours=3)
    with freeze_time(returned_at, real_asyncio=True):
        again = await client.post(
            "/api/v1/machines/register", json=_register_body(mt), headers=headers
        )

    assert again.status_code == 200, again.text
    async with AsyncSessionLocal() as db:
        row = await db.get(ComputeAllocation, UUID(machine_id))
        assert row is not None
        assert row.last_metered_at is not None
        assert row.last_metered_at - booted_at >= timedelta(hours=2, minutes=59)


async def test_a_second_pod_in_the_same_org_is_still_its_own_row(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Idempotence is keyed on the pod, not on the org: a second box the grant
    admits must not be folded onto the first one's row."""
    mt, _ = await _catalog_and_grant(real_session, org_admin, ceiling=2)
    headers = await _daemon_headers(org_admin)
    first = await client.post(
        "/api/v1/machines/register", json=_register_body(mt, pod="pod-two-a"), headers=headers
    )
    second = await client.post(
        "/api/v1/machines/register",
        json=_register_body(mt, pod="pod-two-b", name="other-box"),
        headers=headers,
    )
    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    assert first.json()["id"] != second.json()["id"]
    assert len(await _org_machines(org_admin.org_id)) == 2


async def test_a_reaped_box_that_the_grant_no_longer_admits_is_refused_not_revived(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Taking a row back is a new occupancy and goes through admission like any
    other: a box whose grant has lapsed while it was gone does not walk back
    onto the plane on the strength of a row it used to own."""
    mt, grant = await _catalog_and_grant(real_session, org_admin, ceiling=1)
    headers = await _daemon_headers(org_admin)
    first = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    machine_id = first.json()["id"]
    await _kill_row(machine_id)
    grant.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await real_session.commit()

    again = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)

    assert again.status_code == 429, again.text
    assert again.json()["error"]["code"] == "no_compute_grant"
    async with AsyncSessionLocal() as db:
        row = await db.get(ComputeAllocation, UUID(machine_id))
        assert row is not None and row.state == "released"


# --------------------------------------------------------------------------- #
# what the register route writes, and the meter pass that depends on it
# --------------------------------------------------------------------------- #


async def _allocation(session: AsyncSession, machine_id: str) -> ComputeAllocation:
    row = (
        await session.execute(
            select(ComputeAllocation).where(ComputeAllocation.id == UUID(machine_id))
        )
    ).scalar_one()
    await session.refresh(row)
    return row


async def test_a_box_registered_through_the_route_survives_a_provider_that_disowns_its_pod(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The stamp the route writes is the one the meter reads.

    A box the daemon registered is one the plane never provisioned, so the
    provider answers "no such pod" about it for ever. Only ``origin`` tells the
    meter to take the heartbeat as liveness instead; without it the box is
    reaped on the very next tick and flaps ready -> none -> ready while the
    stretch in between goes unbilled. Every other case in the tree hands the
    meter a row it seeded itself, so the route and the mechanism that depends
    on what the route writes are joined only here: registered through the real
    endpoint, then driven through a real meter pass.
    """
    mt, _ = await _catalog_and_grant(real_session, org_admin)
    headers = await _daemon_headers(org_admin)
    resp = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert resp.status_code == 201, resp.text
    row = await _allocation(real_session, resp.json()["id"])
    assert row.origin == "registered"
    anchor = row.last_metered_at
    assert anchor is not None

    # The provider a registered box meets: it never created this pod.
    provider = FakeProvider(
        status_errors={"pod-demo-1": ComputeProviderError("no such pod", status_code=404)}
    )
    for tick in (1, 2):
        at = anchor + timedelta(minutes=tick)
        row.last_heartbeat_at = at - timedelta(seconds=10)
        await real_session.commit()
        with freeze_time(at, real_asyncio=True):
            await meter_and_cutoff(real_session, provider=provider)
        row = await _allocation(real_session, resp.json()["id"])
        assert (row.state, row.terminated_reason) == ("ready", ""), f"reaped on tick {tick}"
        assert machine_status(row, now=at) == "ready"
    assert "pod-demo-1" not in provider.terminate_calls
    # Nothing was announced: the box never left ready, so there was no transition.
    assert len(await _machine_frames(resp.json()["id"])) == 1


async def test_re_registering_a_reaped_pod_puts_the_registered_stamp_back_on_it(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A box that went quiet and came back is a registered box again.

    The revive path rewrites the row rather than making a new one, so the stamp
    has to be rewritten with it — a revived row still carrying ``provisioned``
    is reaped by the very next meter pass, which is the flap this column exists
    to stop. The lapse is driven through the real meter, not hand-written.
    """
    mt, _ = await _catalog_and_grant(real_session, org_admin, ceiling=1)
    headers = await _daemon_headers(org_admin)
    first = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert first.status_code == 201, first.text
    machine_id = first.json()["id"]
    row = await _allocation(real_session, machine_id)
    anchor = row.last_metered_at
    assert anchor is not None

    provider = FakeProvider(
        status_errors={"pod-demo-1": ComputeProviderError("no such pod", status_code=404)}
    )
    lapsed = anchor + timedelta(seconds=settings.compute_heartbeat_ready_seconds + 1)
    with freeze_time(lapsed, real_asyncio=True):
        await meter_and_cutoff(real_session, provider=provider)
    row = await _allocation(real_session, machine_id)
    assert row.state == "failed"

    # Now demote the stamp the way a bad revive would, and re-register.
    row.origin = "provisioned"
    await real_session.commit()
    again = await client.post("/api/v1/machines/register", json=_register_body(mt), headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == machine_id, "the reaped row is revived, not duplicated"
    row = await _allocation(real_session, machine_id)
    assert (row.origin, row.state) == ("registered", "ready")

    # And the revived box survives the same disowning provider the first one did.
    beat_at = row.last_metered_at
    assert beat_at is not None
    at = beat_at + timedelta(minutes=1)
    row.last_heartbeat_at = at - timedelta(seconds=10)
    await real_session.commit()
    with freeze_time(at, real_asyncio=True):
        await meter_and_cutoff(real_session, provider=provider)
    row = await _allocation(real_session, machine_id)
    assert (row.state, row.terminated_reason) == ("ready", "")


# --------------------------------------------------------------------------- #
# only the current daemon process moves the row
# --------------------------------------------------------------------------- #


async def _register_as(
    client: AsyncClient, mt: Any, headers: dict[str, str], instance: str | None
) -> str:
    body: dict[str, Any] = dict(_register_body(mt))
    if instance is not None:
        body["daemon_instance_id"] = instance
    resp = await client.post("/api/v1/machines/register", json=body, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    machine_id: str = resp.json()["id"]
    return machine_id


async def test_register_records_the_daemon_instance_and_a_re_register_replaces_it(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt, _ = await _catalog_and_grant(real_session, org_admin)
    headers = await _daemon_headers(org_admin)
    machine_id = await _register_as(client, mt, headers, "proc-a")
    assert (await _allocation(real_session, machine_id)).daemon_instance_id == "proc-a"
    again = await _register_as(client, mt, headers, "proc-b")
    assert again == machine_id
    assert (await _allocation(real_session, machine_id)).daemon_instance_id == "proc-b"


async def test_a_draining_beat_from_the_previous_process_is_refused_and_changes_nothing(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The restart race: the new process registered, then the exiting one's
    last beat (saying ``draining``) lands. Without the instance check that beat
    drains the row the new process is serving from."""
    mt, _ = await _catalog_and_grant(real_session, org_admin)
    headers = await _daemon_headers(org_admin)
    machine_id = await _register_as(client, mt, headers, "proc-old")
    url = f"/api/v1/machines/{machine_id}/heartbeat"
    assert (
        await client.post(url, json={"daemon_instance_id": "proc-old"}, headers=headers)
    ).status_code == 204
    await _register_as(client, mt, headers, "proc-new")
    fresh = await client.post(url, json={"daemon_instance_id": "proc-new"}, headers=headers)
    assert fresh.status_code == 204
    before = await _allocation(real_session, machine_id)
    stamp = before.last_heartbeat_at
    frames_before = len(await _machine_frames(machine_id))

    late = await client.post(
        url, json={"daemon_instance_id": "proc-old", "draining": True}, headers=headers
    )

    assert late.status_code == 409, late.text
    assert late.json()["error"]["code"] == "machine_stale_instance"
    row = await _allocation(real_session, machine_id)
    assert row.state == "ready"
    assert machine_status(row) == "ready"
    assert row.last_heartbeat_at == stamp
    assert len(await _machine_frames(machine_id)) == frames_before


@pytest.mark.parametrize(
    ("registered", "beating"),
    [
        pytest.param("proc-a", "proc-a", id="same-process"),
        pytest.param("proc-a", None, id="older-daemon-names-none"),
        pytest.param(None, "proc-a", id="row-recorded-none"),
    ],
)
async def test_a_beat_the_instance_check_does_not_apply_to_is_taken(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    registered: str | None,
    beating: str | None,
) -> None:
    mt, _ = await _catalog_and_grant(real_session, org_admin)
    headers = await _daemon_headers(org_admin)
    machine_id = await _register_as(client, mt, headers, registered)
    body = {"draining": True} | ({"daemon_instance_id": beating} if beating else {})
    resp = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", json=body, headers=headers)
    assert resp.status_code == 204, resp.text
    assert (await _allocation(real_session, machine_id)).state == "draining"
