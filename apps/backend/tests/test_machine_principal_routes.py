"""A box on its own machine credential, through the real routes.

The credential is the bearer; there is no user behind the request. The box
beats for its own machine, claims the machine its credential holds, and reads
and reports on the chats bound to it — in the org it is dedicated to, or in any
org for a pool box. Every other door, and every chat it does not hold, answers
exactly as it would a stranger: a 401 where a person is required, the opaque
404 elsewhere. Each case pins the status contract AND the ``authz.decision``
row the route left behind, chain and all.

A platform box is never an org's current workspace machine, so unlike the
operator-session path it reports on no chat but the ones bound to it.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import (
    GATEWAY_PARENT_MACHINE,
    GATEWAY_TOKEN_TTL_SECONDS,
    decode_gateway_token,
)
from alkera_core.compute.machines import WORKSPACE
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgComputeAssignment, User
from alkera_core.models.compute import PROVISIONED, PROVISIONING, READY, ComputeAllocation
from alkera_core.models.machine_credential import MachineCredential
from backend.api.routes.compute.machines import heartbeat_decision_sink
from backend.services.chats import chat_service
from backend.services.credentials import machine_credentials as machine_credential_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

NOT_FOUND = "Not found"


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


@pytest.fixture(autouse=True)
def _forget_beats() -> Iterator[None]:
    heartbeat_decision_sink().clear()
    yield
    heartbeat_decision_sink().clear()


class Box:
    """A platform box: its credential, the machine it holds, and the org it is
    dedicated to (``None`` for a pool box)."""

    def __init__(self, raw: str, credential_id: UUID, machine_id: UUID, org_id: UUID) -> None:
        self.raw = raw
        self.credential_id = credential_id
        self.machine_id = machine_id
        self.org_id = org_id

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.raw}"}


async def _org() -> tuple[UUID, User]:
    """A fresh org and a verified member of it (a chat owner)."""
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Served Org {secrets.token_hex(4)}",
            admin_email=f"served-admin-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="Served",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
        member, _pw = await make_member(session, org_id=org.id, verified=True)
        return org.id, member


async def _box(
    operator: OrgWithAdmin,
    *,
    tenancy: str,
    served_org: UUID | None = None,
    state: str = READY,
    origin: str | None = None,
    provider_pod_id: str = "",
) -> Box:
    """A credential minted in the operator org holding a workspace machine
    there, as ``/machines/claim`` leaves one; ``served_org`` names the org a
    dedicated box is assigned to."""
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session)
        credential, raw = await machine_credential_service.mint(
            session,
            org_id=operator.org_id,
            created_by=operator.admin_id,
            machine_type=machine_type,
            tenancy=tenancy,
            label="box",
        )
        machine = ComputeAllocation(
            user_id=operator.admin_id,
            org_team_id=operator.org_id,
            machine_type_id=machine_type.id,
            lifecycle=WORKSPACE,
            state=state,
            tenancy=tenancy,
            provider_machine_id=provider_pod_id,
            **({"origin": origin} if origin else {}),
        )
        session.add(machine)
        await session.flush()
        credential.machine_id = machine.id
        if tenancy == "dedicated" and served_org is not None:
            session.add(OrgComputeAssignment(org_team_id=served_org, machine_id=machine.id))
        await session.commit()
        return Box(raw, credential.id, machine.id, operator.org_id)


async def _chat(owner: User, *, machine_id: UUID | None) -> str:
    async with AsyncSessionLocal() as session:
        chat, _created = await chat_service.create_chat(
            session,
            owner=owner,
            org_id=owner.home_org_team_id,
            title="Quarterly plan",
            client_id=None,
            machine_id=str(machine_id) if machine_id else None,
            machine_status="ready",
        )
        await session.commit()
        return str(chat.id)


async def _decisions(org_id: UUID, entity: str) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == entity,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]


def _chain(row: EventOutbox) -> list[tuple[str, str]]:
    return [(link["kind"], link["id"]) for link in row.actor["chain"]]


def _error(resp: Any) -> dict[str, Any]:
    return dict(resp.json()["error"])


async def _row(machine_id: UUID) -> ComputeAllocation:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(ComputeAllocation).where(ComputeAllocation.id == machine_id)
            )
        ).scalar_one()


# =========================================================================== #
# the machine's own row: heartbeat and claim
# =========================================================================== #


async def test_a_box_beats_for_its_own_machine_and_both_allows_are_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _box(org_admin, tenancy="pool")
    resp = await client.post(
        f"/api/v1/machines/{box.machine_id}/heartbeat",
        json={"capacity": 4, "chats_served": 1, "daemon_version": "9"},
        headers=box.headers,
    )
    assert resp.status_code == 204, resp.text
    row = await _row(box.machine_id)
    assert row.last_heartbeat_at is not None
    assert (row.capacity, row.chats_served, row.daemon_version) == (4, 1, "9")

    credential_rows = await _decisions(org_admin.org_id, "machine_credential")
    assert _effects(credential_rows) == [("allow", "machine_self")]
    machine_rows = await _decisions(org_admin.org_id, "compute_machine")
    assert _effects(machine_rows) == [("allow", "machine_holds_machine")]
    for row_ in (*credential_rows, *machine_rows):
        assert _chain(row_) == [("machine", str(box.machine_id))]
        assert row_.actor["delegating_user"] is None
        assert row_.entity_id == str(box.machine_id)
    assert machine_rows[0].payload["attrs"] == {
        "in_org": True,
        "roles": [],
        "email_verified": False,
        "is_owner": False,
        "lifecycle": "workspace",
        "controls": False,
    }


async def test_a_box_cannot_beat_for_another_machine(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _box(org_admin, tenancy="pool")
    other = await _box(org_admin, tenancy="pool")
    resp = await client.post(f"/api/v1/machines/{other.machine_id}/heartbeat", headers=box.headers)
    assert resp.status_code == 404, resp.text
    assert _error(resp)["message"] == "Machine not found"
    assert (await _row(other.machine_id)).last_heartbeat_at is None
    rows = await _decisions(org_admin.org_id, "machine_credential")
    assert _effects(rows) == [("deny", "not_this_machine")]
    assert _chain(rows[0]) == [("machine", str(box.machine_id))]


async def test_a_box_cannot_beat_for_a_machine_that_is_not_a_uuid_or_does_not_exist(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _box(org_admin, tenancy="pool")
    for path in ("/api/v1/machines/not-a-uuid/heartbeat", f"/api/v1/machines/{uuid4()}/heartbeat"):
        resp = await client.post(path, headers=box.headers)
        assert resp.status_code == 404, (path, resp.text)


async def test_a_provisioned_box_claims_its_machine_on_the_credential_alone(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The row provisioning wrote is in ``provisioning`` with the credential
    pre-bound to it and the provider's id for the machine already recorded;
    the first claim on the credential — no box user, no login — is what makes
    it ready."""
    pod = f"i-{uuid4().hex[:12]}"
    box = await _box(
        org_admin, tenancy="pool", state=PROVISIONING, origin=PROVISIONED, provider_pod_id=pod
    )
    before = await _row(box.machine_id)
    resp = await client.post(
        "/api/v1/machines/claim",
        json={
            "provider_pod_id": pod,
            "name": "box",
            "capacity": 8,
            "daemon_version": "1",
            "daemon_instance_id": "proc-1",
        },
        headers=box.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(box.machine_id)
    after = await _row(box.machine_id)
    assert after.state == READY
    assert after.daemon_instance_id == "proc-1"
    # Who stood the box up is unchanged: the claim on the credential names nobody.
    assert after.user_id == before.user_id
    rows = await _decisions(org_admin.org_id, "machine_credential")
    assert _effects(rows) == [("allow", "machine_self")]
    assert _chain(rows[0]) == [("machine", str(box.machine_id))]
    assert rows[0].payload["attrs"] == {
        "is_machine": True,
        "credential_live": True,
        "machine_matches": True,
        "own_standing": True,
        "runs_org_workers": False,
        "org_reached": True,
    }


async def test_a_claim_for_a_pod_another_credential_holds_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A box that learned another box's pod id does not become it: the
    credential holds ONE machine, and a claim naming a pod registered under a
    different one is refused as if the pod did not exist."""
    pod = f"i-{uuid4().hex[:12]}"
    holder = await _box(
        org_admin, tenancy="pool", state=PROVISIONING, origin=PROVISIONED, provider_pod_id=pod
    )
    first = await client.post(
        "/api/v1/machines/claim",
        json={"provider_pod_id": pod, "name": "box", "capacity": 8, "daemon_version": "1"},
        headers=holder.headers,
    )
    assert first.status_code == 200, first.text
    intruder = await _box(org_admin, tenancy="pool", state=PROVISIONING, origin=PROVISIONED)
    resp = await client.post(
        "/api/v1/machines/claim",
        json={"provider_pod_id": pod, "name": "box", "capacity": 8, "daemon_version": "1"},
        headers=intruder.headers,
    )
    assert resp.status_code == 404, resp.text
    denies = [
        r
        for r in await _decisions(org_admin.org_id, "machine_credential")
        if r.payload["effect"] == "deny"
    ]
    assert _effects(denies) == [("deny", "not_this_machine")]
    assert _chain(denies[0]) == [("machine", str(intruder.machine_id))]


# =========================================================================== #
# the chats the box holds — and the ones it does not
# =========================================================================== #


async def test_a_dedicated_box_reads_the_chat_bound_to_it_in_the_org_it_serves(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(owner, machine_id=box.machine_id)

    resp = await client.get(f"/api/v1/chats/{chat_id}", headers=box.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == chat_id
    assert body["machine_id"] == str(box.machine_id)
    # Nothing a person could do is offered to the box.
    assert (body["can_send"], body["can_delete"]) == (False, False)

    messages = await client.get(f"/api/v1/chats/{chat_id}/messages", headers=box.headers)
    assert messages.status_code == 200, messages.text

    rows = await _decisions(served, "chat")
    assert _effects(rows) == [("allow", "machine_holds_chat")] * 2
    for row in rows:
        assert row.entity_id == chat_id
        assert _chain(row) == [("machine", str(box.machine_id))]
        assert row.actor["delegating_user"] is None
        assert row.payload["attrs"]["bound_machine_id"] == str(box.machine_id)
        assert row.payload["attrs"]["in_org"] is False
        assert row.payload["attrs"]["roles"] == []


async def test_a_pool_box_reads_a_bound_chat_in_any_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    anywhere, owner = await _org()
    box = await _box(org_admin, tenancy="pool")
    chat_id = await _chat(owner, machine_id=box.machine_id)
    resp = await client.get(f"/api/v1/chats/{chat_id}", headers=box.headers)
    assert resp.status_code == 200, resp.text
    assert _effects(await _decisions(anywhere, "chat")) == [("allow", "machine_holds_chat")]


@pytest.mark.parametrize("door", ["read", "messages", "publisher-state", "gateway-token"])
async def test_a_chat_the_box_does_not_hold_is_the_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, door: str
) -> None:
    """An unbound chat of the served org and a chat bound to another box are
    both a stranger's 404, and the refusal is on record naming why."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    other = await _box(org_admin, tenancy="pool")
    unbound = await _chat(owner, machine_id=None)
    elsewhere = await _chat(owner, machine_id=other.machine_id)
    for chat_id in (unbound, elsewhere):
        if door == "publisher-state":
            resp = await client.put(
                f"/api/v1/chats/{chat_id}/publisher-state",
                json={"state": "refused", "reason": "gateway"},
                headers=box.headers,
            )
        elif door == "gateway-token":
            resp = await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=box.headers)
        else:
            path = f"/api/v1/chats/{chat_id}" + ("/messages" if door == "messages" else "")
            resp = await client.get(path, headers=box.headers)
        assert resp.status_code == 404, (chat_id, resp.text)
        assert _error(resp)["message"] == NOT_FOUND
    rows = await _decisions(served, "chat")
    assert _effects(rows) == [("deny", "machine_does_not_hold_chat")] * 2
    assert {r.entity_id for r in rows} == {unbound, elsewhere}
    # The unbound chat is untouched: no publisher state was written.
    async with AsyncSessionLocal() as session:
        from alkera_core.models import WorkspaceObject

        chat = await session.get(WorkspaceObject, UUID(unbound))
        assert chat is not None
        assert chat_service.chat_spec_of(chat).publisher_refusal in (None, "")


