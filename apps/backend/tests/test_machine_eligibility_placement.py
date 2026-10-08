"""A heartbeat is not a placement decision, and a box nobody stands behind
serves, speaks for, and is billed for nothing.

Registering the box an org's chats run on takes an org admin. That rule is only
half of it: the rows that already exist were registered under the old one, and
an admin can be demoted at any time. So eligibility is asked live, in ONE
predicate, wherever a chat could change hands — the banner, placement, the
stranded-chat sweep a box's own heartbeat drives, the assertion a box makes
when it speaks for a chat, and the meter that decides whether to keep billing.
These are the cases at those seams, driven through the real routes: a box that
beats is not thereby allowed to harvest the org's chats, nor to take one off a
box that merely missed a window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.auth import encode_cli_token, register_token
from alkera_core.authz import agent_headers
from alkera_core.authz.enums import PrincipalKind, Role, ScopeKind, expand_roles
from alkera_core.compute.machines import stands_behind_its_org
from alkera_core.compute.meter import NO_ELIGIBLE_OPERATOR, meter_and_cutoff
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import RoleAssignment, TeamMembership, TokenType, WorkspaceObject
from alkera_core.models._enums import TeamRole
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    DEDICATED_TENANCY,
    ORG_TENANCY,
    POOL_TENANCY,
    ComputeAllocation,
)
from backend.services.compute import placement
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import FakeProvider, hold_with_credential, make_machine_type
from tests.conftest import OrgWithAdmin, app_client, login, make_member, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("compute-fleet")]


def _client() -> AsyncClient:
    """A client on the real app — one for the person's browser, one for the
    daemon, so a test can drive both at once. The browser one signs in with a
    cookie, so it must carry the origin the guard asks of a cookie-authed
    write; the daemon one authenticates with a bearer token, which the guard
    never binds, so the same header is inert on it."""
    return app_client(base_url="http://testserver")


async def _headers(*, user_id: UUID, email: str, org_id: UUID) -> dict[str, str]:
    jwt = await mint_cli_token(user_id=user_id, email=email, org_team_id=org_id)
    return {"Authorization": f"Bearer {jwt}", **agent_headers("sess-box")}


async def _demote(session: AsyncSession, *, user_id: UUID, org_id: UUID) -> None:
    await session.execute(
        update(TeamMembership)
        .where(TeamMembership.user_id == user_id, TeamMembership.team_id == org_id)
        .values(role=TeamRole.MEMBER)
    )
    await session.commit()


async def _box(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    machine_type_id: UUID,
    name: str,
    heartbeat: datetime | None,
    created_at: datetime | None = None,
    tenancy: str = ORG_TENANCY,
    capacity: int = 10,
) -> ComputeAllocation:
    """A live workspace row written straight into the database — the shape a
    registration leaves behind, including the ones made before the admin rule."""
    alloc, _credential = await _box_and_credential(
        session,
        org_id=org_id,
        user_id=user_id,
        machine_type_id=machine_type_id,
        name=name,
        heartbeat=heartbeat,
        created_at=created_at,
        tenancy=tenancy,
        capacity=capacity,
    )
    return alloc


async def _box_and_credential(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    machine_type_id: UUID,
    name: str,
    heartbeat: datetime | None,
    created_at: datetime | None = None,
    tenancy: str = ORG_TENANCY,
    capacity: int = 10,
) -> tuple[ComputeAllocation, str | None]:
    """:func:`_box`, plus the raw machine credential a platform box holds, so
    a test can beat as the box itself. ``None`` for an org box."""
    born = created_at or datetime.now(UTC)
    alloc = ComputeAllocation(
        user_id=user_id,
        org_team_id=org_id,
        machine_type_id=machine_type_id,
        lifecycle="workspace",
        origin="registered",
        name=name,
        tenancy=tenancy,
        sandbox="gvisor" if tenancy != ORG_TENANCY else "none",
        state="ready",
        provider_machine_id=f"pod-{name}",
        created_at=born,
        ready_at=born,
        last_metered_at=born,
        last_heartbeat_at=heartbeat,
        capacity=capacity,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=1,
    )
    session.add(alloc)
    await session.commit()
    credential = None
    if tenancy != ORG_TENANCY:
        credential = await hold_with_credential(session, alloc)
    return alloc, credential


async def _open_chat(org: OrgWithAdmin, title: str) -> dict[str, Any]:
    async with _client() as browser:
        await login(browser, org.admin_email, org.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": title})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


async def _bound_machine_id(chat_id: str) -> str | None:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        bound = chat.spec.get("machine_id")
        return str(bound) if bound else None


async def _pin_to(chat_id: str, machine_id: UUID) -> None:
    """The binding a placement made before the rule existed."""
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        chat.spec = {**chat.spec, "machine_id": str(machine_id), "machine_status": "ready"}
        await session.commit()


async def _beat(
    machine_id: UUID, headers: dict[str, str], body: dict[str, Any] | None = None
) -> int:
    async with _client() as daemon:
        resp = await daemon.post(
            f"/api/v1/machines/{machine_id}/heartbeat", headers=headers, json=body
        )
    return resp.status_code


async def _most_recent_first(chat_ids: list[str]) -> None:
    """Stamp the chats as the most recently active in the database, in list
    order (the first the most recent): a box coming up serves the most recently
    active stranded chats first, and the database is shared with other tests'
    leftovers, so a test about which chats are taken has to own the front of
    the queue."""
    base = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        for index, chat_id in enumerate(chat_ids):
            await session.execute(
                update(WorkspaceObject)
                .where(WorkspaceObject.id == UUID(chat_id))
                .values(updated_at=base - timedelta(milliseconds=index))
            )
        await session.commit()


async def _quiet_the_pool() -> None:
    """The pool is global: a pool box another test left live would be the
    answer for this test's org. Every platform box is retired first."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ComputeAllocation)
            .where(ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)))
            .where(ComputeAllocation.state != "released")
            .values(state="released", released_at=datetime.now(UTC))
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# a heartbeat must not harvest the org's chats
# --------------------------------------------------------------------------- #


