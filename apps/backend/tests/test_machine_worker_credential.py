"""The org-bound worker credential, through the real routes.

A box that serves several orgs keeps its machine credential in its root
process and gives each org's process a short-lived credential bound to
``(machine, org)``. That credential must reach its org and nothing else: every
chat door answers a chat of another org bound to the very same machine as a
stranger's 404, it cannot mint another credential or act on the machine's own
standing, and it dies with the machine credential that minted it. The machine
credential, meanwhile, reads the routing feed: ids and states only.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import decode_socket_ticket, mint_machine_worker_token
from alkera_core.auth.machine_token import MACHINE_WORKER_TOKEN_PREFIX
from alkera_core.auth.tokens import WsMachineTicket
from alkera_core.authz import ActingContext, agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import BOUND_MACHINE_KEY, VISIBILITY_ORG, HubEvent
from alkera_core.machine_refusals import (
    MACHINE_CREDENTIAL_REFUSED,
    MACHINE_WORKER_CREDENTIAL_EXPIRED,
)
from alkera_core.models.machine_credential import MachineCredential
from backend.auth.dependencies import machine_context_if_standing
from backend.services.realtime.filters import (
    CHAT_CHANNEL_PREFIX,
    MachineScope,
    load_machine_scope,
    machine_routing_visible_to,
    machine_visible_to,
)
from httpx import AsyncClient
from sqlalchemy import update
from tests.conftest import OrgWithAdmin, app_client, login
from tests.test_machine_principal_routes import (
    NOT_FOUND,
    Box,
    _beat,
    _box,
    _chat,
    _decisions,
    _effects,
    _error,
    _org,
    _spare_chat,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MINT = "/api/v1/machines/me/worker-credentials"
ROUTING = "/api/v1/machines/me/routing"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _worker(client: AsyncClient, box: Box, org_id: UUID) -> str:
    resp = await client.post(MINT, json={"org_id": str(org_id)}, headers=box.headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["org_id"] == str(org_id)
    assert body["token"].startswith(MACHINE_WORKER_TOKEN_PREFIX)
    assert 0 < body["expires_in"] <= 3600
    token: str = body["token"]
    return token


async def _two_orgs_on_a_pool_box(
    client: AsyncClient, operator: OrgWithAdmin
) -> tuple[Box, UUID, str, UUID, str]:
    org_a, a = await _org()
    org_b, b = await _org()
    box = await _box(operator, tenancy="pool")
    await _beat(client, box)
    in_a = await _chat(a, machine_id=box.machine_id)
    in_b = await _chat(b, machine_id=box.machine_id)
    return box, org_a, in_a, org_b, in_b


async def _door(client: AsyncClient, door: str, chat_id: str, headers: dict[str, str]) -> Any:
    if door == "publisher-state":
        return await client.put(
            f"/api/v1/chats/{chat_id}/publisher-state",
            json={"state": "refused", "reason": "gateway"},
            headers=headers,
        )
    if door == "gateway-token":
        return await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=headers)
    path = f"/api/v1/chats/{chat_id}" + ("/messages" if door == "messages" else "")
    return await client.get(path, headers=headers)


DOORS = ["read", "messages", "publisher-state", "gateway-token"]


# =========================================================================== #
# the worker reaches its org and no other
# =========================================================================== #


@pytest.mark.parametrize("door", DOORS)
async def test_a_worker_reaches_its_orgs_chat_and_not_the_other_orgs_on_the_same_box(
    client: AsyncClient, org_admin: OrgWithAdmin, door: str
) -> None:
    """Both chats are bound to the one pool machine, which as itself reaches
    both. The worker credential for org A reaches A's chat and gets a
    stranger's 404 for B's, refused by the tenancy floor and on record in B's
    lane."""
    box, org_a, in_a, org_b, in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    worker = _bearer(await _worker(client, box, org_a))

    assert (await _door(client, door, in_b, box.headers)).status_code < 300, "the machine reaches B"
    mine = await _door(client, door, in_a, worker)
    assert mine.status_code < 300, mine.text
    theirs = await _door(client, door, in_b, worker)
    assert theirs.status_code == 404, theirs.text
    assert _error(theirs)["message"] == NOT_FOUND
    refusals = [r for r in await _decisions(org_b, "chat") if r.payload["effect"] == "deny"]
    assert _effects(refusals) == [("deny", "cross_org")]
    assert refusals[0].entity_id == in_b


async def test_a_workers_listing_holds_its_orgs_chats_only(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box, org_a, in_a, org_b, in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    resp = await client.get("/api/v1/chats", headers=_bearer(await _worker(client, box, org_a)))
    assert resp.status_code == 200, resp.text
    assert {(c["id"], c["org_id"]) for c in resp.json()["items"]} == {(in_a, str(org_a))}
    other = await client.get("/api/v1/chats", headers=_bearer(await _worker(client, box, org_b)))
    assert {(c["id"], c["org_id"]) for c in other.json()["items"]} == {(in_b, str(org_b))}


async def test_a_worker_cannot_assert_a_chat_of_another_org_bound_to_its_machine(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The agent assertion on a worker credential may name only its machine or
    a chat bound to it in its own org."""
    box, org_a, in_a, _org_b, in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    worker = _bearer(await _worker(client, box, org_a))
    ok = await client.get(f"/api/v1/chats/{in_a}", headers={**worker, **agent_headers(in_a)})
    assert ok.status_code == 200, ok.text
    refused = await client.get(f"/api/v1/chats/{in_a}", headers={**worker, **agent_headers(in_b)})
    assert refused.status_code == 400, refused.text
    assert _error(refused)["code"] == "agent_actor_requires_bound_chat"