async def test_a_dedicated_box_never_reaches_a_chat_in_an_org_it_does_not_serve(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Even a chat bound to it (a binding that placement would never write)
    is out of reach in an org the box is not assigned to: the engine's
    tenancy floor refuses before any policy runs."""
    served, _owner = await _org()
    elsewhere, stranger = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(stranger, machine_id=box.machine_id)
    resp = await client.get(f"/api/v1/chats/{chat_id}", headers=box.headers)
    assert resp.status_code == 404, resp.text
    rows = await _decisions(elsewhere, "chat")
    assert _effects(rows) == [("deny", "cross_org")]


async def test_a_box_reports_its_chats_publisher_state_and_the_allow_is_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(owner, machine_id=box.machine_id)
    resp = await client.put(
        f"/api/v1/chats/{chat_id}/publisher-state",
        json={"state": "refused", "reason": "gateway refused the chat"},
        headers=box.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["machine_status"] == "refused"
    assert resp.json()["machine_refusal_reason"] == "gateway refused the chat"
    rows = await _decisions(served, "chat")
    assert _effects(rows) == [("allow", "machine_holds_chat")]
    assert rows[0].payload["action"] == "write"
    assert rows[0].payload["attrs"]["publisher_machine_ids"] == [str(box.machine_id)]
    assert _chain(rows[0]) == [("machine", str(box.machine_id))]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", "/api/v1/auth/me", None, id="auth-me"),
        pytest.param("GET", "/api/v1/machines/current", None, id="machine-banner"),
        pytest.param("POST", "/api/v1/chats", {"title": "x"}, id="chat-create"),
        pytest.param("DELETE", "/api/v1/chats/{chat}", None, id="chat-delete"),
        pytest.param("POST", "/api/v1/chats/{chat}/messages", {"content": "hi"}, id="chat-send"),
        pytest.param("GET", "/api/v1/teams", None, id="teams"),
    ],
)
async def test_every_door_that_wants_a_person_refuses_the_box(
    client: AsyncClient, org_admin: OrgWithAdmin, method: str, path: str, body: Any
) -> None:
    """The credential is nobody's session: the routes that bind a user answer
    401 — even on the box's own bound chat — and nothing is written."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(owner, machine_id=box.machine_id)
    resp = await client.request(method, path.format(chat=chat_id), json=body, headers=box.headers)
    assert resp.status_code == 401, (path, resp.text)


# =========================================================================== #
# the box's listing: the chats bound to it, in every org it serves
# =========================================================================== #


async def _spare_chat(owner: User, *, machine_id: UUID) -> str:
    """A chat warmed ahead of its owner's first message, placed on the box."""
    async with AsyncSessionLocal() as session:
        chat, _created = await chat_service.create_chat(
            session,
            owner=owner,
            org_id=owner.home_org_team_id,
            title=None,
            client_id=None,
            machine_id=str(machine_id),
            machine_status="ready",
            spare=True,
        )
        await session.commit()
        return str(chat.id)


async def _listed(client: AsyncClient, box: Box, **params: Any) -> dict[str, Any]:
    resp = await client.get("/api/v1/chats", params=params, headers=box.headers)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _beat(client: AsyncClient, box: Box) -> None:
    """The box answers once, so the chats it holds read ``ready`` — which
    proves each row's status came from the chat's own org's live machines."""
    resp = await client.post(f"/api/v1/machines/{box.machine_id}/heartbeat", headers=box.headers)
    assert resp.status_code == 204, resp.text


async def test_a_dedicated_box_lists_exactly_the_chats_bound_to_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The listing is how the box discovers the turns it owes, so it holds
    every chat bound to the box and nothing else: not a spare made ahead of a
    member's first message (no agent starts for it until that send claims it),
    not the served org's unbound chats, not a chat another box holds. A listing decides without a
    decision row, as a person's does."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    other = await _box(org_admin, tenancy="pool")
    # The box comes up before the chats exist: its readiness is what places the
    # org's stranded chats on it, and this test is about the listing, not
    # placement — the unbound chat below must stay unbound.
    await _beat(client, box)
    mine = await _chat(owner, machine_id=box.machine_id)
    spare = await _spare_chat(owner, machine_id=box.machine_id)
    await _chat(owner, machine_id=None)
    await _chat(owner, machine_id=other.machine_id)

    body = await _listed(client, box)
    assert {c["id"] for c in body["items"]} == {mine}
    assert spare not in {c["id"] for c in body["items"]}
    assert body["next_cursor"] is None
    for row in body["items"]:
        assert row["machine_id"] == str(box.machine_id)
        assert row["machine_status"] == "ready"
        # Nothing a person could do is offered to the box.
        assert (row["can_send"], row["can_delete"]) == (False, False)
    assert await _decisions(served, "chat") == []


async def test_neither_a_person_nor_the_box_lists_an_unclaimed_spare(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The org admin's own spare, on the box that serves their org, is absent
    from their listing and from the box's: nobody has written in it, so the box
    must not start an agent for it."""
    box = await _box(org_admin, tenancy="dedicated", served_org=org_admin.org_id)
    async with AsyncSessionLocal() as session:
        admin = await session.get(User, org_admin.admin_id)
        assert admin is not None
    spare = await _spare_chat(admin, machine_id=box.machine_id)
    plain = await _chat(admin, machine_id=box.machine_id)

    assert {c["id"] for c in (await _listed(client, box))["items"]} == {plain}
    assert spare != plain
    person = app_client()
    try:
        await login(person, org_admin.admin_email, org_admin.admin_password)
        theirs = await person.get("/api/v1/chats")
    finally:
        await person.aclose()
    assert theirs.status_code == 200, theirs.text
    assert {c["id"] for c in theirs.json()["items"]} == {plain}


async def test_a_pool_box_lists_its_bound_chats_across_orgs(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A pool box serves every org, so its listing spans them — and each row
    carries its own org's machine status and sandbox limits, not the operator
    org's."""
    org_a, a = await _org()
    org_b, b = await _org()
    box = await _box(org_admin, tenancy="pool")
    await _beat(client, box)
    in_a = await _chat(a, machine_id=box.machine_id)
    in_b = await _chat(b, machine_id=box.machine_id)
    await _chat(a, machine_id=None)

    body = await _listed(client, box)
    assert {c["id"] for c in body["items"]} == {in_a, in_b}
    assert {c["machine_status"] for c in body["items"]} == {"ready"}
    assert await _decisions(org_a, "chat") == [] and await _decisions(org_b, "chat") == []


async def test_a_pool_box_is_told_each_chats_own_org_on_the_listing_and_the_single_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A pool box keys every per-tenant store on the chat's org, and the row
    is the only place it may learn it: each chat, listed or read alone, names
    the org it was created in, never the operator org that minted the box's
    credential and never the other org on the same page."""
    org_a, a = await _org()
    org_b, b = await _org()
    box = await _box(org_admin, tenancy="pool")
    await _beat(client, box)
    in_a = await _chat(a, machine_id=box.machine_id)
    in_b = await _chat(b, machine_id=box.machine_id)
    expected = {in_a: str(org_a), in_b: str(org_b)}

    listed = {c["id"]: c["org_id"] for c in (await _listed(client, box))["items"]}
    assert listed == expected
    assert str(org_admin.org_id) not in listed.values()

    for chat_id, org_id in expected.items():
        read = await client.get(f"/api/v1/chats/{chat_id}", headers=box.headers)
        assert read.status_code == 200, read.text
        assert read.json()["org_id"] == org_id


async def test_a_dedicated_box_is_told_the_org_it_serves_not_the_one_that_minted_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A dedicated box's credential is minted in the operator org and assigned
    to the org it serves; the chats it is handed name the served org."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(owner, machine_id=box.machine_id)
    [row] = (await _listed(client, box))["items"]
    assert (row["id"], row["org_id"]) == (chat_id, str(served))
    read = await client.get(f"/api/v1/chats/{chat_id}", headers=box.headers)
    assert read.json()["org_id"] == str(served)


async def test_a_dedicated_box_does_not_list_a_chat_bound_to_it_outside_the_org_it_serves(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A binding placement would never write is still out of reach: the
    tenancy floor cuts the row from the listing exactly as it refuses the
    single read."""
    served, owner = await _org()
    _elsewhere, stranger = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    here = await _chat(owner, machine_id=box.machine_id)
    await _chat(stranger, machine_id=box.machine_id)
    assert {c["id"] for c in (await _listed(client, box))["items"]} == {here}


async def test_the_boxs_listing_pages_and_its_cursor_is_its_own(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Keyset paging over the box's chats, newest first; a cursor minted for
    one box's listing is refused on another box's and on a person's, and a
    person's cursor is refused on the box's — a position is never a way into
    somebody else's listing."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    other = await _box(org_admin, tenancy="pool")
    ids = [await _chat(owner, machine_id=box.machine_id) for _ in range(3)]

    first = await _listed(client, box, limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = await _listed(client, box, limit=2, cursor=first["next_cursor"])
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert [c["id"] for c in first["items"] + second["items"]] == list(reversed(ids))

    refused = await client.get(
        "/api/v1/chats", params={"limit": 2, "cursor": first["next_cursor"]}, headers=other.headers
    )
    assert refused.status_code == 422, refused.text

    async with AsyncSessionLocal() as session:
        admin = await session.get(User, org_admin.admin_id)
        assert admin is not None
    for _ in range(2):
        await _chat(admin, machine_id=None)
    person = app_client()
    try:
        await login(person, org_admin.admin_email, org_admin.admin_password)
        theirs = await person.get("/api/v1/chats", params={"limit": 1})
        assert theirs.status_code == 200, theirs.text
        persons_cursor = theirs.json()["next_cursor"]
        assert persons_cursor
        crossed = await person.get(
            "/api/v1/chats", params={"limit": 1, "cursor": first["next_cursor"]}
        )
    finally:
        await person.aclose()
    assert crossed.status_code == 422, crossed.text
    on_box = await client.get(
        "/api/v1/chats", params={"limit": 1, "cursor": persons_cursor}, headers=box.headers
    )
    assert on_box.status_code == 422, on_box.text


# =========================================================================== #
# the chat's gateway credential, minted under the box's own credential
# =========================================================================== #


async def test_a_box_mints_its_bound_chats_gateway_token_under_its_credential_for_the_owner(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The box has no session and no person: the chat's gateway token is
    minted under the box's machine credential, for the chat's OWNER — billed
    to the person whose chat it is, in their org, never to the box's operator
    — and it is a session nowhere on this API. The allow is on record as the
    publisher's WRITE, under the served org, in the machine's own chain."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(owner, machine_id=box.machine_id)

    before = int(time.time())
    minted = await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=box.headers)
    assert minted.status_code == 200, minted.text
    body = minted.json()
    claims = decode_gateway_token(body["token"])
    assert claims.parent_kind == GATEWAY_PARENT_MACHINE
    assert claims.parent_jti == box.credential_id.hex
    assert claims.chat_id == UUID(chat_id)
    assert (claims.user_id, claims.org_id) == (owner.id, served)
    assert claims.org_id != org_admin.org_id, "the operator's org bills nothing"
    assert before <= claims.issued_at
    assert 0 < claims.expires_at - claims.issued_at <= GATEWAY_TOKEN_TTL_SECONDS
    assert datetime.fromisoformat(body["expires_at"]).timestamp() == claims.expires_at

    bearer = {"Authorization": f"Bearer {body['token']}"}
    assert (await client.get("/api/v1/auth/me", headers=bearer)).status_code == 401
    assert (await client.get(f"/api/v1/chats/{chat_id}", headers=bearer)).status_code == 401
    again = await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=bearer)
    assert again.status_code == 401, "it cannot mint its own successor"

    rows = await _decisions(served, "chat")
    assert _effects(rows) == [("allow", "machine_holds_chat")]
    assert rows[0].payload["action"] == "write"
    assert rows[0].entity_id == chat_id
    assert _chain(rows[0]) == [("machine", str(box.machine_id))]
    assert rows[0].actor["delegating_user"] is None


async def test_a_pool_box_mints_the_gateway_token_of_a_bound_chat_in_any_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    anywhere, owner = await _org()
    box = await _box(org_admin, tenancy="pool")
    chat_id = await _chat(owner, machine_id=box.machine_id)
    minted = await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=box.headers)
    assert minted.status_code == 200, minted.text
    claims = decode_gateway_token(minted.json()["token"])
    assert (claims.user_id, claims.org_id, claims.parent_jti) == (
        owner.id,
        anywhere,
        box.credential_id.hex,
    )


async def _deactivate(user_id: UUID, org_id: UUID) -> None:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.is_active = False
        await session.commit()


async def _remove_from_org(user_id: UUID, org_id: UUID) -> None:
    from alkera_core.auth.revocation import revoke_membership
    from alkera_core.models import MembershipStatus, OrgMembership

    async with AsyncSessionLocal() as session:
        membership = await session.scalar(
            select(OrgMembership).where(
                OrgMembership.user_id == user_id, OrgMembership.org_team_id == org_id
            )
        )
        assert membership is not None
        membership.status = MembershipStatus.DEACTIVATED
        await revoke_membership(session, membership, reason="removed")
        await session.commit()


@pytest.mark.parametrize(
    "leave",
    [
        pytest.param(_deactivate, id="owner-deactivated"),
        pytest.param(_remove_from_org, id="owner-removed-from-the-org"),
    ],
)
async def test_a_box_mints_nothing_that_bills_a_payer_who_lost_the_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, leave: Any
) -> None:
    """With no person at the box the chat's payer is billed, so a payer who can
    no longer read the chat is refused by name rather than charged for turns
    they cannot open, stop or delete. The same chat minted while they stood."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(owner, machine_id=box.machine_id)
    first = await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=box.headers)
    assert first.status_code == 200, first.text

    await leave(owner.id, served)
    refused = await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=box.headers)

    assert refused.status_code == 403, refused.text
    assert _error(refused)["code"] == "payer_lost_access"


async def test_a_revoked_credential_mints_no_gateway_token(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The door refuses the credential itself, with the one opaque answer a
    credential that is gone always gets, and nothing is on record for a
    caller who never resolved."""
    served, owner = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    chat_id = await _chat(owner, machine_id=box.machine_id)
    async with AsyncSessionLocal() as session:
        await machine_credential_service.revoke(session, box.credential_id)
        await session.commit()
    resp = await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=box.headers)
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == "machine_credential_refused"
    assert await _decisions(served, "chat") == []


