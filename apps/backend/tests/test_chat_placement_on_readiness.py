"""A chat with no machine is served the moment a machine for its org is ready.

Placement used to run at create and on the chat's NEXT message, and nowhere
else. A chat opened while the org's box was down (a Slack mention during a
restart, a browser chat while the row was reaped) bound to nothing, and the
box coming back adopted only the chats bound to its own id — so the recorded
question sat unanswered until somebody said something else. The transition to
ready is now a placement moment too: the box registering (``starting``) and its
first heartbeat (``ready``) both bind every chat of the org that has no machine
or is on one that no longer answers, and announce each one the way the send
path does, so an open browser refreshes and the box's next list adopts it.

Driven through the real routes: the daemon registers and heartbeats with the
device token plus the agent assertion, the browser opens chats with a cookie,
and the assertion is on the chat's stored binding and the frames on the outbox.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from _statement_log import counting
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from alkera_core.models.compute import ComputeAllocation
from backend.services.compute.placement import MachineBinding, bind_stranded_chats
from backend.services.org import teams as team_service
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, _unique_email, _unique_org_name, login
from tests.test_chat_machine_binding_seam import _browser, _heartbeat, _register

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

T0 = datetime(2026, 9, 15, 10, 30, tzinfo=UTC)


async def _chat_frames(chat_id: str) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.type == "chat.updated", EventOutbox.entity_id == chat_id)
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def _kill_row(machine_id: str, *, state: str = "released") -> None:
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.state = state
        alloc.released_at = datetime.now(UTC)
        await db.commit()


async def _open_chat(org: OrgWithAdmin, title: str) -> dict[str, Any]:
    async with _browser() as browser:
        await login(browser, org.admin_email, org.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": title})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


async def _read_chat(org: OrgWithAdmin, chat_id: str) -> dict[str, Any]:
    async with _browser() as browser:
        await login(browser, org.admin_email, org.admin_password)
        reread = await browser.get(f"/api/v1/chats/{chat_id}")
    assert reread.status_code == 200, reread.text
    body: dict[str, Any] = reread.json()
    return body


async def _second_org(session: AsyncSession) -> OrgWithAdmin:
    """Another tenant, built the way the ``org_admin`` fixture builds one."""
    email = _unique_email("other")
    password = "other-pass-12345"
    org, admin = await team_service.create_org_with_admin(
        session,
        org_name=_unique_org_name(),
        admin_email=email,
        admin_first_name="Other",
        admin_last_name="Tenant",
        admin_password=password,
    )
    admin.email_verified_at = datetime.now(UTC)
    await session.commit()
    return OrgWithAdmin(
        org_id=org.id, admin_id=admin.id, admin_email=email, admin_password=password
    )


async def test_a_chat_opened_with_no_machine_is_bound_when_the_orgs_box_comes_back(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The failure: the box's row was reaped while it restarted, a mention
    opened a chat into nothing, and the box came back ready to an org whose
    only unanswered chat it would never list. Coming back has to bind it —
    with no second message from anyone — and say so exactly once."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-1")
    await _heartbeat(org_admin, machine_id)
    await _kill_row(machine_id)

    chat = await _open_chat(org_admin, "asked while the box was down")
    assert (chat["machine_id"], chat["machine_status"]) == (None, "none")
    frames_at_create = len(await _chat_frames(chat["id"]))

    back = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-1")
    assert back == machine_id
    await _heartbeat(org_admin, machine_id)

    bound = await _read_chat(org_admin, chat["id"])
    assert (bound["machine_id"], bound["machine_status"]) == (machine_id, "ready")
    frames = await _chat_frames(chat["id"])
    assert len(frames) == frames_at_create + 1, "the binding is announced once"
    assert frames[-1].actor["acting"]["kind"] == "agent", "announced by the box that took it"

    # The next beats and a re-register change nothing and say nothing.
    await _heartbeat(org_admin, machine_id)
    assert (
        await _register(org_admin, machine_type.provider_type_id, "pod-readiness-1") == machine_id
    )
    await _heartbeat(org_admin, machine_id)
    assert len(await _chat_frames(chat["id"])) == frames_at_create + 1
    again = await _read_chat(org_admin, chat["id"])
    assert (again["machine_id"], again["machine_status"]) == (machine_id, "ready")


async def test_a_chat_on_a_box_that_still_answers_stays_when_another_comes_up(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A second box becoming ready is not a reason to move a working chat: the
    box mid-turn on it would lose the turn and its local state with it."""
    machine_type = await make_machine_type(real_session)
    await make_grant(
        real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id, ceiling=2
    )
    first = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-2")
    await _heartbeat(org_admin, first)
    chat = await _open_chat(org_admin, "already served")
    assert chat["machine_id"] == first
    frames_before = len(await _chat_frames(chat["id"]))

    second = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-3")
    await _heartbeat(org_admin, second)

    still = await _read_chat(org_admin, chat["id"])
    assert (still["machine_id"], still["machine_status"]) == (first, "ready")
    assert len(await _chat_frames(chat["id"])) == frames_before


