"""A person's own box, registered through the device flow, through the real routes.

The person approves the box on their own session; the box receives a machine
credential (never a session) bound to their org and to them. It claims its
machine on that credential and from then on runs their own private chats and
nobody else's: a colleague's chat and another org's chat are never placed on
it, a chat bound to it by any other path is still not reachable on it, and it
stops standing the moment its credential is revoked or its person leaves the
org.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from alkera_core.auth.machine_credential_standing import live_machine_of
from alkera_core.auth.machine_token import MACHINE_TOKEN_PREFIX
from alkera_core.compute.machines import live_workspace_machines
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    ComputeAllocation,
    MachineCredential,
    OrgAuditEvent,
    TeamMembership,
    User,
    WorkspaceObject,
)
from alkera_core.models.compute import PERSONAL_TENANCY
from backend.api.routes.compute.machines import heartbeat_decision_sink
from backend.services.chats import chat_service
from backend.services.compute.placement import (
    MachineBinding,
    bind_stranded_chats,
    personal_machine_for,
)
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@pytest.fixture(autouse=True)
def _quiet_limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from backend.services.identity.device_authorization import _user_code_limiter

    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)
    _user_code_limiter.reset()
    heartbeat_decision_sink().clear()
    yield
    _user_code_limiter.reset()
    heartbeat_decision_sink().clear()


class Person:
    def __init__(self, user: User, password: str) -> None:
        self.user = user
        self.password = password

    @property
    def id(self) -> UUID:
        return self.user.id

    async def client(self) -> AsyncClient:
        signed_in = app_client()
        await login(signed_in, self.user.email, self.password)
        return signed_in


async def _member(org_id: UUID) -> Person:
    async with AsyncSessionLocal() as session:
        user, password = await make_member(session, org_id=org_id, verified=True)
    assert password is not None
    return Person(user, password)


async def _other_org() -> UUID:
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Elsewhere {secrets.token_hex(4)}",
            admin_email=f"elsewhere-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="Else",
            admin_last_name="Where",
            admin_password="pw-1234567890",
        )
        await session.commit()
        return org.id


async def _register(client: AsyncClient, person: Person) -> str:
    """The device flow, as the box and the person drive it."""
    code = await client.post(
        "/api/v1/auth/device/code", data={"client_id": "alkera-box", "scope": "box"}
    )
    assert code.status_code == 200, code.text
    browser = await person.client()
    try:
        approved = await browser.post(
            "/api/v1/auth/device/approve", json={"user_code": code.json()["user_code"]}
        )
        assert approved.status_code == 200, approved.text
    finally:
        await browser.aclose()
    token = await client.post(
        "/api/v1/auth/device/token",
        data={
            "grant_type": GRANT,
            "device_code": code.json()["device_code"],
            "client_id": "alkera-box",
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert body["credential_type"] == "machine"
    assert body["org_id"] == str(person.user.home_org_team_id)
    raw: str = body["access_token"]
    return raw


class Box:
    def __init__(self, raw: str, machine_id: str) -> None:
        self.raw = raw
        self.machine_id = machine_id

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.raw}"}


async def _personal_box(client: AsyncClient, person: Person) -> Box:
    raw = await _register(client, person)
    claimed = await client.post(
        "/api/v1/machines/claim",
        json={"provider_pod_id": f"laptop-{secrets.token_hex(4)}", "name": "my laptop"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert claimed.status_code == 201, claimed.text
    box = Box(raw, claimed.json()["id"])
    beat = await client.post(f"/api/v1/machines/{box.machine_id}/heartbeat", headers=box.headers)
    assert beat.status_code == 204, beat.text
    return box


async def _create(person: Person) -> dict[str, Any]:
    browser = await person.client()
    try:
        resp = await browser.post("/api/v1/chats", json={"title": "Q4"})
    finally:
        await browser.aclose()
    assert resp.status_code in (200, 201), resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _bound(chat_id: str) -> str | None:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        return chat_service.chat_spec_of(chat).machine_id


async def _force_bind(chat_id: str, machine_id: str) -> None:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        await chat_service.rebind_machine(
            session, chat=chat, machine_id=machine_id, machine_status="ready"
        )
        await session.commit()


# =========================================================================== #
# registration
# =========================================================================== #


async def test_the_device_flow_gives_a_box_a_personal_machine_credential(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _member(org_admin.org_id)
    raw = await _register(client, owner)
    assert raw.startswith(MACHINE_TOKEN_PREFIX)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(MachineCredential).where(MachineCredential.created_by == owner.id)
            )
        ).scalar_one()
        audit = (
            await session.execute(
                select(OrgAuditEvent).where(
                    OrgAuditEvent.org_team_id == org_admin.org_id,
                    OrgAuditEvent.action == "compute.personal_box_registered",
                )
            )
        ).scalar_one()
    assert (row.tenancy, row.org_team_id) == (PERSONAL_TENANCY, org_admin.org_id)
    assert audit.target == str(row.id)


async def test_the_claimed_machine_is_the_persons_whoever_stood_it_up(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(box.machine_id))
    assert alloc is not None
    assert (alloc.tenancy, alloc.user_id, alloc.org_team_id) == (
        PERSONAL_TENANCY,
        owner.id,
        org_admin.org_id,
    )
    assert alloc.price_per_minute_nanos == 0 and alloc.grant_id is None


async def test_a_personal_credential_cannot_take_over_a_colleagues_box_by_its_pod_id(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner, colleague = await _member(org_admin.org_id), await _member(org_admin.org_id)
    pod = f"laptop-{secrets.token_hex(4)}"
    theirs = await _register(client, owner)
    first = await client.post(
        "/api/v1/machines/claim",
        json={"provider_pod_id": pod, "name": "a"},
        headers={"Authorization": f"Bearer {theirs}"},
    )
    assert first.status_code == 201, first.text
    mine = await _register(client, colleague)
    taken = await client.post(
        "/api/v1/machines/claim",
        json={"provider_pod_id": pod, "name": "b"},
        headers={"Authorization": f"Bearer {mine}"},
    )
    assert taken.status_code == 404, taken.text
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(first.json()["id"]))
    assert alloc is not None and alloc.user_id == owner.id


# =========================================================================== #
# placement: the owner's private chats, and nobody else's
# =========================================================================== #


async def test_the_owners_new_chat_runs_on_their_box_and_a_colleagues_does_not(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner, colleague = await _member(org_admin.org_id), await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    mine = await _create(owner)
    theirs = await _create(colleague)
    assert mine["machine_id"] == box.machine_id
    assert theirs["machine_id"] != box.machine_id
    assert await _bound(theirs["id"]) != box.machine_id


async def test_another_orgs_chat_is_never_placed_on_a_personal_box(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    stranger = await _member(await _other_org())
    theirs = await _create(stranger)
    assert theirs["machine_id"] != box.machine_id


async def test_a_box_coming_up_takes_only_its_owners_stranded_chats(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The box's claim is a placement moment: every stranded chat whose
    placement now lands on it moves. Only the owner's do."""
    owner, colleague = await _member(org_admin.org_id), await _member(org_admin.org_id)
    stranger = await _member(await _other_org())
    waiting = [await _create(owner), await _create(colleague), await _create(stranger)]
    assert all(chat["machine_id"] is None for chat in waiting)
    box = await _personal_box(client, owner)
    assert [await _bound(chat["id"]) for chat in waiting] == [box.machine_id, None, None]


