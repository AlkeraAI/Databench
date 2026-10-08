"""A machine that leaves service takes its placement facts with it, in the
transaction that moves it — never by a later sweep.

The failure this pins: a dedicated box was released through the console
while sixty-seven of its org's chats were bound to it. Nothing could take
them (the org had no pool fallback and no other box), so ``rebind_chats_off``
left every one exactly as it was: ``machine_status = ready`` on a released
machine, the console reading "67 / 10" on a row that was gone, and the org's
dedicated assignment still naming the dead box — which kept the org's next
chat off the pool it was otherwise entitled to.

Three invariants, each driven through the real routes and the reconcile:

* a chat is never reported ``ready`` / ``starting`` against a machine that is
  not serving — a release restates the chats it cannot move as ``stranded``
  and announces each, and a chat stored as ``ready`` on a box that is gone
  (a row no transaction ever restated) reads ``stranded`` on every surface;
* the console's count beside a machine is live load, and a released row
  carries its stranded count separately, never as load;
* an org's dedicated assignment never outlives its machine: the release drops
  it, records the org audit event, names the org in its answer, and the org
  places by the ordinary rules from that moment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.compute.node_reconcile import reconcile_nodes
from alkera_core.compute.provider import GONE
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OrgAuditEvent, OrgComputeAssignment
from alkera_core.models.compute import POOL_TENANCY, ComputeAllocation
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from backend.services.compute import provisioning
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._org_machine_helpers import set_fallback
from tests._placement_helpers import (
    assign,
    assignment_of,
    beat,
    chat_frames,
    console_detail,
    console_rows,
    current_machine,
    legacy_chat,
    lifecycle_action,
    listed_chat,
    machine_edges,
    machine_row,
    open_chat,
    platform_box,
    read_chat,
    spec_of,
)
from tests.conftest import OrgWithAdmin

# Runs the node reconcile — a fleet-wide pass — so it shares the serial lane.
pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("compute-fleet")]


@pytest.fixture(autouse=True)
async def _quiet_plane(real_session: AsyncSession) -> None:
    """The pool is global and an assignment is per org: nothing another test
    left standing may serve this test's org."""
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.state.not_in(("released", "failed")))
        .values(state="released")
    )
    await real_session.commit()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeNodeProvider:
    """The one node provider every box in a test is started at; a terminate
    is confirmed on the next describe, so a release finishes inline."""
    provider = FakeNodeProvider(kind="ec2")
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: provider)
    return provider


async def _audit(org: OrgWithAdmin) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_team_id == org.org_id,
                OrgAuditEvent.action == provisioning.DEDICATED_RELEASED_ACTION,
            )
        )
        return list(rows.scalars().all())


async def _terminate(
    client: AsyncClient, admin: OrgWithAdmin, box: ComputeAllocation, *, force: bool = True
) -> dict[str, Any]:
    return await lifecycle_action(
        client, admin, box.id, "terminate", json={"force": force}, expect=202
    )


