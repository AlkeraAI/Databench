"""No pod this deployment pays for is untracked, and none it does not own is touched.

A pod becomes money the moment the provider builds it, and until this branch the
only way the plane could find one was through an id a row had committed. Three
holes followed, each of them a pod billing us that nothing would ever stop: a
create cancelled while it was in flight, a create that timed out (the provider
may still have acted on it), and the retry that either invites — which bought a
second machine.

The close is two-sided. The intent is committed BEFORE the provider is called,
under a name derived from the allocation id, so a pod can always be traced back
to the row that asked for it. And a reconciler looks the other way down that
join: it adopts a pod whose row lost it, terminates a pod no live row owns once
it is past the grace, and writes off a create the provider never confirmed.

Every case here drives the real service and the real reconciler against a fake
provider, and each asserts the invariant the probe broke: after one pass, every
pod the provider holds is either named by a live row or has been terminated.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.provider import (
    RUNPOD,
    STARTING,
    ComputeProviderError,
    ComputeProviderUnavailableError,
    ProviderPod,
)
from alkera_core.compute.reconcile import (
    CREATE_UNCONFIRMED,
    TruncationWatch,
    is_our_pod_name,
    pod_name_for,
    pod_name_prefix,
    reconcile_pods,
)
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from backend.services.compute import service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import FakeProvider, make_grant, make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.xdist_group("compute-fleet")

NOW = datetime.now(UTC)


@dataclass
class CancelDuringCreate(FakeProvider):
    """Cancels the CALLING task at a chosen point inside the provider call.

    The two points that matter are the two sides of the moment money starts:
    before the pod exists and after it does. The cancellation is delivered
    through the loop and then waited for on an event — a barrier, not a sleep —
    so the test is deterministic rather than timing-dependent.
    """

    caller: asyncio.Task[object] | None = None
    cancel_at: str = "after_create"

    async def _cancel_the_caller(self) -> None:
        assert self.caller is not None
        gate = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_soon(self.caller.cancel)
        # Queued after the cancel, so waiting on it means the cancellation has
        # already been delivered to the calling task.
        loop.call_soon(gate.set)
        await gate.wait()

    async def create_pod(
        self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
    ) -> str:
        if self.cancel_at == "before_create":
            await self._cancel_the_caller()
        pod_id = await super().create_pod(
            name=name, machine_type=machine_type, ssh_public_key=ssh_public_key
        )
        if self.cancel_at == "after_create":
            await self._cancel_the_caller()
        return pod_id


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def _rows(org: OrgWithAdmin) -> list[ComputeAllocation]:
    async with AsyncSessionLocal() as probe:
        return list(
            (
                await probe.execute(
                    select(ComputeAllocation)
                    .where(ComputeAllocation.org_team_id == org.org_id)
                    .order_by(ComputeAllocation.created_at.asc())
                )
            )
            .scalars()
            .all()
        )


async def _provision(
    org: OrgWithAdmin,
    machine_type: ComputeMachineType,
    provider: FakeProvider,
    *,
    session_id: str,
) -> ComputeAllocation:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, org.admin_id)
        assert user is not None
        return await service.create_allocation(
            session,
            ctx=_ctx(org),
            user=user,
            machine_type=machine_type,
            project_path="scratch",
            session_id=session_id,
            provider=provider,
        )


async def _assert_every_pod_is_accounted_for(provider: FakeProvider, org: OrgWithAdmin) -> None:
    """The invariant the probe broke: the provider holds nothing this org's
    rows do not name."""
    tracked = {r.provider_machine_id for r in await _rows(org) if r.provider_machine_id}
    held = set(provider.pods)
    assert held <= tracked, f"untracked pods at the provider: {sorted(held - tracked)}"


# --------------------------------------------------------------------------- #
# The create path: the intent is durable before the money is spent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cancel_at", ["before_create", "after_create"])
async def test_a_cancel_inside_the_provider_call_leaks_no_pod_and_the_retry_buys_none(
    real_session: AsyncSession, org_admin: OrgWithAdmin, cancel_at: str
) -> None:
    """The probe, inverted. A client disconnect lands inside ``create_pod``;
    the client then retries the same session. Exactly one pod exists, a row
    names it, and nothing was bought twice."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=4)
    provider = CancelDuringCreate(cancel_at=cancel_at, created_at=NOW)

    async def run() -> ComputeAllocation:
        return await _provision(org_admin, mt, provider, session_id="s-cut")

    task = asyncio.ensure_future(run())
    provider.caller = task
    with pytest.raises(asyncio.CancelledError):
        await task

    # The retry the client's own control timeout invites.
    again = await _provision(org_admin, mt, provider, session_id="s-cut")

    assert len(provider.create_calls) == 1, "the retry bought a second machine"
    assert len(provider.pods) == 1
    rows = await _rows(org_admin)
    assert len(rows) == 1
    assert rows[0].id == again.id
    assert rows[0].provider_machine_id == next(iter(provider.pods))
    await _assert_every_pod_is_accounted_for(provider, org_admin)

    # And a reconciler pass leaves the machine alone: it is owned.
    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW + timedelta(hours=1)
        )
    assert (summary.adopted, summary.terminated) == (0, 0)
    assert len(provider.pods) == 1