async def test_an_org_box_coming_up_does_not_take_a_chat_off_its_owners_box(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    mine = await _create(owner)
    assert mine["machine_id"] == box.machine_id
    async with AsyncSessionLocal() as session:
        # Any binding the org's own boxes are asked to take: the owner's chat is
        # served, so nothing is stranded.
        serving = (await session.execute(live_workspace_machines(org_admin.org_id))).scalars()
        assert box.machine_id not in {str(m.id) for m in serving}
        moved = await bind_stranded_chats(
            session,
            org_team_id=org_admin.org_id,
            binding=MachineBinding(machine_id=UUID(box.machine_id), status="ready", name=""),
            actor=None,
        )
    assert moved == []
    assert await _bound(mine["id"]) == box.machine_id


# =========================================================================== #
# the box reaches its owner's chats only
# =========================================================================== #


async def test_a_colleagues_chat_bound_to_the_box_by_any_path_is_still_not_reachable(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner, colleague = await _member(org_admin.org_id), await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    mine = await _create(owner)
    theirs = await _create(colleague)
    await _force_bind(theirs["id"], box.machine_id)

    ok = await client.get(f"/api/v1/chats/{mine['id']}", headers=box.headers)
    assert ok.status_code == 200, ok.text
    for door in ("", "/messages"):
        refused = await client.get(f"/api/v1/chats/{theirs['id']}{door}", headers=box.headers)
        assert refused.status_code == 404, refused.text
    minted = await client.post(f"/api/v1/chats/{theirs['id']}/gateway-token", headers=box.headers)
    assert minted.status_code == 404, minted.text
    listed = await client.get("/api/v1/chats", headers=box.headers)
    assert {c["id"] for c in listed.json()["items"]} == {mine["id"]}


async def test_a_personal_box_mints_a_worker_credential_for_its_own_org_only(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    elsewhere = await _other_org()
    # A worker is minted for an org the box holds work for: its person's chat.
    assert (await _create(owner))["machine_id"] == box.machine_id
    own = await client.post(
        "/api/v1/machines/me/worker-credentials",
        json={"org_id": str(org_admin.org_id)},
        headers=box.headers,
    )
    assert own.status_code == 201, own.text
    other = await client.post(
        "/api/v1/machines/me/worker-credentials",
        json={"org_id": str(elsewhere)},
        headers=box.headers,
    )
    assert other.status_code == 404, other.text


# =========================================================================== #
# the box's standing: its credential and its person
# =========================================================================== #


async def _revoke(machine_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(MachineCredential)
            .where(MachineCredential.machine_id == UUID(machine_id))
            .values(revoked_at=datetime.now(UTC))
        )
        await session.commit()


async def _deactivate(person: Person) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.id == person.id).values(is_active=False))
        await session.commit()


async def _leave_root(person: Person, org_id: UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(TeamMembership).where(
                TeamMembership.user_id == person.id, TeamMembership.team_id == org_id
            )
        )
        await session.commit()


@pytest.mark.parametrize("ends", ["revoked", "owner-deactivated", "owner-left-the-org"])
async def test_a_box_whose_standing_ends_speaks_for_nothing_and_takes_no_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, ends: str
) -> None:
    owner = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    mine = await _create(owner)
    assert mine["machine_id"] == box.machine_id
    if ends == "revoked":
        await _revoke(box.machine_id)
    elif ends == "owner-deactivated":
        await _deactivate(owner)
    else:
        await _leave_root(owner, org_admin.org_id)

    beat = await client.post(f"/api/v1/machines/{box.machine_id}/heartbeat", headers=box.headers)
    assert beat.status_code == 401, beat.text
    read = await client.get(f"/api/v1/chats/{mine['id']}", headers=box.headers)
    assert read.status_code == 401, read.text
    async with AsyncSessionLocal() as session:
        assert (
            await personal_machine_for(
                session, org_team_id=org_admin.org_id, owner_user_id=owner.id
            )
            is None
        )
        credential = (
            await session.execute(
                select(MachineCredential.id).where(
                    MachineCredential.machine_id == UUID(box.machine_id)
                )
            )
        ).scalar_one()
        assert await live_machine_of(session, credential) is None
    if ends == "revoked":
        # The person is still here: their next chat runs elsewhere.
        assert (await _create(owner))["machine_id"] != box.machine_id