async def _last_used(box: Box) -> datetime | None:
    async with AsyncSessionLocal() as session:
        credential = await session.get(MachineCredential, box.credential_id)
        assert credential is not None
        return credential.last_used_at


async def test_a_busy_box_writes_its_credentials_last_use_at_most_once_a_minute(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A box presents its credential on every call. A write per call queued
    its own heartbeats behind the row lock under load until they timed out
    and the box read ``unreachable``; the stamp is kept to the minute."""
    from freezegun import freeze_time

    box = await _box(org_admin, tenancy="dedicated", served_org=org_admin.org_id)
    with freeze_time("2026-10-05 09:00:00", real_asyncio=True) as frozen:
        await _listed(client, box)
        first = await _last_used(box)
        assert first is not None
        frozen.tick(timedelta(seconds=30))
        await _listed(client, box)
        assert await _last_used(box) == first, "a call inside the minute writes nothing"
        frozen.tick(timedelta(seconds=31))
        await _listed(client, box)
        moved = await _last_used(box)
        assert moved is not None and moved > first


async def test_a_box_reads_the_chat_owner_s_name_on_the_listing_and_the_single_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The box names a chat's agent "Alkera agent for <name>" in the work it
    shows live, so both reads it opens chats from carry the owner's name: their
    first and last name, else their email."""
    served, _ = await _org()
    async with AsyncSessionLocal() as session:
        named, _pw = await make_member(
            session, org_id=served, first_name="Ada", last_name="King", verified=True
        )
        bare, _pw = await make_member(
            session,
            org_id=served,
            email=f"no-name-{secrets.token_hex(4)}@alkera.dev",
            first_name="",
            last_name="",
            verified=True,
        )
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    named_chat = await _chat(named, machine_id=box.machine_id)
    bare_chat = await _chat(bare, machine_id=box.machine_id)
    expected = {named_chat: "Ada King", bare_chat: bare.email}

    listed = await _listed(client, box)
    names = {item["id"]: item["owner_display_name"] for item in listed["items"]}
    assert names == expected
    for chat_id, name in expected.items():
        resp = await client.get(f"/api/v1/chats/{chat_id}", headers=box.headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_display_name"] == name