async def test_a_create_that_times_out_leaves_a_row_the_reconciler_can_resolve(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A read timeout is not "the provider did nothing". Filing the row
    ``failed`` with no pod id closed it, freed its grant slot and left whatever
    the provider built billing us for ever; it stays ``provisioning`` under its
    pod name instead."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider(
        create_error="RunPod request failed: ReadTimeout", create_status_code=None
    )

    with pytest.raises(ComputeProviderError):
        await _provision(org_admin, mt, provider, session_id="s-timeout")

    (row,) = await _rows(org_admin)
    assert row.state == "provisioning"
    assert row.provider_machine_id == ""
    assert row.terminated_reason == ""
    assert "ReadTimeout" in row.error


async def test_a_provider_that_refuses_leaves_the_row_failed(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case: a provider that ANSWERED, with a status, made no
    machine — so the row is closed and the grant slot goes straight back."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider()
    provider.create_error = None

    class Refusing(FakeProvider):
        async def create_pod(
            self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
        ) -> str:
            raise ComputeProviderError("no capacity", status_code=400)

    with pytest.raises(ComputeProviderError):
        await _provision(org_admin, mt, Refusing(), session_id="s-refused")

    (row,) = await _rows(org_admin)
    assert (row.state, row.provider_machine_id) == ("failed", "")


async def test_the_page_never_calls_an_unconfirmed_machine_ready_or_provisioning(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """What the customer is looking at during the unconfirmed window. The retry
    after a timeout is answered 201 with a live-looking allocation, and the row
    can sit there until the reconciler settles it, so the one thing the page
    must never do is claim a machine that may not exist is up. It does not say
    "Running", and it does not say "Provisioning…" either — that would assert a
    machine is on its way, which is exactly what nobody knows yet."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider(
        create_error="RunPod request failed: ReadTimeout", create_status_code=None
    )
    with pytest.raises(ComputeProviderError):
        await _provision(org_admin, mt, provider, session_id="s-status")
    (row,) = await _rows(org_admin)

    info = service.allocation_info(row, mt, now=NOW)

    assert info.state == "provisioning"
    assert info.provider_machine_id == ""
    assert "Running" not in info.status_message
    assert "ready" not in info.status_message.lower()
    assert info.status_message == "Waiting for the provider to confirm this machine…"
    # A row that DID get its id back reads as ordinary provisioning again.
    row.provider_machine_id = "fakepod-9"
    assert service.allocation_info(row, mt, now=NOW).status_message == "Provisioning…"


# --------------------------------------------------------------------------- #
# The reconciler
# --------------------------------------------------------------------------- #


async def test_a_provider_this_deployment_cannot_act_through_leaves_the_row_failed(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The third shape, and the one that is easy to get wrong: a provider this
    deployment cannot act through never reached anything, so no machine exists.
    It carries no status code — but treating it as "we never heard" would pin a
    grant slot open, and leave the reconciler looking for a pod that was never
    going to be there."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)

    class Unreachable(FakeProvider):
        async def create_pod(
            self, *, name: str, machine_type: ComputeMachineType, ssh_public_key: str
        ) -> str:
            raise ComputeProviderUnavailableError("the container service is not provisioned")

    with pytest.raises(ComputeProviderUnavailableError):
        await _provision(org_admin, mt, Unreachable(), session_id="s-unreachable")

    (row,) = await _rows(org_admin)
    assert (row.state, row.provider_machine_id) == ("failed", "")


async def test_a_pod_a_timed_out_create_built_is_adopted_by_its_row(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The other half of the timeout: the provider DID build the machine. The
    name is the join, so the row that paid for it takes it back and the meter
    starts billing on the next tick."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider(
        create_error="RunPod request failed: ReadTimeout", create_status_code=None, created_at=NOW
    )
    with pytest.raises(ComputeProviderError):
        await _provision(org_admin, mt, provider, session_id="s-adopt")
    (row,) = await _rows(org_admin)
    # The pod the timed-out request actually created, under the committed name.
    provider.pods["pod-real"] = ProviderPod(
        pod_id="pod-real", name=pod_name_for(row.id), created_at=NOW, phase=STARTING
    )

    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW + timedelta(hours=1)
        )

    assert (summary.adopted, summary.terminated) == (1, 0)
    (row,) = await _rows(org_admin)
    assert (row.state, row.provider_machine_id) == ("provisioning", "pod-real")
    assert provider.terminate_calls == []


async def test_a_second_pod_under_a_live_row_s_name_is_reaped_and_the_row_s_own_is_kept(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """RunPod's create takes no idempotency token: a create that timed out
    after RunPod built the pod, retried before the listing showed it, leaves
    two pods under one name. The row owns the one it names by id. The other
    billed us for as long as the machine lived; it is reaped after the grace."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider(created_at=NOW)
    row = await _provision(org_admin, mt, provider, session_id="s-twin")
    (own,) = provider.pods
    provider.pods["pod-twin"] = ProviderPod(
        pod_id="pod-twin", name=pod_name_for(row.id), created_at=NOW, phase=STARTING
    )

    async with AsyncSessionLocal() as db:
        early = await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=NOW)
    assert (early.terminated, provider.terminate_calls) == (0, [])

    async with AsyncSessionLocal() as db:
        late = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW + timedelta(hours=1)
        )
    assert (late.adopted, late.terminated) == (0, 1)
    assert provider.terminate_calls == ["pod-twin"]
    (kept,) = await _rows(org_admin)
    assert kept.provider_machine_id == own


