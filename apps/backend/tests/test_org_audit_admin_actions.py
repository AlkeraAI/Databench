"""The platform's actions on an org, on that org's Activity feed.

What Alkera's staff do to a tenant from the admin console is recorded in the
platform's own audit log. The org's feed merges the rows that concern it —
read, never written into the chain — named in the chain's ``platform.``
vocabulary and marked with who on Alkera's side acted. Driven through the real
admin routes and both readers of the feed: the org admin's audit page and the
admin console's Activity tab.
"""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team, User
from backend.services.audit import org_audit as org_audit_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, app_client, login, make_org_enterprise

pytestmark = pytest.mark.asyncio

ORG_FEED = "/api/v1/org/audit-events"


async def _other_org() -> OrgWithAdmin:
    async with AsyncSessionLocal() as session:
        password = "pw-1234567890"
        org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other Org {secrets.token_hex(4)}",
            admin_email=f"other-admin-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password=password,
        )
        await session.commit()
        return OrgWithAdmin(
            org_id=org.id, admin_id=admin.id, admin_email=admin.email, admin_password=password
        )


async def _subteam(org_id: UUID) -> UUID:
    async with AsyncSessionLocal() as session:
        team = Team(name=f"team-{secrets.token_hex(4)}", is_root=False, parent_team_id=org_id)
        session.add(team)
        await session.commit()
        return team.id


_ENROLLED: set[UUID] = set()


async def _org_feed(org: OrgWithAdmin, **params: Any) -> dict[str, Any]:
    """The org admin's own audit page (Enterprise-gated on SaaS; enrolled once)."""
    if org.org_id not in _ENROLLED:
        await make_org_enterprise(org.org_id)
        _ENROLLED.add(org.org_id)
    reader = app_client()
    try:
        await login(reader, org.admin_email, org.admin_password)
        resp = await reader.get(ORG_FEED, params=params)
    finally:
        await reader.aclose()
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _console_feed(staff: AsyncClient, org_id: UUID) -> dict[str, Any]:
    """The admin console's Activity tab for the org."""
    resp = await staff.get(f"/admin/v1/orgs/{org_id}/audit")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _platform_actions(feed: dict[str, Any]) -> list[str]:
    key = "events" if "events" in feed else "items"
    return [e["action"] for e in feed[key] if e["action"].startswith("platform.")]


