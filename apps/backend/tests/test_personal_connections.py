"""A connection that belongs to one person, at the API boundary.

The same table, the same save pipeline and the same credential custody as a
team's connection — with one difference that has to hold everywhere: the row is
that member's, and nobody else in the org ever sees it, not the team admin above
them and not the org admin above that. The cases below are the ways that could
leak: a list, a credential fetch, a lease, a verify, a delete, and the identity
key that lets one person's ``prod`` sit next to the team's.

The worker is the only stand-in. A verification is settled here by writing the
verdict a worker would have written, because dialling a real warehouse is the
costly boundary; everything else — routes, policy, encryption, outbox — is the
production path.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest
from alkera_core.connections import Outcome, VerificationState
from alkera_core.connections.models import ConnectionVerification, TeamConnection
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.models import EventOutbox, TeamMembership, User
from alkera_core.models._enums import TeamRole
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# The one form these cases send, and the save pipeline around it
# --------------------------------------------------------------------------- #

_PG_FIELDS: dict[str, str] = {
    "host": "db.internal",
    "port": "5432",
    "dbname": "analytics",
    "user": "svc_alkera",
    "password": "s3cret-pw",
    "sslmode": "verify-full",
    "sslrootcert": "",
    "environment": "prod",
}


def _payload(handle: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "plugin": "postgres",
        "handle": handle,
        "fields": dict(_PG_FIELDS),
        "shared_fields": list(_PG_FIELDS),
        "auth_method": "password",
    }
    payload.update(overrides)
    return payload


async def _settle(verification_id: str) -> None:
    """Write the verdict a worker would have written.

    The record is the save's gate, so a test that wants to reach the save has to
    answer it — dialling a real Postgres from here is the boundary this suite
    deliberately does not cross.
    """
    async with AsyncSessionLocal() as s:
        record = (
            await s.execute(
                select(ConnectionVerification).where(
                    ConnectionVerification.id == UUID(verification_id)
                )
            )
        ).scalar_one()
        record.state = VerificationState.settled.value
        record.outcome = Outcome.ok.value
        record.detail = "Connected."
        await s.commit()


async def _save_personal(client: AsyncClient, handle: str, **overrides: object) -> Any:
    """The personal add exactly as the page runs it: verify, then save quoting
    the verification that answered for this payload."""
    payload = _payload(handle, **overrides)
    started = await client.post("/api/v1/me/connections/verifications", json=payload)
    assert started.status_code == 202, started.text
    await _settle(started.json()["id"])
    return await client.put(
        "/api/v1/me/connections", json={**payload, "verification_id": started.json()["id"]}
    )


async def _save_team(client: AsyncClient, team_id: UUID, handle: str, **overrides: object) -> Any:
    payload = _payload(handle, **overrides)
    started = await client.post(f"/api/v1/teams/{team_id}/connections/verifications", json=payload)
    assert started.status_code == 202, started.text
    await _settle(started.json()["id"])
    return await client.put(
        f"/api/v1/teams/{team_id}/connections",
        json={**payload, "verification_id": started.json()["id"]},
    )


async def _subteam_with(org: OrgWithAdmin, *, role: TeamRole) -> tuple[UUID, UUID, str, str]:
    """A subteam under the org root and one person holding ``role`` on it."""
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        sub = await team_service.create_subteam(
            s, org_team_id=org.org_id, name=f"sub-{uuid.uuid4().hex[:6]}"
        )
        user, password = await make_member(s, org_id=org.org_id, verified=True)
        await membership_service.add_member(s, team_id=sub.id, user_id=user.id, role=role)
        await s.commit()
        return sub.id, user.id, user.email, password or ""


async def _member_of_root(org: OrgWithAdmin) -> tuple[UUID, str, str]:
    async with AsyncSessionLocal() as s:
        user, password = await make_member(
            s, org_id=org.org_id, verified=True, role=TeamRole.MEMBER
        )
        await s.commit()
        return user.id, user.email, password or ""


async def _row(connection_id: str) -> TeamConnection:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(select(TeamConnection).where(TeamConnection.id == UUID(connection_id)))
        ).scalar_one()


# --------------------------------------------------------------------------- #
# A first save is told to run the check, not that it lost one
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "door",
    [
        pytest.param("personal", id="the-personal-door"),
        pytest.param("team", id="the-team-door"),
    ],
)
async def test_a_save_that_names_no_check_is_told_to_run_one(
    client: AsyncClient, org_admin: OrgWithAdmin, door: str
) -> None:
    """The dialog's FIRST save names no verification because none has been run.
    Answering that with "not one of yours" describes a check the person never
    had and sends them hunting for it; the reason has to be the one the dialog
    switches on to start the test."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    url = (
        "/api/v1/me/connections"
        if door == "personal"
        else f"/api/v1/teams/{org_admin.org_id}/connections"
    )

    refused = await client.put(url, json=_payload("first_save"))

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["details"]["reason"] == "verification_required"
    assert error["message"] == "Test this connection before saving it."