async def test_an_orphan_pod_is_terminated_once_it_is_past_the_grace_and_never_before(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A pod no live row names is pure loss — but a young one may belong to a
    create still in flight, so the grace is not negotiable."""
    provider = FakeProvider()
    born = NOW - timedelta(minutes=2)
    provider.pods["orphan"] = ProviderPod(
        pod_id="orphan", name=f"{pod_name_prefix()}deadbeef0001", created_at=born
    )

    async with AsyncSessionLocal() as db:
        early = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=born + timedelta(minutes=1)
        )
    assert (early.terminated, early.held) == (0, 1)
    assert provider.terminate_calls == []

    async with AsyncSessionLocal() as db:
        late = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=born + timedelta(hours=2)
        )
    assert late.terminated == 1
    assert provider.terminate_calls == ["orphan"]
    assert provider.pods == {}


async def test_a_pod_whose_age_the_provider_did_not_report_is_never_reaped(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The grace is the only thing between this pass and a machine somebody is
    mid-way through creating; an unknown age cannot clear it."""
    provider = FakeProvider()
    provider.pods["ageless"] = ProviderPod(
        pod_id="ageless", name=f"{pod_name_prefix()}deadbeef0002", created_at=None
    )

    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW + timedelta(days=30)
        )

    assert (summary.terminated, summary.held) == (0, 1)
    assert provider.terminate_calls == []


