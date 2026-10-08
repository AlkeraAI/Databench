"""The node reconcile, driven edge by edge with a scripted provider and an
explicit clock that crosses each time-out.

Each case seeds one provisioned machine in a given state, scripts what its
provider says, runs one pass (or two) at a chosen moment, and asserts the row's
state, its recorded edges, what the provider was asked to do, and that a
machine leaving the plane gives back its secret and its credential.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.auth import encode_cli_token, register_token
from alkera_core.compute.node_reconcile import reconcile_nodes
from alkera_core.compute.nodes import NodeLaunch
from alkera_core.compute.provider import ComputeProviderError
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuthToken, MachineCredential, TokenType
from alkera_core.models.compute import ComputeAllocation, ComputeAllocationEvent
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from backend.services.credentials import machine_credentials as machine_credential_service
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("compute-fleet")]

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
KIND = "fakenode"


@pytest.fixture(autouse=True)
async def _only_this_test_provisions(real_session: AsyncSession) -> None:
    await real_session.execute(
        update(ComputeAllocation)
        .where(
            ComputeAllocation.origin == "provisioned",
            ComputeAllocation.lifecycle == "workspace",
            ComputeAllocation.state.not_in(("released", "failed")),
        )
        .values(state="released")
    )
    await real_session.commit()


class _Seed:
    def __init__(self, fake: FakeNodeProvider, alloc_id: UUID, machine_id: str) -> None:
        self.fake = fake
        self.alloc_id = alloc_id
        self.machine_id = machine_id


async def _seed(
    session: AsyncSession,
    org: OrgWithAdmin,
    fake: FakeNodeProvider,
    *,
    state: str,
    phase: str,
    chats: int = 0,
    auto_terminate: bool = False,
) -> _Seed:
    mt = await make_machine_type(session, provider="runpod")
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        tenancy="pool",
        state=state,
        created_at=T0,
        state_changed_at=T0,
        chats_served=chats,
        auto_terminate=auto_terminate,
    )
    session.add(alloc)
    await session.flush()
    machine_id = await fake.run(
        NodeLaunch(allocation_id=alloc.id, name="n", type_code="t", storage_gb=10, script="")
    )
    fake.script(machine_id, phase)  # type: ignore[arg-type]
    await fake.store_credential(alloc.id, {"ALKERA_MACHINE_CREDENTIAL": "c"})
    alloc.provider_machine_id = machine_id
    credential, _raw = await machine_credential_service.mint(
        session,
        org_id=org.org_id,
        created_by=org.admin_id,
        machine_type=mt,
        tenancy="pool",
        label="n",
    )
    credential.machine_id = alloc.id
    # The CLI login the node was handed beside its credential, by its jti.
    _token, claims = encode_cli_token(
        user_id=org.admin_id,
        email=f"node-{alloc.id.hex[:8]}@alkera.test",
        org_team_id=org.org_id,
        platform_role=None,
    )
    await register_token(session, claims=claims, token_type=TokenType.CLI, label="node n")
    alloc.node_token_jti = claims.jti
    await session.commit()
    return _Seed(fake, alloc.id, machine_id)


async def _pass(fake: FakeNodeProvider, at: datetime) -> None:
    async with AsyncSessionLocal() as db:
        await reconcile_nodes(db, providers=lambda kind: fake, now=at)


async def _state(alloc_id: UUID) -> tuple[str, list[tuple[str, str]], bool]:
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, alloc_id)
        assert alloc is not None
        edges = (
            await db.execute(
                select(ComputeAllocationEvent.from_state, ComputeAllocationEvent.to_state)
                .where(ComputeAllocationEvent.allocation_id == alloc_id)
                .order_by(ComputeAllocationEvent.at, ComputeAllocationEvent.to_state)
            )
        ).all()
        revoked = (
            await db.execute(
                select(MachineCredential.revoked_at).where(MachineCredential.machine_id == alloc_id)
            )
        ).scalar_one() is not None
        token_revoked = (
            await db.execute(
                select(AuthToken.revoked_at).where(AuthToken.jti == alloc.node_token_jti)
            )
        ).scalar_one() is not None
        # The node's login and its machine credential leave the plane together.
        assert token_revoked is revoked
        return alloc.state, [(a, b) for a, b in edges], revoked


CASES = [
    # (state, phase, chats, auto, at, expected state, edges, terminated, secret gone)
    pytest.param(
        "provisioning",
        "running",
        0,
        False,
        T0 + timedelta(minutes=1),
        "bootstrapping",
        [("provisioning", "bootstrapping")],
        False,
        False,
        id="provisioning-running",
    ),
    pytest.param(
        "provisioning",
        "starting",
        0,
        False,
        T0 + timedelta(minutes=9, seconds=59),
        "provisioning",
        [],
        False,
        False,
        id="provisioning-inside-timeout",
    ),
    pytest.param(
        "provisioning",
        "starting",
        0,
        False,
        T0 + timedelta(minutes=10, seconds=1),
        "failed",
        [("provisioning", "failed")],
        True,
        True,
        id="provisioning-timed-out",
    ),
    pytest.param(
        "provisioning",
        "gone",
        0,
        False,
        T0 + timedelta(minutes=1),
        "failed",
        [("provisioning", "failed")],
        True,
        True,
        id="provisioning-gone",
    ),
    pytest.param(
        "bootstrapping",
        "running",
        0,
        False,
        T0 + timedelta(minutes=14, seconds=59),
        "bootstrapping",
        [],
        False,
        False,
        id="bootstrapping-inside-timeout",
    ),
    pytest.param(
        "bootstrapping",
        "running",
        0,
        False,
        T0 + timedelta(minutes=15, seconds=1),
        "failed",
        [("bootstrapping", "failed")],
        True,
        True,
        id="bootstrapping-timed-out",
    ),
    pytest.param(
        "bootstrapping",
        "gone",
        0,
        False,
        T0 + timedelta(minutes=1),
        "failed",
        [("bootstrapping", "failed")],
        True,
        True,
        id="bootstrapping-gone",
    ),
    pytest.param(
        "ready",
        "gone",
        3,
        False,
        T0 + timedelta(hours=5),
        "released",
        [("ready", "lost"), ("lost", "released")],
        True,
        True,
        id="ready-gone-is-lost-then-released",
    ),
    pytest.param(
        "ready",
        "running",
        3,
        False,
        T0 + timedelta(hours=5),
        "ready",
        [],
        False,
        False,
        id="ready-running-untouched",
    ),
    pytest.param(
        "draining",
        "gone",
        1,
        False,
        T0 + timedelta(hours=5),
        "released",
        [("draining", "lost"), ("lost", "released")],
        True,
        True,
        id="draining-gone",
    ),
    pytest.param(
        "draining",
        "running",
        0,
        True,
        T0 + timedelta(minutes=1),
        "releasing",
        [("draining", "releasing")],
        True,
        False,
        id="draining-empty-auto-terminates",
    ),
    pytest.param(
        "draining",
        "running",
        2,
        True,
        T0 + timedelta(minutes=1),
        "draining",
        [],
        False,
        False,
        id="draining-with-chats-waits",
    ),
    pytest.param(
        "draining",
        "running",
        0,
        False,
        T0 + timedelta(minutes=1),
        "draining",
        [],
        False,
        False,
        id="draining-empty-without-auto-waits",
    ),
    pytest.param(
        "releasing",
        "gone",
        0,
        False,
        T0 + timedelta(minutes=1),
        "released",
        [("releasing", "released")],
        True,
        True,
        id="releasing-gone",
    ),
]


@pytest.mark.parametrize(
    ("state", "phase", "chats", "auto", "at", "expected", "edges", "terminated", "secret_gone"),
    CASES,
)
async def test_one_pass_moves_each_row_along_exactly_its_edge(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    state: str,
    phase: str,
    chats: int,
    auto: bool,
    at: datetime,
    expected: str,
    edges: list[tuple[str, str]],
    terminated: bool,
    secret_gone: bool,
) -> None:
    fake = FakeNodeProvider(kind=KIND)
    seed = await _seed(
        real_session, org_admin, fake, state=state, phase=phase, chats=chats, auto_terminate=auto
    )
    await _pass(fake, at)
    got_state, got_edges, revoked = await _state(seed.alloc_id)
    assert got_state == expected
    assert got_edges == edges
    assert ((await fake.describe(seed.machine_id)).phase == "gone") is (
        terminated or phase == "gone"
    )
    assert (seed.alloc_id not in fake.secrets) is secret_gone
    assert revoked is (expected in ("released", "failed", "releasing"))


@pytest.mark.parametrize(
    ("note", "error"),
    [
        pytest.param(
            "ApiError: unreachable: Connection refused",
            "the daemon never claimed the machine; last error: "
            "ApiError: unreachable: Connection refused",
            id="the-nodes-last-error-is-recorded",
        ),
        pytest.param("", "the daemon never claimed the machine", id="no-error-on-record"),
    ],
)
async def test_a_boot_that_never_registered_records_the_nodes_last_error(
    real_session: AsyncSession, org_admin: OrgWithAdmin, note: str, error: str
) -> None:
    fake = FakeNodeProvider(kind=KIND)
    seed = await _seed(real_session, org_admin, fake, state="bootstrapping", phase="running")
    fake.script(seed.machine_id, "running", note=note)
    await _pass(fake, T0 + timedelta(minutes=15, seconds=1))
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, seed.alloc_id)
        assert alloc is not None
        assert (alloc.state, alloc.terminated_reason, alloc.error) == (
            "failed",
            "boot_failed",
            error,
        )


async def test_a_release_the_provider_did_not_confirm_is_retried_until_it_is(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fake = FakeNodeProvider(kind=KIND)
    seed = await _seed(real_session, org_admin, fake, state="releasing", phase="running")
    fake.fail_terminate.add(seed.machine_id)
    await _pass(fake, T0 + timedelta(minutes=1))
    assert (await _state(seed.alloc_id))[0] == "releasing"
    fake.fail_terminate.clear()
    await _pass(fake, T0 + timedelta(minutes=2))  # terminate goes through
    await _pass(fake, T0 + timedelta(minutes=3))  # and the provider now says gone
    state, edges, _ = await _state(seed.alloc_id)
    assert state == "released"
    assert edges == [("releasing", "released")]


async def test_a_provider_that_cannot_answer_leaves_its_row_and_not_the_others(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    broken = FakeNodeProvider(kind=KIND)
    seed = await _seed(real_session, org_admin, broken, state="ready", phase="gone")

    class _Down(FakeNodeProvider):
        async def describe(self, machine_id: str):  # type: ignore[no-untyped-def]
            raise ComputeProviderError("503 from provider")

    down = _Down(kind=KIND, nodes=broken.nodes, secrets=broken.secrets)
    async with AsyncSessionLocal() as db:
        summary = await reconcile_nodes(db, providers=lambda kind: down, now=T0)
    assert summary.provider_errors >= 1
    assert (await _state(seed.alloc_id))[0] == "ready"