@pytest.mark.parametrize(
    "door",
    [
        pytest.param("personal", id="the-personal-door"),
        pytest.param("team", id="the-team-door"),
    ],
)
async def test_a_save_that_names_a_check_nobody_here_owns_keeps_its_own_reason(
    client: AsyncClient, org_admin: OrgWithAdmin, door: str
) -> None:
    """The asymmetric half: an id that is not the caller's is a different
    situation with a different next step, and must not collapse into the
    first-save wording."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    url = (
        "/api/v1/me/connections"
        if door == "personal"
        else f"/api/v1/teams/{org_admin.org_id}/connections"
    )

    refused = await client.put(
        url, json={**_payload("first_save"), "verification_id": str(uuid.uuid4())}
    )

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["details"]["reason"] == "unknown_verification"
    assert error["message"] == "That check is not one of yours. Run the connection test again."


# --------------------------------------------------------------------------- #
# It is stored as one person's, under the org root
# --------------------------------------------------------------------------- #


async def test_a_personal_save_stamps_the_owner_and_hangs_off_the_org_root(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The row needs a team to be reachable by every org-keyed check on the
    table; the owner is what makes it one person's."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await _save_personal(client, "mine_prod")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owner_user_id"] == str(org_admin.admin_id)
    assert body["team_id"] == str(org_admin.org_id)
    assert body["can_manage"] is True
    assert body["created_by_name"] == "Test Admin"

    row = await _row(body["id"])
    assert row.owner_user_id == org_admin.admin_id
    # The credential is stored the way a team's is: encrypted, never echoed.
    assert row.shared_secret_encrypted
    assert "s3cret-pw" not in str(row.shared_values)


async def test_one_handle_holds_both_a_personal_and_a_team_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The identity key folds the owner in, so a member naming their connection
    ``prod`` does not collide with the team's ``prod`` — nor overwrite it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "prod")
    assert team.status_code == 200, team.text
    personal = await _save_personal(client, "prod")
    assert personal.status_code == 200, personal.text
    assert personal.json()["id"] != team.json()["id"]

    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(TeamConnection).where(
                        TeamConnection.team_id == org_admin.org_id,
                        TeamConnection.handle == "prod",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {r.owner_user_id for r in rows} == {None, org_admin.admin_id}


async def test_saving_the_same_personal_handle_twice_updates_one_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    first = await _save_personal(client, "edit_me")
    assert first.status_code == 200, first.text
    edited = dict(_PG_FIELDS, dbname="warehouse")
    second = await _save_personal(client, "edit_me", fields=edited, shared_fields=list(edited))
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["shared_values"]["dbname"] == "warehouse"


# --------------------------------------------------------------------------- #
# Nobody else sees it
# --------------------------------------------------------------------------- #


async def test_a_personal_row_is_absent_from_the_teams_admin_list(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """It hangs off the org root for reachability, not for display: the admin
    list is what the TEAM has configured."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    personal = await _save_personal(client, "not_the_teams")
    assert personal.status_code == 200, personal.text
    listed = await client.get(f"/api/v1/teams/{org_admin.org_id}/connections")
    assert listed.status_code == 200, listed.text
    assert personal.json()["id"] not in [row["id"] for row in listed.json()]


