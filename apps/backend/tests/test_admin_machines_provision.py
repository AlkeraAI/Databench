"""Machines the platform starts from the console, through the real routes.

The provider is the scripted in-memory node provider behind the one seam the
service reads (``provisioning.make_node_provider``); everything else — the
credential, the row, its history, the authorization decision — is the real
thing against Postgres.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alkera_core.auth import InvalidTokenError, decode_session_token
from alkera_core.auth.machine_token import (
    MACHINE_TOKEN_PREFIX,
    looks_like_machine_token,
    machine_credential_headers,
)
from alkera_core.compute import availability as availability_core
from alkera_core.compute.box_contract import ReleaseHostError
from alkera_core.compute.liveness import (
    ENV_CHAT_IDLE_MINUTES,
    ENV_CHAT_MEMORY_PRESSURE_PERCENT,
    ENV_DRAIN_CEILING_SECONDS,
    unit_stop_timeout_seconds,
)
from alkera_core.compute.provider import EC2, GONE, RUNPOD
from alkera_core.compute.transitions import transition
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    AuthToken,
    EventOutbox,
    MachineCredential,
    OrgComputeAssignment,
    TokenType,
)
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from alkera_test_support.compute.fake_release_host import FAKE_STABLE_VERSION, FakeReleaseHost
from backend.services.compute import provisioning
from backend.services.compute.machine_credential import NODE_CREDENTIAL_KEY
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, login

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.compute_rows,
    # A release the provider did not confirm is finished by a real reconcile pass.
    pytest.mark.xdist_group("compute-fleet"),
]

MACHINES = "/admin/v1/machines"

#: Every field the dedicated-box picker reads off a row today.
PICKER_FIELDS = {
    "credential_id",
    "label",
    "provider",
    "instance_type",
    "region",
    "tenancy",
    "created_at",
    "revoked_at",
    "last_used_at",
    "machine_id",
    "machine_name",
    "status",
    "last_heartbeat_at",
    "chats_served",
    "capacity",
    "daemon_version",
    "true_cost_per_minute_nanos",
    "assigned_org_id",
    "assigned_org_name",
}
ROW_FIELDS = {
    "id",
    "name",
    "provider_machine_id",
    "machine_type_code",
    "state",
    "liveness",
    "dedicated_org",
    "storage_gb",
    "heartbeat_at",
    "state_changed_at",
    "drain",
    "cost",
    "resources",
    "origin",
}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNodeProvider]:
    provider = FakeNodeProvider(kind=EC2)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: provider)
    availability_core.CACHE.clear()
    yield provider
    availability_core.CACHE.clear()


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(delete(MachineCredential))
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.state.not_in(("released", "failed")))
        .values(state="released")
    )
    await real_session.commit()


async def _ec2_type(session: AsyncSession) -> ComputeMachineType:
    return await make_machine_type(session, provider=EC2, provider_price_per_minute_nanos=3_200_000)


def _body(mt: ComputeMachineType, **over: Any) -> dict[str, Any]:
    return {
        "provider": "ec2",
        "machine_type_code": mt.provider_type_id,
        "storage_gb": 100,
        "tenancy": "pool",
        "name": "pool-a",
        **over,
    }


async def _decisions(org_id: UUID) -> list[tuple[str, str, str]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "platform_machine",
            )
            .order_by(EventOutbox.id)
        )
        return [
            (r.payload["effect"], r.payload["reason"], r.payload["attrs"]["operation"])
            for r in rows.scalars().all()
        ]


async def _provision(client: AsyncClient, admin: OrgWithAdmin, mt: ComputeMachineType) -> dict:
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.post(f"{MACHINES}/provision", json=_body(mt))
    assert resp.status_code == 202, resp.text
    return dict(resp.json())


def _box_headers(secrets: dict[str, str]) -> dict[str, str]:
    """What the node sends on its own calls: the credential as the bearer and,
    on the claim, in its own header too. No agent assertion — the box has no
    machine to name until the claim answers."""
    secret = secrets[NODE_CREDENTIAL_KEY]
    return {"Authorization": f"Bearer {secret}", **machine_credential_headers(secret)}


async def _cli_logins(user_id: UUID) -> int:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(AuthToken).where(
                AuthToken.user_id == user_id, AuthToken.token_type == TokenType.CLI
            )
        )
        return len(rows.scalars().all())


async def _make_ready(machine_id: str, *, chats: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        transition(session, alloc, "bootstrapping")
        transition(session, alloc, "ready")
        alloc.chats_served = chats
        await session.commit()


async def test_an_admin_provisions_a_machine_that_boots_with_its_credential_alone(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    assert row["state"] == "provisioning"
    assert row["origin"] == "provisioned"
    assert (row["provider"], row["machine_type_code"], row["storage_gb"]) == (
        "ec2",
        mt.provider_type_id,
        100,
    )
    (machine_id,) = fake.nodes
    assert row["provider_machine_id"] == machine_id
    launched = fake.nodes[machine_id].launch
    assert launched.storage_gb == 100
    assert launched.tags["alkera:allocation_id"] == row["id"]
    assert "ALKERA_MACHINE_NAME=pool-a" in launched.script
    # The node's secret was stored before it ran: the credential, and only it.
    secrets = fake.secrets[UUID(row["id"])]
    assert set(secrets) == {NODE_CREDENTIAL_KEY}
    assert secrets[NODE_CREDENTIAL_KEY].startswith(MACHINE_TOKEN_PREFIX)
    for value in secrets.values():
        assert value not in launched.script
    async with AsyncSessionLocal() as session:
        credential = (
            await session.execute(
                select(MachineCredential).where(MachineCredential.machine_id == UUID(row["id"]))
            )
        ).scalar_one()
        assert credential.revoked_at is None
    detail = (await client.get(f"{MACHINES}/{row['id']}")).json()
    assert [(e["from_state"], e["to_state"]) for e in detail["events"]] == [
        ("pending", "provisioning")
    ]
    assert detail["events"][0]["actor"]["kind"] == "admin"
    assert detail["events"][0]["actor"]["email"] == platform_admin.admin_email
    assert ("allow", "admin_changes", "provision") in await _decisions(platform_admin.org_id)


async def test_the_node_is_told_the_platforms_drain_ceiling_and_its_unit_stops_past_it(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment's drain ceiling reaches the node twice from one number:
    in its environment, where the daemon drains by it, and in its service
    unit's stop timeout, which must be finite and past it — an unbounded one let
    ``systemctl restart`` hold a pool node's chats for as long as anybody
    watched."""
    monkeypatch.setattr(provisioning.settings, "compute_drain_ceiling_seconds", 900)
    mt = await _ec2_type(real_session)
    await _provision(client, platform_admin, mt)
    (machine_id,) = fake.nodes
    script = fake.nodes[machine_id].launch.script
    assert f"{ENV_DRAIN_CEILING_SECONDS}=900\n" in script
    assert f"TimeoutStopSec={unit_stop_timeout_seconds(900)}\n" in script
    assert "TimeoutStopSec=infinity" not in script