# =========================================================================== #
# what only the machine credential may do
# =========================================================================== #


async def test_a_worker_cannot_mint_a_worker_credential_for_any_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box, org_a, _in_a, org_b, _in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    worker = _bearer(await _worker(client, box, org_a))
    for org in (org_a, org_b):
        resp = await client.post(MINT, json={"org_id": str(org)}, headers=worker)
        assert resp.status_code == 404, resp.text
    reasons = {r.payload["reason"] for r in await _decisions(org_a, "machine_credential")}
    assert "org_bound_credential" in reasons
    assert _effects(await _decisions(org_b, "machine_credential")) == [("deny", "cross_org")]


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(("GET", ROUTING, None), id="routing-feed"),
        pytest.param(("POST", "/api/v1/machines/{machine}/heartbeat", None), id="heartbeat"),
        pytest.param(
            ("POST", "/api/v1/machines/claim", {"provider_pod_id": "pod-x", "name": "x"}),
            id="claim",
        ),
    ],
)
async def test_a_worker_cannot_act_on_the_machines_own_standing(
    client: AsyncClient, org_admin: OrgWithAdmin, door: tuple[str, str, Any]
) -> None:
    box, org_a, _in_a, _org_b, _in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    worker = _bearer(await _worker(client, box, org_a))
    method, path, body = door
    resp = await client.request(
        method, path.format(machine=box.machine_id), json=body, headers=worker
    )
    assert resp.status_code == 404, resp.text


async def test_a_worker_saying_it_publishes_its_orgs_chat_wakes_the_chat(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The org's process reports a chat it opened exactly as the box does (the
    body ``CloudRest.report_publisher_state`` sends), on its worker
    credential: the report lands and the chat reads ``awake`` on the ready
    box, where before it read ``asleep``. The machine's own standing stays the
    supervisor's (the beat above is refused to the worker), so this door is
    how the org's process says a chat is served."""
    box, org_a, in_a, _org_b, _in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    worker = _bearer(await _worker(client, box, org_a))
    before = await client.get(f"/api/v1/chats/{in_a}", headers=worker)
    assert before.status_code == 200, before.text
    assert (before.json()["machine_status"], before.json()["session_state"]) == ("ready", "asleep")

    said = await client.put(
        f"/api/v1/chats/{in_a}/publisher-state",
        json={"state": "publishing", "reason": ""},
        headers=worker,
    )
    assert said.status_code == 200, said.text
    assert said.json()["session_state"] == "awake"
    after = await client.get(f"/api/v1/chats/{in_a}", headers=worker)
    assert (after.json()["machine_status"], after.json()["session_state"]) == ("ready", "awake")