async def test_a_sibling_deployment_whose_prefix_extends_ours_is_never_touched(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision the advice itself invites. The module tells an operator
    sharing a provider account to give each deployment its own prefix, and the
    obvious way to do that is to EXTEND the default — ``alkera`` and
    ``alkera-staging``. Under a prefix TEST, ``alkera-staging-<hex>`` starts
    with ``alkera-``, so production's pass lists staging's machines, finds no
    live row of that name, and reaps them. The match is anchored, so the two
    deployments are disjoint in both directions."""
    staging = ProviderPod(
        pod_id="staging-box",
        name="alkera-staging-aabbccddeeff",
        created_at=NOW - timedelta(days=3),
    )
    production = ProviderPod(
        pod_id="prod-box",
        name="alkera-aabbccddeeff",
        created_at=NOW - timedelta(days=3),
    )

    # Production's pass must not see staging's machine.
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "alkera")
    provider = FakeProvider(pods={"s": staging, "p": production})
    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=NOW)
    assert summary.listed == 1, "production listed a pod that is not its own"
    assert provider.terminate_calls == ["prod-box"]
    assert "s" in provider.pods, "production terminated staging's machine"

    # And staging's pass must not see production's.
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "alkera-staging")
    provider = FakeProvider(pods={"s": staging, "p": production})
    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=NOW)
    assert summary.listed == 1
    assert provider.terminate_calls == ["staging-box"]
    assert "p" in provider.pods, "staging terminated production's machine"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("alkera-aabbccddeef", id="eleven-hex-is-not-an-allocation-id"),
        pytest.param("alkera-aabbccddeeff0", id="thirteen-hex-is-not-either"),
        pytest.param("alkera-AABBCCDDEEFF", id="uppercase-is-not-a-uuid-hex"),
        pytest.param("alkera-aabbccddeefg", id="g-is-not-hex"),
        pytest.param("alkera-aabbccddeeff-2", id="a-suffix-is-somebody-else-s"),
        pytest.param("xalkera-aabbccddeeff", id="a-longer-prefix-is-somebody-else-s"),
    ],
)
def test_only_a_name_shaped_exactly_like_ours_is_ours(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything a terminate could be aimed at, and is not."""
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "alkera")
    assert is_our_pod_name(name) is False
    assert is_our_pod_name("alkera-aabbccddeeff") is True


async def test_another_deployments_pods_are_never_listed_adopted_or_terminated(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The name prefix is the whole blast radius. Two deployments share one
    provider account; a staging reconciler that treated production's pods as
    orphans would terminate them."""
    provider = FakeProvider()
    provider.pods["theirs"] = ProviderPod(
        pod_id="theirs",
        name="otherdeploy-aaaabbbbcccc",
        created_at=NOW - timedelta(days=3),
    )
    provider.pods["mine"] = ProviderPod(
        pod_id="mine",
        name=f"{pod_name_prefix()}deadbeef0003",
        created_at=NOW - timedelta(days=3),
    )

    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=NOW)

    assert summary.listed == 1
    assert provider.terminate_calls == ["mine"]
    assert "theirs" in provider.pods


async def test_a_provider_that_cannot_enumerate_terminates_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A refusal is "not here", never "there are no pods" — reading an empty
    list out of an error would be a licence to terminate the fleet."""
    for error in (
        ComputeProviderUnavailableError("the container service is not provisioned"),
        ComputeProviderError("HTTP 500", status_code=500),
    ):
        provider = FakeProvider(list_error=error)
        provider.pods["mine"] = ProviderPod(
            pod_id="mine",
            name=f"{pod_name_prefix()}deadbeef0004",
            created_at=NOW - timedelta(days=3),
        )
        async with AsyncSessionLocal() as db:
            summary = await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=NOW)
        assert summary.unavailable is True
        assert (summary.terminated, summary.adopted) == (0, 0)
        assert provider.terminate_calls == []


async def test_a_create_no_pod_answers_for_is_written_off_once_the_window_passes(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The last state a timed-out create can end in: no pod of that name exists,
    so the provider never built one. The row is closed — otherwise it holds a
    grant slot for a machine nobody has."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider(
        create_error="RunPod request failed: ReadTimeout", create_status_code=None
    )
    with pytest.raises(ComputeProviderError):
        await _provision(org_admin, mt, provider, session_id="s-writeoff")
    (row,) = await _rows(org_admin)
    created = row.created_at

    # Inside the window: the row is left alone, because the pod may yet appear.
    async with AsyncSessionLocal() as db:
        await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=created + timedelta(minutes=1)
        )
    (row,) = await _rows(org_admin)
    assert row.state == "provisioning"

    async with AsyncSessionLocal() as db:
        late = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=created + timedelta(hours=2)
        )
    assert late.written_off >= 1
    (row,) = await _rows(org_admin)
    assert (row.state, row.terminated_reason) == ("failed", CREATE_UNCONFIRMED)
    assert row.released_at is not None


