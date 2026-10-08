"""Compute routes through the real app: the catalog priced for the caller, the
session allocation lifecycle, refusals answered as 402/429 with a body, a
foreign allocation indistinguishable from a missing one — and every decision
on record as an ``authz.decision`` row (allows in the request transaction,
denies surviving the refused request).

The RunPod provider is substituted with an in-memory fake at the routes'
``get_provider`` seam — no network, no API key. No route returns a private key.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.compute.provider import ComputeProviderError
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from alkera_core.models.compute import ComputeAllocation
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import FakeProvider, make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.compute_rows

AUTHZ_TYPE = "authz.decision"


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr(
        "backend.api.routes.compute.compute.get_provider", lambda _machine_type: provider
    )
    return provider


async def _decisions(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == AUTHZ_TYPE,
                EventOutbox.entity == "compute_machine",
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]


async def _provision(client: AsyncClient, machine_type_id: str, **extra: Any) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/compute/allocations",
        json={"machine_type_id": machine_type_id, "project_path": "/tmp/proj", **extra},
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _granted(real_session: AsyncSession, org: OrgWithAdmin, **kw: Any) -> Any:
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org.org_id, machine_type_id=mt.id, **kw)
    return mt


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #


async def test_catalog_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/compute/catalog")
    assert resp.status_code == 401


async def test_catalog_lists_active_types_priced_for_the_caller_and_never_our_cost(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    granted = await make_machine_type(real_session)
    ungranted = await make_machine_type(real_session)
    inactive = await make_machine_type(real_session, active=False)
    await make_grant(
        real_session,
        org_team_id=org_admin.org_id,
        machine_type_id=granted.id,
        rate_per_minute_nanos=2_000_000,
    )
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/compute/catalog")
    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()}
    assert str(granted.id) in by_id and str(ungranted.id) in by_id
    assert str(inactive.id) not in by_id
    assert by_id[str(granted.id)]["rate_per_minute_nanos"] == 2_000_000
    assert by_id[str(ungranted.id)]["rate_per_minute_nanos"] is None
    row = by_id[str(granted.id)]
    assert row["provider"] == "runpod" and row["compute_class"] == "cpu"
    # The provider's price — our cost — is on no key of a tenant response.
    for item in resp.json():
        assert not [k for k in item if "cost" in k or "provider_price" in k], item
    rows = await _decisions(org_admin.org_id)
    assert _effects(rows) == [("allow", "member_read")]
    assert rows[0].visibility == "platform"
    assert rows[0].payload["policy"] == "compute.machine"
    assert rows[0].payload["attrs"] == {
        "email_verified": True,
        "in_org": True,
        "is_owner": True,
        "lifecycle": "",
        "controls": False,
        "roles": ["admin", "member", "owner", "viewer"],
    }


# --------------------------------------------------------------------------- #
# allocations
# --------------------------------------------------------------------------- #


async def test_provision_happy_path_pins_the_rate_and_records_the_allow(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin, rate_per_minute_nanos=3_000_000)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = await _provision(client, str(mt.id))
    assert body["state"] == "provisioning"
    assert body["lifecycle"] == "session"
    assert body["provider_machine_id"] == "fakepod-1"
    assert body["machine_type"]["id"] == str(mt.id)
    assert body["machine_type"]["rate_per_minute_nanos"] == 3_000_000
    assert body["ssh_user"] == "root"
    assert body["project_path"] == "/tmp/proj"
    assert body["spend_nanos"] == 0
    assert body["machine_status"] is None
    assert "ssh_private_key" not in str(body)
    # The server minted an ephemeral ed25519 key and sent the PUBLIC half out.
    assert str(fake_provider.create_calls[0]["ssh_public_key"]).startswith("ssh-ed25519 ")
    row = (
        await real_session.execute(
            select(ComputeAllocation).where(ComputeAllocation.id == UUID(body["id"]))
        )
    ).scalar_one()
    assert row.price_per_minute_nanos == 3_000_000
    assert row.true_cost_per_minute_nanos == mt.provider_price_per_minute_nanos
    assert row.grant_id is not None
    assert row.ssh_private_key_enc.startswith("gAAAA")
    rows = await _decisions(org_admin.org_id)
    # Starting a session allocation decides whether it exists, so it comes in
    # under the branch that names that — for a session allocation, the member's
    # own business.
    assert _effects(rows) == [("allow", "member_controls_own_allocation")]
    assert rows[0].payload["resource"] == {
        "type": "compute_machine",
        "id": str(mt.id),
        "team_id": None,
    }
    assert rows[0].payload["attrs"]["lifecycle"] == "session"


@pytest.mark.parametrize(
    "machine_type_id",
    [
        pytest.param("00000000-0000-0000-0000-000000000000", id="unknown-uuid"),
        pytest.param("not-a-uuid", id="malformed-uuid"),
    ],
)
async def test_provision_unknown_machine_type_404(
    client: AsyncClient, org_admin: OrgWithAdmin, fake_provider: FakeProvider, machine_type_id: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(
        "/api/v1/compute/allocations", json={"machine_type_id": machine_type_id}
    )
    assert resp.status_code == 404
    assert fake_provider.create_calls == []


async def test_provision_inactive_machine_type_404(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await make_machine_type(real_session, active=False)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)})
    assert resp.status_code == 404


async def test_provision_out_of_stock_type_409(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await make_machine_type(real_session, available_for_new=False)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)})
    assert resp.status_code == 409
    assert "not currently available" in resp.json()["error"]["message"]
    assert fake_provider.create_calls == []


async def test_provision_without_a_grant_is_a_visible_429(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await make_machine_type(real_session)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)})
    assert resp.status_code == 429, resp.text
    error = resp.json()["error"]
    assert (error["code"], error["message"]) == (
        "no_compute_grant",
        "No compute grant admits this machine type for your team.",
    )
    assert fake_provider.create_calls == []
    async with AsyncSessionLocal() as session:
        frames = (
            (
                await session.execute(
                    select(EventOutbox).where(
                        EventOutbox.org_id == org_admin.org_id,
                        EventOutbox.type == "compute_machine.changed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [f.payload for f in frames] == [{"status": "refused", "reason": "no_compute_grant"}]
    # No allocation row was left behind by the refused request.
    assert (
        await real_session.execute(
            select(ComputeAllocation).where(ComputeAllocation.org_team_id == org_admin.org_id)
        )
    ).scalars().all() == []


async def test_provision_over_the_ceiling_is_a_visible_429(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin, ceiling=1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await _provision(client, str(mt.id))
    resp = await client.post("/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)})
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "compute_limit_reached"
    assert "1/1" in resp.json()["error"]["message"]
    assert len(fake_provider.create_calls) == 1


async def test_provision_without_credit_is_a_visible_402(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin, rate_per_minute_nanos=10**15)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)})
    assert resp.status_code == 402, resp.text
    assert resp.json()["error"]["code"] == "insufficient_credit"
    assert fake_provider.create_calls == []


async def test_provider_failure_502_and_failed_row_persisted(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    fake_provider.create_error = "no capacity in region"
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)})
    assert resp.status_code == 502
    listing = await client.get("/api/v1/compute/allocations")
    assert [a["state"] for a in listing.json()] == ["failed"]
    assert "no capacity" in listing.json()[0]["error"]
    active = await client.get("/api/v1/compute/allocations", params={"active": "true"})
    assert active.json() == []
    # A failed row no longer holds the ceiling.
    fake_provider.create_error = None
    await _provision(client, str(mt.id))


#: What a provider's failure text can carry: a runtime's stderr, an instance id
#: and the host it called. None of it may reach the caller.
_PROVIDER_LEAK = (
    "docker: Error response from daemon: stderr: conflict on i-0abc123def4567890 "
    "calling https://ec2.us-east-1.amazonaws.com/?Action=RunInstances"
)
_LEAK_FRAGMENTS = ("stderr", "i-0abc123def4567890", "ec2.us-east-1.amazonaws.com", "https://")


@pytest.mark.parametrize("operation", [pytest.param("create"), pytest.param("terminate")])
async def test_a_provider_failure_answers_a_coded_502_without_the_providers_text(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
    operation: str,
) -> None:
    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    if operation == "create":
        fake_provider.create_error = _PROVIDER_LEAK
        resp = await client.post(
            "/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)}
        )
    else:
        created = await _provision(client, str(mt.id))
        fake_provider.terminate_error = ComputeProviderError(_PROVIDER_LEAK, status_code=500)
        resp = await client.delete(f"/api/v1/compute/allocations/{created['id']}")
    assert resp.status_code == 502, resp.text
    error = resp.json()["error"]
    assert error["code"] == "provider_error"
    # The caller learns which side failed; the provider's own words stay in the log.
    assert "compute provider" in error["message"]
    for fragment in _LEAK_FRAGMENTS:
        assert fragment not in resp.text


async def test_status_refresh_transitions_to_ready(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _provision(client, str(mt.id))
    resp = await client.get(f"/api/v1/compute/allocations/{created['id']}")
    assert resp.json()["state"] == "provisioning"
    assert resp.json()["ready_at"] is None
    fake_provider.mark_ready(created["provider_machine_id"], ip="198.51.100.9", port=22022)
    resp = await client.get(f"/api/v1/compute/allocations/{created['id']}")
    body = resp.json()
    assert body["state"] == "ready"
    assert body["public_ip"] == "198.51.100.9"
    assert body["ssh_port"] == 22022
    assert body["ready_at"] is not None
    assert body["status_message"].startswith("Running")


async def test_active_filter_excludes_released_and_release_is_announced(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin, ceiling=2)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    keep = await _provision(client, str(mt.id))
    kill = await _provision(client, str(mt.id))
    resp = await client.delete(f"/api/v1/compute/allocations/{kill['id']}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "released"
    assert resp.json()["released_at"] is not None
    assert resp.json()["terminated_reason"] == "user_released"
    active = await client.get("/api/v1/compute/allocations", params={"active": "true"})
    assert [a["id"] for a in active.json()] == [keep["id"]]
    everything = await client.get("/api/v1/compute/allocations")
    assert len(everything.json()) == 2
    async with AsyncSessionLocal() as session:
        frames = (
            (
                await session.execute(
                    select(EventOutbox).where(
                        EventOutbox.type == "compute_machine.changed",
                        EventOutbox.entity_id == kill["id"],
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [f.payload for f in frames] == [{"status": "none", "reason": "user_released"}]
    assert frames[0].actor["acting"]["id"] == str(org_admin.admin_id)


async def test_terminate_is_idempotent(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _provision(client, str(mt.id))
    first = await client.delete(f"/api/v1/compute/allocations/{created['id']}")
    second = await client.delete(f"/api/v1/compute/allocations/{created['id']}")
    assert first.status_code == second.status_code == 200
    assert second.json()["state"] == "released"
    assert fake_provider.terminate_calls == [created["provider_machine_id"]]


async def test_a_foreign_allocation_is_indistinguishable_from_missing_and_the_deny_is_recorded(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _provision(client, str(mt.id))
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await login(client, member.email, password or "")
    before = len(await _decisions(org_admin.org_id))
    for method, url in [
        ("GET", f"/api/v1/compute/allocations/{created['id']}"),
        ("DELETE", f"/api/v1/compute/allocations/{created['id']}"),
        ("POST", f"/api/v1/compute/allocations/{created['id']}/renew"),
        ("GET", f"/api/v1/compute/allocations/{UUID(int=0)}"),
    ]:
        resp = await client.request(method, url, json={} if method == "POST" else None)
        assert resp.status_code == 404, (method, url, resp.text)
        assert resp.json()["error"]["message"] == "Machine not found"
    rows = (await _decisions(org_admin.org_id))[before:]
    assert _effects(rows) == [("deny", "not_owner")] * 4
    assert all(r.payload["attrs"]["is_owner"] is False for r in rows)
    assert all(r.actor["acting"]["id"] == str(member.id) for r in rows)
    # The owner still sees it.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"/api/v1/compute/allocations/{created['id']}")).status_code == 200


async def test_an_unverified_member_may_read_but_not_start(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=False)
    await login(client, member.email, password or "")
    assert (await client.get("/api/v1/compute/catalog")).status_code == 200
    resp = await client.post("/api/v1/compute/allocations", json={"machine_type_id": str(mt.id)})
    assert resp.status_code == 403
    error = resp.json()["error"]
    assert (error["code"], error["message"]) == (
        "email_verification_required",
        "Verify your email address to perform this action.",
    )
    assert fake_provider.create_calls == []
    rows = await _decisions(org_admin.org_id)
    assert _effects(rows) == [("allow", "member_read"), ("deny", "email_verification_required")]


async def test_provision_without_max_minutes_has_no_time_cap(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = await _provision(client, str(mt.id))
    assert body["max_lease_minutes"] is None


async def test_provision_with_max_minutes_sets_the_lease(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = await _provision(client, str(mt.id), max_minutes=30)
    assert body["max_lease_minutes"] == 30


async def test_renew_route_sets_then_clears_the_lease(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:
    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _provision(client, str(mt.id))
    resp = await client.post(
        f"/api/v1/compute/allocations/{created['id']}/renew", json={"max_minutes": 45}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_lease_minutes"] == 45
    resp = await client.post(f"/api/v1/compute/allocations/{created['id']}/renew", json={})
    assert resp.status_code == 200
    assert resp.json()["max_lease_minutes"] is None
    row = (
        await real_session.execute(
            select(ComputeAllocation).where(ComputeAllocation.id == UUID(created["id"]))
        )
    ).scalar_one()
    assert row.max_lease_minutes is None


async def test_no_route_returns_the_private_key(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fake_provider: FakeProvider,
) -> None:

    mt = await _granted(real_session, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _provision(client, str(mt.id))
    resp = await client.get(f"/api/v1/compute/allocations/{created['id']}/ssh-key")
    assert resp.status_code in (404, 405)
    for url in (
        f"/api/v1/compute/allocations/{created['id']}",
        "/api/v1/compute/allocations",
    ):
        text = (await client.get(url)).text
        assert "PRIVATE KEY" not in text and "ssh_private_key" not in text
