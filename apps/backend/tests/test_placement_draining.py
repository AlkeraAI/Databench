"""A box that was told to stop takes nothing new, and its chats move.

A deploy stops a box by draining it: the box says so on its next heartbeat and
keeps answering the turns it already holds. From that moment the plane must
treat it as leaving — placement never picks it, and the chats sitting on it are
offered to whichever box is up — while still treating it as LIVE, because it is
running, it is metered, and the turn it is finishing is somebody's answer.

Every case here is one a careless filter gets wrong in one of two ways that
both show up as a chat nobody answers: a chat placed on a process that is
exiting, or a live box dropped off the plane while it still holds turns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.machines import (
    current_machine,
    live_workspace_machines,
    machine_state,
    machine_status,
)
from alkera_core.compute.provider import EC2, RUNPOD
from alkera_core.models.compute import (
    DEDICATED_TENANCY,
    DRAINING,
    ORG_TENANCY,
    POOL_TENANCY,
    ComputeAllocation,
    ComputeMachineType,
)
from backend.services.compute import machines as machine_service
from backend.services.compute import placement
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import backfilled_org_machine
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The pool is global, so a box another test left live would serve this
    test's org. Release every platform box before each case."""
    from sqlalchemy import update

    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)))
        .values(state="released")
    )
    await real_session.commit()


async def _box(
    session: AsyncSession,
    *,
    org: OrgWithAdmin,
    name: str,
    tenancy: str = ORG_TENANCY,
    state: str = "ready",
    beat: datetime | None | str = "fresh",
    capacity: int = 6,
    chats_served: int = 0,
    mt: ComputeMachineType | None = None,
) -> ComputeAllocation:
    machine_type = mt or await make_machine_type(
        session, provider=EC2 if tenancy != ORG_TENANCY else RUNPOD
    )
    stamp = datetime.now(UTC) if beat == "fresh" else (None if beat == "never" else beat)
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        name=name,
        tenancy=tenancy,
        sandbox="gvisor" if tenancy != ORG_TENANCY else "none",
        capacity=capacity,
        chats_served=chats_served,
        state=state,
        provider_machine_id=f"pod-{name}",
        last_heartbeat_at=stamp if not isinstance(stamp, str) else None,
    )
    session.add(alloc)
    await session.commit()
    if tenancy != ORG_TENANCY:
        await hold_with_credential(session, alloc)
    return alloc


async def _place(
    session: AsyncSession, org: OrgWithAdmin, *, prefer: UUID | None = None
) -> str | None:
    ctx = ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)
    binding = await placement.resolve_machine_for(
        session, ctx=ctx, org_team_id=org.org_id, purpose="chat", prefer=prefer
    )
    return None if binding is None else binding.name


# --------------------------------------------------------------------------- #
# what "draining" reads as
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "beat", "expected"),
    [
        pytest.param("ready", "fresh", "ready", id="beating-and-serving"),
        pytest.param(DRAINING, "fresh", "draining", id="beating-and-leaving"),
        pytest.param(DRAINING, "stale", "unreachable", id="silence-outranks-draining"),
        pytest.param(DRAINING, "never", "draining", id="never-beat-but-leaving"),
        pytest.param("ready", "never", "starting", id="never-beat-and-serving"),
    ],
)
async def test_what_a_reader_is_told_about_a_box_that_is_leaving(
    real_session: AsyncSession, org_admin: OrgWithAdmin, state: str, beat: str, expected: str
) -> None:
    stamp: datetime | None | str
    if beat == "stale":
        stamp = datetime.now(UTC) - timedelta(hours=1)
    else:
        stamp = beat
    box = await _box(real_session, org=org_admin, name="b", state=state, beat=stamp)
    assert machine_status(box) == expected
    assert machine_state(box) == expected