async def test_a_truncated_listing_never_writes_off_a_row_whose_machine_is_running(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The write-off is an argument FROM ABSENCE, so it is only sound against a
    listing that is actually complete. With a capped listing, a row whose pod
    exists but fell off the end reads as "no pod of this name" — the row is
    closed, its grant slot freed, and on the next pass the very machine the
    customer is paying for matches no live row and is terminated as an orphan.
    A pass that cannot see the whole fleet writes nothing off."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider(
        create_error="RunPod request failed: ReadTimeout", create_status_code=None, created_at=NOW
    )
    with pytest.raises(ComputeProviderError):
        await _provision(org_admin, mt, provider, session_id="s-truncated")
    (row,) = await _rows(org_admin)
    # The machine the timed-out create really built, plus enough other pods
    # that a capped listing would not reach it.
    provider.pods["decoy"] = ProviderPod(
        pod_id="decoy", name=f"{pod_name_prefix()}deadbeef0009", created_at=NOW
    )
    provider.pods["mine"] = ProviderPod(pod_id="mine", name=pod_name_for(row.id), created_at=NOW)

    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db,
            provider=provider,
            provider_kind=RUNPOD,
            now=NOW + timedelta(hours=2),
            max_pods=1,
        )

    assert summary.listing_truncated is True
    assert summary.written_off == 0
    (row,) = await _rows(org_admin)
    assert row.state == "provisioning", "a row was closed while its machine was running"
    assert "mine" in provider.pods, "the customer's machine was terminated"

    # With the whole fleet visible the same pass adopts it instead.
    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW + timedelta(hours=2)
        )
    assert summary.listing_truncated is False
    (row,) = await _rows(org_admin)
    assert row.provider_machine_id == "mine"


async def test_a_run_of_untrusted_listings_is_named_as_a_stall_not_just_repeated(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing endpoint takes no name filter, so the ceiling is measured
    against the provider's WHOLE account. On an account this deployment shares,
    a sibling holding more pods than the ceiling makes every one of our passes
    read as untrusted: we fail safe and write nothing off, but grant-slot
    recovery stops and every period logs a line identical to the last. The
    streak is counted so the stall becomes a distinct fact with a number on
    it, and so it CLEARS the moment a listing is trustworthy again."""
    # Production's pass, on an account staging shares.
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "alkera")
    provider = FakeProvider()
    # A sibling deployment's machine, named the way the docs tell an operator to
    # name one. It reaches us because the listing endpoint has no name filter —
    # which is the whole reason the ceiling counts the account and not just ours.
    provider.pods["sibling"] = ProviderPod(
        pod_id="sibling", name="alkera-staging-aaaabbbbcccc", created_at=NOW
    )
    watch = TruncationWatch(alert_after=3)

    streaks = []
    for _ in range(4):
        async with AsyncSessionLocal() as db:
            summary = await reconcile_pods(
                db,
                provider=provider,
                provider_kind=RUNPOD,
                now=NOW,
                max_pods=1,
                watch=watch,
            )
        streaks.append(summary.truncated_streak)

    assert streaks == [1, 2, 3, 4]
    assert summary.listing_truncated is True
    # Ours is zero even though the account is full — that distinction is the
    # whole point of reporting both counts.
    assert (summary.listed, summary.listed_all) == (0, 1)

    # A pass that CAN see the whole account clears the streak.
    async with AsyncSessionLocal() as db:
        recovered = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW, watch=watch
        )
    assert recovered.listing_truncated is False
    assert recovered.truncated_streak == 0
    assert watch.streak == 0


def test_the_stall_is_announced_once_the_run_is_long_enough_and_not_before() -> None:
    """The watch on its own: it must not cry stall on the first bad pass, and
    it must keep saying so while the stall lasts."""
    watch = TruncationWatch(alert_after=3)

    assert [watch.record(truncated=True) for _ in range(5)] == [False, False, True, True, True]
    assert watch.record(truncated=False) is False
    assert watch.streak == 0
    assert watch.record(truncated=True) is False  # the count starts over


async def test_one_pass_terminates_no_more_orphans_than_its_budget(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The destructive step is the budgeted one. A pass that suddenly finds a
    great many unowned machines is likelier misconfigured than right, so it
    stops at the budget, says so, and leaves the rest for the next tick — and
    it does NOT write anything off while it is behind."""
    provider = FakeProvider()
    for n in range(5):
        provider.pods[f"orphan-{n}"] = ProviderPod(
            pod_id=f"orphan-{n}",
            name=f"{pod_name_prefix()}beef0000000{n}",
            created_at=NOW - timedelta(days=1),
        )

    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW, max_terminations=2
        )

    assert summary.terminated == 2
    assert summary.deferred_terminations == 3
    assert len(provider.terminate_calls) == 2
    assert len(provider.pods) == 3  # the rest survive to the next pass

    # The next pass picks up where this one stopped.
    async with AsyncSessionLocal() as db:
        again = await reconcile_pods(
            db, provider=provider, provider_kind=RUNPOD, now=NOW, max_terminations=2
        )
    assert again.terminated == 2
    assert len(provider.pods) == 1


