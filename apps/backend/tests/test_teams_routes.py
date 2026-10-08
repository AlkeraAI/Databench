"""Team CRUD route tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member


@pytest.mark.asyncio
async def test_list_teams_in_my_org(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # Initially: just the root team.
    resp = await client.get("/api/v1/teams")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert any(t["is_root"] for t in resp.json())

    # Create a subteam.
    create = await client.post("/api/v1/teams", json={"name": "Engineering"})
    assert create.status_code == 201

    resp = await client.get("/api/v1/teams")
    names = {t["name"] for t in resp.json()}
    assert "Engineering" in names


@pytest.mark.asyncio
async def test_create_team_requires_org_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, member_pw or "")
    resp = await client.post("/api/v1/teams", json={"name": "Sneaky"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_team_cross_org_returns_404(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    import secrets

    from backend.services.org import teams as team_service

    other_root, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Stranger Org {secrets.token_hex(4)}",
        admin_email=f"stranger-admin-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Stranger",
        admin_last_name="Admin",
        admin_password="x" * 16,
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/teams/{other_root.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_team_renames(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    create = await client.post("/api/v1/teams", json={"name": "Old"})
    team_id = create.json()["id"]

    patch = await client.patch(f"/api/v1/teams/{team_id}", json={"name": "New"})
    assert patch.status_code == 200
    assert patch.json()["name"] == "New"


@pytest.mark.asyncio
async def test_delete_team_rejects_root(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/teams/{org_admin.org_id}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_subteam_succeeds(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    create = await client.post("/api/v1/teams", json={"name": "ToDelete"})
    team_id = create.json()["id"]
    resp = await client.delete(f"/api/v1/teams/{team_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_team_with_subteam_rejected_cleanly(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """A team with children is a clean 400 (not a 500 from the FK RESTRICT)."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    parent = (await client.post("/api/v1/teams", json={"name": "Parent"})).json()
    child = await client.post(
        "/api/v1/teams", json={"name": "Child", "parent_team_id": parent["id"]}
    )
    assert child.status_code == 201
    resp = await client.delete(f"/api/v1/teams/{parent['id']}")
    assert resp.status_code == 400
    assert "sub-teams" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_delete_team_with_member_rejected(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    from uuid import UUID

    from backend.services.org import memberships as membership_service

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = (await client.post("/api/v1/teams", json={"name": "HasMember"})).json()
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await membership_service.add_member(real_session, team_id=UUID(team["id"]), user_id=member.id)
    await real_session.commit()
    resp = await client.delete(f"/api/v1/teams/{team['id']}")
    assert resp.status_code == 400
    assert "members" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_member_count_present_in_list_and_get(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    from uuid import UUID

    from backend.services.org import memberships as membership_service

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = (await client.post("/api/v1/teams", json={"name": "Counted"})).json()
    assert team["member_count"] == 0
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await membership_service.add_member(real_session, team_id=UUID(team["id"]), user_id=member.id)
    await real_session.commit()
    got = (await client.get(f"/api/v1/teams/{team['id']}")).json()
    assert got["member_count"] == 1
    listed = (await client.get("/api/v1/teams")).json()
    root = next(t for t in listed if t["is_root"])
    assert root["member_count"] >= 2  # admin + the materialized new member


@pytest.mark.asyncio
async def test_reparent_subteam_moves_under_new_parent(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = (await client.post("/api/v1/teams", json={"name": "A"})).json()
    b = (await client.post("/api/v1/teams", json={"name": "B"})).json()
    child = (
        await client.post("/api/v1/teams", json={"name": "Child", "parent_team_id": a["id"]})
    ).json()
    resp = await client.post(
        f"/api/v1/teams/{child['id']}/move", json={"new_parent_team_id": b["id"]}
    )
    assert resp.status_code == 200
    assert resp.json()["parent_team_id"] == b["id"]


@pytest.mark.asyncio
async def test_reparent_under_own_descendant_rejected(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = (await client.post("/api/v1/teams", json={"name": "A"})).json()
    child = (
        await client.post("/api/v1/teams", json={"name": "Child", "parent_team_id": a["id"]})
    ).json()
    resp = await client.post(
        f"/api/v1/teams/{a['id']}/move", json={"new_parent_team_id": child["id"]}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reparent_root_rejected(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    b = (await client.post("/api/v1/teams", json={"name": "B"})).json()
    resp = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/move", json={"new_parent_team_id": b["id"]}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reparent_requires_org_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = (await client.post("/api/v1/teams", json={"name": "A"})).json()
    b = (await client.post("/api/v1/teams", json={"name": "B"})).json()
    child = (
        await client.post("/api/v1/teams", json={"name": "C", "parent_team_id": a["id"]})
    ).json()
    await login(client, member.email, member_pw or "")
    resp = await client.post(
        f"/api/v1/teams/{child['id']}/move", json={"new_parent_team_id": b["id"]}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reparent_cross_org_parent_returns_404(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    import secrets

    from backend.services.org import teams as team_service

    other_root, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=f"other-admin-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="x" * 16,
    )
    await real_session.commit()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    child = (await client.post("/api/v1/teams", json={"name": "Child"})).json()
    resp = await client.post(
        f"/api/v1/teams/{child['id']}/move",
        json={"new_parent_team_id": str(other_root.id)},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# delete guards: the CASCADE tables a team owns
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# structural caps: team creation is otherwise an unbounded INSERT loop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_team_creation_is_capped_per_org(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
):
    """Every tenancy check in the platform walks this tree; an org that can grow
    it without bound is a self-service denial of service."""
    from backend.services.org import teams as team_service

    monkeypatch.setattr(team_service, "MAX_TEAMS_PER_ORG", 3)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.post("/api/v1/teams", json={"name": "q1"})).status_code == 201
    assert (await client.post("/api/v1/teams", json={"name": "q2"})).status_code == 201
    over = await client.post("/api/v1/teams", json={"name": "q3"})
    assert over.status_code == 400
    assert "maximum" in over.json()["error"]["message"]


@pytest.mark.asyncio
async def test_team_nesting_depth_is_capped(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
):
    from backend.services.org import teams as team_service

    monkeypatch.setattr(team_service, "MAX_TEAM_DEPTH", 2)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    first = await client.post("/api/v1/teams", json={"name": "d1"})
    assert first.status_code == 201  # chain(root) == 1 < 2
    second = await client.post(
        "/api/v1/teams", json={"name": "d2", "parent_team_id": first.json()["id"]}
    )
    assert second.status_code == 400  # chain(d1) == 2
    assert "levels deep" in second.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# the tree walkers must be total
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tree_walks_terminate_when_the_table_holds_a_cycle(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """A `parent_team_id` cycle used to make `list_in_org`'s unguarded upward
    walk spin forever — inside an `async def` with no await, so it pinned the
    whole uvicorn event loop for EVERY tenant, not just the request.

    The cycle is written directly (the API refuses to create one); the contract
    under test is that the walkers degrade to a truncated answer instead of
    hanging."""
    import asyncio
    from uuid import UUID

    from backend.services.org import teams as team_service
    from sqlalchemy import text

    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = UUID((await client.post("/api/v1/teams", json={"name": "cycA"})).json()["id"])
    b = UUID(
        (
            await client.post("/api/v1/teams", json={"name": "cycB", "parent_team_id": str(a)})
        ).json()["id"]
    )
    await real_session.execute(
        text("UPDATE teams SET parent_team_id = :b WHERE id = :a"), {"a": a, "b": b}
    )
    await real_session.commit()

    try:
        async with asyncio.timeout(30):
            chain = await team_service.ancestor_chain(real_session, a)
            assert [t.id for t in chain] == [a, b]
            assert await team_service.descendant_ids(real_session, a) == [b]
            in_org = {t.id for t in await team_service.list_in_org(real_session, org_admin.org_id)}
            assert org_admin.org_id in in_org
            assert a not in in_org  # detached from the root by the cycle
            # And the victim path — any authenticated user's first page load.
            assert (await client.get("/api/v1/teams")).status_code == 200
    finally:
        # Leave the table walkable for the rest of the session.
        await real_session.execute(
            text("UPDATE teams SET parent_team_id = :root WHERE id = :a"),
            {"a": a, "root": org_admin.org_id},
        )
        await real_session.commit()


@pytest.mark.asyncio
async def test_concurrent_opposing_moves_cannot_create_a_cycle(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """Sequentially the second move is refused. Concurrently, the check-then-write
    used to interleave so BOTH committed — leaving `A.parent=B, B.parent=A`, a
    corruption no API call can repair (every route 404s a team whose chain never
    reaches the org root)."""
    import asyncio
    from uuid import UUID

    from alkera_core.db.session import AsyncSessionLocal
    from backend.services.org import teams as team_service
    from backend.services.org.teams import TeamConflictError

    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = UUID((await client.post("/api/v1/teams", json={"name": "raceA"})).json()["id"])
    b = UUID((await client.post("/api/v1/teams", json={"name": "raceB"})).json()["id"])

    barrier = asyncio.Barrier(2)

    async def _move(team_id: UUID, new_parent_id: UUID) -> str:
        async with AsyncSessionLocal() as s:
            team = await team_service.get_by_id(s, team_id)
            assert team is not None
            await barrier.wait()  # both sides have read the pre-move tree
            try:
                await team_service.reparent_team(s, team=team, new_parent_id=new_parent_id)
                await s.commit()
                return "moved"
            except TeamConflictError:
                await s.rollback()
                return "refused"

    async with asyncio.timeout(60):
        outcome = sorted(await asyncio.gather(_move(a, b), _move(b, a)))
    assert outcome == ["moved", "refused"]

    # The tree is still walkable from the root: both teams reach it.
    async with AsyncSessionLocal() as s:
        for team_id in (a, b):
            chain = await team_service.ancestor_chain(s, team_id)
            assert chain[-1].id == org_admin.org_id


@pytest.mark.asyncio
async def test_me_names_every_team_the_caller_administers(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """A client that offers a team picker asks once, not team by team.

    An ADMIN role is granted on one team and descends to every team below it, so
    the set is the granted teams plus their subtrees — and a team beside them is
    not in it however close it sits in the tree.
    """
    import uuid

    from alkera_core.models._enums import TeamRole
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service

    upper = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name=f"upper-{uuid.uuid4().hex[:6]}"
    )
    lower = await team_service.create_subteam(
        real_session,
        org_team_id=org_admin.org_id,
        name=f"lower-{uuid.uuid4().hex[:6]}",
        parent_team_id=upper.id,
    )
    beside = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name=f"beside-{uuid.uuid4().hex[:6]}"
    )
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await membership_service.add_member(
        real_session, team_id=upper.id, user_id=member.id, role=TeamRole.ADMIN
    )
    await real_session.commit()

    # The org admin administers the root, so descent reaches every team in it.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    admin_teams = set((await client.get("/api/v1/auth/me")).json()["admin_team_ids"])
    assert {str(org_admin.org_id), str(upper.id), str(lower.id), str(beside.id)} <= admin_teams

    # Their admin holds one team and everything under it — and nothing beside it.
    await login(client, member.email, member_pw or "")
    mine = set((await client.get("/api/v1/auth/me")).json()["admin_team_ids"])
    assert mine == {str(upper.id), str(lower.id)}

    # And a login seeds the same answer, so a client caching the response never
    # shows an empty picker to somebody who does administer a team.
    seeded = await client.post(
        "/api/v1/auth/login", json={"email": member.email, "password": member_pw or ""}
    )
    assert set(seeded.json()["user"]["admin_team_ids"]) == mine


@pytest.mark.asyncio
async def test_a_plain_member_administers_no_team(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await real_session.commit()
    await login(client, member.email, member_pw or "")
    assert (await client.get("/api/v1/auth/me")).json()["admin_team_ids"] == []