# =========================================================================== #
# the box ends with its person's sessions
# =========================================================================== #


async def _logout_all(person: Person) -> None:
    browser = await person.client()
    try:
        resp = await browser.post("/api/v1/auth/logout-all")
        assert resp.status_code == 200, resp.text
    finally:
        await browser.aclose()


async def _reset_password(person: Person) -> None:
    from backend.services.identity import password_reset as password_reset_service

    async with AsyncSessionLocal() as session:
        user = await session.get(User, person.id)
        assert user is not None
        token = await password_reset_service.issue_token(session, user)
        await password_reset_service.consume_token(
            session, token, new_password="a-new-password-98765!"
        )
        await session.commit()


async def _ban(person: Person, actor_id: UUID) -> None:
    from backend.services.abuse import bans as ban_service

    async with AsyncSessionLocal() as session:
        target = await session.get(User, person.id)
        actor = await session.get(User, actor_id)
        assert target is not None and actor is not None
        await ban_service.ban_user(session, target=target, actor=actor, reason="abuse")
        await session.commit()


async def _ban_row_only(person: Person, actor_id: UUID) -> None:
    """A ban as a row and nothing else: what the standing read must refuse on
    its own, for a credential no revocation reached."""
    from alkera_core.models.ban import UserBan

    async with AsyncSessionLocal() as session:
        session.add(UserBan(user_id=person.id, reason="abuse", created_by_id=actor_id))
        await session.commit()