async def test_a_row_of_another_provider_is_never_written_off_by_this_ones_listing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The listing came from ONE provider. A row whose machine type belongs to
    another was not looked for at all, so closing it frees a grant slot while
    its machine runs somewhere this pass never asked about."""
    mt = await make_machine_type(real_session, provider="ec2")
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, org_admin.admin_id)
        assert user is not None
        alloc = ComputeAllocation(
            user_id=user.id,
            org_team_id=org_admin.org_id,
            machine_type_id=mt.id,
            lifecycle="session",
            state="provisioning",
            provider_machine_id="",
            created_at=NOW - timedelta(days=1),
            session_id="s-other-provider",
        )
        session.add(alloc)
        await session.commit()

    # A RunPod pass, with an empty RunPod fleet, must leave it alone.
    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=FakeProvider(), provider_kind=RUNPOD, now=NOW + timedelta(days=2)
        )

    assert summary.written_off == 0
    (row,) = await _rows(org_admin)
    assert row.state == "provisioning"

    # The pass that DID list that provider closes it.
    async with AsyncSessionLocal() as db:
        summary = await reconcile_pods(
            db, provider=FakeProvider(), provider_kind="ec2", now=NOW + timedelta(days=2)
        )
    assert summary.written_off == 1
    (row,) = await _rows(org_admin)
    assert (row.state, row.terminated_reason) == ("failed", CREATE_UNCONFIRMED)


async def test_a_second_pass_changes_nothing_it_already_did(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Idempotent by construction: a schedule that fires twice, or a retry
    after a partial pass, must not adopt twice or terminate what is gone."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    provider = FakeProvider(
        create_error="RunPod request failed: ReadTimeout", create_status_code=None, created_at=NOW
    )
    with pytest.raises(ComputeProviderError):
        await _provision(org_admin, mt, provider, session_id="s-twice")
    (row,) = await _rows(org_admin)
    provider.pods["pod-real"] = ProviderPod(
        pod_id="pod-real", name=pod_name_for(row.id), created_at=NOW
    )
    provider.pods["orphan"] = ProviderPod(
        pod_id="orphan",
        name=f"{pod_name_prefix()}deadbeef0005",
        created_at=NOW - timedelta(days=1),
    )

    at = NOW + timedelta(hours=2)
    async with AsyncSessionLocal() as db:
        first = await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=at)
    async with AsyncSessionLocal() as db:
        second = await reconcile_pods(db, provider=provider, provider_kind=RUNPOD, now=at)

    assert (first.adopted, first.terminated) == (1, 1)
    assert (second.adopted, second.terminated) == (0, 0)
    assert provider.terminate_calls == ["orphan"]
    await _assert_every_pod_is_accounted_for(provider, org_admin)