async def test_a_member_boxs_heartbeat_does_not_adopt_the_orgs_stranded_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A heartbeat says a box is alive. It does not say the box may hold the
    org's chats — a chat opened while the org had no eligible box waits rather
    than going to whichever box beats next."""
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="members-own",
        heartbeat=None,  # starting; the first beat is the ready transition
    )
    chat = await _open_chat(org_admin, "asked with no box")
    assert chat["machine_id"] is None, chat

    headers = await _headers(user_id=member.id, email=member.email, org_id=org_admin.org_id)
    assert await _beat(box.id, headers) == 204

    assert await _bound_machine_id(chat["id"]) is None


async def test_a_demoted_operators_heartbeat_does_not_adopt_the_orgs_stranded_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session)
    operator, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=operator.id,
        machine_type_id=mt.id,
        name="operators-box",
        heartbeat=None,
    )
    await _demote(real_session, user_id=operator.id, org_id=org_admin.org_id)
    chat = await _open_chat(org_admin, "after the demotion")
    assert chat["machine_id"] is None, chat

    headers = await _headers(user_id=operator.id, email=operator.email, org_id=org_admin.org_id)
    assert await _beat(box.id, headers) == 204

    assert await _bound_machine_id(chat["id"]) is None


async def test_an_ineligible_box_cannot_take_a_chat_off_a_box_that_missed_a_window(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart, a deploy, a dropped beat — none of that is consent to hand the
    chat to somebody else's laptop."""
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    admin_box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="admin-box",
        heartbeat=datetime.now(UTC),
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    chat = await _open_chat(org_admin, "on the real box")
    assert chat["machine_id"] == str(admin_box.id), chat

    async with AsyncSessionLocal() as session:
        row = await session.get(ComputeAllocation, admin_box.id)
        assert row is not None
        row.last_heartbeat_at = datetime.now(UTC) - timedelta(
            seconds=settings.compute_heartbeat_ready_seconds + 60
        )
        await session.commit()

    member_box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="member-box",
        heartbeat=None,
    )
    headers = await _headers(user_id=member.id, email=member.email, org_id=org_admin.org_id)
    assert await _beat(member_box.id, headers) == 204

    assert await _bound_machine_id(chat["id"]) == str(admin_box.id)