async def _membership_revoked(person: Person, org_id: UUID) -> None:
    """The person's credentials into the org end while the membership stays
    (an org turning on strict SSO does this to every member)."""
    from alkera_core.auth.revocation import revoke_membership
    from alkera_core.models import OrgMembership

    async with AsyncSessionLocal() as session:
        membership = (
            await session.execute(
                select(OrgMembership).where(
                    OrgMembership.user_id == person.id, OrgMembership.org_team_id == org_id
                )
            )
        ).scalar_one()
        await revoke_membership(session, membership, reason="sso_enforced")
        await session.commit()


@pytest.mark.parametrize(
    "ends", ["logout-all", "password-reset", "banned", "ban-row", "membership-revoked"]
)
async def test_a_box_ends_with_its_persons_sessions(
    client: AsyncClient, org_admin: OrgWithAdmin, ends: str
) -> None:
    """A box's machine credential never expires, and anyone who held the
    person's session for a moment could have approved one. Every way the
    person's sessions end, ends the box too: its next request is refused and
    nothing is placed on it."""
    owner = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    if ends == "logout-all":
        await _logout_all(owner)
    elif ends == "password-reset":
        await _reset_password(owner)
    elif ends == "banned":
        await _ban(owner, org_admin.admin_id)
    elif ends == "ban-row":
        await _ban_row_only(owner, org_admin.admin_id)
    else:
        await _membership_revoked(owner, org_admin.org_id)

    beat = await client.post(f"/api/v1/machines/{box.machine_id}/heartbeat", headers=box.headers)
    assert beat.status_code == 401, beat.text
    async with AsyncSessionLocal() as session:
        credential = (
            await session.execute(
                select(MachineCredential.id).where(
                    MachineCredential.machine_id == UUID(box.machine_id)
                )
            )
        ).scalar_one()
        assert await live_machine_of(session, credential) is None
        assert (
            await personal_machine_for(
                session, org_team_id=org_admin.org_id, owner_user_id=owner.id
            )
            is None
        )


# =========================================================================== #
# the person lists and revokes their own boxes
# =========================================================================== #


async def _credential_of(box: Box) -> str:
    async with AsyncSessionLocal() as session:
        return str(
            (
                await session.execute(
                    select(MachineCredential.id).where(
                        MachineCredential.machine_id == UUID(box.machine_id)
                    )
                )
            ).scalar_one()
        )


async def _decisions_on(box_id: str) -> list[tuple[str, str]]:
    from alkera_core.models import EventOutbox

    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "personal_box",
                EventOutbox.entity_id == box_id,
            )
            .order_by(EventOutbox.id)
        )
        return [(r.payload["effect"], r.payload["reason"]) for r in rows.scalars().all()]


async def test_a_person_lists_and_revokes_their_own_box(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    credential_id = await _credential_of(box)
    browser = await owner.client()
    try:
        listed = await browser.get("/api/v1/me/boxes")
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert [(i["id"], i["machine_id"], i["revoked_at"]) for i in items] == [
            (credential_id, box.machine_id, None)
        ]
        revoked = await browser.delete(f"/api/v1/me/boxes/{credential_id}")
        assert revoked.status_code == 204, revoked.text
        assert await _decisions_on(credential_id) == [("allow", "box_owner")]
        after = (await browser.get("/api/v1/me/boxes")).json()["items"]
        assert after[0]["revoked_at"] is not None
    finally:
        await browser.aclose()
    beat = await client.post(f"/api/v1/machines/{box.machine_id}/heartbeat", headers=box.headers)
    assert beat.status_code == 401, beat.text


async def test_a_colleague_cannot_see_or_revoke_someone_elses_box(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _member(org_admin.org_id)
    colleague = await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    credential_id = await _credential_of(box)
    browser = await colleague.client()
    try:
        assert (await browser.get("/api/v1/me/boxes")).json()["items"] == []
        refused = await browser.delete(f"/api/v1/me/boxes/{credential_id}")
        assert refused.status_code == 404, refused.text
    finally:
        await browser.aclose()
    beat = await client.post(f"/api/v1/machines/{box.machine_id}/heartbeat", headers=box.headers)
    assert beat.status_code == 204, beat.text
    assert await _decisions_on(credential_id) == [("deny", "not_your_box")]