async def test_another_orgs_unplaced_chat_is_never_bound_to_this_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A tenant's box serves that tenant's chats and nobody else's, however
    long the other org's chat has waited for a machine."""
    other = await _second_org(real_session)
    stranded = await _open_chat(other, "another tenant, no box")
    assert stranded["machine_status"] == "none"

    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-4")
    await _heartbeat(org_admin, machine_id)

    untouched = await _read_chat(other, stranded["id"])
    assert (untouched["machine_id"], untouched["machine_status"]) == (None, "none")


async def test_a_chat_on_a_box_that_is_gone_moves_to_the_one_that_comes_up(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Bound to a released row: nothing will ever serve it there, so the box
    that comes up takes it — the same judgment the send path makes, made
    without waiting for a send."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    first = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-5")
    await _heartbeat(org_admin, first)
    chat = await _open_chat(org_admin, "outlives its box")
    assert chat["machine_id"] == first
    await _kill_row(first, state="released")
    assert (await _read_chat(org_admin, chat["id"]))["machine_status"] == "stranded"

    second = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-6")
    await _heartbeat(org_admin, second)

    moved = await _read_chat(org_admin, chat["id"])
    assert (moved["machine_id"], moved["machine_status"]) == (second, "ready")


async def test_a_chat_on_a_box_that_went_quiet_moves_to_the_one_that_comes_up(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A box whose heartbeat lapsed is unserveable even though its row is still
    on the plane — the same ``unreachable`` the send path moves a chat off."""
    machine_type = await make_machine_type(real_session)
    await make_grant(
        real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id, ceiling=2
    )
    window = settings.compute_heartbeat_ready_seconds
    # `real_asyncio` keeps the event loop on the real monotonic clock. A frozen
    # one never reaches a `call_later` deadline, so the first request in here to
    # arm one — a catalog fetch's timeout, a pool's acquire — waits forever.
    with freeze_time(T0, real_asyncio=True) as frozen:
        first = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-7")
        await _heartbeat(org_admin, first)
        chat = await _open_chat(org_admin, "on a box that goes quiet")
        assert chat["machine_id"] == first

        frozen.move_to(T0 + timedelta(seconds=window + 1))
        second = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-8")
        await _heartbeat(org_admin, second)

        moved = await _read_chat(org_admin, chat["id"])
        assert (moved["machine_id"], moved["machine_status"]) == (second, "ready")


async def test_a_deleted_chat_is_not_bound(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A tombstone is not a chat waiting for an answer."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    chat = await _open_chat(org_admin, "deleted before any box")
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        gone = await browser.delete(f"/api/v1/chats/{chat['id']}")
    assert gone.status_code == 204, gone.text
    frames_before = len(await _chat_frames(chat["id"]))

    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-9")
    await _heartbeat(org_admin, machine_id)

    assert len(await _chat_frames(chat["id"])) == frames_before


@pytest.mark.parametrize("stranded", [pytest.param(1, id="one-chat"), pytest.param(4, id="four")])
async def test_the_transition_reads_the_orgs_chats_once_however_many_wait(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    stranded: int,
) -> None:
    """A heartbeat is a hot path. The pass costs one read of the org's machines
    and ONE read of its chats; only the chats it moves cost a lock and a write
    each. A pass that read the chats per row, or per heartbeat per chat, would
    grow with the org's history."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    # The box's row exists but is off the plane while the chats open, so each
    # binds to nothing; it is put back by hand so the pass below — not the
    # register route, which runs the same pass — is what binds them.
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-readiness-10")
    await _kill_row(machine_id)
    chat_ids = [(await _open_chat(org_admin, f"waiting {n}"))["id"] for n in range(stranded)]

    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.state = "ready"
        alloc.released_at = None
        await db.flush()
        binding = MachineBinding(machine_id=alloc.id, status="ready", name=alloc.name)
        with counting() as captured_sql:
            moved = await bind_stranded_chats(
                db, org_team_id=org_admin.org_id, binding=binding, actor=None
            )
            await db.commit()

    assert sorted(str(c.id) for c in moved) == sorted(chat_ids)
    # One read of the chats, then at most one each of their workspaces' pins (a
    # pinned chat waits for its own machine) and of the workspaces that lost
    # their machine (their chats wait for a choice): none grows with how many
    # chats wait.
    reads = [s for s in captured_sql if "FROM workspace_objects" in s and "FOR UPDATE" not in s]
    workspace_reads = [s for s in reads if "workspace_objects.id IN" in s]
    chat_reads = [s for s in reads if "workspace_objects.id IN" not in s]
    assert len(chat_reads) == 1, captured_sql
    # The choice read filters on a key being unset; the pin read does not.
    choice_reads = [s for s in workspace_reads if "IS NULL" in s]
    pin_reads = [s for s in workspace_reads if "IS NULL" not in s]
    assert len(pin_reads) <= 1, captured_sql
    assert len(choice_reads) <= 1, captured_sql
    machine_reads = [s for s in captured_sql if "FROM compute_allocations" in s]
    assert len(machine_reads) == 1, captured_sql
    locks = [s for s in captured_sql if "FROM workspace_objects" in s and "FOR UPDATE" in s]
    # Each moved chat costs its own lock (by id alone), and at most one more:
    # its workspace's (by id, org and type), whose report a move may drop.
    chat_locks = [s for s in locks if "workspace_objects.type =" not in s]
    assert len(chat_locks) == stranded
    assert len(locks) - len(chat_locks) <= stranded