async def test_an_admin_boxs_heartbeat_still_adopts_the_orgs_stranded_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The negative half: the eligible box coming up is exactly the moment a
    waiting chat is supposed to be answered, and it still is."""
    mt = await make_machine_type(real_session)
    chat = await _open_chat(org_admin, "waiting for the box")
    assert chat["machine_id"] is None, chat
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="admins-box",
        heartbeat=None,
    )

    headers = await _headers(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_id=org_admin.org_id
    )
    assert await _beat(box.id, headers) == 204

    assert await _bound_machine_id(chat["id"]) == str(box.id)


async def test_a_pool_box_reclaims_a_chat_pinned_to_an_ineligible_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat left on a box nobody stands behind IS stranded, so the next
    platform box to come up takes it — the org gets its compute back without
    waiting for somebody to type."""
    await _quiet_the_pool()
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    member_box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="member-stuck",
        heartbeat=datetime.now(UTC),
    )
    chat = await _open_chat(org_admin, "stuck")
    await _pin_to(chat["id"], member_box.id)

    pool = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="pool-box",
        heartbeat=datetime.now(UTC),
        tenancy=POOL_TENANCY,
    )
    try:
        await placement.bind_stranded_chats_platform(real_session, alloc=pool, actor=None)
        await real_session.commit()

        assert await _bound_machine_id(chat["id"]) == str(pool.id)
    finally:
        # A pool box serves EVERY org, so one left behind in a shared test
        # database becomes the answer for any other case that asks what its org
        # runs on. Retired the moment this case is done with it.
        async with AsyncSessionLocal() as session:
            row = await session.get(ComputeAllocation, pool.id)
            assert row is not None
            row.state = "released"
            row.released_at = datetime.now(UTC)
            await session.commit()


async def test_a_pool_boxs_first_heartbeat_binds_stranded_chats_only_up_to_its_room(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The real path a node takes when it claims: five chats were opened while
    the org had no box; a pool box registered with capacity 3 beats for the
    first time as itself, on its machine credential, and the beat is the ready
    transition that offers it the stranded chats. Exactly three — the most
    recently active — bind; two stay stranded for the next box or their next
    message. Before
    the room bound, this beat took all five, and on the live stack seventy-nine
    onto a box of four."""
    await _quiet_the_pool()
    mt = await make_machine_type(real_session)
    chats = [await _open_chat(org_admin, f"waiting-{i}") for i in range(5)]
    assert all(chat["machine_id"] is None for chat in chats), chats
    await _most_recent_first([chat["id"] for chat in chats])
    pool, credential = await _box_and_credential(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="pool-room",
        heartbeat=None,  # starting; the first beat is the ready transition
        tenancy=POOL_TENANCY,
        capacity=3,
    )
    assert credential is not None
    try:
        # The box speaks as itself: its credential is the bearer, no user behind it.
        status = await _beat(
            pool.id, {"Authorization": f"Bearer {credential}"}, {"capacity": 3, "chats_served": 0}
        )
        assert status == 204

        bound = [c["id"] for c in chats if await _bound_machine_id(c["id"]) == str(pool.id)]
        waiting = [c["id"] for c in chats if await _bound_machine_id(c["id"]) is None]
        assert bound == [c["id"] for c in chats[:3]]
        assert waiting == [c["id"] for c in chats[3:]]
    finally:
        async with AsyncSessionLocal() as session:
            row = await session.get(ComputeAllocation, pool.id)
            assert row is not None
            row.state = "released"
            row.released_at = datetime.now(UTC)
            await session.commit()


# --------------------------------------------------------------------------- #
# a box nobody stands behind speaks for no chat
# --------------------------------------------------------------------------- #


async def test_an_ineligible_box_cannot_speak_for_the_chat_it_is_named_on(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Defence behind placement: even with the chat's own spec naming it — the
    state a pre-rule binding leaves behind — a box nobody stands behind is not
    that machine, so it reads nothing."""
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    token, claims = encode_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id, platform_role=None
    )
    async with AsyncSessionLocal() as session:
        await register_token(session, claims=claims, token_type=TokenType.CLI)
        await session.commit()
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="members-reader",
        heartbeat=datetime.now(UTC),
    )
    box.registered_jti = claims.jti
    await real_session.commit()

    chat = await _open_chat(org_admin, "the admin's private chat")
    async with _client() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        await browser.post(
            f"/api/v1/chats/{chat['id']}/messages",
            json={"text": "a secret prompt", "client_id": "probe-1"},
        )
    await _pin_to(chat["id"], box.id)

    box_headers = {"Authorization": f"Bearer {token}", **agent_headers(str(box.id))}
    async with _client() as speaker:
        read = await speaker.get(f"/api/v1/chats/{chat['id']}", headers=box_headers)
        messages = await speaker.get(f"/api/v1/chats/{chat['id']}/messages", headers=box_headers)

    assert read.status_code == 404, read.text
    assert "a secret prompt" not in messages.text


