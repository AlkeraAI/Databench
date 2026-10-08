"""Admin compute grants — the platform-admin act that used to be a script.

Every branch of the ``compute.grant`` policy driven through the real route,
with the decision row the outbox holds afterwards, plus the end-to-end proof
the whole feature exists for: a granted org's box registers, an ungranted one
is still refused, and a revoked grant puts it back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from alkera_core.models.compute import ComputeAllocation
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, app_client, login, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

FUTURE = (datetime.now(UTC) + timedelta(days=14)).isoformat()


def _url(org_id: UUID | str) -> str:
    return f"/admin/v1/orgs/{org_id}/compute"


def _body(**over: Any) -> dict[str, Any]:
    return {"ceiling": 2, "rate_per_minute_nanos": 0, "expires_at": FUTURE, **over}


async def _decisions(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "compute_grant",
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]


# --------------------------------------------------------------------------- #
# the staff floor
# --------------------------------------------------------------------------- #


async def test_grants_require_authentication(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    assert (await client.get(_url(org_admin.org_id))).status_code == 401
    assert (await client.put(_url(org_admin.org_id), json=_body())).status_code == 401


async def test_a_plain_org_admin_cannot_read_or_grant_compute(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A tenant's own admin is not platform staff: the admin surface refuses
    before the route runs, so a customer can never widen their own ceiling."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(_url(org_admin.org_id))).status_code == 403
    assert (await client.put(_url(org_admin.org_id), json=_body())).status_code == 403


# --------------------------------------------------------------------------- #
# read: the support floor
# --------------------------------------------------------------------------- #


async def test_support_reads_an_orgs_grants_and_the_allow_is_recorded(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(_url(org_admin.org_id))
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    rows = await _decisions(platform_support.org_id)
    assert _effects(rows) == [("allow", "staff_read")]
    (row,) = rows
    assert row.visibility == "platform"
    assert row.payload["policy"] == "compute.grant"
    assert row.payload["action"] == "read"
    assert row.payload["attrs"] == {
        "platform_staff": True,
        "platform_admin": False,
        "org_exists": True,
        "target_org_id": str(org_admin.org_id),
    }


async def test_support_may_not_write_a_grant_and_the_deny_survives(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_support: OrgWithAdmin
) -> None:
    """Support can look; only ALKERA_ADMIN may move the dial that spends our
    provider money. The refusal rolls the request back — the row must not."""
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.put(_url(org_admin.org_id), json=_body())
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "Platform admin role required"

    rows = await _decisions(platform_support.org_id)
    assert _effects(rows) == [("deny", "platform_admin_required")]
    assert rows[0].payload["action"] == "admin"

    async with AsyncSessionLocal() as session:
        held = await session.execute(
            select(EventOutbox).where(EventOutbox.type == "compute_grant.created")
        )
        assert held.scalars().all() == []


async def test_an_unknown_org_is_an_opaque_not_found(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    """A staff caller may probe org ids; the answer is the same 404 shape a
    real-but-empty org would never produce, and it is on record."""
    ghost = uuid4()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.get(_url(ghost))
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Organization not found"

    rows = await _decisions(platform_admin.org_id)
    assert _effects(rows) == [("deny", "org_not_found")]
    assert rows[0].payload["attrs"]["org_exists"] is False


async def test_a_sub_team_id_is_not_an_org(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """The route is addressed by ORG. Pointing it at a team inside one reads as
    not-found rather than silently granting against the wrong node."""
    from alkera_core.models import Team

    async with AsyncSessionLocal() as session:
        team = Team(name="sub", is_root=False, parent_team_id=org_admin.org_id)
        session.add(team)
        await session.commit()
        sub_id = team.id
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (await client.get(_url(sub_id))).status_code == 404
    rows = await _decisions(platform_admin.org_id)
    assert _effects(rows) == [("deny", "org_not_found")]


# --------------------------------------------------------------------------- #
# write + revoke
# --------------------------------------------------------------------------- #


async def test_a_platform_admin_grants_compute_and_the_allow_is_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(_url(org_admin.org_id), json=_body(ceiling=3, note="onboarding"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ceiling"] == 3
    assert body["org_team_id"] == str(org_admin.org_id)
    assert body["machine_type_id"] is None
    assert body["machine_type"] == "any"
    assert body["machine_type_display_name"] == "Any machine type"
    assert body["note"] == "onboarding"

    listed = await client.get(_url(org_admin.org_id))
    assert [g["id"] for g in listed.json()] == [body["id"]]

    rows = await _decisions(platform_admin.org_id)
    assert _effects(rows) == [("allow", "admin_grant"), ("allow", "staff_read")]
    assert rows[0].payload["attrs"]["platform_admin"] is True


async def test_granting_twice_updates_the_live_grant_instead_of_stacking(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The idempotence the provisioning script relied on, kept in the product:
    a second grant at the same target raises the ceiling, it does not double it."""
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    first = await client.put(_url(org_admin.org_id), json=_body(ceiling=1))
    assert first.status_code == 201
    second = await client.put(_url(org_admin.org_id), json=_body(ceiling=9))
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["ceiling"] == 9

    listed = (await client.get(_url(org_admin.org_id))).json()
    assert len(listed) == 1
    assert listed[0]["ceiling"] == 9