async def test_a_dedicated_box_mints_for_the_org_it_serves_and_no_other(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    served, owner = await _org()
    elsewhere, _stranger = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    await _chat(owner, machine_id=box.machine_id)
    await _worker(client, box, served)
    refused = await client.post(MINT, json={"org_id": str(elsewhere)}, headers=box.headers)
    assert refused.status_code == 404, refused.text
    assert _effects(await _decisions(elsewhere, "machine_credential")) == [("deny", "cross_org")]


async def test_a_person_cannot_mint_a_worker_credential(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    person = app_client()
    try:
        await login(person, org_admin.admin_email, org_admin.admin_password)
        resp = await person.post(MINT, json={"org_id": str(org_admin.org_id)})
    finally:
        await person.aclose()
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == "machine_credential_required"


# =========================================================================== #
# the credential's life
# =========================================================================== #


async def test_revoking_the_machine_credential_ends_every_worker_it_minted(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box, org_a, in_a, _org_b, _in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    worker = _bearer(await _worker(client, box, org_a))
    assert (await client.get(f"/api/v1/chats/{in_a}", headers=worker)).status_code == 200
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(MachineCredential)
            .where(MachineCredential.id == box.credential_id)
            .values(revoked_at=MachineCredential.created_at)
        )
        await session.commit()
    resp = await client.get(f"/api/v1/chats/{in_a}", headers=worker)
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == MACHINE_CREDENTIAL_REFUSED


async def test_an_expired_worker_credential_is_told_to_refresh_not_to_stop(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box, org_a, in_a, _org_b, _in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    stale, _claims = mint_machine_worker_token(
        credential_id=box.credential_id,
        machine_id=box.machine_id,
        org_id=org_a,
        ttl_seconds=60,
        now=int(time.time()) - 600,
    )
    resp = await client.get(f"/api/v1/chats/{in_a}", headers=_bearer(stale))
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == MACHINE_WORKER_CREDENTIAL_EXPIRED


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param("org-not-served", id="signed-for-an-org-the-box-does-not-serve"),
        pytest.param("other-machine", id="signed-for-a-machine-the-credential-does-not-hold"),
        pytest.param("tampered", id="signature-tampered"),
        pytest.param("no-marker", id="the-signed-token-without-its-marker"),
    ],
)
async def test_a_worker_credential_the_box_could_not_have_minted_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, forge: str
) -> None:
    """The signature is not enough: the machine credential behind it must
    still hold the machine it names and still serve the org it names."""
    served, owner = await _org()
    elsewhere, stranger = await _org()
    box = await _box(org_admin, tenancy="dedicated", served_org=served)
    await _chat(owner, machine_id=box.machine_id)
    theirs = await _chat(stranger, machine_id=box.machine_id)
    org = elsewhere if forge == "org-not-served" else served
    machine = uuid4() if forge == "other-machine" else box.machine_id
    token, _claims = mint_machine_worker_token(
        credential_id=box.credential_id, machine_id=machine, org_id=org
    )
    if forge == "tampered":
        token = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    if forge == "no-marker":
        token = token.removeprefix(MACHINE_WORKER_TOKEN_PREFIX)
    resp = await client.get(f"/api/v1/chats/{theirs}", headers=_bearer(token))
    assert resp.status_code == 401, resp.text


# =========================================================================== #
# the worker's socket and stream stay bound to its org
# =========================================================================== #