async def test_the_node_is_told_the_platforms_sleep_policy(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment's chat idle window and memory pressure line reach the
    node it provisions, so a deployment sets them once for every box."""
    monkeypatch.setattr(provisioning.settings, "compute_chat_idle_minutes", 240)
    monkeypatch.setattr(provisioning.settings, "compute_chat_memory_pressure_percent", 75)
    mt = await _ec2_type(real_session)
    await _provision(client, platform_admin, mt)
    (machine_id,) = fake.nodes
    script = fake.nodes[machine_id].launch.script
    assert f"{ENV_CHAT_IDLE_MINUTES}=240\n" in script
    assert f"{ENV_CHAT_MEMORY_PRESSURE_PERCENT}=75\n" in script


@pytest.mark.parametrize(
    ("channel", "installed", "mode"),
    [
        pytest.param(
            "staging",
            "0.5.0-g951dac943d65",
            "single",
            id="the-environments-channel-on-a-build-without-a-manifest",
        ),
        pytest.param("", FAKE_STABLE_VERSION, "supervise", id="no-channel-is-the-stable-release"),
    ],
)
async def test_a_new_node_installs_from_the_environments_node_channel(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    release_host: FakeReleaseHost,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    installed: str,
    mode: str,
) -> None:
    """A node started between two deploys runs the build the last deploy
    rolled the others onto, in the mode that build has: the backend
    resolves the deployment's channel before it renders the bootstrap."""
    release_host.documents["cli/nodes/staging.json"] = {"version": "0.5.0-g951dac943d65"}
    monkeypatch.setattr(provisioning.settings, "alkera_node_release_channel", channel)
    monkeypatch.setattr(provisioning.settings, "alkera_node_daemon_version", "")
    mt = await _ec2_type(real_session)
    await _provision(client, platform_admin, mt)
    (machine_id,) = fake.nodes
    script = fake.nodes[machine_id].launch.script
    assert f"DAEMON_VERSION={installed}\n" in script
    assert "ExecStart=$BOX_ROOT/alkera.dist/alkera cloud-mirror run\n" in script
    assert f"ALKERA_BOX_START_MODE={mode}\n" in script


async def test_a_release_host_that_cannot_answer_starts_nothing(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    release_host: FakeReleaseHost,
) -> None:
    release_host.documents["cli/stable.json"] = ReleaseHostError("timed out")
    mt = await _ec2_type(real_session)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(f"{MACHINES}/provision", json=_body(mt))
    assert resp.status_code == 502
    assert fake.nodes == {}


async def test_support_may_not_provision_and_nothing_is_started(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(f"{MACHINES}/provision", json=_body(mt))
    assert resp.status_code == 403
    assert fake.nodes == {}
    assert await _decisions(platform_support.org_id) == [
        ("deny", "platform_admin_required", "provision")
    ]


async def test_a_tenant_admin_cannot_reach_any_machine_route(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get("/admin/v1/machine-types")).status_code == 403
    assert (await client.post(f"{MACHINES}/provision", json={})).status_code in (403, 422)


@pytest.mark.parametrize(
    ("setup", "status", "code"),
    [
        pytest.param("unconfigured", 409, "provider_not_configured", id="unconfigured"),
        pytest.param("unknown-type", 404, "machine_type_not_found", id="unknown-type"),
        pytest.param("dedicated-no-org", 400, "org_required", id="dedicated-no-org"),
    ],
)
async def test_a_provision_the_plane_refuses_starts_nothing(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    setup: str,
    status: int,
    code: str,
) -> None:
    mt = await _ec2_type(real_session)
    body = _body(mt)
    if setup == "unconfigured":
        fake.is_configured = False
    elif setup == "unknown-type":
        body["machine_type_code"] = "x9.huge"
    else:
        body["tenancy"] = "dedicated"
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(f"{MACHINES}/provision", json=body)
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code
    assert fake.nodes == {}


async def test_a_provider_refusal_fails_the_row_and_takes_the_secrets_back(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    fake.fail_run = "InsufficientInstanceCapacity"
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(f"{MACHINES}/provision", json=_body(mt))
    assert resp.status_code == 502
    # The provider's reason is the answer, not the generic server sentence.
    assert resp.json()["error"]["code"] == "provider_error"
    assert resp.json()["error"]["message"] == "The provider refused: InsufficientInstanceCapacity"
    assert fake.secrets == {}
    async with AsyncSessionLocal() as session:
        alloc = (
            await session.execute(
                select(ComputeAllocation).where(ComputeAllocation.machine_type_id == mt.id)
            )
        ).scalar_one()
        assert alloc.state == "failed"
        assert "InsufficientInstanceCapacity" in alloc.error
        credential = (
            await session.execute(
                select(MachineCredential).where(MachineCredential.machine_id == alloc.id)
            )
        ).scalar_one()
        assert credential.revoked_at is not None


async def test_a_dedicated_machine_is_assigned_to_its_org(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(
        f"{MACHINES}/provision",
        json=_body(mt, tenancy="dedicated", org_id=str(org_admin.org_id)),
    )
    assert resp.status_code == 202, resp.text
    row = resp.json()
    assert row["tenancy"] == "dedicated"
    assert row["dedicated_org"]["id"] == str(org_admin.org_id)
    again = await client.post(
        f"{MACHINES}/provision",
        json=_body(mt, tenancy="dedicated", org_id=str(org_admin.org_id)),
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "org_has_machine"
    # The box is that org's for life: let go of, it still goes to nobody else.
    dedicated = "/admin/v1/orgs/{}/compute/dedicated"
    await _make_ready(row["id"])
    let_go = await client.put(dedicated.format(org_admin.org_id), json={"machine_id": None})
    assert let_go.status_code == 200
    elsewhere = await client.put(
        dedicated.format(platform_admin.org_id), json={"machine_id": row["id"]}
    )
    assert elsewhere.status_code == 409
    assert elsewhere.json()["error"]["code"] == "machine_held_another_org"


async def test_drain_undrain_and_the_edges_they_refuse(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    url = f"{MACHINES}/{row['id']}"
    early = await client.post(f"{url}/drain", json={"reason": "resize"})
    assert early.status_code == 409  # still provisioning
    await _make_ready(row["id"])
    drained = await client.post(f"{url}/drain", json={"reason": "resize"})
    assert drained.status_code == 200, drained.text
    assert drained.json()["state"] == "draining"
    assert drained.json()["drain"]["reason"] == "resize"
    assert (await client.post(f"{url}/drain", json={})).status_code == 409
    back = await client.post(f"{url}/undrain")
    assert back.status_code == 200
    assert (back.json()["state"], back.json()["drain"]) == ("ready", None)
    assert (await client.post(f"{url}/undrain")).status_code == 409
    # A refused edge rolls its allow back with it: only the moves are on record.
    ops = [d[2] for d in await _decisions(platform_admin.org_id)]
    assert ops == ["provision", "drain", "undrain"]


async def test_terminate_refuses_a_machine_with_chats_unless_forced(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _make_ready(row["id"], chats=2)
    url = f"{MACHINES}/{row['id']}/terminate"
    refused = await client.post(url, json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "machine_has_chats"
    assert (await fake.describe(row["provider_machine_id"])).phase != "gone"
    forced = await client.post(url, json={"force": True})
    assert forced.status_code == 202, forced.text
    assert forced.json()["state"] == "released"
    assert (await fake.describe(row["provider_machine_id"])).phase == "gone"
    assert (await client.post(url, json={"force": True})).status_code == 409


async def _as_box(client: AsyncClient, secret: str) -> int:
    """The node on its credential, at a door that admits a machine: its own
    chat listing."""
    resp = await client.get("/api/v1/chats", headers={"Authorization": f"Bearer {secret}"})
    return resp.status_code


async def test_a_terminated_machine_reads_released_at_once_with_nothing_left_it_could_use(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    """The provider confirmed the terminate, so the release finishes in the
    same request: the very next read says ``released`` (no reconcile pass in
    between), the node's secret is gone, and its machine credential is not
    accepted any more."""
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _make_ready(row["id"])
    secret = fake.secrets[UUID(row["id"])][NODE_CREDENTIAL_KEY]
    client.cookies.clear()
    assert await _as_box(client, secret) == 200, "the node's credential works while it runs"
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    resp = await client.post(f"{MACHINES}/{row['id']}/terminate", json={})

    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "released"
    detail = (await client.get(f"{MACHINES}/{row['id']}")).json()
    assert detail["state"] == "released"
    edges = [(e["from_state"], e["to_state"]) for e in detail["events"]]
    assert ("ready", "releasing") in edges and ("releasing", "released") in edges
    assert UUID(row["id"]) not in fake.secrets, "the node secret outlived the release"
    async with AsyncSessionLocal() as session:
        credential = (
            await session.execute(
                select(MachineCredential).where(MachineCredential.machine_id == UUID(row["id"]))
            )
        ).scalar_one()
        assert credential.revoked_at is not None
    client.cookies.clear()
    assert await _as_box(client, secret) == 401, "the node's credential survived the release"


class _Lingering(FakeNodeProvider):
    """A provider that accepts the terminate and still reports the machine."""

    async def terminate(self, machine_id: str) -> None:
        return None


async def test_a_machine_the_provider_still_reports_stays_releasing_for_the_reconcile(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Released is said only once the provider confirms it: a machine still
    running after the terminate stays ``releasing`` (its credentials already
    revoked), and the reconcile pass finishes it once the provider lets go."""
    from alkera_core.compute.node_reconcile import ProviderErrorLog, reconcile_nodes
    from alkera_core.compute.provider import GONE, RUNNING

    lingering = _Lingering(kind=EC2)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: lingering)
    availability_core.CACHE.clear()
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _make_ready(row["id"])
    lingering.script(row["provider_machine_id"], RUNNING)

    resp = await client.post(f"{MACHINES}/{row['id']}/terminate", json={})

    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "releasing"
    lingering.script(row["provider_machine_id"], GONE)
    async with AsyncSessionLocal() as session:
        await reconcile_nodes(session, providers=lambda _k: lingering, errors=ProviderErrorLog())
    assert (await client.get(f"{MACHINES}/{row['id']}")).json()["state"] == "released"


async def test_a_terminate_the_provider_refuses_is_left_releasing_not_reported_gone(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _make_ready(row["id"])
    fake.fail_terminate.add(row["provider_machine_id"])

    resp = await client.post(f"{MACHINES}/{row['id']}/terminate", json={})

    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "releasing"


async def test_every_row_carries_the_picker_fields_and_the_machine_fields(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    provisioned = await _provision(client, platform_admin, mt)
    minted = await client.post(
        MACHINES,
        json={
            "label": "by-hand",
            "provider": "ec2",
            "instance_type": mt.provider_type_id,
            "tenancy": "pool",
        },
    )
    assert minted.status_code == 201
    items = (await client.get(MACHINES)).json()["items"]
    assert len(items) == 2
    for item in items:
        assert PICKER_FIELDS | ROW_FIELDS <= set(item)
    by_origin = {item["origin"]: item for item in items}
    assert by_origin["provisioned"]["id"] == provisioned["id"]
    assert by_origin["provisioned"]["machine_id"] == provisioned["id"]
    # A credential nothing claimed yet is a row of its own, named by the credential.
    hand = by_origin["registered"]
    assert hand["machine_id"] is None
    assert hand["id"] == hand["credential_id"]
    assert hand["label"] == "by-hand"


async def test_machine_types_report_whether_this_deployment_can_start_them(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    items = (await client.get("/admin/v1/machine-types")).json()["items"]
    (row,) = [i for i in items if i["code"] == mt.provider_type_id]
    assert row["available"] is True
    assert row["price_per_minute_nanos"] == 3_200_000
    assert row["storage"]["kind"] == "gp3"
    fake.is_configured = False
    items = (await client.get("/admin/v1/machine-types")).json()["items"]
    (row,) = [i for i in items if i["code"] == mt.provider_type_id]
    assert row["available"] is False


async def _verified(user_id: UUID) -> None:
    from datetime import UTC, datetime

    from alkera_core.models import User

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.email_verified_at = datetime.now(UTC)
        await session.commit()


@pytest.mark.parametrize("stage", ["provisioning", "bootstrapping"])
async def test_the_node_claims_with_the_secrets_it_was_given_and_becomes_ready(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    stage: str,
) -> None:
    """The credential the node was handed is what works: the claim is made
    with it and nothing else, lands on the provisioned row (never a second
    one) and is what makes it ready."""
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _verified(platform_admin.admin_id)
    if stage == "bootstrapping":
        async with AsyncSessionLocal() as session:
            alloc = await session.get(ComputeAllocation, UUID(row["id"]))
            assert alloc is not None
            transition(session, alloc, "bootstrapping")
            await session.commit()
    headers = _box_headers(fake.secrets[UUID(row["id"])])
    claim = await client.post(
        "/api/v1/machines/claim",
        json={
            "provider_pod_id": row["provider_machine_id"],
            "name": "pool-a",
            "capacity": 8,
            "daemon_version": "1.4.2",
        },
        headers=headers,
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["id"] == row["id"]
    detail = (await client.get(f"{MACHINES}/{row['id']}")).json()
    assert detail["state"] == "ready"
    assert detail["daemon_version"] == "1.4.2"
    assert detail["events"][0]["to_state"] == "ready"
    assert detail["events"][0]["actor"]["kind"] == "box"


@pytest.mark.parametrize("stage", ["provisioning", "bootstrapping"])
async def test_claim_below_minimum_refused(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    stage: str,
) -> None:
    """A box that came up on a build older than the backend serves is refused
    with a stable code, and its machine fails at once with words the console
    can show, instead of waiting out the boot timeout: the provider machine
    is released and the credential stops working."""
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _verified(platform_admin.admin_id)
    if stage == "bootstrapping":
        async with AsyncSessionLocal() as session:
            alloc = await session.get(ComputeAllocation, UUID(row["id"]))
            assert alloc is not None
            transition(session, alloc, "bootstrapping")
            await session.commit()
    headers = _box_headers(fake.secrets[UUID(row["id"])])
    body = {
        "provider_pod_id": row["provider_machine_id"],
        "name": "pool-a",
        "capacity": 8,
        "daemon_version": "0.4.9 (build 1234abcd)",
    }
    claim = await client.post("/api/v1/machines/claim", json=body, headers=headers)
    assert claim.status_code == 409, claim.text
    assert claim.json()["error"]["code"] == "box_too_old"
    assert claim.json()["error"]["message"] == (
        "This machine's software is too old. Replace it to update."
    )
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(row["id"]))
        assert alloc is not None
        assert (alloc.state, alloc.terminated_reason, alloc.error) == (
            "failed",
            "box_too_old",
            "This machine's software is too old. Replace it to update.",
        )
    assert fake.nodes[row["provider_machine_id"]].phase == GONE
    # The credential went with the machine: a newer box cannot bring it back.
    again = await client.post(
        "/api/v1/machines/claim", json={**body, "daemon_version": "0.5.0"}, headers=headers
    )
    assert again.status_code in (401, 403, 404, 409)
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(row["id"]))
        assert alloc is not None
        assert alloc.state == "failed"


async def test_a_heartbeat_below_minimum_is_refused_and_changes_nothing(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _verified(platform_admin.admin_id)
    headers = _box_headers(fake.secrets[UUID(row["id"])])
    body = {"provider_pod_id": row["provider_machine_id"], "name": "pool-a", "capacity": 8}
    claimed = await client.post(
        "/api/v1/machines/claim", json={**body, "daemon_version": "0.5.0"}, headers=headers
    )
    assert claimed.status_code == 200, claimed.text
    beat_url = f"/api/v1/machines/{row['id']}/heartbeat"
    async with AsyncSessionLocal() as session:
        before = await session.get(ComputeAllocation, UUID(row["id"]))
        assert before is not None
        stamped = before.last_heartbeat_at
    refused = await client.post(
        beat_url, json={"chats_served": 2, "daemon_version": "0.4.0"}, headers=headers
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "box_too_old"
    async with AsyncSessionLocal() as session:
        after = await session.get(ComputeAllocation, UUID(row["id"]))
        assert after is not None
        assert (after.last_heartbeat_at, after.chats_served, after.daemon_version) == (
            stamped,
            0,
            "0.5.0",
        )
    accepted = await client.post(
        beat_url, json={"chats_served": 2, "daemon_version": "0.5.0"}, headers=headers
    )
    assert accepted.status_code == 204


async def test_a_daemon_restart_does_not_lift_an_operators_drain(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _verified(platform_admin.admin_id)
    headers = _box_headers(fake.secrets[UUID(row["id"])])
    body = {"provider_pod_id": row["provider_machine_id"], "name": "pool-a", "capacity": 8}
    assert (
        await client.post("/api/v1/machines/claim", json=body, headers=headers)
    ).status_code == 200
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (await client.post(f"{MACHINES}/{row['id']}/drain", json={})).status_code == 200
    again = await client.post("/api/v1/machines/claim", json=body, headers=headers)
    assert again.status_code == 200
    assert (await client.get(f"{MACHINES}/{row['id']}")).json()["state"] == "draining"


async def test_the_heartbeat_resources_reach_the_machine_row_and_an_older_beat_keeps_them(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _verified(platform_admin.admin_id)
    headers = _box_headers(fake.secrets[UUID(row["id"])])
    body = {"provider_pod_id": row["provider_machine_id"], "name": "pool-a", "capacity": 8}
    assert (
        await client.post("/api/v1/machines/claim", json=body, headers=headers)
    ).status_code == 200
    sample = {
        "cpu_percent": 37.5,
        "memory_used_bytes": 3 << 30,
        "memory_limit_bytes": 8 << 30,
        "disk_used_bytes": 20 << 30,
        "disk_total_bytes": 100 << 30,
        "org_workers": 3,
        "org_worker_capacity": 10,
        "org_worker_memory_bytes": 1 << 30,
        "org_workers_failing": 1,
        "org_slots_free": 4000,
    }
    beat_url = f"/api/v1/machines/{row['id']}/heartbeat"
    assert (
        await client.post(beat_url, json={"chats_served": 1, "resources": sample}, headers=headers)
    ).status_code == 204
    assert (
        await client.post(beat_url, json={"chats_served": 1}, headers=headers)
    ).status_code == 204
    bad = {**sample, "cpu_percent": -1}
    assert (
        await client.post(beat_url, json={"resources": bad}, headers=headers)
    ).status_code == 422
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    detail = (await client.get(f"{MACHINES}/{row['id']}")).json()
    assert detail["resources"] == sample


@pytest.mark.parametrize("status", ["available", "limited", "unavailable"])
async def test_machine_types_carry_the_providers_live_answer(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    status: str,
) -> None:
    mt = await _ec2_type(real_session)
    fake.stock[mt.provider_type_id] = status
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    items = (await client.get("/admin/v1/machine-types")).json()["items"]
    (row,) = [i for i in items if i["code"] == mt.provider_type_id]
    assert row["availability"]["status"] == status
    assert row["availability"]["checked_at"]
    assert row["available"] is (status != "unavailable")


async def test_a_provider_slower_than_the_budget_reads_unknown(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mt = await _ec2_type(real_session)
    fake.stock[mt.provider_type_id] = "available"
    fake.probe_delay = 1.0
    monkeypatch.setattr(availability_core.CACHE, "timeout", 0.05)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    items = (await client.get("/admin/v1/machine-types")).json()["items"]
    (row,) = [i for i in items if i["code"] == mt.provider_type_id]
    assert row["availability"]["status"] == "unknown"
    assert "did not answer" in row["availability"]["detail"]


async def test_the_node_claims_with_the_client_the_box_speaks_with(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    """The daemon's own REST client, built the way a box on its credential
    builds it, against the real route: the claim lands with the credential as
    the bearer and no agent assertion, and the beat after it — the client
    re-keyed to the machine id the claim answered — is accepted with the
    assertion naming that machine. The admin's account is never consulted:
    their email is unverified throughout."""
    from alkera_cli.cloud.service import MirrorSettings, box_rest_client

    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    secret = fake.secrets[UUID(row["id"])][NODE_CREDENTIAL_KEY]
    box = box_rest_client(
        MirrorSettings(
            api_url="http://testserver",
            token=secret,
            project_dir=Path("."),
            machine_name="pool-a",
            provider="ec2",
            provider_pod_id=row["provider_machine_id"],
            machine_credential=secret,
        ),
        transport=ASGITransport(app=fastapi_app),
    )

    claimed = await box.claim_machine(
        credential=secret,
        name="pool-a",
        provider_pod_id=row["provider_machine_id"],
        capacity=8,
        daemon_version="1.4.2",
    )
    assert claimed["id"] == row["id"]
    await box.for_agent(row["id"]).heartbeat_machine(
        row["id"], capacity=8, chats_served=0, daemon_version="1.4.2", credential=secret
    )

    detail = (await client.get(f"{MACHINES}/{row['id']}")).json()
    assert detail["state"] == "ready"
    assert detail["events"][0]["to_state"] == "ready"
    assert detail["events"][0]["actor"]["kind"] == "box"


@pytest.mark.parametrize("provider", [EC2, RUNPOD])
async def test_a_node_boots_with_its_machine_credential_and_no_person_behind_it(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """What leaves the plane for a node — the boot script the provider runs and
    the secret it stores — is the machine credential and nothing that is
    anyone's: no session token of any shape, not the admin's id, not their
    email. Nothing is minted for the admin on the way either: no CLI login is
    registered, and the row records no login to revoke."""
    fake = FakeNodeProvider(kind=provider)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    mt = await make_machine_type(real_session, provider=provider)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    logins_before = await _cli_logins(platform_admin.admin_id)

    resp = await client.post(f"{MACHINES}/provision", json=_body(mt, provider=provider))

    assert resp.status_code == 202, resp.text
    row = resp.json()
    secrets = fake.secrets[UUID(row["id"])]
    launch = fake.nodes[row["provider_machine_id"]].launch
    assert set(secrets) == {NODE_CREDENTIAL_KEY}
    assert launch.secrets == secrets
    for value in secrets.values():
        assert looks_like_machine_token(value)
        with pytest.raises(InvalidTokenError):
            decode_session_token(value)
    for text in (launch.script, json.dumps(secrets)):
        assert str(platform_admin.admin_id) not in text
        assert platform_admin.admin_email not in text
    assert "eyJ" not in launch.script
    assert await _cli_logins(platform_admin.admin_id) == logins_before
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(row["id"]))
        assert alloc is not None
        assert alloc.node_token_jti is None


class _SecretRefused(FakeNodeProvider):
    """A provider whose credentials expired: the very first call, storing the
    node's secret, is refused — and its error quotes what it was handed."""

    async def store_credential(self, allocation_id: UUID, secrets: dict[str, str]) -> None:
        from alkera_core.compute.provider import ComputeProviderError

        raise ComputeProviderError(
            "EC2 create_secret failed: ExpiredTokenException: The security token included "
            f"in the request is expired (SecretString={secrets[NODE_CREDENTIAL_KEY]})"
        )


async def _rows_of(mt: ComputeMachineType) -> list[ComputeAllocation]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ComputeAllocation).where(ComputeAllocation.machine_type_id == mt.id)
        )
        return list(rows.scalars().all())


@pytest.mark.parametrize("tenancy", ["pool", "dedicated"])
async def test_a_provider_that_refuses_before_acting_leaves_no_row_and_says_why(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tenancy: str,
) -> None:
    """The provider refused the first thing it was asked, so nothing exists
    there: the admin reads the provider's reason (never the node secret it
    quoted), and the plane has no ``failed`` machine, no credential and no
    dedicated assignment to show for an attempt that never reached anyone."""
    refused = _SecretRefused(kind=EC2)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: refused)
    availability_core.CACHE.clear()
    mt = await _ec2_type(real_session)
    extra: dict[str, Any] = {"tenancy": tenancy}
    if tenancy == "dedicated":
        extra["org_id"] = str(org_admin.org_id)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    resp = await client.post(f"{MACHINES}/provision", json=_body(mt, **extra))

    assert resp.status_code == 502, resp.text
    error = resp.json()["error"]
    assert error["code"] == "provider_error"
    assert error["message"].startswith(
        "The provider refused: EC2 create_secret failed: ExpiredTokenException"
    )
    assert "[redacted]" in error["message"]
    assert MACHINE_TOKEN_PREFIX not in resp.text
    assert await _rows_of(mt) == []
    async with AsyncSessionLocal() as session:
        assert (await session.execute(select(MachineCredential))).scalars().all() == []
        assert await session.get(OrgComputeAssignment, org_admin.org_id) is None
    assert refused.nodes == {}
    # Nothing held the org: the next attempt, on a provider that answers, goes through.
    monkeypatch.setattr(
        provisioning, "make_node_provider", lambda kind, config: FakeNodeProvider(kind=EC2)
    )
    again = await client.post(f"{MACHINES}/provision", json=_body(mt, **extra))
    assert again.status_code == 202, again.text


async def _fail(machine_id: str) -> None:
    """The row fails the way the reconcile fails a box that never claimed."""
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        transition(session, alloc, "bootstrapping")
        transition(session, alloc, "failed", reason="the node never claimed")
        await session.commit()


async def test_a_failed_machine_still_running_at_the_provider_is_released_by_terminate(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _fail(row["id"])
    failed_at = (await client.get(f"{MACHINES}/{row['id']}")).json()["state_changed_at"]
    assert (await fake.describe(row["provider_machine_id"])).phase != "gone"

    resp = await client.post(f"{MACHINES}/{row['id']}/terminate", json={})

    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "released"
    assert (await fake.describe(row["provider_machine_id"])).phase == "gone"
    detail = (await client.get(f"{MACHINES}/{row['id']}")).json()
    edges = [(e["from_state"], e["to_state"]) for e in detail["events"]]
    assert ("failed", "released") in edges
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(row["id"]))
        assert alloc is not None
        # The row's end is when it failed; the release is an entry in its history.
        assert alloc.released_at == datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
    again = await client.post(f"{MACHINES}/{row['id']}/terminate", json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_released"


async def test_a_failed_machine_the_provider_never_started_releases_without_asking_it(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    mt = await _ec2_type(real_session)
    fake.fail_run = "InsufficientInstanceCapacity"
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (await client.post(f"{MACHINES}/provision", json=_body(mt))).status_code == 502
    (alloc,) = await _rows_of(mt)
    assert (alloc.state, alloc.provider_machine_id or None) == ("failed", None)

    resp = await client.post(f"{MACHINES}/{alloc.id}/terminate", json={})

    assert resp.status_code == 202, resp.text
    assert resp.json()["state"] == "released"


async def test_a_failed_machine_whose_terminate_the_provider_refuses_stays_failed(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    """Released is never said over a machine still running: the refusal is
    the admin's answer, in the provider's words, and the row waits for another
    try."""
    mt = await _ec2_type(real_session)
    row = await _provision(client, platform_admin, mt)
    await _fail(row["id"])
    fake.fail_terminate.add(row["provider_machine_id"])

    resp = await client.post(f"{MACHINES}/{row['id']}/terminate", json={})

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "provider_error"
    assert row["provider_machine_id"] in resp.json()["error"]["message"]
    assert (await client.get(f"{MACHINES}/{row['id']}")).json()["state"] == "failed"
    fake.fail_terminate.clear()
    assert (await client.post(f"{MACHINES}/{row['id']}/terminate", json={})).status_code == 202