# --------------------------------------------------------------------------- #
# taking the box down takes the same authority as standing it up
# --------------------------------------------------------------------------- #


async def test_a_demoted_operator_cannot_terminate_the_orgs_box(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session)
    operator, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=operator.id,
        machine_type_id=mt.id,
        name="the-org-box",
        heartbeat=datetime.now(UTC),
    )
    await _demote(real_session, user_id=operator.id, org_id=org_admin.org_id)
    headers = await _headers(user_id=operator.id, email=operator.email, org_id=org_admin.org_id)

    resp = await client.delete(f"/api/v1/compute/allocations/{box.id}", headers=headers)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "org_admin_required"
    async with AsyncSessionLocal() as session:
        row = await session.get(ComputeAllocation, box.id)
        assert row is not None and row.state == "ready"


async def test_a_member_still_releases_their_own_session_allocation(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The admin rule is about the org's box, not a member's own rented
    compute: releasing that is still theirs to do."""
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    alloc = ComputeAllocation(
        user_id=member.id,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        lifecycle="session",
        origin="registered",
        name="my-session",
        tenancy=ORG_TENANCY,
        state="ready",
        provider_machine_id="",
        created_at=datetime.now(UTC),
        ready_at=datetime.now(UTC),
        last_metered_at=datetime.now(UTC),
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=1,
    )
    real_session.add(alloc)
    await real_session.commit()
    headers = await _headers(user_id=member.id, email=member.email, org_id=org_admin.org_id)

    resp = await client.delete(f"/api/v1/compute/allocations/{alloc.id}", headers=headers)

    assert resp.status_code != 403, resp.text


# --------------------------------------------------------------------------- #
# the money: a box nobody stands behind is not billed for ever
# --------------------------------------------------------------------------- #


async def test_the_meter_takes_down_a_box_nobody_stands_behind(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Placement, the banner and the assertion all stop seeing the box; the
    meter is what stops the bill and the pod."""
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="ineligible",
        heartbeat=datetime.now(UTC),
    )

    await meter_and_cutoff(real_session, provider=FakeProvider())

    async with AsyncSessionLocal() as session:
        row = await session.get(ComputeAllocation, box.id)
        assert row is not None
        assert row.state in (*COMPUTE_TERMINAL_STATES, "releasing")
        assert row.terminated_reason == NO_ELIGIBLE_OPERATOR


async def test_the_meter_leaves_an_admins_box_running(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The negative half: the same tick must not touch a box an admin stands
    behind, or the rule would take the fleet down."""
    mt = await make_machine_type(real_session)
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="admins-box",
        heartbeat=datetime.now(UTC),
    )

    await meter_and_cutoff(real_session, provider=FakeProvider())

    async with AsyncSessionLocal() as session:
        row = await session.get(ComputeAllocation, box.id)
        assert row is not None
        assert row.state == "ready"
        assert row.terminated_reason == ""


# --------------------------------------------------------------------------- #
# the SQL predicate and the role ladder say the same thing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "granted",
    [pytest.param(role, id=role.value) for role in Role],
)
async def test_the_sql_predicate_admits_exactly_the_roles_that_expand_to_admin(
    real_session: AsyncSession, org_admin: OrgWithAdmin, granted: Role
) -> None:
    """Eligibility is spelled in SQL; the policy spells the same authority with
    ``expand_roles``. Two spellings drift silently, so this pins them to one
    answer: a grant makes a box eligible exactly when the role it carries
    expands to include ADMIN."""
    mt = await make_machine_type(real_session)
    operator, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=operator.id,
        machine_type_id=mt.id,
        name=f"granted-{granted.value}",
        heartbeat=datetime.now(UTC),
    )
    real_session.add(
        RoleAssignment(
            org_team_id=org_admin.org_id,
            principal_kind=PrincipalKind.USER,
            principal_id=operator.id,
            scope_kind=ScopeKind.TEAM,
            scope_id=org_admin.org_id,
            role=granted,
        )
    )
    await real_session.commit()

    eligible = (
        await real_session.execute(
            select(ComputeAllocation.id).where(
                ComputeAllocation.id == box.id, stands_behind_its_org()
            )
        )
    ).scalar_one_or_none() is not None

    assert eligible is (Role.ADMIN in expand_roles(frozenset({granted})))