async def test_a_draining_box_is_still_on_the_plane(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """It is running and metered, and the turn it is finishing is an answer
    somebody is waiting on. Dropping it from the live read is how the meter
    would stop billing a machine that is still up."""
    box = await _box(real_session, org=org_admin, name="leaving", state=DRAINING)
    live = (await real_session.execute(live_workspace_machines(org_admin.org_id))).scalars().all()
    assert [row.id for row in live] == [box.id]
    placeable = (
        (await real_session.execute(live_workspace_machines(org_admin.org_id, placeable=True)))
        .scalars()
        .all()
    )
    assert placeable == []


# --------------------------------------------------------------------------- #
# the allocator
# --------------------------------------------------------------------------- #


async def test_the_pool_skips_a_draining_box_for_a_ready_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The draining box is the emptier one, so load alone would pick it."""
    await _box(
        real_session,
        org=platform_admin,
        name="leaving",
        tenancy=POOL_TENANCY,
        state=DRAINING,
        chats_served=0,
    )
    await _box(
        real_session,
        org=platform_admin,
        name="staying",
        tenancy=POOL_TENANCY,
        chats_served=5,
    )
    assert await _place(real_session, org_admin) == "staying"


async def test_a_pool_of_nothing_but_draining_boxes_places_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """No box is better than the one that is exiting: a chat placed there is
    a chat the next process has to move again, having answered nothing."""
    await _box(
        real_session, org=platform_admin, name="leaving", tenancy=POOL_TENANCY, state=DRAINING
    )
    assert await _place(real_session, org_admin) is None


async def test_a_draining_box_is_not_kept_just_because_the_chat_is_on_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """``prefer`` keeps a chat where it is — but not onto a box that is
    leaving, which is the one case where staying is the wrong answer."""
    leaving = await _box(
        real_session, org=platform_admin, name="leaving", tenancy=POOL_TENANCY, state=DRAINING
    )
    await _box(real_session, org=platform_admin, name="staying", tenancy=POOL_TENANCY)
    assert await _place(real_session, org_admin, prefer=leaving.id) == "staying"


async def test_an_orgs_own_draining_box_does_not_hold_its_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """An org's own box outranks the pool — until it is leaving, at which
    point the pool is where the org's next chat runs."""
    await _box(real_session, org=org_admin, name="own", state=DRAINING)
    await _box(real_session, org=platform_admin, name="pool", tenancy=POOL_TENANCY)
    assert await current_machine(real_session, org_id=org_admin.org_id) is None
    assert await _place(real_session, org_admin) == "pool"


async def test_a_dedicated_box_that_is_leaving_falls_back_when_allowed(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    dedicated = await _box(
        real_session,
        org=platform_admin,
        name="dedicated",
        tenancy=DEDICATED_TENANCY,
        state=DRAINING,
    )
    await _box(real_session, org=platform_admin, name="pool", tenancy=POOL_TENANCY)
    await backfilled_org_machine(
        real_session, org_id=org_admin.org_id, box=dedicated, fallback=True
    )
    assert await _place(real_session, org_admin) == "pool"


async def test_a_dedicated_box_that_is_leaving_with_no_fallback_places_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The org runs on its own box and no other, so a drain is a wait — never
    a quiet move onto the shared pool it was kept off on purpose."""
    dedicated = await _box(
        real_session,
        org=platform_admin,
        name="dedicated",
        tenancy=DEDICATED_TENANCY,
        state=DRAINING,
    )
    await _box(real_session, org=platform_admin, name="pool", tenancy=POOL_TENANCY)
    await backfilled_org_machine(
        real_session, org_id=org_admin.org_id, box=dedicated, fallback=False
    )
    assert await _place(real_session, org_admin) is None


async def test_a_chat_on_a_draining_box_is_stranded_and_moves(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The hand-over, from the plane's side: the box the chat is bound to is
    leaving, so the chat reads as unserveable and the next message places it
    somewhere else."""
    leaving = await _box(
        real_session, org=platform_admin, name="leaving", tenancy=POOL_TENANCY, state=DRAINING
    )
    assert (
        await placement.bound_machine_state(
            real_session, org_team_id=org_admin.org_id, machine_id=str(leaving.id)
        )
        in placement.UNSERVEABLE
    )


# --------------------------------------------------------------------------- #
# how a box gets into and out of the state
# --------------------------------------------------------------------------- #


async def test_a_heartbeat_that_says_draining_takes_the_box_out_of_placement(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    box = await _box(real_session, org=platform_admin, name="leaving", tenancy=POOL_TENANCY)
    assert await _place(real_session, org_admin) == "leaving"
    ctx = ActingContext.for_user(
        user_id=platform_admin.admin_id,
        org_id=platform_admin.org_id,
        email=platform_admin.admin_email,
    )
    await machine_service.heartbeat(real_session, box, ctx=ctx, draining=True)
    assert box.state == DRAINING
    assert await _place(real_session, org_admin) is None


@pytest.mark.parametrize("said", [None, False], ids=["said-nothing", "said-not-draining"])
async def test_a_later_beat_cannot_put_a_leaving_box_back_in_the_rotation(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    said: bool | None,
) -> None:
    """A drain is one-way for the life of the row. The process that drained is
    exiting; a beat that un-drained it would hand a chat to a box that is on
    its way out, which is exactly the deploy hazard the drain exists for. Only
    a fresh registration clears it."""
    box = await _box(
        real_session, org=platform_admin, name="leaving", tenancy=POOL_TENANCY, state=DRAINING
    )
    ctx = ActingContext.for_user(
        user_id=platform_admin.admin_id,
        org_id=platform_admin.org_id,
        email=platform_admin.admin_email,
    )
    await machine_service.heartbeat(real_session, box, ctx=ctx, draining=said)
    assert box.state == DRAINING
    assert await _place(real_session, org_admin) is None


async def test_registering_again_is_what_puts_a_box_back_in_the_rotation(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A registration on a draining row is a NEW daemon on the box: the one
    that drained has exited. Without this the replacement process would beat
    for ever against a row nothing may be placed on."""
    machine_type = await make_machine_type(real_session, provider=RUNPOD)
    box = await _box(real_session, org=org_admin, name="own", state=DRAINING, mt=machine_type)
    ctx = ActingContext.for_user(
        user_id=org_admin.admin_id, org_id=org_admin.org_id, email=org_admin.admin_email
    )
    from alkera_core.models import User

    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    again, created = await machine_service.register_machine(
        real_session,
        ctx=ctx,
        user=admin,
        machine_type=machine_type,
        provider_pod_id=box.provider_machine_id,
        name="own",
    )
    assert created is False
    assert again.id == box.id
    assert again.state == "ready"
    assert await _place(real_session, org_admin) == "own"


# --------------------------------------------------------------------------- #
# a supervised restart: out of placement, chats stay, the row says restarting
# --------------------------------------------------------------------------- #


async def _frames(session: AsyncSession, box: ComputeAllocation) -> list[str]:
    from alkera_core.models import EventOutbox
    from sqlalchemy import select

    rows = await session.execute(
        select(EventOutbox)
        .where(EventOutbox.type == "compute_machine.changed", EventOutbox.entity_id == str(box.id))
        .order_by(EventOutbox.id)
    )
    return [row.payload["status"] for row in rows.scalars().all()]


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def test_a_restarting_beat_takes_the_box_out_of_placement_and_says_restarting(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    box = await _box(real_session, org=platform_admin, name="restarting", tenancy=POOL_TENANCY)
    # Beats as a current pool box does: a worker per org, in namespaces that
    # keep the orgs apart, so whatever other org's stranded chat its readiness
    # adopts does not close it to this org.
    workers = [BoxCapability.ORG_WORKERS, BoxCapability.ORG_ISOLATION]
    await machine_service.heartbeat(
        real_session, box, ctx=_ctx(platform_admin), capabilities=workers
    )
    assert await _place(real_session, org_admin) == "restarting"

    await machine_service.heartbeat(
        real_session, box, ctx=_ctx(platform_admin), restarting=True, capabilities=workers
    )

    assert machine_status(box) == "restarting"
    assert machine_state(box) == "restarting"
    assert await _place(real_session, org_admin) is None


async def test_a_chat_on_a_restarting_box_stays_where_it_is(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The box keeps its leases through the restart and takes the chats back
    within seconds; moving them off would hand them to a box that cannot take
    their folders."""
    box = await _box(real_session, org=platform_admin, name="restarting", tenancy=POOL_TENANCY)
    await machine_service.heartbeat(real_session, box, ctx=_ctx(platform_admin), restarting=True)
    state = await placement.bound_machine_state(
        real_session, org_team_id=org_admin.org_id, machine_id=str(box.id)
    )
    assert state == "restarting"
    assert state not in placement.UNSERVEABLE


async def test_a_plain_drain_after_a_restart_turns_it_into_an_ordinary_drain(
    real_session: AsyncSession, platform_admin: OrgWithAdmin
) -> None:
    box = await _box(real_session, org=platform_admin, name="b", tenancy=POOL_TENANCY)
    ctx = _ctx(platform_admin)
    await machine_service.heartbeat(real_session, box, ctx=ctx, restarting=True)
    await machine_service.heartbeat(real_session, box, ctx=ctx, draining=True)
    assert machine_status(box) == "draining"
    await machine_service.heartbeat(real_session, box, ctx=ctx, restarting=True)
    assert machine_status(box) == "draining", "a drain is one-way; a restart cannot soften it"


async def test_each_transition_of_a_restart_is_announced_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """ready -> restarting on the beat, restarting -> ready on the new
    process's registration; a repeated beat that changes nothing emits nothing."""
    from alkera_core.models import User

    machine_type = await make_machine_type(real_session, provider=RUNPOD)
    box = await _box(real_session, org=org_admin, name="own", mt=machine_type)
    ctx = _ctx(org_admin)
    await machine_service.heartbeat(real_session, box, ctx=ctx)
    await machine_service.heartbeat(real_session, box, ctx=ctx, restarting=True)
    await machine_service.heartbeat(real_session, box, ctx=ctx, restarting=True)
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    again, _ = await machine_service.register_machine(
        real_session,
        ctx=ctx,
        user=admin,
        machine_type=machine_type,
        provider_pod_id=box.provider_machine_id,
        name="own",
        daemon_instance_id="proc-next",
    )
    await machine_service.heartbeat(real_session, again, ctx=ctx, daemon_instance_id="proc-next")

    assert machine_status(again) == "ready"
    assert again.drain_kind is None
    assert await _frames(real_session, box) == ["ready", "restarting", "ready"]
    assert await _place(real_session, org_admin) == "own"


@pytest.mark.parametrize("kind", ["draining", "restarting"])
async def test_registration_clears_both_kinds_of_leaving(
    real_session: AsyncSession, org_admin: OrgWithAdmin, kind: str
) -> None:
    from alkera_core.models import User

    machine_type = await make_machine_type(real_session, provider=RUNPOD)
    box = await _box(real_session, org=org_admin, name="own", mt=machine_type)
    ctx = _ctx(org_admin)
    await machine_service.heartbeat(real_session, box, ctx=ctx, **{kind: True})
    assert machine_status(box) == kind
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    again, _ = await machine_service.register_machine(
        real_session,
        ctx=ctx,
        user=admin,
        machine_type=machine_type,
        provider_pod_id=box.provider_machine_id,
        name="own",
    )
    assert machine_status(again) == "ready"
