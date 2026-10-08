"""Where a chat runs once the platform has a pool and org machines.

The rule, in order (unpinned chats): the org's own box; the org pool, for an
Enterprise org, whose machines are the only ones its chats run on unless the
org allows the shared pool while they are down; the shared pool, spread by
load and kept where the chat already is. Every branch here is
one a careless query would get wrong in a way that lands another org's chat
on a box it must never touch, so each is pinned on the placement service
directly and once more through the binding a box's arrival makes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alkera_core.authz import ActingContext
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.provider import EC2, RUNPOD
from alkera_core.models import OrgComputeAssignment, WorkspaceObject
from alkera_core.models.compute import (
    DEDICATED_TENANCY,
    ORG_TENANCY,
    POOL_TENANCY,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.permission_presentation import PERMISSION_MODES, PermissionModeSpec
from backend.services.chats import chat_service
from backend.services.compute import placement
from backend.services.org import teams
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import backfilled_org_machine
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

#: What a current pool box says it can do: a worker per org, each in
#: namespaces of its own (``alkera_core.compute.box_isolation``).
CURRENT_POOL_BOX = [BoxCapability.ORG_WORKERS, BoxCapability.ORG_ISOLATION]

Beat = Literal["fresh", "stale", "never"]
"""When a box last heartbeated, relative to the moment the row is written:
``fresh`` is inside the ready window, ``stale`` is an hour past it, ``never``
is a box that registered and has not beaten. Resolved at call time, never at
import: the window is 45 s, and a stamp taken when the module was collected
reads as unreachable by the time a long run reaches these cases."""


def _beat_at(beat: Beat) -> datetime | None:
    if beat == "never":
        return None
    now = datetime.now(UTC)
    return now if beat == "fresh" else now - timedelta(hours=1)


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The pool is global by design, so a pool box another test left live
    would serve this test's org. Every platform box on the plane is released
    first; an org's own boxes are org-scoped and need no such sweep."""
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
    beat: Beat = "fresh",
    capacity: int = 6,
    chats_served: int = 0,
    sandbox: str = "gvisor",
    mt: ComputeMachineType | None = None,
) -> ComputeAllocation:
    """A live workspace machine. ``org`` is its operator org: for a platform
    box that is the org whose admin minted it, never the org it serves. A
    platform box reports ``gvisor`` by default (what a real pool box claims
    with); a guard test overrides it to ``none``. A pool box runs a worker per
    org, as a current pool box says it does; a test about a box that serves
    every org from one process clears its capabilities, and one about a box
    that cannot keep orgs apart drops ``org_isolation``."""
    machine_type = mt or await make_machine_type(
        session, provider=EC2 if tenancy != ORG_TENANCY else RUNPOD
    )
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        name=name,
        tenancy=tenancy,
        sandbox=sandbox,
        capacity=capacity,
        chats_served=chats_served,
        state="ready",
        provider_machine_id=f"pod-{name}",
        last_heartbeat_at=_beat_at(beat),
        capabilities_json=CURRENT_POOL_BOX if tenancy == POOL_TENANCY else None,
    )
    session.add(alloc)
    await session.commit()
    if tenancy != ORG_TENANCY:
        await hold_with_credential(session, alloc)
    return alloc


async def _assign(
    session: AsyncSession, *, org: OrgWithAdmin, box: ComputeAllocation, fallback: bool = False
) -> None:
    await backfilled_org_machine(session, org_id=org.org_id, box=box, fallback=fallback)


async def _place(
    session: AsyncSession,
    org: OrgWithAdmin,
    *,
    prefer: UUID | None = None,
    permission_mode: str | None = None,
) -> str | None:
    ctx = ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)
    binding = await placement.resolve_machine_for(
        session,
        ctx=ctx,
        org_team_id=org.org_id,
        purpose="chat",
        prefer=prefer,
        permission_mode=permission_mode,  # type: ignore[arg-type]
    )
    return None if binding is None else binding.name


# --------------------------------------------------------------------------- #
# the pool
# --------------------------------------------------------------------------- #