async def _grant(
    staff: AsyncClient, *, scope: str, target_id: UUID, amount: str = "25.00"
) -> dict[str, Any]:
    resp = await staff.post(
        "/admin/v1/billing/grants",
        json={
            "scope": scope,
            "target_id": str(target_id),
            "amount_usd": amount,
            "credit_class": "promotional",
        },
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_an_admin_grant_appears_on_the_orgs_feed_and_on_no_others(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """The one the brief names: credit granted to an org is on that org's
    feed — both readers — as a platform action naming who did it and what was
    asked, and on no other org's feed. The chain itself holds no such row."""
    other = await _other_org()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    await _grant(client, scope="org", target_id=org_admin.org_id)

    feed = await _org_feed(org_admin)
    (event,) = [e for e in feed["events"] if e["action"] == "platform.grant_credits"]
    assert event["actor_email"] == platform_admin.admin_email
    assert event["target"] == "/admin/v1/billing/grants"
    assert event["detail"]["platform"]["actor_role"] == "alkera_admin"
    assert event["detail"]["platform"]["method"] == "POST"
    assert event["detail"]["body"]["amount_usd"] == "25.00"
    assert event["detail"]["body"]["target_id"] == str(org_admin.org_id)
    assert feed["total"] == len(feed["events"])

    console = await _console_feed(client, org_admin.org_id)
    assert "platform.grant_credits" in [e["action"] for e in console["items"]]
    assert console["total"] == len(console["items"])

    assert _platform_actions(await _org_feed(other)) == []
    assert _platform_actions(await _console_feed(client, other.org_id)) == []
    # Read, never written: the chain holds only the org's own rows.
    async with AsyncSessionLocal() as session:
        rows, _total = await org_audit_service.list_page(
            session, org_id=org_admin.org_id, offset=0, limit=50
        )
        assert all(row.entry_hash is not None or row.action.startswith("platform.") for row in rows)
        verification = await org_audit_service.verify_chain(session, org_id=org_admin.org_id)
        assert verification.ok


async def test_a_platform_row_about_the_org_names_it_not_the_request_path(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(
        f"/admin/v1/orgs/{org_admin.org_id}/settings", json={"password_login_enabled": True}
    )
    assert resp.status_code == 200, resp.text
    async with AsyncSessionLocal() as session:
        name = (
            await session.execute(select(Team.name).where(Team.id == org_admin.org_id))
        ).scalar_one()

    feed = await _org_feed(org_admin, action="platform.")
    targets = {e["target"] for e in feed["events"] if e["action"].endswith("settings")}
    assert targets == {name}


async def test_every_way_an_admin_reaches_an_org_lands_on_its_feed_alone(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """A platform row names no org; the feed places it by what the route
    recorded — the org in the path, a team or member as the grant's target,
    the pool a cap was set on, the schedule that was edited — and never on a
    neighbouring org."""
    other = await _other_org()
    team_id = await _subteam(org_admin.org_id)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    settings = await client.put(
        f"/admin/v1/orgs/{org_admin.org_id}/settings", json={"password_login_enabled": True}
    )
    assert settings.status_code == 200, settings.text
    pool = await _grant(client, scope="org", target_id=org_admin.org_id)
    await _grant(client, scope="team", target_id=team_id)
    await _grant(client, scope="user", target_id=org_admin.admin_id)
    cap = f"/admin/v1/billing/pools/{pool['billing_account_id']}/members/{org_admin.admin_id}/limit"
    assert (
        await client.put(cap, json={"limit_usd": "5.00", "credit_class": "promotional"})
    ).status_code == 200
    assert (await client.delete(f"{cap}?credit_class=promotional")).status_code == 204
    schedule = await client.post(
        "/admin/v1/billing/recurring-grants",
        json={"scope": "org", "target_id": str(org_admin.org_id), "amount_usd": "100.00"},
    )
    assert schedule.status_code == 201, schedule.text
    edited = await client.patch(
        f"/admin/v1/billing/recurring-grants/{schedule.json()['id']}",
        json={"amount_usd": "50.00"},
    )
    assert edited.status_code == 200, edited.text
    # The neighbour gets a grant of its own, which must stay its own.
    await _grant(client, scope="org", target_id=other.org_id, amount="1.00")

    ours = sorted(_platform_actions(await _org_feed(org_admin)))
    assert ours == sorted(
        [
            "platform.update_org_settings",
            "platform.grant_credits",
            "platform.grant_credits",
            "platform.grant_credits",
            "platform.set_pool_limit",
            "platform.clear_pool_limit",
            "platform.create_recurring_grant",
            "platform.update_recurring_grant",
        ]
    )
    theirs = _platform_actions(await _org_feed(other))
    assert theirs == ["platform.grant_credits"]
    (their_grant,) = [
        e for e in (await _org_feed(other))["events"] if e["action"] == "platform.grant_credits"
    ]
    assert their_grant["detail"]["body"]["target_id"] == str(other.org_id)


async def test_an_action_the_route_already_wrote_into_the_chain_is_not_shown_twice(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """A ban is written into the chain of each org the person belongs to by
    the route itself as ``platform.user_banned``; its platform row is left out
    of the merge, so the feed shows the one action once."""
    async with AsyncSessionLocal() as session:
        from tests.conftest import make_member

        member, _pw = await make_member(session, org_id=org_admin.org_id, verified=True)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    banned = await client.post(
        "/admin/v1/bans/users", json={"user_id": str(member.id), "reason": "fraud"}
    )
    assert banned.status_code == 201, banned.text

    actions = _platform_actions(await _org_feed(org_admin))
    assert actions.count("platform.user_banned") == 1
    assert "platform.ban_user" not in actions


async def test_a_platform_row_naming_no_org_is_on_no_feed(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """A catalog write concerns every org and none: it is the platform's own
    business and appears on nobody's feed — the row is real, the scope is not."""
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    created = await client.post(
        "/admin/v1/catalog/models",
        json={"id": f"m-{secrets.token_hex(4)}", "display_name": "M", "family": "t"},
    )
    assert created.status_code == 201, created.text
    assert _platform_actions(await _org_feed(org_admin)) == []


async def test_filters_and_paging_span_both_sources_in_time_order(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """One listing: the org's own rows and the platform's interleave by time,
    the total counts both, an offset walks across the seam, the action prefix
    selects the platform rows by the name the feed shows, and the actor filter
    finds the staff member by address."""
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    async with AsyncSessionLocal() as session:
        actor = await session.get(User, org_admin.admin_id)
        assert actor is not None
        await org_audit_service.record(
            session, org_id=org_admin.org_id, actor=actor, action="team.created", target="one"
        )
        await session.commit()
    await _grant(client, scope="org", target_id=org_admin.org_id, amount="1.00")
    async with AsyncSessionLocal() as session:
        actor = await session.get(User, org_admin.admin_id)
        assert actor is not None
        await org_audit_service.record(
            session, org_id=org_admin.org_id, actor=actor, action="team.created", target="two"
        )
        await session.commit()
    await _grant(client, scope="org", target_id=org_admin.org_id, amount="2.00")

    whole = await _org_feed(org_admin, limit=50)
    shown = [
        (e["action"], e.get("target"))
        for e in whole["events"]
        if e["action"] in ("team.created", "platform.grant_credits")
    ]
    assert shown == [
        ("platform.grant_credits", "/admin/v1/billing/grants"),
        ("team.created", "two"),
        ("platform.grant_credits", "/admin/v1/billing/grants"),
        ("team.created", "one"),
    ]
    stamps = [e["created_at"] for e in whole["events"]]
    assert stamps == sorted(stamps, reverse=True)
    assert whole["total"] == len(whole["events"])

    # An offset walks across the seam without a repeat or a gap.
    walked: list[str] = []
    for offset in range(whole["total"]):
        page = await _org_feed(org_admin, limit=1, offset=offset)
        assert page["total"] == whole["total"]
        walked.extend(e["id"] for e in page["events"])
    assert walked == [e["id"] for e in whole["events"]]

    only_platform = await _org_feed(org_admin, action="platform.")
    assert [e["action"] for e in only_platform["events"]] == ["platform.grant_credits"] * 2
    assert only_platform["total"] == 2
    by_staff = await _org_feed(org_admin, actor_email=platform_admin.admin_email.upper())
    assert by_staff["total"] == 2
    assert {e["actor_email"] for e in by_staff["events"]} == {platform_admin.admin_email}
    by_owner = await _org_feed(org_admin, actor_email=org_admin.admin_email)
    assert all(not e["action"].startswith("platform.") for e in by_owner["events"])
    since_second_grant = await _org_feed(org_admin, created_after=whole["events"][0]["created_at"])
    assert [e["action"] for e in since_second_grant["events"]] == ["platform.grant_credits"]
