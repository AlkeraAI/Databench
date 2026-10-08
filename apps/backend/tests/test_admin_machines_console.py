"""What the admin machines console reads, through the real routes and Postgres.

The provider behind each kind is a scripted in-memory double substituted at
the one seam the routes read (``provisioning.make_node_provider``); nothing
here reaches a cloud.

- the served count is the chats bound to the machine, not the box's stored
  counter;
- the fleet leaves released and failed machines out unless asked;
- the machine types name every provider and whether this deployment can
  start machines there, with what it is missing;
- the machines a provider holds that no row owns are listed read-only, and a
  provider that cannot be listed is named rather than read as empty.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.compute import availability as availability_core
from alkera_core.compute.provider import (
    EC2,
    LOCALDEV,
    RUNNING,
    RUNPOD,
    ComputeProviderError,
    ComputeProviderUnavailableError,
    ProviderPod,
)
from alkera_core.compute.reconcile import pod_name_for
from alkera_core.compute.transitions import transition
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MachineCredential, OrgComputeAssignment, WorkspaceObject
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from backend.api.admin import machines as machines_route
from backend.services.compute import provisioning
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, login

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MACHINES = "/admin/v1/machines"
TYPES = "/admin/v1/machine-types"
UNMANAGED = "/admin/v1/machines/unmanaged"


@dataclass
class ListingProvider(FakeNodeProvider):
    """The scripted node provider, plus the account listing the console reads."""

    extra_pods: list[ProviderPod] = field(default_factory=list)
    list_error: BaseException | None = None
    list_delay: float = 0.0

    async def list_pods(self, *, name_prefix: str = "") -> list[ProviderPod]:
        if self.list_delay:
            await asyncio.sleep(self.list_delay)
        if self.list_error is not None:
            raise self.list_error
        ran = [
            ProviderPod(pod_id=machine_id, name=f"ran-{machine_id}", phase=RUNNING)
            for machine_id in self.nodes
        ]
        return [*ran, *self.extra_pods]


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, ListingProvider]]:
    by_kind = {
        EC2: ListingProvider(kind=EC2),
        RUNPOD: ListingProvider(kind=RUNPOD),
        # A local box is offered only in a local deployment; the console's
        # tests run as a cloud one unless a test says otherwise.
        LOCALDEV: ListingProvider(kind=LOCALDEV, is_configured=False),
    }
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: by_kind[kind])
    availability_core.CACHE.clear()
    yield by_kind
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


async def _provision(
    client: AsyncClient, admin: OrgWithAdmin, mt: ComputeMachineType, name: str
) -> dict[str, Any]:
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.post(
        f"{MACHINES}/provision",
        json={
            "provider": mt.provider,
            "machine_type_code": mt.provider_type_id,
            "storage_gb": 100,
            "tenancy": "pool",
            "name": name,
        },
    )
    assert resp.status_code == 202, resp.text
    return dict(resp.json())


async def _ready(machine_id: str, *, counter: int) -> None:
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        transition(session, alloc, "bootstrapping")
        transition(session, alloc, "ready")
        alloc.chats_served = counter
        await session.commit()


async def _chat(
    org: OrgWithAdmin, machine_id: str, *, deleted: bool = False, mirror: str | None = None
) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            WorkspaceObject(
                org_team_id=org.org_id,
                logical_id=f"console-{uuid4().hex[:10]}",
                namespace="workspace",
                type="chat",
                title="",
                version=1,
                status="ready",
                spec={
                    "machine_id": machine_id,
                    **({"mirror_state": mirror} if mirror is not None else {}),
                },
                owner_user_id=org.admin_id,
                visibility_scope="private",
                deleted_at=1 if deleted else 0,
            )
        )
        await session.commit()


async def _heard_from(machine_id: str) -> None:
    """The box beat just now, so it reads ``ready`` rather than silent."""
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.last_heartbeat_at = datetime.now(UTC)
        await session.commit()


async def _set_state(machine_id: str, state: str) -> None:
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.state = state
        await session.commit()


# --------------------------------------------------------------------------- #
# the served count
# --------------------------------------------------------------------------- #


async def test_served_is_the_chats_bound_to_the_machine_not_the_stored_counter(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    busy = await _provision(client, platform_admin, mt, "busy")
    idle = await _provision(client, platform_admin, mt, "idle")
    # The counter says the opposite of the truth on both boxes.
    await _ready(busy["id"], counter=0)
    await _ready(idle["id"], counter=7)
    for _ in range(3):
        await _chat(org_admin, busy["id"])
    await _chat(org_admin, busy["id"], deleted=True)

    rows = {m["id"]: m for m in (await client.get(MACHINES)).json()["items"]}
    assert rows[busy["id"]]["chats_served"] == 3
    assert rows[idle["id"]]["chats_served"] == 0

    detail = (await client.get(f"{MACHINES}/{busy['id']}")).json()
    assert detail["chats_served"] == len(detail["chats"]) == 3


@pytest.mark.parametrize(
    ("awake", "parked"),
    [
        pytest.param(4, 4, id="half-parked"),
        pytest.param(3, 0, id="none-parked"),
        pytest.param(0, 2, id="all-parked"),
    ],
)
async def test_a_ready_box_counts_the_chats_it_parked_as_asleep_not_served(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
    monkeypatch: pytest.MonkeyPatch,
    awake: int,
    parked: int,
) -> None:
    """A ready box that closed some chats' sessions reports every session it
    mirrors in its heartbeat; the console splits them the way each chat row
    reads — the list, the detail and the ops total all agree."""
    from backend.services.ops import ops_summary as ops_service

    monkeypatch.setattr(ops_service, "HEALTH_CHECKS", ())
    mt = await make_machine_type(real_session, provider=EC2)
    box = await _provision(client, platform_admin, mt, "half-asleep")
    await _ready(box["id"], counter=awake + parked)
    await _heard_from(box["id"])
    for _ in range(awake):
        await _chat(org_admin, box["id"], mirror="awake")
    # A chat bound before its box ever spoke of its session reads awake.
    await _chat(org_admin, box["id"])
    for _ in range(parked):
        await _chat(org_admin, box["id"], mirror="asleep")

    figures = ("chats_served", "chats_asleep", "chats_stranded")
    want = (awake + 1, parked, 0)
    row = {m["id"]: m for m in (await client.get(MACHINES)).json()["items"]}[box["id"]]
    assert tuple(row[f] for f in figures) == want

    detail = (await client.get(f"{MACHINES}/{box['id']}")).json()
    assert tuple(detail[f] for f in figures) == want
    statuses = [chat["machine_status"] for chat in detail["chats"]]
    assert (statuses.count("ready"), statuses.count("asleep")) == (awake + 1, parked)

    ops = await client.get("/admin/v1/ops/summary")
    assert ops.status_code == 200, ops.text
    (listed,) = [m for m in ops.json()["machines"] if m["id"] == box["id"]]
    assert listed["chats_served"] == awake + 1
    assert ops.json()["chats_served_total"] == sum(
        m["chats_served"] for m in ops.json()["machines"]
    )


@pytest.mark.parametrize(
    ("state", "want"),
    [
        pytest.param("draining", (2, 0, 0), id="draining-box-still-serves-them"),
        pytest.param("released", (0, 0, 2), id="gone-box-strands-them-all"),
    ],
)
async def test_a_parked_chat_on_a_box_that_is_not_ready_reads_by_the_box(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
    state: str,
    want: tuple[int, int, int],
) -> None:
    """Only a ready box's word parks a chat; a draining box carries it as
    load and a box off the plane strands it, as the chat page says."""
    mt = await make_machine_type(real_session, provider=EC2)
    box = await _provision(client, platform_admin, mt, f"parked-{state}")
    await _ready(box["id"], counter=2)
    await _heard_from(box["id"])
    await _chat(org_admin, box["id"], mirror="asleep")
    await _chat(org_admin, box["id"], mirror="awake")
    await _set_state(box["id"], state)

    detail = (await client.get(f"{MACHINES}/{box['id']}")).json()
    assert (detail["chats_served"], detail["chats_asleep"], detail["chats_stranded"]) == want


# --------------------------------------------------------------------------- #
# the default fleet leaves history out
# --------------------------------------------------------------------------- #


async def test_released_and_failed_machines_leave_the_default_fleet_but_not_the_full_one(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    by_state = {}
    for state in ("released", "failed", "lost", "ready"):
        row = await _provision(client, platform_admin, mt, f"box-{state}")
        await _set_state(row["id"], state)
        by_state[state] = row["id"]

    default = {m["id"] for m in (await client.get(MACHINES)).json()["items"]}
    assert by_state["released"] not in default
    assert by_state["failed"] not in default
    assert {by_state["lost"], by_state["ready"]} <= default

    everything = await client.get(MACHINES, params={"include_gone": "true"})
    full = {m["id"] for m in everything.json()["items"]}
    assert set(by_state.values()) <= full


async def test_an_unclaimed_credential_stays_in_the_default_fleet(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    minted = await client.post(
        MACHINES,
        json={"label": "by-hand", "provider": "ec2", "instance_type": mt.provider_type_id},
    )
    assert minted.status_code == 201, minted.text
    items = (await client.get(MACHINES)).json()["items"]
    assert [m["label"] for m in items if m["machine_id"] is None] == ["by-hand"]


# --------------------------------------------------------------------------- #
# which providers this deployment can start machines at
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ec2_on", "runpod_on"),
    [
        pytest.param(False, True, id="ec2-unconfigured"),
        pytest.param(True, False, id="runpod-unconfigured"),
        pytest.param(True, True, id="both"),
    ],
)
async def test_machine_types_name_every_provider_and_whether_it_is_configured(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
    ec2_on: bool,
    runpod_on: bool,
) -> None:
    # No catalog row at all for RunPod: the provider is still reported.
    await make_machine_type(real_session, provider=EC2)
    providers[EC2].is_configured = ec2_on
    providers[RUNPOD].is_configured = runpod_on
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    answer = (await client.get(TYPES)).json()
    status = {p["kind"]: p for p in answer["providers"]}
    assert set(status) == {"ec2", "localdev", "runpod"}
    assert status["ec2"]["configured"] is ec2_on
    assert status["runpod"]["configured"] is runpod_on
    assert ("ALKERA_EC2_LAUNCH_TEMPLATE_NAME" in status["ec2"]["reason"]) is (not ec2_on)
    assert ("RUNPOD_API_KEY" in status["runpod"]["reason"]) is (not runpod_on)
    assert status["localdev"]["configured"] is False
    assert "APP_ENV=local" in status["localdev"]["reason"]


async def test_a_gpu_type_the_feed_added_is_listed_for_the_offerings_editor(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    """The offerings editor picks its machine type from this list: a GPU type
    the RunPod feed added is on it, so an admin can make an offering of it."""
    from alkera_core.compute.refresh import refresh_catalog

    code = f"NVIDIA L40S {uuid4().hex[:8]}"
    entry = {
        "price_nanos": 14_000_000,
        "availability": "HIGH",
        "max_count": 4,
        "gpu_name": "L40S",
        "gpu_memory_gb": 48,
    }
    out = await refresh_catalog(real_session, provider_kind=RUNPOD, entries={code: entry})
    assert out["added"] == 1
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    listed = {t["code"]: t for t in (await client.get(TYPES)).json()["items"]}
    assert code in listed
    assert (listed[code]["provider"], listed[code]["gpu"]) == ("runpod", 1)
    assert listed[code]["price_per_minute_nanos"] == 14_000_000
    assert listed[code]["available"] is True


# --------------------------------------------------------------------------- #
# machines no row owns
# --------------------------------------------------------------------------- #


async def test_a_machine_no_row_owns_is_listed_read_only_and_owned_ones_are_not(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    owned = await _provision(client, platform_admin, mt, "owned")
    # A create whose id never reached its row: owned by the name it was given.
    async with AsyncSessionLocal() as session:
        in_flight = ComputeAllocation(
            user_id=platform_admin.admin_id,
            org_team_id=platform_admin.org_id,
            machine_type_id=mt.id,
            state="provisioning",
            provider_machine_id="",
        )
        session.add(in_flight)
        await session.commit()
        in_flight_name = pod_name_for(in_flight.id)
    providers[EC2].extra_pods = [
        ProviderPod(pod_id="i-handmade", name="demo-box", phase=RUNNING, raw_status="running"),
        ProviderPod(pod_id="i-inflight", name=in_flight_name, phase=RUNNING),
    ]

    answer = (await client.get(UNMANAGED)).json()

    listed = {m["provider_machine_id"]: m for m in answer["items"]}
    assert set(listed) == {"i-handmade"}
    assert owned["provider_machine_id"] not in listed
    assert listed["i-handmade"]["provider"] == "ec2"
    assert listed["i-handmade"]["name"] == "demo-box"
    assert answer["unavailable"] == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ComputeProviderError("RunPod answered 500"), id="provider-error"),
        pytest.param(ComputeProviderUnavailableError("401 from RunPod"), id="unavailable"),
        pytest.param(None, id="too-slow"),
    ],
)
async def test_a_provider_that_cannot_be_listed_is_named_and_the_others_still_answer(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, ListingProvider],
    failure: BaseException | None,
) -> None:
    providers[EC2].extra_pods = [ProviderPod(pod_id="i-stray", name="stray", phase=RUNNING)]
    if failure is None:
        monkeypatch.setattr(machines_route, "_LIST_BUDGET_S", 0.05)
        providers[RUNPOD].list_delay = 1.0
    else:
        providers[RUNPOD].list_error = failure
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    resp = await client.get(UNMANAGED)

    assert resp.status_code == 200, resp.text
    answer = resp.json()
    assert [m["provider_machine_id"] for m in answer["items"]] == ["i-stray"]
    assert [n["provider"] for n in answer["unavailable"]] == ["runpod"]
    # A sentence for the admin: the provider named as the console names it.
    assert answer["unavailable"][0]["detail"].startswith("RunPod ")


async def test_an_unconfigured_provider_is_not_asked_for_its_machines(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    providers: dict[str, ListingProvider],
) -> None:
    providers[RUNPOD].is_configured = False
    providers[RUNPOD].list_error = AssertionError("an unconfigured provider was listed")
    providers[RUNPOD].extra_pods = [ProviderPod(pod_id="pod-x", name="x")]
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    answer = (await client.get(UNMANAGED)).json()
    assert answer == {"items": [], "unavailable": []}


async def test_support_reads_unmanaged_machines_and_a_tenant_admin_cannot(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    providers: dict[str, ListingProvider],
) -> None:
    providers[EC2].extra_pods = [ProviderPod(pod_id="i-stray", name="stray", phase=RUNNING)]
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(UNMANAGED)).status_code == 403
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(UNMANAGED)
    assert resp.status_code == 200
    assert [m["provider_machine_id"] for m in resp.json()["items"]] == ["i-stray"]


# --------------------------------------------------------------------------- #
# the boundary each machine runs chats under
# --------------------------------------------------------------------------- #


async def _set_sandbox(machine_id: str, sandbox: str) -> None:
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.sandbox = sandbox
        await session.commit()


@pytest.mark.parametrize("sandbox", ["gvisor", "none"])
async def test_the_fleet_and_the_detail_report_the_sandbox_the_box_reported(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
    sandbox: str,
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    reported = await _provision(client, platform_admin, mt, f"box-{sandbox}")
    other = await _provision(client, platform_admin, mt, "box-other")
    await _ready(reported["id"], counter=0)
    await _ready(other["id"], counter=0)
    await _set_sandbox(reported["id"], sandbox)
    await _set_sandbox(other["id"], "none" if sandbox == "gvisor" else "gvisor")

    rows = {m["id"]: m for m in (await client.get(MACHINES)).json()["items"]}
    assert rows[reported["id"]]["sandbox"] == sandbox
    assert rows[other["id"]]["sandbox"] != sandbox

    detail = (await client.get(f"{MACHINES}/{reported['id']}")).json()
    assert detail["sandbox"] == sandbox


@pytest.mark.parametrize("sandbox", ["gvisor", "none"])
async def test_a_machine_no_credential_names_reports_its_sandbox(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
    sandbox: str,
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    async with AsyncSessionLocal() as session:
        alloc = ComputeAllocation(
            user_id=platform_admin.admin_id,
            org_team_id=platform_admin.org_id,
            machine_type_id=mt.id,
            state="ready",
            tenancy="pool",
            sandbox=sandbox,
            provider_machine_id=f"i-{uuid4().hex[:8]}",
        )
        session.add(alloc)
        await session.commit()
        machine_id = str(alloc.id)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    rows = {m["id"]: m for m in (await client.get(MACHINES)).json()["items"]}
    assert rows[machine_id]["sandbox"] == sandbox
    detail = (await client.get(f"{MACHINES}/{machine_id}")).json()
    assert detail["sandbox"] == sandbox


async def test_a_provisioned_box_that_has_not_reported_reads_no_sandbox(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    row = await _provision(client, platform_admin, mt, "unreported")
    assert row["sandbox"] == "none"
    rows = {m["id"]: m for m in (await client.get(MACHINES)).json()["items"]}
    assert rows[row["id"]]["sandbox"] == "none"


async def test_an_unclaimed_credential_reads_no_sandbox(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    minted = await client.post(
        MACHINES,
        json={"label": "by-hand", "provider": "ec2", "instance_type": mt.provider_type_id},
    )
    assert minted.status_code == 201, minted.text
    items = (await client.get(MACHINES)).json()["items"]
    assert [m["sandbox"] for m in items if m["machine_id"] is None] == ["none"]


# --------------------------------------------------------------------------- #
# a sleeping machine is a row the console can show
# --------------------------------------------------------------------------- #


async def test_a_slept_machine_answers_its_sleep_its_reads_and_its_wake(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    """The row's wire ``state`` has to admit every lifecycle state. When it
    lagged ``asleep``, the sleep committed the row and then failed to
    serialize its answer, and every read that rendered the row — the detail,
    the fleet — failed the same way: the console could not show a sleeping
    machine at all. Through the routes: sleep answers the row as ``asleep``,
    the detail and both fleet listings answer it, and the wake answers it
    ``ready``."""
    mt = await make_machine_type(real_session, provider=EC2)
    row = await _provision(client, platform_admin, mt, "napper")
    await _ready(row["id"], counter=0)

    slept = await client.post(f"{MACHINES}/{row['id']}/sleep")
    assert slept.status_code == 200, slept.text
    assert (slept.json()["state"], slept.json()["liveness"]) == ("asleep", "asleep")

    detail = await client.get(f"{MACHINES}/{row['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["state"] == "asleep"
    fleet = await client.get(MACHINES)
    assert fleet.status_code == 200, fleet.text
    assert {m["id"]: m["state"] for m in fleet.json()["items"]}[row["id"]] == "asleep", (
        "a sleeping machine is fleet, not history"
    )
    everything = await client.get(MACHINES, params={"include_gone": "true"})
    assert everything.status_code == 200, everything.text
    assert {m["id"]: m["state"] for m in everything.json()["items"]}[row["id"]] == "asleep"

    woke = await client.post(f"{MACHINES}/{row['id']}/wake")
    assert woke.status_code == 200, woke.text
    assert (woke.json()["state"], woke.json()["liveness"]) == ("ready", "starting")


async def test_a_row_already_asleep_in_the_database_is_readable(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    providers: dict[str, ListingProvider],
) -> None:
    """A row put to sleep before the wire admitted it.
    Its detail and its fleet row must answer without the sleep being redone."""
    mt = await make_machine_type(real_session, provider=EC2)
    row = await _provision(client, platform_admin, mt, "left-asleep")
    await _ready(row["id"], counter=0)
    await _set_state(row["id"], "asleep")

    detail = await client.get(f"{MACHINES}/{row['id']}")
    assert detail.status_code == 200, detail.text
    assert (detail.json()["state"], detail.json()["liveness"]) == ("asleep", "asleep")
    fleet = await client.get(MACHINES)
    assert fleet.status_code == 200, fleet.text
    assert row["id"] in {m["id"] for m in fleet.json()["items"]}