async def test_an_org_with_nothing_of_its_own_runs_on_the_pool(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    assert await _place(real_session, org_admin) == "pool-a"


@pytest.mark.parametrize("tenancy", [POOL_TENANCY, DEDICATED_TENANCY])
async def test_a_platform_box_whose_credential_was_revoked_takes_no_chat(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    tenancy: str,
) -> None:
    """A platform box stands on the credential the platform minted for it.
    Revoked, the box is taken away from placement at once — even the emptiest
    box in the pool, even the box an org is dedicated to — rather than taking
    chats until the meter notices it went quiet."""
    from alkera_core.models import MachineCredential
    from sqlalchemy import update

    taken = await _box(
        real_session, org=platform_admin, name="taken-away", tenancy=tenancy, capacity=50
    )
    if tenancy == DEDICATED_TENANCY:
        await _assign(real_session, org=org_admin, box=taken, fallback=True)
    await _box(
        real_session,
        org=platform_admin,
        name="still-held",
        tenancy=POOL_TENANCY,
        capacity=4,
        chats_served=3,
    )
    assert await _place(real_session, org_admin) == "taken-away"

    await real_session.execute(
        update(MachineCredential)
        .where(MachineCredential.machine_id == taken.id)
        .values(revoked_at=datetime.now(UTC))
    )
    await real_session.commit()

    assert await _place(real_session, org_admin) == "still-held"


async def test_the_pool_is_spread_to_the_box_with_the_most_room(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """Load is chats against capacity, not a raw count: a big box holding
    more chats can still be the emptier one."""
    await _box(
        real_session,
        org=platform_admin,
        name="small-busy",
        tenancy=POOL_TENANCY,
        capacity=4,
        chats_served=3,
    )
    await _box(
        real_session,
        org=platform_admin,
        name="big-calm",
        tenancy=POOL_TENANCY,
        capacity=20,
        chats_served=5,
    )
    assert await _place(real_session, org_admin) == "big-calm"


async def test_a_box_at_capacity_is_passed_over_while_another_has_room(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(
        real_session,
        org=platform_admin,
        name="full",
        tenancy=POOL_TENANCY,
        capacity=2,
        chats_served=2,
    )
    await _box(
        real_session,
        org=platform_admin,
        name="room",
        tenancy=POOL_TENANCY,
        capacity=2,
        chats_served=1,
    )
    assert await _place(real_session, org_admin) == "room"


async def test_a_pool_where_every_box_is_full_still_answers(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A busy pool answers slowly; it does not strand the chat."""
    await _box(
        real_session,
        org=platform_admin,
        name="fuller",
        tenancy=POOL_TENANCY,
        capacity=2,
        chats_served=4,
    )
    await _box(
        real_session,
        org=platform_admin,
        name="full",
        tenancy=POOL_TENANCY,
        capacity=2,
        chats_served=2,
    )
    assert await _place(real_session, org_admin) == "full"


async def test_a_chat_stays_on_the_pool_box_that_holds_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The box already serving the chat holds its folder: it is kept even when
    another box has more room."""
    holder = await _box(
        real_session,
        org=platform_admin,
        name="holder",
        tenancy=POOL_TENANCY,
        capacity=2,
        chats_served=2,
    )
    await _box(real_session, org=platform_admin, name="empty", tenancy=POOL_TENANCY, capacity=6)
    assert await _place(real_session, org_admin, prefer=holder.id) == "holder"


async def test_a_chat_leaves_a_pool_box_that_went_quiet(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    gone = await _box(
        real_session, org=platform_admin, name="gone", tenancy=POOL_TENANCY, beat="stale"
    )
    await _box(real_session, org=platform_admin, name="alive", tenancy=POOL_TENANCY)
    assert await _place(real_session, org_admin, prefer=gone.id) == "alive"


async def _failing_for(session: AsyncSession, box: ComputeAllocation, org: OrgWithAdmin) -> None:
    """The box's last beat said the org's worker keeps failing there."""
    box.resources_json = {"org_workers_failing_ids": [str(org.org_id)]}
    await session.commit()


async def test_a_pool_box_failing_an_orgs_worker_is_passed_over_for_that_org_only(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """Even the box holding the chat is passed over: its worker for the org
    is not running, so nothing there answers the org's chats. Every other
    org is still placed on it."""
    failing = await _box(real_session, org=platform_admin, name="failing", tenancy=POOL_TENANCY)
    await _box(real_session, org=platform_admin, name="other", tenancy=POOL_TENANCY, chats_served=5)
    await _failing_for(real_session, failing, org_admin)
    assert await _place(real_session, org_admin, prefer=failing.id) == "other"
    assert await _place(real_session, second_org_admin, prefer=failing.id) == "failing"


async def test_a_chat_on_a_box_failing_its_orgs_worker_reads_unserved(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """So its next message moves it (``rebind_if_stranded``), as off a box
    that went quiet; another org's chat on the same box stays."""
    box = await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    await _failing_for(real_session, box, org_admin)
    for org, expected in ((org_admin, "none"), (second_org_admin, "ready")):
        state = await placement.bound_machine_state(
            real_session, org_team_id=org.org_id, machine_id=str(box.id)
        )
        assert state == expected


async def test_a_pool_box_that_registered_but_has_not_beaten_serves_only_when_none_is_ready(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(
        real_session, org=platform_admin, name="starting", tenancy=POOL_TENANCY, beat="never"
    )
    assert await _place(real_session, org_admin) == "starting"
    await _box(real_session, org=platform_admin, name="ready", tenancy=POOL_TENANCY)
    assert await _place(real_session, org_admin) == "ready"


async def test_a_quiet_pool_places_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(real_session, org=platform_admin, name="gone", tenancy=POOL_TENANCY, beat="stale")
    assert await _place(real_session, org_admin) is None


async def test_an_orgs_own_box_beats_the_pool(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    await _box(real_session, org=org_admin, name="own")
    assert await _place(real_session, org_admin) == "own"


async def test_a_pool_box_is_not_the_operator_orgs_own_machine(
    real_session: AsyncSession, platform_admin: OrgWithAdmin
) -> None:
    """The operator org is the pool box's ``org_team_id``; it must reach the
    box through the pool like anyone else, not as its own machine — the
    banner and the org-preferred placement would otherwise show every tenant's
    pool box as the operator's."""
    from alkera_core.compute.machines import current_machine

    await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    assert await current_machine(real_session, org_id=platform_admin.org_id) is None
    assert await _place(real_session, platform_admin) == "pool-a"


# --------------------------------------------------------------------------- #
# dedicated compute
# --------------------------------------------------------------------------- #


async def test_an_assigned_org_runs_only_on_its_dedicated_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    dedicated = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=org_admin, box=dedicated)
    assert await _place(real_session, org_admin) == "theirs"


async def test_the_orgs_own_box_beats_its_org_pool(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A box a member registered for the org is asked before the org pool."""
    await _box(real_session, org=org_admin, name="own")
    dedicated = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=org_admin, box=dedicated)
    assert await _place(real_session, org_admin) == "own"


async def test_an_assigned_org_waits_while_its_box_is_down(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """Fallback is off by default: an enterprise org's chats run on its box
    or not at all, never on a shared host it did not agree to."""
    await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    down = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY, beat="stale"
    )
    await _assign(real_session, org=org_admin, box=down)
    assert await _place(real_session, org_admin) is None


async def test_an_assigned_org_falls_back_to_the_pool_only_when_the_flag_says_so(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    down = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY, beat="stale"
    )
    await _assign(real_session, org=org_admin, box=down, fallback=True)
    assert await _place(real_session, org_admin) == "pool-a"


async def test_a_fallen_back_org_returns_to_its_box_when_it_is_up(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    pool = await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    dedicated = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=org_admin, box=dedicated, fallback=True)
    assert await _place(real_session, org_admin, prefer=pool.id) == "theirs"


async def test_a_dedicated_box_never_takes_another_orgs_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The only dedicated box is somebody else's: an unassigned org sees no
    machine at all rather than that one."""
    dedicated = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=platform_admin, box=dedicated)
    assert await _place(real_session, org_admin) is None


async def test_an_unassigned_dedicated_box_serves_nobody(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _box(real_session, org=platform_admin, name="idle", tenancy=DEDICATED_TENANCY)
    assert await _place(real_session, org_admin) is None
    assert await _place(real_session, platform_admin) is None


async def test_the_schema_holds_one_org_per_dedicated_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    from sqlalchemy.exc import IntegrityError

    dedicated = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY
    )
    real_session.add(OrgComputeAssignment(org_team_id=org_admin.org_id, machine_id=dedicated.id))
    await real_session.commit()
    real_session.add(
        OrgComputeAssignment(org_team_id=platform_admin.org_id, machine_id=dedicated.id)
    )
    with pytest.raises(IntegrityError):
        await real_session.commit()
    await real_session.rollback()


# --------------------------------------------------------------------------- #
# a chat's binding reads the tenancy too
# --------------------------------------------------------------------------- #


async def test_a_chat_bound_to_a_pool_box_reads_that_box_as_live(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The pool box's row belongs to the operator org; a chat in any other org
    bound to it must still read ``ready``, or every pool chat would be
    rebound on every message."""
    pool = await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)
    state = await placement.bound_machine_state(
        real_session, org_team_id=org_admin.org_id, machine_id=str(pool.id)
    )
    assert state == "ready"


async def test_a_chat_bound_to_another_orgs_dedicated_box_reads_none(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    dedicated = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=platform_admin, box=dedicated)
    state = await placement.bound_machine_state(
        real_session, org_team_id=org_admin.org_id, machine_id=str(dedicated.id)
    )
    assert state == "none"


# --------------------------------------------------------------------------- #
# a platform box coming up binds the stranded chats it now serves
# --------------------------------------------------------------------------- #


async def _stranded_chat(
    session: AsyncSession, org: OrgWithAdmin, title: str, *, permission_mode: str | None = None
) -> WorkspaceObject:
    from alkera_core.models import User

    owner = await session.get(User, org.admin_id)
    assert owner is not None
    extra = {"permission_mode": permission_mode} if permission_mode is not None else {}
    chat, _ = await chat_service.create_chat(
        session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title=title,
        client_id=None,
        machine_id=None,
        machine_status="none",
        **extra,  # type: ignore[arg-type]
    )
    await session.commit()
    return chat


async def _most_recent_first(session: AsyncSession, chats: list[WorkspaceObject]) -> None:
    """Stamp ``chats`` as the most recently active chats in the database, in
    list order (the first the most recent). A box coming up serves the most
    recently active stranded chats first, and this database holds every other
    test's leftovers, so a test about which chats are taken has to own the
    front of the queue; and two creates a millisecond apart must not tie."""
    base = datetime.now(UTC)
    for index, chat in enumerate(chats):
        await session.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.id == chat.id)
            .values(updated_at=base - timedelta(milliseconds=index))
        )
    await session.commit()


async def _bound_to(session: AsyncSession, chat: WorkspaceObject) -> str | None:
    await session.refresh(chat)
    return chat_service.chat_spec_of(chat).machine_id


async def test_a_pool_box_coming_up_takes_stranded_chats_of_orgs_that_place_on_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    waiting = await _stranded_chat(real_session, org_admin, "waiting")
    pool = await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
    await real_session.commit()

    # The pool is global: whatever other orgs' chats were stranded ride along.
    assert waiting.id in {c.id for c in moved}
    assert await _bound_to(real_session, waiting) == str(pool.id)


async def test_a_pool_box_leaves_the_stranded_chats_of_an_org_that_waits_for_its_own_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    waiting = await _stranded_chat(real_session, org_admin, "waiting")
    down = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY, beat="stale"
    )
    await _assign(real_session, org=org_admin, box=down)
    pool = await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
    await real_session.commit()

    assert waiting.id not in {c.id for c in moved}
    assert await _bound_to(real_session, waiting) is None


async def test_a_dedicated_box_coming_up_takes_only_its_orgs_stranded_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    theirs = await _stranded_chat(real_session, org_admin, "theirs")
    others = await _stranded_chat(real_session, platform_admin, "others")
    dedicated = await _box(
        real_session, org=platform_admin, name="theirs", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=org_admin, box=dedicated)

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=dedicated, actor=None)
    await real_session.commit()

    assert [c.id for c in moved] == [theirs.id]
    assert await _bound_to(real_session, theirs) == str(dedicated.id)
    assert await _bound_to(real_session, others) is None


# -- a box coming up respects its capacity as first placement does -------------


async def test_a_pool_box_coming_up_takes_the_most_recently_active_stranded_chats_up_to_its_room(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A box of capacity 4 already serving 1 has room for 3. Five stranded
    chats of two orgs wait: the three most recently active bind, whatever
    their org; the two quietest stay stranded; and the box's own report is
    untouched. Before the room bound, a pool box that came up took every
    stranded chat of every org that placed on it — seventy-nine on a box of
    four."""
    orgs = (org_admin, platform_admin)
    chats = [await _stranded_chat(real_session, orgs[i % 2], f"waiting-{i}") for i in range(5)]
    await _most_recent_first(real_session, chats)
    pool = await _box(
        real_session,
        org=platform_admin,
        name="pool-room",
        tenancy=POOL_TENANCY,
        capacity=4,
        chats_served=1,
    )

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
    await real_session.commit()

    assert [c.id for c in moved] == [c.id for c in chats[:3]]
    for chat in chats[:3]:
        assert await _bound_to(real_session, chat) == str(pool.id)
    for chat in chats[3:]:
        assert await _bound_to(real_session, chat) is None
    await real_session.refresh(pool)
    assert pool.chats_served == 1, "the report is the box's own; nothing here writes it"


async def test_a_pool_box_with_no_room_binds_nothing_and_the_chats_wait_for_the_next_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """First placement takes a full box only for a message that must be
    answered now. A box coming up has no such message: it takes nothing past
    its room, and the chats wait — the next box with room takes them, in the
    same order."""
    chats = [await _stranded_chat(real_session, org_admin, f"waiting-{i}") for i in range(3)]
    await _most_recent_first(real_session, chats)
    full = await _box(
        real_session,
        org=platform_admin,
        name="pool-full",
        tenancy=POOL_TENANCY,
        capacity=2,
        chats_served=2,
    )

    assert await placement.bind_stranded_chats_platform(real_session, alloc=full, actor=None) == []
    await real_session.commit()
    for chat in chats:
        assert await _bound_to(real_session, chat) is None

    roomy = await _box(
        real_session, org=platform_admin, name="pool-roomy", tenancy=POOL_TENANCY, capacity=5
    )
    moved = await placement.bind_stranded_chats_platform(real_session, alloc=roomy, actor=None)
    await real_session.commit()
    assert [c.id for c in moved][:3] == [c.id for c in chats]
    for chat in chats:
        assert await _bound_to(real_session, chat) == str(roomy.id)


async def test_a_second_pass_before_the_boxs_next_report_sees_the_room_already_taken(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A box comes up twice in quick succession — its registration, then its
    first heartbeat — and between them it has reported nothing. The chats the
    first pass bound are counted against the room, so the second pass binds no
    more; when two of them are deleted the room reopens, and the next pass
    takes the rest, in the same order."""
    chats = [await _stranded_chat(real_session, org_admin, f"waiting-{i}") for i in range(5)]
    await _most_recent_first(real_session, chats)
    pool = await _box(
        real_session, org=platform_admin, name="pool-twice", tenancy=POOL_TENANCY, capacity=3
    )

    first = await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
    await real_session.commit()
    assert [c.id for c in first] == [c.id for c in chats[:3]]

    again = await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
    await real_session.commit()
    assert again == []
    for chat in chats[3:]:
        assert await _bound_to(real_session, chat) is None

    for gone in chats[:2]:
        await real_session.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.id == gone.id)
            .values(deleted_at=datetime.now(UTC).timestamp())
        )
    await real_session.commit()
    third = await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
    await real_session.commit()
    assert [c.id for c in third] == [c.id for c in chats[3:]]


async def test_a_dedicated_box_coming_up_is_not_bounded_by_the_pools_room_rule(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A single-tenant box is its org's only box and first placement binds to
    it whatever its load; a box coming up does the same."""
    chats = [await _stranded_chat(real_session, org_admin, f"theirs-{i}") for i in range(3)]
    await _most_recent_first(real_session, chats)
    dedicated = await _box(
        real_session,
        org=platform_admin,
        name="ded-small",
        tenancy=DEDICATED_TENANCY,
        capacity=1,
        chats_served=1,
    )
    await _assign(real_session, org=org_admin, box=dedicated)

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=dedicated, actor=None)
    await real_session.commit()
    assert [c.id for c in moved] == [c.id for c in chats]
    assert placement.pool_room(dedicated, bound=3) is None


# -- a box coming up admits an org exactly as first placement does -------------


async def test_a_pool_box_coming_up_admits_each_org_as_first_placement_would(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
) -> None:
    """Three orgs, one pool box coming up. An org with nothing of its own
    places on the pool, so its chat binds — and that includes the operator's
    own org, which reaches the pool like anyone else (there is no tier or
    ownership rule that keeps it off). An org whose own box is live places
    there, so its chat stays. An org on a dedicated box that is down with no
    fallback waits, so its chat stays. Each answer is the one
    :func:`resolve_machine_for` gives that org's next message."""
    on_pool = await _stranded_chat(real_session, platform_admin, "operator-org-on-the-pool")
    has_own = await _stranded_chat(real_session, org_admin, "has-its-own-box")
    waits = await _stranded_chat(real_session, platform_support, "waits-for-its-box")
    await _most_recent_first(real_session, [waits, has_own, on_pool])
    await _box(real_session, org=org_admin, name="own-box")
    down = await _box(
        real_session,
        org=platform_admin,
        name="theirs-down",
        tenancy=DEDICATED_TENANCY,
        beat="stale",
    )
    await _assign(real_session, org=platform_support, box=down)
    pool = await _box(real_session, org=platform_admin, name="pool-a", tenancy=POOL_TENANCY)

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
    await real_session.commit()

    assert on_pool.id in {c.id for c in moved}
    assert await _bound_to(real_session, on_pool) == str(pool.id)
    assert await _bound_to(real_session, has_own) is None
    assert await _bound_to(real_session, waits) is None
    assert await _place(real_session, platform_admin) == "pool-a"
    assert await _place(real_session, org_admin) == "own-box"
    assert await _place(real_session, platform_support) is None


async def test_a_none_pool_box_coming_up_takes_no_chat_writable_or_not(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The sandbox rule first placement applies, applied to the handoff: a
    pool box that reports no boundary is off the pool, so a box coming up
    that way binds nothing — not a writable chat, not even a read-only one,
    exactly as :func:`resolve_machine_for` answers both."""
    writable = await _stranded_chat(real_session, org_admin, "writable", permission_mode="default")
    reading = await _stranded_chat(real_session, org_admin, "reading", permission_mode="read_only")
    await _most_recent_first(real_session, [writable, reading])
    none_pool = await _box(
        real_session, org=platform_admin, name="none-pool", tenancy=POOL_TENANCY, sandbox="none"
    )

    assert (
        await placement.bind_stranded_chats_platform(real_session, alloc=none_pool, actor=None)
        == []
    )
    await real_session.commit()
    assert await _bound_to(real_session, writable) is None
    assert await _bound_to(real_session, reading) is None
    assert await _place(real_session, org_admin, permission_mode="default") is None
    assert await _place(real_session, org_admin, permission_mode="read_only") is None


async def test_a_dedicated_none_box_coming_up_takes_its_orgs_writable_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    writable = await _stranded_chat(real_session, org_admin, "writable", permission_mode="default")
    dedicated = await _box(
        real_session, org=platform_admin, name="ded-none", tenancy=DEDICATED_TENANCY, sandbox="none"
    )
    await _assign(real_session, org=org_admin, box=dedicated)

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=dedicated, actor=None)
    await real_session.commit()
    assert [c.id for c in moved] == [writable.id]
    assert await _place(real_session, org_admin, permission_mode="default") == "ded-none"


@pytest.mark.parametrize(
    ("tenancy", "capacity", "served", "bound", "room"),
    [
        pytest.param(POOL_TENANCY, 4, 1, 0, 3, id="pool-with-room"),
        pytest.param(POOL_TENANCY, 4, 1, 3, 1, id="what-is-bound-counts-before-the-report-does"),
        pytest.param(POOL_TENANCY, 4, 3, 1, 1, id="the-report-counts-when-it-is-the-larger"),
        pytest.param(POOL_TENANCY, 2, 2, 0, 0, id="pool-full"),
        pytest.param(POOL_TENANCY, 2, 5, 0, 0, id="pool-past-capacity-is-not-negative-room"),
        pytest.param(POOL_TENANCY, 2, 0, 7, 0, id="bound-past-capacity-is-not-negative-room"),
        pytest.param(DEDICATED_TENANCY, 1, 1, 4, None, id="dedicated-unbounded"),
        pytest.param(ORG_TENANCY, 1, 1, 4, None, id="org-unbounded"),
    ],
)
def test_pool_room_is_what_the_report_or_the_bound_chats_leave_and_only_on_the_pool(
    tenancy: str, capacity: int, served: int, bound: int, room: int | None
) -> None:
    alloc = ComputeAllocation(tenancy=tenancy, capacity=capacity, chats_served=served)
    assert placement.pool_room(alloc, bound=bound) == room


# -- the sandbox placement guard (W5 / finding F1) ----------------------------


@pytest.mark.parametrize(
    ("tenancy", "sandbox", "writable", "allowed"),
    [
        pytest.param(POOL_TENANCY, "none", True, False, id="writable-pool-none-refused"),
        pytest.param(POOL_TENANCY, "gvisor", True, True, id="writable-pool-gvisor-allowed"),
        pytest.param(DEDICATED_TENANCY, "none", True, True, id="writable-dedicated-none-allowed"),
        pytest.param(ORG_TENANCY, "none", True, True, id="writable-org-none-allowed"),
        pytest.param(POOL_TENANCY, "none", False, True, id="readonly-pool-none-allowed"),
    ],
)
def test_may_place_chat_gates_only_a_writable_chat_on_a_non_gvisor_pool_box(
    tenancy: str, sandbox: str, writable: bool, allowed: bool
) -> None:
    alloc = ComputeAllocation(tenancy=tenancy, sandbox=sandbox)
    assert placement.may_place_chat(alloc, writable=writable) is allowed


@pytest.mark.parametrize(
    ("mode", "writable"),
    [
        ("default", True),
        ("auto", True),
        ("bypass", True),
        ("read_only", False),
        ("plan", False),
        (None, True),  # unknown/missing is treated as writable — fail safe
        ("nonsense", True),
    ],
)
def test_chat_is_writable_reads_the_permission_mode(mode: str | None, writable: bool) -> None:
    assert placement.chat_is_writable(mode) is writable


@pytest.mark.parametrize(
    "mode", [pytest.param(m, id=m.value) for m in PERMISSION_MODES if m.writes]
)
def test_every_mode_the_owner_marks_as_writing_needs_a_sandboxed_pool_box(
    mode: PermissionModeSpec,
) -> None:
    """Read from the mode vocabulary's owner, so a writing stance added there is
    covered here without editing this test: it is refused a pool box with no
    sandbox and admitted on a gVisor one."""
    writable = placement.chat_is_writable(mode.value)
    none_box = ComputeAllocation(tenancy=POOL_TENANCY, sandbox="none")
    gvisor_box = ComputeAllocation(tenancy=POOL_TENANCY, sandbox="gvisor")
    assert placement.may_place_chat(none_box, writable=writable) is False
    assert placement.may_place_chat(gvisor_box, writable=writable) is True


@pytest.mark.parametrize(
    "mode", [pytest.param(m, id=m.value) for m in PERMISSION_MODES if not m.writes]
)
def test_no_read_only_mode_needs_a_sandboxed_box(mode: PermissionModeSpec) -> None:
    none_box = ComputeAllocation(tenancy=POOL_TENANCY, sandbox="none")
    assert placement.may_place_chat(none_box, writable=placement.chat_is_writable(mode.value))


def test_the_writing_modes_are_today_default_auto_and_bypass() -> None:
    assert placement.WRITABLE_PERMISSION_MODES == frozenset({"default", "auto", "bypass"})


async def test_a_writable_chat_lands_on_a_gvisor_pool_box_never_a_none_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    # Only a none pool box exists: a writable chat has nowhere to go.
    await _box(
        real_session, org=platform_admin, name="none-pool", tenancy=POOL_TENANCY, sandbox="none"
    )
    assert await _place(real_session, org_admin, permission_mode="default") is None

    # A gvisor pool box comes up: the same chat is placed on it.
    await _box(
        real_session, org=platform_admin, name="gvisor-pool", tenancy=POOL_TENANCY, sandbox="gvisor"
    )
    assert await _place(real_session, org_admin, permission_mode="default") == "gvisor-pool"


async def test_the_allocator_never_returns_a_none_pool_box_even_for_a_read_only_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    # A pool box must report gvisor to serve anyone: a none pool box is off the
    # pool entirely, so even a read-only chat is not placed on it.
    await _box(
        real_session, org=platform_admin, name="none-pool", tenancy=POOL_TENANCY, sandbox="none"
    )
    assert await _place(real_session, org_admin, permission_mode="read_only") is None


async def test_a_dedicated_none_box_serves_its_orgs_writable_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    # A single-tenant dedicated box that accepted no boundary still serves its
    # own org's writable chats — one tenant's own code on its own box.
    dedicated = await _box(
        real_session, org=platform_admin, name="ded-none", tenancy=DEDICATED_TENANCY, sandbox="none"
    )
    await _assign(real_session, org=org_admin, box=dedicated)
    assert await _place(real_session, org_admin, permission_mode="default") == "ded-none"


# --------------------------------------------------------------------------- #
# the worker budget of a box that runs a worker per org
# --------------------------------------------------------------------------- #


async def _budgeted_box(
    session: AsyncSession,
    platform_admin: OrgWithAdmin,
    name: str,
    *,
    workers: int,
    slots_free: int | None = None,
) -> ComputeAllocation:
    """A pool box that runs a worker per org, reporting ``workers`` as its
    budget (0: none reported) and ``slots_free`` as the slots a new org can
    still take (``None``: not reported)."""
    box = await _box(session, org=platform_admin, name=name, tenancy=POOL_TENANCY, capacity=20)
    box.capabilities_json = CURRENT_POOL_BOX
    box.resources_json = {"org_worker_capacity": workers}
    if slots_free is not None:
        box.resources_json["org_slots_free"] = slots_free
    await session.commit()
    return box


async def _bind_here(session: AsyncSession, box: ComputeAllocation, chat: WorkspaceObject) -> None:
    await placement._move(  # the org now has a chat (a worker) on the box
        session, [chat], binding=placement._binding(box), actor=None, org_id=None
    )
    await session.commit()


@pytest_asyncio.fixture
async def second_org_admin(real_session: AsyncSession) -> OrgWithAdmin:
    """A second org, so a box can be asked about two."""
    tag = uuid4().hex[:10]
    org, admin = await teams.create_org_with_admin(
        real_session,
        org_name=f"Second org {tag}",
        admin_email=f"second-{tag}@example.com",
        admin_first_name="Second",
        admin_last_name="Admin",
        admin_password="admin-pass-12345",
    )
    await real_session.commit()
    return OrgWithAdmin(
        org_id=org.id,
        admin_id=admin.id,
        admin_email=f"second-{tag}@example.com",
        admin_password="admin-pass-12345",
    )


async def test_a_box_whose_worker_budget_is_spent_takes_no_new_org(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """Each org costs the box a worker process; one whose budget is spent is
    passed over for an org it does not already run, and still takes more of
    the org it does."""
    full = await _budgeted_box(real_session, platform_admin, "one-worker", workers=1)
    held = await _stranded_chat(real_session, second_org_admin, "already here")
    await placement._move(  # the org already has a worker there
        real_session, [held], binding=placement._binding(full), actor=None, org_id=None
    )
    await real_session.commit()
    assert await _place(real_session, org_admin) is None
    assert await _place(real_session, second_org_admin) == "one-worker"
    await _budgeted_box(real_session, platform_admin, "room-for-two", workers=2)
    assert await _place(real_session, org_admin) == "room-for-two"


async def test_a_box_running_org_workers_that_reports_no_budget_is_not_gated(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    box = await _budgeted_box(real_session, platform_admin, "no-budget", workers=0)
    await _bind_here(real_session, box, await _stranded_chat(real_session, second_org_admin, "x"))
    assert await _place(real_session, org_admin) == "no-budget"


@pytest.mark.parametrize(
    ("slots_free", "new_org_placed"),
    [
        pytest.param(0, False, id="no-slot-free"),
        pytest.param(1, True, id="a-slot-free"),
        pytest.param(None, True, id="not-reported"),
    ],
)
async def test_a_box_with_no_worker_slot_free_takes_no_new_org(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    slots_free: int | None,
    new_org_placed: bool,
) -> None:
    """A box that said no worker slot is free is passed over for an org it
    holds nothing for, within its budget or not; an org already there still
    lands on it. A box that never reports slots (an older box) is not gated
    by them."""
    box = await _budgeted_box(
        real_session, platform_admin, "slots", workers=5, slots_free=slots_free
    )
    await _bind_here(real_session, box, await _stranded_chat(real_session, second_org_admin, "x"))
    assert (await _place(real_session, org_admin) == "slots") is new_org_placed
    assert await _place(real_session, second_org_admin) == "slots"


async def test_a_box_coming_up_with_no_slot_free_takes_no_new_orgs_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    waiting = await _stranded_chat(real_session, org_admin, "waiting")
    await _most_recent_first(real_session, [waiting])
    box = await _budgeted_box(real_session, platform_admin, "full-up", workers=5, slots_free=0)
    await placement.bind_stranded_chats_platform(real_session, alloc=box, actor=None)
    await real_session.commit()
    assert await _bound_to(real_session, waiting) is None


@pytest.mark.parametrize(
    "capabilities",
    [pytest.param(None, id="never-said"), pytest.param(["workspaces"], id="said-other-things")],
)
async def test_a_pool_box_serving_every_org_in_one_process_takes_one_org(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    capabilities: list[str] | None,
) -> None:
    """A pool box that does not say ``org_workers`` runs every chat it holds
    in one process, one home and one state tree: once one org's chat is bound
    there, a second org is refused it, whatever budget the box reports, and
    lands on another box when one is up. The org already there keeps it."""
    old = await _box(real_session, org=platform_admin, name="one-process", tenancy=POOL_TENANCY)
    old.capabilities_json = capabilities
    old.resources_json = {"org_worker_capacity": 10}
    await real_session.commit()
    assert await _place(real_session, org_admin) == "one-process"  # empty: any one org
    await _bind_here(real_session, old, await _stranded_chat(real_session, second_org_admin, "x"))
    assert await _place(real_session, org_admin) is None
    assert await _place(real_session, second_org_admin) == "one-process"
    await _box(real_session, org=platform_admin, name="elsewhere", tenancy=POOL_TENANCY)
    assert await _place(real_session, org_admin) == "elsewhere"


@pytest.mark.parametrize(
    ("ran_org_workers", "capabilities", "placed"),
    [
        pytest.param(True, None, False, id="rolled-back"),
        pytest.param(True, [BoxCapability.ORG_WORKERS], True, id="still-running-workers"),
        pytest.param(False, None, True, id="never-ran-workers"),
    ],
)
async def test_a_pool_box_rolled_back_from_org_workers_is_given_no_chat(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    ran_org_workers: bool,
    capabilities: list[str] | None,
    placed: bool,
) -> None:
    """A box that once ran a worker per org and now serves every org from one
    process is refused every chat route on its machine credential, so it can
    serve no chat: placement gives it none, not even a first org, and its
    coming up rescues nothing onto it. A box that never ran workers is the
    one-org box it always was."""
    box = await _box(real_session, org=platform_admin, name="maybe", tenancy=POOL_TENANCY)
    box.capabilities_json = capabilities
    box.ran_org_workers_at = datetime.now(UTC) if ran_org_workers else None
    await real_session.commit()
    assert (await _place(real_session, org_admin) == "maybe") is placed
    waiting = await _stranded_chat(real_session, org_admin, "waiting")
    await _most_recent_first(real_session, [waiting])
    await placement.bind_stranded_chats_platform(real_session, alloc=box, actor=None)
    await real_session.commit()
    assert (await _bound_to(real_session, waiting) == str(box.id)) is placed


async def test_a_single_tenant_box_serving_in_one_process_is_not_held_to_one_org(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The one-org rule is the shared pool's: a dedicated box serves its org
    and its operator org from one process as it always did."""
    dedicated = await _box(
        real_session, org=platform_admin, name="ded-one-process", tenancy=DEDICATED_TENANCY
    )
    await _assign(real_session, org=org_admin, box=dedicated)
    await _bind_here(
        real_session, dedicated, await _stranded_chat(real_session, platform_admin, "op")
    )
    assert await _place(real_session, org_admin) == "ded-one-process"


async def test_a_one_process_box_coming_up_takes_the_stranded_chats_of_one_org(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    first = await _stranded_chat(real_session, org_admin, "first org")
    also_first = await _stranded_chat(real_session, org_admin, "first org again")
    second = await _stranded_chat(real_session, second_org_admin, "second org")
    await _most_recent_first(real_session, [first, second, also_first])
    box = await _box(real_session, org=platform_admin, name="old-pool", tenancy=POOL_TENANCY)
    box.capabilities_json = None
    await real_session.commit()
    await placement.bind_stranded_chats_platform(real_session, alloc=box, actor=None)
    await real_session.commit()
    assert await _bound_to(real_session, first) == str(box.id)
    assert await _bound_to(real_session, also_first) == str(box.id)
    assert await _bound_to(real_session, second) is None


async def test_a_box_coming_up_admits_no_more_new_orgs_than_its_budget(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    first = await _stranded_chat(real_session, org_admin, "first org")
    second = await _stranded_chat(real_session, second_org_admin, "second org")
    await _most_recent_first(real_session, [first, second])
    box = await _budgeted_box(real_session, platform_admin, "budget-one", workers=1)
    await placement.bind_stranded_chats_platform(real_session, alloc=box, actor=None)
    await real_session.commit()
    assert await _bound_to(real_session, first) == str(box.id)
    assert await _bound_to(real_session, second) is None


def test_the_worker_budget_reads_only_a_positive_whole_number() -> None:
    for raw, want in ((3, 3), (0, 0), (-1, 0), (True, 0), ("3", 0), (None, 0)):
        box = ComputeAllocation(resources_json={"org_worker_capacity": raw})
        assert placement.org_worker_capacity(box) == want
    assert placement.org_worker_capacity(ComputeAllocation(resources_json=None)) == 0


@pytest.mark.parametrize(
    ("resources", "want"),
    [
        pytest.param({"org_slots_free": 2}, 2, id="slots"),
        pytest.param({"org_slots_free": 0}, 0, id="none-free"),
        pytest.param({"org_slots_free": -1}, None, id="negative"),
        pytest.param({"org_slots_free": True}, None, id="a-bool"),
        pytest.param({"org_slots_free": None}, None, id="null"),
        pytest.param({}, None, id="not-reported"),
        pytest.param(None, None, id="no-sample"),
    ],
)
def test_the_free_slots_read_only_a_whole_number(
    resources: dict[str, object] | None, want: int | None
) -> None:
    assert placement.org_slots_free(ComputeAllocation(resources_json=resources)) == want


# -- a box that cannot keep orgs apart serves one --------------------------------


@pytest.mark.parametrize(
    ("tenancy", "capabilities"),
    [
        pytest.param(POOL_TENANCY, [BoxCapability.ORG_WORKERS], id="pool-box-on-a-container-host"),
        pytest.param(POOL_TENANCY, [BoxCapability.ORG_ISOLATION], id="isolation-without-workers"),
        pytest.param(DEDICATED_TENANCY, [BoxCapability.ORG_WORKERS], id="dedicated-runpod-box"),
    ],
)
async def test_a_box_that_cannot_keep_orgs_apart_never_gets_a_second_org(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    tenancy: str,
    capabilities: list[str],
) -> None:
    """A box runs a worker per org but its host gave it no namespaces to put
    them in (a RunPod pod): every worker shares the host's processes and
    files, so once one org is there no other org is placed on it, whatever
    worker budget and free slots it reports. The org already there keeps it,
    and the other org lands on a box that proved it can keep orgs apart."""
    box = await _box(real_session, org=platform_admin, name="no-isolation", tenancy=tenancy)
    box.capabilities_json = capabilities
    box.resources_json = {"org_worker_capacity": 50, "org_slots_free": 49}
    await real_session.commit()
    await _bind_here(real_session, box, await _stranded_chat(real_session, second_org_admin, "x"))
    assert not placement.has_worker_room(box, {second_org_admin.org_id}, org_admin.org_id)
    assert placement.has_worker_room(box, {second_org_admin.org_id}, second_org_admin.org_id)
    if tenancy == POOL_TENANCY:
        assert await _place(real_session, org_admin) is None
        assert await _place(real_session, second_org_admin) == "no-isolation"
        await _box(real_session, org=platform_admin, name="isolating", tenancy=POOL_TENANCY)
        assert await _place(real_session, org_admin) == "isolating"


async def test_a_box_coming_up_without_isolation_adopts_one_orgs_stranded_chats(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second_org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """The other door: a box arriving binds the stranded chats waiting for
    one. A box that cannot keep orgs apart takes the chats of one org only."""
    first = await _stranded_chat(real_session, org_admin, "first")
    second = await _stranded_chat(real_session, second_org_admin, "second")
    await _most_recent_first(real_session, [first, second])
    box = await _box(real_session, org=platform_admin, name="pod", tenancy=POOL_TENANCY)
    box.capabilities_json = [BoxCapability.ORG_WORKERS]
    await real_session.commit()
    await placement.bind_stranded_chats_platform(real_session, alloc=box, actor=None)
    await real_session.commit()
    bound = {await _bound_to(real_session, first), await _bound_to(real_session, second)}
    assert bound == {str(box.id), None}


async def test_a_box_whose_worker_cannot_serve_is_given_no_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A box that answers but whose last beat said no worker of it can serve
    is passed over while it says so, and taken again once it serves."""
    box = await _box(real_session, org=platform_admin, name="faulted", tenancy=POOL_TENANCY)
    box.fault_code = "cgroup_refused"
    box.fault_since = datetime.now(UTC)
    await real_session.commit()
    assert await _place(real_session, org_admin) is None
    box.fault_code = None
    box.fault_until = datetime.now(UTC)
    await real_session.commit()
    assert await _place(real_session, org_admin) == "faulted"