async def test_a_workers_socket_ticket_rebuilds_bound_to_its_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The socket a worker opens is rebuilt from its ticket on the handshake
    and on every tick. It must come back as the worker for its one org, never
    as the machine, whose reach spans every org on the box."""
    box, org_a, in_a, org_b, in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    minted = await client.post(
        "/api/v1/ws/tickets", headers=_bearer(await _worker(client, box, org_a))
    )
    assert minted.status_code == 200, minted.text
    ticket = decode_socket_ticket(minted.json()["ticket"])
    assert isinstance(ticket, WsMachineTicket)
    assert (ticket.org_id, ticket.org_bound) == (org_a, True)
    async with AsyncSessionLocal() as session:
        ctx = await machine_context_if_standing(
            session,
            credential_id=ticket.credential_id,
            machine_id=ticket.machine_id,
            org_id=ticket.org_id,
            org_bound=ticket.org_bound,
        )
        assert ctx is not None and ctx.is_machine_worker
        assert ctx.serves(org_a) and not ctx.serves(org_b)
        scope = await load_machine_scope(session, ctx)
    assert scope.chat_ids == {in_a} and scope.org_ids == {org_a}
    assert in_b not in scope.chat_ids

    machines = await client.post("/api/v1/ws/tickets", headers=box.headers)
    own = decode_socket_ticket(machines.json()["ticket"])
    assert isinstance(own, WsMachineTicket) and own.org_bound is False


# =========================================================================== #
# the routing feed: ids and states, for the machine credential alone
# =========================================================================== #


async def test_the_routing_feed_names_each_bound_chat_its_org_and_nothing_it_says(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box, org_a, in_a, org_b, in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    _a2, owner = await _org()
    await _spare_chat(owner, machine_id=box.machine_id)
    await _chat(owner, machine_id=None)

    resp = await client.get(ROUTING, headers=box.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_cursor"] is None
    assert {(i["chat_id"], i["org_id"]) for i in body["items"]} == {
        (in_a, str(org_a)),
        (in_b, str(org_b)),
    }
    for item in body["items"]:
        # Ids and states only: nothing a chat says rides the feed.
        assert set(item) == {
            "chat_id",
            "org_id",
            "workspace_id",
            "state",
            "pending_turn",
            "wake_requested",
            "end_seq",
        }
        assert item["wake_requested"] is False and item["end_seq"] == 0


async def test_the_routing_feed_pages(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    box, _org_a, in_a, _org_b, in_b = await _two_orgs_on_a_pool_box(client, org_admin)
    first = await client.get(ROUTING, params={"limit": 1}, headers=box.headers)
    assert len(first.json()["items"]) == 1 and first.json()["next_cursor"]
    second = await client.get(
        ROUTING, params={"limit": 1, "cursor": first.json()["next_cursor"]}, headers=box.headers
    )
    assert second.json()["next_cursor"] is None
    seen = [i["chat_id"] for i in first.json()["items"] + second.json()["items"]]
    assert sorted(seen) == sorted([in_a, in_b])


async def test_a_person_cannot_read_the_routing_feed(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    person = app_client()
    try:
        await login(person, org_admin.admin_email, org_admin.admin_password)
        resp = await person.get(ROUTING)
    finally:
        await person.aclose()
    assert resp.status_code == 401, resp.text


# =========================================================================== #
# the routing stream: chat doorbells and the machine's row, nothing else
# =========================================================================== #


@pytest.mark.parametrize("as_worker", [True, False], ids=["worker", "person"])
async def test_the_routing_stream_is_the_machine_credentials_alone(
    client: AsyncClient, org_admin: OrgWithAdmin, as_worker: bool
) -> None:
    if as_worker:
        box, org_a, _in_a, _org_b, _in_b = await _two_orgs_on_a_pool_box(client, org_admin)
        resp = await client.get(
            "/api/v1/events/routing", headers=_bearer(await _worker(client, box, org_a))
        )
        assert resp.status_code == 404, resp.text
        return
    person = app_client()
    try:
        await login(person, org_admin.admin_email, org_admin.admin_password)
        resp = await person.get("/api/v1/events/routing")
    finally:
        await person.aclose()
    assert resp.status_code == 401, resp.text


def _event(org_id: UUID, **fields: Any) -> HubEvent:
    base: dict[str, Any] = {
        "lane": "durable",
        "org_id": org_id,
        "type": "chat.updated",
        "entity": "chat",
        "entity_id": str(uuid4()),
        "version": 1,
        "visibility": VISIBILITY_ORG,
        "id": 1,
    }
    return HubEvent(**{**base, **fields})


def test_the_routing_stream_hears_doorbells_and_the_machines_row_only() -> None:
    """Of everything the box's full stream admits, the root process hears the
    doorbell of a chat bound to its machine and its machine's own row, and no
    document frame, folder change or org-wide traffic, which are the org
    process's to hear on its own credential."""
    machine, org, chat, node = uuid4(), uuid4(), str(uuid4()), str(uuid4())
    scope = MachineScope(
        ctx=ActingContext.for_machine(
            machine_id=machine,
            credential_id=uuid4(),
            org_id=uuid4(),
            label="pool",
            serves_every_org=True,
        ),
        chat_ids=frozenset({chat}),
        org_ids=frozenset({org}),
        node_ids=frozenset({node}),
    )
    heard = {
        "doorbell": _event(org, entity_id=chat),
        "doorbell-binding": _event(org, payload={BOUND_MACHINE_KEY: str(machine)}),
        "machine-row": _event(
            org, type="compute_machine.changed", entity="compute_machine", entity_id=str(machine)
        ),
    }
    unheard = {
        "document-frame": _event(org, entity_id=chat, channel=f"{CHAT_CHANNEL_PREFIX}{chat}"),
        "folder": _event(org, type="file_node.changed", entity="file_node", entity_id=node),
        "org-wide": _event(org, type="team_connection.updated", entity="team_connection"),
    }
    for name, event in {**heard, **unheard}.items():
        assert machine_visible_to(event, scope=scope), f"{name}: the full stream admits it"
    for name, event in heard.items():
        assert machine_routing_visible_to(event, scope=scope), name
    for name, event in unheard.items():
        assert not machine_routing_visible_to(event, scope=scope), name