# --------------------------------------------------------------------------- #
# the release transaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "provider_confirms",
    [
        pytest.param(True, id="the-provider-confirms-and-the-row-is-released"),
        pytest.param(False, id="the-provider-refuses-and-the-row-stays-releasing"),
    ],
)
async def test_a_release_strands_the_chats_nothing_can_take_and_says_so(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
    provider_confirms: bool,
) -> None:
    """The failure, replayed: a dedicated org with no fallback and no other
    box. The moment the machine is taken out of service — ``releasing``, before
    the provider is even asked — every chat bound to it is restated
    ``stranded`` and announced, the org's assignment is gone with an audit
    event, and the answer names the org. Whether the provider then confirms
    the terminate changes the row's state and nothing else."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="none-6")
    await assign(real_session, org=org_admin, box=box)
    if not provider_confirms:
        fake.fail_terminate.add("i-none-6")
    chats = [await open_chat(org_admin, f"chat {i}") for i in range(3)]
    assert {c["machine_id"] for c in chats} == {str(box.id)}
    assert {c["machine_status"] for c in chats} == {"ready"}
    frames_before = {c["id"]: await chat_frames(c["id"]) for c in chats}
    audit_before = len(await _audit(org_admin))

    answer = await _terminate(client, platform_admin, box)

    assert answer["state"] == ("released" if provider_confirms else "releasing")
    assert [org["id"] for org in answer["released_orgs"]] == [str(org_admin.org_id)]
    assert answer["chats_moved"] == 0
    assert answer["chats_stranded"] == 3
    assert answer["chats_served"] == 0, "a row that left service carries no load"
    # Counted before anyone reads the chats: a stranded chat reads asleep, and a
    # reader opening it asks for its wake (one frame of its own).
    for chat in chats:
        assert await chat_frames(chat["id"]) == frames_before[chat["id"]] + 1, (
            "each stranded chat is announced once, so an open page re-reads it"
        )
    for chat in chats:
        spec = await spec_of(chat["id"])
        assert spec["machine_status"] == "stranded"
        assert spec["machine_id"] == str(box.id), "the binding keeps which box last had it"
        read = await read_chat(org_admin, chat["id"])
        assert (read["machine_id"], read["machine_status"]) == (str(box.id), "stranded")
        assert (await listed_chat(org_admin, chat["id"]))["machine_status"] == "stranded"
    assert await assignment_of(org_admin) is None, "the assignment does not outlive the machine"
    events = await _audit(org_admin)
    assert len(events) == audit_before + 1
    assert events[-1].target == str(box.id)
    assert events[-1].actor_email == platform_admin.admin_email
    assert events[-1].detail is not None and events[-1].detail["machine_name"] == "none-6"
    # Nothing serves the org now, and the page says so rather than "ready".
    assert (await current_machine(org_admin))["status"] == "none"


async def test_a_release_hands_the_chats_to_the_pool_once_the_assignment_is_gone(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """An org that allows the shared pool while its own machines cannot serve
    is served by the pool the moment its machine's box is released: its
    existing chats move there in the release transaction, and its next chat
    is placed there."""
    pool = await platform_box(
        real_session, fake, operator=platform_admin, name="pool-a", tenancy=POOL_TENANCY
    )
    box = await platform_box(real_session, fake, operator=platform_admin, name="dedicated-a")
    await assign(real_session, org=org_admin, box=box, fallback=True)
    chat = await open_chat(org_admin, "moves to the pool")
    assert chat["machine_id"] == str(box.id)

    answer = await _terminate(client, platform_admin, box)

    assert (answer["chats_moved"], answer["chats_stranded"]) == (1, 0)
    moved = await read_chat(org_admin, chat["id"])
    assert (moved["machine_id"], moved["machine_status"]) == (str(pool.id), "ready")
    assert (await current_machine(org_admin))["status"] == "pool"
    fresh = await open_chat(org_admin, "placed on the pool")
    assert (fresh["machine_id"], fresh["machine_status"]) == (str(pool.id), "ready")


async def test_a_release_without_force_still_strands_the_chats_the_box_did_not_report(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """``chats_served`` is the box's own last report; a box reporting zero
    can still have chats bound to it (parked, or bound after its last beat).
    The hand-off is not a ``force`` feature — it runs on every release."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="quiet")
    await assign(real_session, org=org_admin, box=box)
    chat = await open_chat(org_admin, "bound but unreported")
    await real_session.execute(
        update(ComputeAllocation).where(ComputeAllocation.id == box.id).values(chats_served=0)
    )
    await real_session.commit()

    answer = await _terminate(client, platform_admin, box, force=False)

    assert answer["chats_stranded"] == 1
    assert (await read_chat(org_admin, chat["id"]))["machine_status"] == "stranded"