async def test_a_typed_grant_names_its_machine_type(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    mt = await make_machine_type(real_session)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(_url(org_admin.org_id), json=_body(machine_type_id=str(mt.id)))
    assert resp.status_code == 201, resp.text
    assert resp.json()["machine_type_id"] == str(mt.id)
    assert resp.json()["machine_type"] == mt.provider_type_id
    assert resp.json()["machine_type_display_name"] == mt.display_name


async def test_an_unknown_machine_type_is_404_and_writes_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(_url(org_admin.org_id), json=_body(machine_type_id=str(uuid4())))
    assert resp.status_code == 404
    assert (await client.get(_url(org_admin.org_id))).json() == []


async def test_an_expiry_in_the_past_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A grant that has already lapsed admits nothing — writing one would look
    like a successful grant and refuse every box."""
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(_url(org_admin.org_id), json=_body(expires_at=past))
    assert resp.status_code == 400
    assert (await client.get(_url(org_admin.org_id))).json() == []


async def test_a_grant_target_outside_the_org_is_refused(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """``org_team_id`` narrows the grant to a team INSIDE the addressed org; a
    team id from another tenant must not become a grant filed under this one."""
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(
        _url(org_admin.org_id), json=_body(org_team_id=str(platform_support.org_id))
    )
    assert resp.status_code == 400
    assert (await client.get(_url(platform_support.org_id))).json() == []


async def test_revoking_removes_the_grant(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    created = (await client.put(_url(org_admin.org_id), json=_body())).json()
    resp = await client.delete(f"{_url(org_admin.org_id)}/{created['id']}")
    assert resp.status_code == 204
    assert (await client.get(_url(org_admin.org_id))).json() == []


async def test_revoking_a_grant_held_by_another_org_is_not_found(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    """The tenancy guard on a cross-tenant route: knowing a grant id is not
    permission to revoke it through some other org's URL."""
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    created = (await client.put(_url(org_admin.org_id), json=_body())).json()
    resp = await client.delete(f"{_url(platform_support.org_id)}/{created['id']}")
    assert resp.status_code == 404
    assert [g["id"] for g in (await client.get(_url(org_admin.org_id))).json()] == [created["id"]]


async def test_support_may_not_revoke(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    created = (await client.put(_url(org_admin.org_id), json=_body())).json()
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.delete(f"{_url(org_admin.org_id)}/{created['id']}")
    assert resp.status_code == 403
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert [g["id"] for g in (await client.get(_url(org_admin.org_id))).json()] == [created["id"]]


# --------------------------------------------------------------------------- #
# the whole point: a granted org's box registers
# --------------------------------------------------------------------------- #


async def test_the_portal_grant_admits_a_box_that_was_refused_before_it(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """End to end, in the order an onboarding actually runs: the new org's box
    is refused, a platform admin grants compute from the product, the same box
    registers — and revoking the grant refuses it again."""
    mt = await make_machine_type(real_session)
    jwt = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {jwt}", **agent_headers("sess-onboard")}
    register = {
        "provider": "runpod",
        "provider_pod_id": "pod-onboard-1",
        "name": "onboarding-box",
        "machine_type_code": mt.provider_type_id,
    }

    before = await client.post("/api/v1/machines/register", json=register, headers=headers)
    assert before.status_code == 429
    assert before.json()["error"]["code"] == "no_compute_grant"

    staff = app_client()
    await login(staff, platform_admin.admin_email, platform_admin.admin_password)
    granted = await staff.put(
        _url(org_admin.org_id), json=_body(ceiling=1, machine_type_id=str(mt.id))
    )
    assert granted.status_code == 201, granted.text

    after = await client.post("/api/v1/machines/register", json=register, headers=headers)
    assert after.status_code == 201, after.text

    revoked = await staff.delete(f"{_url(org_admin.org_id)}/{granted.json()['id']}")
    assert revoked.status_code == 204
    again = await client.post(
        "/api/v1/machines/register",
        json={**register, "provider_pod_id": "pod-onboard-2"},
        headers=headers,
    )
    assert again.status_code == 429
    assert again.json()["error"]["code"] == "no_compute_grant"


async def test_a_typed_grant_does_not_admit_another_machine_type(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """A grant written for one machine type admits that type and no other — the
    ceiling an operator sets is not a blanket one."""
    granted_type = await make_machine_type(real_session)
    other_type = await make_machine_type(real_session)
    staff = app_client()
    await login(staff, platform_admin.admin_email, platform_admin.admin_password)
    assert (
        await staff.put(_url(org_admin.org_id), json=_body(machine_type_id=str(granted_type.id)))
    ).status_code == 201

    jwt = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {jwt}", **agent_headers("sess-typed")}
    ok = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": "pod-typed-ok",
            "name": "granted-box",
            "machine_type_code": granted_type.provider_type_id,
        },
        headers=headers,
    )
    assert ok.status_code == 201, ok.text

    refused = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": "pod-typed-no",
            "name": "ungranted-box",
            "machine_type_code": other_type.provider_type_id,
        },
        headers=headers,
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "no_compute_grant"


async def test_the_rate_an_admin_grants_is_the_rate_the_box_is_billed_at(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """The number the operator typed, followed to the column a minute is billed
    against.

    Every other case here grants at zero, which is the one value a route that
    dropped the field entirely would also produce — so the rate is carried
    through the write's own body, the listing that reads it back, and finally
    the allocation a real registration pins it onto. A grant that arrived free
    is compute nobody is charged for, and nothing else in the suite would say so.
    """
    rate = 80_000_000
    mt = await make_machine_type(real_session)
    staff = app_client()
    await login(staff, platform_admin.admin_email, platform_admin.admin_password)

    granted = await staff.put(
        _url(org_admin.org_id),
        json=_body(ceiling=1, rate_per_minute_nanos=rate, machine_type_id=str(mt.id)),
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()["rate_per_minute_nanos"] == rate
    grant_id = granted.json()["id"]

    listed = await staff.get(_url(org_admin.org_id))
    assert listed.status_code == 200, listed.text
    assert [g["rate_per_minute_nanos"] for g in listed.json() if g["id"] == grant_id] == [rate]

    jwt = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    resp = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": "pod-priced-1",
            "name": "priced-box",
            "machine_type_code": mt.provider_type_id,
        },
        headers={"Authorization": f"Bearer {jwt}", **agent_headers("sess-priced")},
    )
    assert resp.status_code == 201, resp.text
    row = (
        await real_session.execute(
            select(ComputeAllocation).where(ComputeAllocation.id == UUID(resp.json()["id"]))
        )
    ).scalar_one()
    assert row.grant_id == UUID(grant_id)
    assert row.price_per_minute_nanos == rate
    # The rate is the customer's, not the catalog's: the true cost rides beside
    # it, so a grant that silently took one for the other would show here.
    assert row.true_cost_per_minute_nanos == mt.provider_price_per_minute_nanos
    assert row.true_cost_per_minute_nanos != rate


async def test_a_listing_tells_a_wildcard_grant_apart_from_a_typed_one(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    """The read names what each grant admits, so an operator can read the list.

    Both facts are load-bearing and opposite: a wildcard grant lets the org
    spend on ANY machine type (including a GPU), a typed grant on exactly one.
    Reporting both as a blank machine type makes the most expensive grant we
    issue indistinguishable from the cheapest on the screen an admin uses to
    review them.
    """
    gpu = await make_machine_type(real_session)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    wildcard = await client.put(_url(org_admin.org_id), json=_body(ceiling=1))
    assert wildcard.status_code == 201, wildcard.text
    typed = await client.put(
        _url(org_admin.org_id), json=_body(ceiling=1, machine_type_id=str(gpu.id))
    )
    assert typed.status_code == 201, typed.text

    listed = (await client.get(_url(org_admin.org_id))).json()
    by_id = {g["id"]: g for g in listed}
    assert len(by_id) == 2

    wild_row = by_id[wildcard.json()["id"]]
    assert wild_row["machine_type_id"] is None
    assert wild_row["machine_type"] == "any"
    assert wild_row["machine_type_display_name"] == "Any machine type"

    typed_row = by_id[typed.json()["id"]]
    assert typed_row["machine_type_id"] == str(gpu.id)
    assert typed_row["machine_type"] == gpu.provider_type_id
    assert typed_row["machine_type_display_name"] == gpu.display_name

    # Whatever else differs, the two rows never render the same machine type.
    assert wild_row["machine_type"] != typed_row["machine_type"]