async def test_another_members_personal_row_is_invisible_to_the_org_admin(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Descent reaches the rows a team configured, never a member's own
    credential — the admin above them is still not its owner."""
    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    mine = await _save_personal(client, "members_own")
    assert mine.status_code == 200, mine.text
    connection_id = mine.json()["id"]

    await login(client, org_admin.admin_email, org_admin.admin_password)
    for url in (
        "/api/v1/me/connections",
        "/api/v1/me/team-connections",
    ):
        body = (await client.get(url)).json()
        rows = body if isinstance(body, list) else body["connections"]
        assert connection_id not in [row["id"] for row in rows], url
    admin_list = await client.get(f"/api/v1/teams/{org_admin.org_id}/connections")
    assert connection_id not in [row["id"] for row in admin_list.json()]


async def test_a_sibling_team_admin_never_sees_another_members_personal_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    _sub, _uid, email, password = await _subteam_with(org_admin, role=TeamRole.ADMIN)
    _member_id, member_email, member_password = await _member_of_root(org_admin)
    await login(client, member_email, member_password)
    mine = await _save_personal(client, "sibling_blind")
    assert mine.status_code == 200, mine.text

    await login(client, email, password)
    rows = (await client.get("/api/v1/me/connections")).json()
    assert mine.json()["id"] not in [row["id"] for row in rows]


# --------------------------------------------------------------------------- #
# The page's list: one list, two kinds of row, one answer about who may change it
# --------------------------------------------------------------------------- #


async def test_the_list_carries_the_members_own_rows_and_their_teams(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "team_row")
    assert team.status_code == 200, team.text

    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    mine = await _save_personal(client, "my_row")
    assert mine.status_code == 200, mine.text

    rows = {row["id"]: row for row in (await client.get("/api/v1/me/connections")).json()}
    assert {team.json()["id"], mine.json()["id"]} <= set(rows)
    # A member may use the team's row and change their own.
    assert rows[team.json()["id"]]["can_manage"] is False
    assert rows[mine.json()["id"]]["can_manage"] is True
    assert rows[team.json()["id"]]["created_by_name"] == "Test Admin"
    assert rows[team.json()["id"]]["team_name"]


async def test_can_manage_descends_through_the_tree_and_stops_at_a_sibling(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Admin of the owning team OR of any team above it may change the row.

    The person here administers ``upper`` and belongs to ``lower`` beneath it,
    so the row they may change is not only the one on their own team: descent
    decides. The row above them and the row beside them are both refused, and
    the one beside them is not even visible.
    """
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        upper = await team_service.create_subteam(
            s, org_team_id=org_admin.org_id, name=f"upper-{uuid.uuid4().hex[:6]}"
        )
        lower = await team_service.create_subteam(
            s,
            org_team_id=org_admin.org_id,
            name=f"lower-{uuid.uuid4().hex[:6]}",
            parent_team_id=upper.id,
        )
        beside = await team_service.create_subteam(
            s, org_team_id=org_admin.org_id, name=f"beside-{uuid.uuid4().hex[:6]}"
        )
        user, password = await make_member(s, org_id=org_admin.org_id, verified=True)
        await membership_service.add_member(
            s, team_id=upper.id, user_id=user.id, role=TeamRole.ADMIN
        )
        await membership_service.add_member(
            s, team_id=lower.id, user_id=user.id, role=TeamRole.MEMBER
        )
        await s.commit()
        email, upper_id, lower_id, beside_id = user.email, upper.id, lower.id, beside.id

    await login(client, org_admin.admin_email, org_admin.admin_password)
    saved = {}
    for name, team_id in (
        ("root", org_admin.org_id),
        ("upper", upper_id),
        ("lower", lower_id),
        ("beside", beside_id),
    ):
        resp = await _save_team(client, team_id, f"{name}_row")
        assert resp.status_code == 200, resp.text
        saved[name] = resp.json()["id"]

    await login(client, email, password or "")
    rows = {r["id"]: r for r in (await client.get("/api/v1/me/connections")).json()}
    # Their own team, and the one below it they belong to: both theirs to change.
    assert rows[saved["upper"]]["can_manage"] is True
    assert rows[saved["lower"]]["can_manage"] is True
    # The org's own row is above their admin rights: usable, not editable.
    assert rows[saved["root"]]["can_manage"] is False
    # A team they are not in is not on their page at all.
    assert saved["beside"] not in rows


async def test_a_member_reads_no_consent_record_for_a_row_they_cannot_manage(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Who handed a credential to the team is the owning admin's account of it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "consent_row")
    assert team.status_code == 200, team.text
    assert team.json()["shared_consent"]["approved_by"] == str(org_admin.admin_id)

    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    rows = {row["id"]: row for row in (await client.get("/api/v1/me/connections")).json()}
    assert rows[team.json()["id"]]["shared_consent"] is None


async def test_a_member_cannot_re_check_a_team_row_they_only_use(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Re-checking dials the warehouse with the team's stored credential, so it
    answers a manager and not everyone the row is usable by. The member is
    refused on the team route and has no personal route to fall back to — the
    contract the Connections page mirrors by offering them no re-check."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "shared_row")
    assert team.status_code == 200, team.text
    connection_id = team.json()["id"]
    verify_url = f"/api/v1/teams/{org_admin.org_id}/connections/{connection_id}/verify"
    assert (await client.post(verify_url)).status_code == 202, "its admin re-checks it freely"

    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    listed = {row["id"]: row for row in (await client.get("/api/v1/me/connections")).json()}
    assert listed[connection_id]["can_manage"] is False, "usable, not theirs to change"

    refused = await client.post(verify_url)
    assert refused.status_code == 403, refused.text
    # Nothing scoped to this person re-checks a row that is not theirs either.
    missing = await client.post(f"/api/v1/me/connections/{connection_id}/verify")
    assert missing.status_code == 404, missing.text


# --------------------------------------------------------------------------- #
# The daemon's sync route carries them too
# --------------------------------------------------------------------------- #


async def test_the_daemon_sync_route_includes_the_members_own_rows(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A daemon that syncs team rows syncs personal ones, on the same pass."""
    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    mine = await _save_personal(client, "daemon_row")
    assert mine.status_code == 200, mine.text

    body = (await client.get("/api/v1/me/team-connections")).json()
    row = next(r for r in body["connections"] if r["id"] == mine.json()["id"])
    assert row["owner_user_id"] == str(_member_id)
    assert row["can_manage"] is True
    assert row["shared_custody"] == "lease"


# --------------------------------------------------------------------------- #
# The credential is the owner's, and only the owner's
# --------------------------------------------------------------------------- #


async def test_only_the_owner_can_fetch_or_lease_a_personal_credential(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    mine = await _save_personal(client, "owned_secret")
    assert mine.status_code == 200, mine.text
    connection_id = mine.json()["id"]
    assert member_id

    fetched = await client.get(f"/api/v1/me/team-connections/{connection_id}/credential")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["secret"] == "s3cret-pw"
    leased = await client.post(
        f"/api/v1/me/team-connections/{connection_id}/credential-lease", json={}
    )
    assert leased.status_code == 200, leased.text

    # The org admin above them is entitled to nothing here, and learns nothing
    # about whether the row exists.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (
        await client.get(f"/api/v1/me/team-connections/{connection_id}/credential")
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/me/team-connections/{connection_id}/credential-lease", json={})
    ).status_code == 404


async def test_a_team_row_is_not_reachable_through_the_personal_routes(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The ``/me/connections`` verbs act on one person's own rows. A team row —
    even one this caller administers — is the same 404 as a row that does not
    exist, so the personal path can never become a second admin door."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "team_only")
    assert team.status_code == 200, team.text
    connection_id = team.json()["id"]

    assert (await client.delete(f"/api/v1/me/connections/{connection_id}")).status_code == 404
    assert (await client.post(f"/api/v1/me/connections/{connection_id}/verify")).status_code == 404
    assert (
        await client.post(
            f"/api/v1/me/connections/{connection_id}/rotate-secret",
            json={"shared_secret": "rotated"},
        )
    ).status_code == 404
    # And it is still there.
    assert await _row(connection_id)


async def test_only_the_owner_may_verify_rotate_or_remove(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    mine = await _save_personal(client, "owner_only")
    assert mine.status_code == 200, mine.text
    connection_id = mine.json()["id"]

    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.post(f"/api/v1/me/connections/{connection_id}/verify")).status_code == 404
    assert (
        await client.post(
            f"/api/v1/me/connections/{connection_id}/rotate-secret",
            json={"shared_secret": "rotated"},
        )
    ).status_code == 404
    assert (await client.delete(f"/api/v1/me/connections/{connection_id}")).status_code == 404
    assert await _row(connection_id)

    await login(client, email, password)
    assert (await client.post(f"/api/v1/me/connections/{connection_id}/verify")).status_code == 202
    rotated = await client.post(
        f"/api/v1/me/connections/{connection_id}/rotate-secret",
        json={"shared_secret": "rotated-pw"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["credential_version"] > mine.json()["credential_version"]
    assert (await client.delete(f"/api/v1/me/connections/{connection_id}")).status_code == 204
    async with AsyncSessionLocal() as s:
        assert (
            await s.execute(select(TeamConnection).where(TeamConnection.id == UUID(connection_id)))
        ).scalar_one_or_none() is None


async def test_the_team_routes_refuse_a_personal_row_at_its_own_org_root(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The org admin holds the id and calls the TEAM routes with it.

    A personal row hangs off the org root team, and the org admin is an admin of
    that root by descent — so the team half of these routes' guard passes on a
    row that is not the team's. Every one of them must still refuse: deleting it
    destroys a member's credential, rotating it replaces the member's stored
    secret with one the admin chose, and verifying it dials the member's
    warehouse with their credential.
    """
    member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    mine = await _save_personal(client, "not_the_teams")
    assert mine.status_code == 200, mine.text
    connection_id = mine.json()["id"]
    before = await _row(connection_id)
    stored_secret, stored_version = before.shared_secret_encrypted, before.credential_version

    await login(client, org_admin.admin_email, org_admin.admin_password)
    root = org_admin.org_id
    assert (
        await client.post(f"/api/v1/teams/{root}/connections/{connection_id}/verify")
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/teams/{root}/connections/{connection_id}/rotate-secret",
            json={"shared_secret": "admin-chose-this"},
        )
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/teams/{root}/connections/{connection_id}")
    ).status_code == 404

    after = await _row(connection_id)
    assert after.owner_user_id == member_id
    assert after.shared_secret_encrypted == stored_secret
    assert after.credential_version == stored_version
    # And the owner still reads their own credential, unchanged.
    await login(client, email, password)
    fetched = await client.get(f"/api/v1/me/team-connections/{connection_id}/credential")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["secret"] == _PG_FIELDS["password"]


async def test_the_team_routes_still_reach_the_teams_own_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The counterpart: refusing the owned row must not have refused the ordinary
    admin write on a team row sitting at the same org root."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "the_teams_own")
    assert team.status_code == 200, team.text
    connection_id = team.json()["id"]
    root = org_admin.org_id

    assert (
        await client.post(f"/api/v1/teams/{root}/connections/{connection_id}/verify")
    ).status_code == 202
    rotated = await client.post(
        f"/api/v1/teams/{root}/connections/{connection_id}/rotate-secret",
        json={"shared_secret": "rotated-pw"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["credential_version"] > team.json()["credential_version"]
    assert (
        await client.delete(f"/api/v1/teams/{root}/connections/{connection_id}")
    ).status_code == 204


async def test_a_personal_edit_never_inherits_the_teams_stored_credential(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A blank secret keeps the stored one — of THIS row. The team's row shares
    the names, and resolving the keep against it would hand its credential to a
    member's own connection."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "same_name")
    assert team.status_code == 200, team.text

    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    own_fields = dict(_PG_FIELDS, password="my-own-pw")
    mine = await _save_personal(
        client, "same_name", fields=own_fields, shared_fields=list(own_fields)
    )
    assert mine.status_code == 200, mine.text

    fetched = await client.get(f"/api/v1/me/team-connections/{mine.json()['id']}/credential")
    assert fetched.json()["secret"] == "my-own-pw"


# --------------------------------------------------------------------------- #
# The daemon hears about it
# --------------------------------------------------------------------------- #


async def _connection_events(connection_id: str) -> list[EventOutbox]:
    """This connection's announcements, oldest first.

    ``id`` is the order, not the order rows happen to come back in: the cases
    below read ``[-1]`` as "the newest frame", and a SELECT without an ORDER BY
    promises nothing about which row that is. The outbox log grows all run —
    nothing truncates it between tests — so by the end of a shard the table is
    large enough for the planner to pick a parallel scan, whose workers deliver
    their blocks in whatever order they finish them. ``id`` is the cursor every
    real consumer reads by (``read_after``), so it is also the order a test
    asserting on the newest frame means.
    """
    async with AsyncSessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(EventOutbox)
                    .where(
                        EventOutbox.type == EventType.TEAM_CONNECTION_UPDATED,
                        EventOutbox.entity_id == connection_id,
                    )
                    .order_by(EventOutbox.id)
                )
            )
            .scalars()
            .all()
        )


async def test_a_personal_write_is_announced_to_its_owner_alone(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The running daemon re-reconciles on this frame. A personal row's frame is
    addressed to the one person who can see it, so it never reaches the org
    stream every other member is reading."""
    member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    mine = await _save_personal(client, "announced")
    assert mine.status_code == 200, mine.text

    events = await _connection_events(mine.json()["id"])
    assert events, "a save announces itself"
    assert {e.visibility for e in events} == {f"user:{member_id}"}
    assert events[-1].payload["owner_user_id"] == str(member_id)
    assert events[-1].payload["deleted"] is False

    assert (await client.delete(f"/api/v1/me/connections/{mine.json()['id']}")).status_code == 204
    events = await _connection_events(mine.json()["id"])
    assert {e.visibility for e in events} == {f"user:{member_id}"}
    assert events[-1].payload["deleted"] is True


async def test_a_team_write_stays_on_the_org_stream(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The counterpart: adding the owner must not have narrowed a team row's
    announcement to whoever saved it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _save_team(client, org_admin.org_id, "org_stream")
    assert team.status_code == 200, team.text
    events = await _connection_events(team.json()["id"])
    assert events
    assert {e.visibility for e in events} == {"org"}
    assert events[-1].payload["owner_user_id"] is None


# --------------------------------------------------------------------------- #
# The save gate applies here too
# --------------------------------------------------------------------------- #


async def test_a_personal_save_without_a_settled_check_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put("/api/v1/me/connections", json=_payload("ungated"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "verification_required"


async def test_one_members_verification_cannot_save_anothers_connection(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A draft belongs to whoever asked for it; the save reads the same rule."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    payload = _payload("borrowed")
    started = await client.post("/api/v1/me/connections/verifications", json=payload)
    assert started.status_code == 202, started.text
    await _settle(started.json()["id"])

    _member_id, email, password = await _member_of_root(org_admin)
    await login(client, email, password)
    stolen = await client.put(
        "/api/v1/me/connections", json={**payload, "verification_id": started.json()["id"]}
    )
    assert stolen.status_code == 409, stolen.text
    assert stolen.json()["error"]["code"] == "verification_required"
    assert (
        await client.get(f"/api/v1/me/connections/verifications/{started.json()['id']}")
    ).status_code == 404


async def test_a_personal_row_holds_nothing_back_for_a_member_to_answer(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """There is no second person on a personal connection, so every value the
    owner typed is stored — a client that tried to hold one back would otherwise
    save a row nobody will ever complete."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await _save_personal(client, "no_member_half", shared_fields=[])
    assert resp.status_code == 200, resp.text
    assert resp.json()["member_fields"] == []


async def test_a_member_of_no_team_still_gets_their_own_rows(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Visibility of a personal row is ownership, not membership: a person with
    no membership row anywhere still reads the connection they added."""
    async with AsyncSessionLocal() as s:
        user, password = await make_member(s, org_id=org_admin.org_id, verified=True)
        await s.execute(TeamMembership.__table__.delete().where(TeamMembership.user_id == user.id))
        await s.commit()
        email = user.email
    await login(client, email, password or "")
    mine = await _save_personal(client, "unteamed")
    assert mine.status_code == 200, mine.text
    rows = (await client.get("/api/v1/me/connections")).json()
    assert [r["id"] for r in rows] == [mine.json()["id"]]


# --------------------------------------------------------------------------- #
# The page lists every team the caller ADMINISTERS, not only the ones they joined
# --------------------------------------------------------------------------- #


async def _tree_with_a_sub_team_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> tuple[UUID, str, dict[str, tuple[str, str]]]:
    """``root → a → a1`` and ``root → b``, with a team connection on ``a1``.

    Four people stand in the four relationships to ``a1``: the org root's admin,
    an admin of ``a`` one level above it, an admin of the sibling branch ``b``,
    and a plain member of the root. None of them holds a membership row on
    ``a1`` — joining a team materializes rows UPWARD, so descent is the only
    thing that can put ``a1``'s row on anybody's page but its own members'.
    """
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        a = await team_service.create_subteam(
            s, org_team_id=org_admin.org_id, name=f"a-{uuid.uuid4().hex[:6]}"
        )
        a1 = await team_service.create_subteam(
            s,
            org_team_id=org_admin.org_id,
            name=f"a1-{uuid.uuid4().hex[:6]}",
            parent_team_id=a.id,
        )
        b = await team_service.create_subteam(
            s, org_team_id=org_admin.org_id, name=f"b-{uuid.uuid4().hex[:6]}"
        )
        people: dict[str, tuple[str, str]] = {}
        for label, team_id in (("a_admin", a.id), ("b_admin", b.id)):
            user, password = await make_member(s, org_id=org_admin.org_id, verified=True)
            await membership_service.add_member(
                s, team_id=team_id, user_id=user.id, role=TeamRole.ADMIN
            )
            people[label] = (user.email, password or "")
        plain, plain_password = await make_member(
            s, org_id=org_admin.org_id, verified=True, role=TeamRole.MEMBER
        )
        people["root_member"] = (plain.email, plain_password or "")
        await s.commit()
        a1_id = a1.id

    await login(client, org_admin.admin_email, org_admin.admin_password)
    saved = await _save_team(client, a1_id, "deep_row")
    assert saved.status_code == 200, saved.text
    people["org_admin"] = (org_admin.admin_email, org_admin.admin_password)
    return a1_id, saved.json()["id"], people


async def _has_membership(user_email: str, team_id: UUID) -> bool:
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == user_email))).scalar_one()
        row = await s.execute(
            select(TeamMembership.id).where(
                TeamMembership.user_id == user.id, TeamMembership.team_id == team_id
            )
        )
        return row.scalar_one_or_none() is not None


@pytest.mark.parametrize(
    ("who", "visible"),
    [
        pytest.param("org_admin", True, id="the-org-root-admin-sees-a-sub-teams-row"),
        pytest.param("a_admin", True, id="an-admin-of-the-parent-sees-the-childs-row"),
        pytest.param("b_admin", False, id="an-admin-of-a-sibling-branch-does-not"),
        pytest.param("root_member", False, id="a-plain-member-of-the-root-does-not"),
    ],
)
async def test_the_page_lists_the_rows_of_every_team_the_caller_administers(
    client: AsyncClient, org_admin: OrgWithAdmin, who: str, visible: bool
) -> None:
    """An admin who creates a sub-team's connection has to find it afterwards.

    Visibility on this page follows the administered set — every team at or
    below one where the caller holds admin — not the caller's own memberships.
    An admin one branch over is unchanged by that: descent goes down, never
    sideways, and a member who administers nothing sees only what they joined.
    """
    a1_id, connection_id, people = await _tree_with_a_sub_team_row(client, org_admin)
    email, password = people[who]
    assert not await _has_membership(email, a1_id), "the case is only about descent"

    await login(client, email, password)
    rows = {row["id"]: row for row in (await client.get("/api/v1/me/connections")).json()}
    assert (connection_id in rows) is visible
    if visible:
        assert rows[connection_id]["can_manage"] is True
        assert rows[connection_id]["team_id"] == str(a1_id)


async def test_a_row_the_caller_only_belongs_to_still_reads_as_unmanageable(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Widening the list did not widen who may change a row: the org's own row
    is above a sub-team admin's rights, and stays usable-not-editable."""
    _a1_id, _connection_id, people = await _tree_with_a_sub_team_row(client, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    root_row = await _save_team(client, org_admin.org_id, "root_row")
    assert root_row.status_code == 200, root_row.text

    email, password = people["a_admin"]
    await login(client, email, password)
    rows = {row["id"]: row for row in (await client.get("/api/v1/me/connections")).json()}
    assert rows[root_row.json()["id"]]["can_manage"] is False


@pytest.mark.parametrize(
    ("who", "synced"),
    [
        pytest.param("org_admin", True, id="the-org-admin-above-it"),
        pytest.param("a_admin", True, id="an-admin-one-level-above-it"),
        pytest.param("b_admin", False, id="an-admin-of-the-sibling-branch"),
        pytest.param("root_member", False, id="a-plain-member-of-the-root"),
    ],
)
async def test_the_daemon_listing_follows_the_same_descent_the_page_does(
    client: AsyncClient, org_admin: OrgWithAdmin, who: str, synced: bool
) -> None:
    """Usage follows the visibility the Connections page has.

    The org owner who created a sub-team's connection could verify and save it
    and see it on the page, and their own box still answered "no added
    connection": the daemon's sync listing counted membership rows only, and
    joining a sub-team materializes rows UPWARD, so the admin above it held
    none. What an administrator configured is theirs to use, so their workspace
    materializes it; a member with no admin still sees only their own teams.
    """
    _a1_id, connection_id, people = await _tree_with_a_sub_team_row(client, org_admin)
    email, password = people[who]
    await login(client, email, password)
    body = (await client.get("/api/v1/me/team-connections")).json()
    assert (connection_id in [row["id"] for row in body["connections"]]) is synced


@pytest.mark.parametrize(
    ("who", "leases"),
    [
        pytest.param("org_admin", True, id="the-org-admin-above-it"),
        pytest.param("a_admin", True, id="an-admin-one-level-above-it"),
        pytest.param("b_admin", False, id="an-admin-of-the-sibling-branch"),
        pytest.param("root_member", False, id="a-plain-member-of-the-root"),
    ],
)
async def test_the_credential_lease_follows_the_same_descent_as_the_listing(
    client: AsyncClient, org_admin: OrgWithAdmin, who: str, leases: bool
) -> None:
    """A row the listing hands a workspace is a row that workspace can lease
    the credential of — a listing that widened without the lease would have
    the box materialize the connection and then fail to dial it. Anybody the
    listing leaves out is told the row does not exist, as before."""
    _a1_id, connection_id, people = await _tree_with_a_sub_team_row(client, org_admin)
    email, password = people[who]
    await login(client, email, password)
    resp = await client.post(f"/api/v1/me/team-connections/{connection_id}/credential-lease")
    assert resp.status_code == (200 if leases else 404), resp.text


# --------------------------------------------------------------------------- #
# A personal row has no members, so it does not answer the team form's rules
# --------------------------------------------------------------------------- #


async def test_an_auth_method_only_a_person_can_hold_is_savable_on_a_personal_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """BigQuery's application-default credentials live on ONE machine belonging to
    ONE person. There is nothing for an admin to preconfigure for a team, which is
    why the team form refuses it — and exactly why a personal connection exists.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    payload = {
        "plugin": "bigquery",
        "handle": "my_adc",
        "auth_method": "adc",
        "fields": {"project": "qa-lane-a-proj", "environment": "dev"},
        "shared_fields": ["project", "environment"],
    }

    started = await client.post("/api/v1/me/connections/verifications", json=payload)

    assert started.status_code == 202, started.text

    team = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/connections/verifications", json=payload
    )
    # The team half is unchanged: nobody can preconfigure somebody else's ADC.
    assert team.status_code == 422, team.text


async def test_a_blank_optional_field_on_a_personal_row_is_blank_not_a_member_slot(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Snowflake's role / warehouse / database / schema are optional session
    defaults. Left out of a TEAM browser sign-in they are fields a member would
    have to answer, and a browser sign-in cannot collect them — so that refusal is
    right. On a personal row there is no member to leave them to: they are simply
    unset, and the save must go through."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    payload = {
        "plugin": "snowflake",
        "handle": "my_sf",
        "auth_method": "oauth",
        "fields": {"account": "myorg-myaccount", "environment": "dev"},
        "shared_fields": ["account", "environment"],
    }

    started = await client.post("/api/v1/me/connections/verifications", json=payload)

    assert started.status_code == 202, started.text
    saved = await client.put(
        "/api/v1/me/connections", json={**payload, "verification_id": started.json()["id"]}
    )
    assert saved.status_code == 200, saved.text

    team = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/connections/verifications", json=payload
    )
    # The team half is unchanged: those fields really are left to members there.
    assert team.status_code == 422, team.text
    assert "left to members" in team.text


async def test_a_personal_save_still_refuses_a_method_the_connector_does_not_offer(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Widening which methods a person may pick is not the same as accepting any
    string: a method Snowflake never declared is still refused, so the personal
    path did not become an unvalidated one."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    payload = {
        "plugin": "snowflake",
        "handle": "bogus",
        "auth_method": "telepathy",
        "fields": {"account": "myorg-myaccount", "environment": "dev"},
        "shared_fields": ["account", "environment"],
    }

    started = await client.post("/api/v1/me/connections/verifications", json=payload)

    assert started.status_code == 422, started.text


async def _settle_as(verification_id: str, *, outcome: Outcome, detail: str) -> None:
    """Settle the gate with a verdict of the caller's choosing."""
    async with AsyncSessionLocal() as s:
        record = (
            await s.execute(
                select(ConnectionVerification).where(
                    ConnectionVerification.id == UUID(verification_id)
                )
            )
        ).scalar_one()
        record.state = VerificationState.settled.value
        record.outcome = outcome.value
        record.detail = detail
        await s.commit()


async def test_a_saved_row_carries_the_verdict_it_was_saved_on(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The row is born verified, so it has to say what it was born verified BY.
    A row whose badge reads "connected" while its own outcome and verification
    state read null cannot be told apart from one nothing ever checked — which
    is what every consumer downstream of the badge reads those two fields for."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    saved = await _save_personal(client, "stamped")

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["badge"] == "connected"
    assert body["outcome"] == "ok"
    assert body["last_verified_at"] is not None
    # In flight is the only thing the row's own state axis says
    # (`ck_tc_verification_state`), and a settled check is not in flight.
    assert body["verification_state"] is None


async def test_a_settlement_that_dialed_nothing_does_not_stamp_the_freshness_clock(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """``unsupported`` means the connector has no server-side read — nothing was
    dialed, so nothing was learned. Stamping ``last_verified_at`` anyway makes an
    unchecked row read as freshly checked, and it keeps reading that way until a
    freshness horizon it never earned expires."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    payload = _payload("undialed")
    started = await client.post("/api/v1/me/connections/verifications", json=payload)
    assert started.status_code == 202, started.text
    await _settle_as(
        started.json()["id"], outcome=Outcome.unsupported, detail="No server-side check."
    )

    saved = await client.put(
        "/api/v1/me/connections", json={**payload, "verification_id": started.json()["id"]}
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["last_verified_at"] is None
    # The settlement still happened and is still on the row — only the freshness
    # claim it never earned is withheld.
    assert body["outcome"] == "unsupported"
    assert body["badge"] == "not_checked"