async def test_the_next_box_that_serves_the_org_binds_its_stranded_chats(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """Stranding keeps the chat's binding on the released box, and placement
    still reads it as stranded: a replacement dedicated box coming up takes
    every one of them on its first heartbeat, and they read ``ready`` again."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="old")
    await assign(real_session, org=org_admin, box=box)
    chats = [await open_chat(org_admin, f"waits {i}") for i in range(2)]
    await _terminate(client, platform_admin, box)
    assert {(await spec_of(c["id"]))["machine_status"] for c in chats} == {"stranded"}

    replacement = await platform_box(
        real_session, fake, operator=platform_admin, name="new", fresh=False
    )
    await assign(real_session, org=org_admin, box=replacement)
    await beat(replacement.id, operator=platform_admin)

    for chat in chats:
        read = await read_chat(org_admin, chat["id"])
        assert (read["machine_id"], read["machine_status"]) == (str(replacement.id), "ready")
        assert (await spec_of(chat["id"]))["machine_status"] == "ready"


# --------------------------------------------------------------------------- #
# what a reader is told about a chat on a machine that is gone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stored", ["ready", "starting"])
async def test_a_chat_stored_as_serving_on_a_released_machine_reads_stranded_everywhere(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
    stored: str,
) -> None:
    """The rows that failure left behind, and any row a crash leaves: the
    chat's spec still says ``ready`` on a box that is released. No sweep
    rewrites them; every reader derives the truth from the machine — the
    chat page, the list, the console's chat rows and its count."""
    box = await platform_box(
        real_session, fake, operator=platform_admin, name="gone", state="released"
    )
    chats = [await legacy_chat(org_admin, box.id, status=stored) for _ in range(2)]

    for chat_id in chats:
        assert (await read_chat(org_admin, chat_id))["machine_status"] == "stranded"
        assert (await listed_chat(org_admin, chat_id))["machine_status"] == "stranded"
        assert (await spec_of(chat_id))["machine_status"] == stored, "no sweep touched the row"
    row = (await console_rows(client, platform_admin))[str(box.id)]
    assert (row["chats_served"], row["chats_stranded"], row["chats_asleep"]) == (0, 2, 0)
    detail = await console_detail(client, platform_admin, box.id)
    assert (detail["chats_served"], detail["chats_stranded"]) == (0, 2)
    assert {c["machine_status"] for c in detail["chats"]} == {"stranded"}


async def test_the_console_reads_load_on_a_serving_row_and_nothing_else_as_load(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """One bound count, three readings: a serving box carries its chats as
    load; a sleeping box has them parked; a released box has them stranded.
    Each row shows exactly one of the three."""
    serving = await platform_box(real_session, fake, operator=platform_admin, name="serving")
    asleep = await platform_box(
        real_session, fake, operator=platform_admin, name="asleep", state="asleep"
    )
    gone = await platform_box(
        real_session, fake, operator=platform_admin, name="gone", state="failed"
    )
    for box, n in ((serving, 3), (asleep, 2), (gone, 4)):
        for _ in range(n):
            await legacy_chat(org_admin, box.id, status="ready")

    rows = await console_rows(client, platform_admin)

    def load(box: ComputeAllocation) -> tuple[int, int, int]:
        row = rows[str(box.id)]
        return (row["chats_served"], row["chats_stranded"], row["chats_asleep"])

    assert (load(serving), load(asleep), load(gone)) == ((3, 0, 0), (0, 0, 2), (0, 4, 0))


async def test_a_pool_machine_whose_box_is_gone_holds_the_org_only_without_fallback(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """An org pool machine whose box is released serves nothing until the
    reconcile replaces it. An org that opted out of the shared pool waits for
    it (the page says nothing serves); one that allows the pool is served by
    it, on the page and at create."""
    pool = await platform_box(
        real_session, fake, operator=platform_admin, name="pool-b", tenancy=POOL_TENANCY
    )
    dead = await platform_box(
        real_session, fake, operator=platform_admin, name="dead", state="released"
    )
    await assign(real_session, org=org_admin, box=dead, fallback=False)
    assert (await current_machine(org_admin))["status"] == "none"
    await set_fallback(real_session, org_id=org_admin.org_id, fallback=True)

    assert (await current_machine(org_admin))["status"] == "pool"
    chat = await open_chat(org_admin, "not blocked by a dead assignment")
    assert (chat["machine_id"], chat["machine_status"]) == (str(pool.id), "ready")


# --------------------------------------------------------------------------- #
# the reconcile: a loss the worker finds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "ends"),
    [
        pytest.param("ready", "released", id="a-serving-box-the-provider-lost"),
        pytest.param("bootstrapping", "failed", id="a-box-that-never-came-up"),
    ],
)
async def test_a_provider_confirmed_loss_strands_the_chats_and_drops_the_assignment(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
    state: str,
    ends: str,
) -> None:
    """The worker's side of the same rule. A machine the provider no longer
    has leaves the plane in one reconcile pass, and in that pass — not on a
    later one, not on a heartbeat — its chats are stranded and announced and
    the org it was dedicated to is returned to placement."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="lost", state=state)
    await assign(real_session, org=org_admin, box=box)
    chat_id = await legacy_chat(org_admin, box.id, status="ready")
    frames_before = await chat_frames(chat_id)
    fake.script("i-lost", GONE)

    async with AsyncSessionLocal() as db:
        await reconcile_nodes(
            db, providers=lambda kind: fake, now=datetime.now(UTC) + timedelta(hours=1)
        )

    assert (await machine_row(box.id)).state == ends
    assert (await spec_of(chat_id))["machine_status"] == "stranded"
    assert await chat_frames(chat_id) == frames_before + 1
    assert await assignment_of(org_admin) is None
    assert (await read_chat(org_admin, chat_id))["machine_status"] == "stranded"


async def test_the_reconcile_finishing_a_release_the_backend_began_changes_nothing_more(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """Idempotence of the hand-off: the terminate stranded the chats at
    ``releasing``; when the provider refused then and the reconcile settles
    the release later, the chats are not announced again and the audit chain
    gets no second event."""
    box = await platform_box(real_session, fake, operator=platform_admin, name="slow")
    await assign(real_session, org=org_admin, box=box)
    chat = await open_chat(org_admin, "stranded once")
    fake.fail_terminate.add("i-slow")
    await _terminate(client, platform_admin, box)
    assert (await machine_row(box.id)).state == "releasing"
    frames = await chat_frames(chat["id"])
    audits = len(await _audit(org_admin))

    fake.fail_terminate.clear()
    async with AsyncSessionLocal() as db:
        await reconcile_nodes(db, providers=lambda kind: fake, now=datetime.now(UTC))
    async with AsyncSessionLocal() as db:
        await reconcile_nodes(
            db, providers=lambda kind: fake, now=datetime.now(UTC) + timedelta(minutes=1)
        )

    assert (await machine_row(box.id)).state == "released"
    assert await chat_frames(chat["id"]) == frames
    assert len(await _audit(org_admin)) == audits
    assert [edge.to_state for edge in await machine_edges(box.id)] == ["releasing", "released"]
