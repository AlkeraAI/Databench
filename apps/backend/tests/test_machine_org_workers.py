"""A box that runs a worker per org, on its machine credential, through the
real routes.

Once a box's heartbeat says ``org_workers``, each org's chats and files are
reached on that org's worker credential, and the machine credential keeps only
the machine's own standing: the claim, the heartbeat, a worker credential's
mint and the routing feed. A chat or data route on the machine credential is a
coded 403 with its ``authz.decision`` row. A worker credential is minted only
for an org with a live chat bound to the machine. And the worker slots a beat
reports reach placement, with a beat that reports none read as not reporting.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.machine_refusals import MACHINE_WORKER_CREDENTIAL_REQUIRED
from backend.services.compute import placement
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin
from tests.test_machine_principal_routes import (
    Box,
    _box,
    _chat,
    _decisions,
    _effects,
    _error,
    _org,
    _row,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MINT = "/api/v1/machines/me/worker-credentials"
ROUTING = "/api/v1/machines/me/routing"


async def _beat(client: AsyncClient, box: Box, **body: Any) -> None:
    resp = await client.post(
        f"/api/v1/machines/{box.machine_id}/heartbeat", json=body, headers=box.headers
    )
    assert resp.status_code == 204, resp.text


async def _worker(client: AsyncClient, box: Box, org_id: UUID) -> dict[str, str]:
    resp = await client.post(MINT, json={"org_id": str(org_id)}, headers=box.headers)
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def _door(client: AsyncClient, door: str, chat_id: str, headers: dict[str, str]) -> Any:
    if door == "publisher-state":
        return await client.put(
            f"/api/v1/chats/{chat_id}/publisher-state",
            json={"state": "refused", "reason": "gateway"},
            headers=headers,
        )
    if door == "gateway-token":
        return await client.post(f"/api/v1/chats/{chat_id}/gateway-token", headers=headers)
    if door == "listing":
        return await client.get("/api/v1/chats", headers=headers)
    path = f"/api/v1/chats/{chat_id}" + ("/messages" if door == "messages" else "")
    return await client.get(path, headers=headers)


DOORS = ["read", "messages", "listing", "publisher-state", "gateway-token"]


@pytest.mark.parametrize("door", DOORS)
async def test_a_box_running_org_workers_is_refused_its_machine_credential_on_a_chat_route(
    client: AsyncClient, org_admin: OrgWithAdmin, door: str
) -> None:
    """The machine credential of a box that said ``org_workers`` no longer
    reaches a chat bound to its own machine: a 403 naming the credential to use,
    on record in the operator's lane as the machine-credential policy's denial.
    The org's worker credential reaches the same chat."""
    org, owner = await _org()
    box = await _box(org_admin, tenancy="pool")
    await _beat(client, box, capabilities=[BoxCapability.ORG_WORKERS])
    chat = await _chat(owner, machine_id=box.machine_id)

    refused = await _door(client, door, chat, box.headers)
    assert refused.status_code == 403, refused.text
    assert _error(refused)["code"] == MACHINE_WORKER_CREDENTIAL_REQUIRED
    rows = [
        r
        for r in await _decisions(org_admin.org_id, "machine_credential")
        if r.payload["effect"] == "deny"
    ]
    assert _effects(rows) == [("deny", "worker_credential_required")]
    assert rows[0].payload["attrs"] == {
        "is_machine": True,
        "credential_live": True,
        "machine_matches": True,
        "own_standing": False,
        "runs_org_workers": True,
        "org_reached": True,
    }
    assert rows[0].entity_id == str(box.machine_id)

    reached = await _door(client, door, chat, await _worker(client, box, org))
    assert reached.status_code < 300, reached.text


@pytest.mark.parametrize("rollback", ["beat-without-it", "re-registered-without-it"])
async def test_a_box_rolled_back_from_org_workers_is_still_refused_on_a_chat_route(
    client: AsyncClient, org_admin: OrgWithAdmin, rollback: str
) -> None:
    """A box that once said ``org_workers`` and now runs a build that does not
    (its process re-registered, or it simply beats without the capability)
    does not get the machine credential's old reach back: the chat route is
    still the coded 403, on record, naming the box as one that runs workers."""
    _org_id, owner = await _org()
    pod = f"i-{uuid4().hex[:12]}"
    box = await _box(org_admin, tenancy="pool", provider_pod_id=pod)
    await _beat(client, box, capabilities=[BoxCapability.ORG_WORKERS], daemon_instance_id="new")
    if rollback == "re-registered-without-it":
        claimed = await client.post(
            "/api/v1/machines/claim",
            json={"provider_pod_id": pod, "name": "box", "daemon_instance_id": "old"},
            headers=box.headers,
        )
        assert claimed.status_code == 200, claimed.text
        await _beat(client, box, daemon_instance_id="old")
    else:
        await _beat(client, box, capabilities=["workspaces"], daemon_instance_id="new")
    row = await _row(box.machine_id)
    assert not row.capabilities_json or BoxCapability.ORG_WORKERS not in row.capabilities_json
    assert row.ran_org_workers_at is not None, "remembered past the rollback"
    chat = await _chat(owner, machine_id=box.machine_id)

    refused = await client.get(f"/api/v1/chats/{chat}", headers=box.headers)
    assert refused.status_code == 403, refused.text
    assert _error(refused)["code"] == MACHINE_WORKER_CREDENTIAL_REQUIRED
    denials = [
        r
        for r in await _decisions(org_admin.org_id, "machine_credential")
        if r.payload["effect"] == "deny"
    ]
    assert _effects(denials) == [("deny", "worker_credential_required")]
    assert denials[0].payload["attrs"]["runs_org_workers"] is True


async def test_a_box_serving_in_one_process_keeps_its_machine_credentials_reach(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A box that never said ``org_workers`` (every build before the
    supervisor) reads the chats bound to it on its machine credential as it
    always did, and no machine-credential decision is written for it."""
    _org_id, owner = await _org()
    box = await _box(org_admin, tenancy="pool")
    await _beat(client, box, capabilities=["workspaces"])
    chat = await _chat(owner, machine_id=box.machine_id)
    resp = await client.get(f"/api/v1/chats/{chat}", headers=box.headers)
    assert resp.status_code == 200, resp.text
    denials = [
        r
        for r in await _decisions(org_admin.org_id, "machine_credential")
        if r.payload["effect"] == "deny"
    ]
    assert denials == []


async def test_a_box_running_org_workers_keeps_its_own_standing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The routes the box's root process lives on stay open to the machine
    credential: another beat, the routing feed, and a worker's mint."""
    org, owner = await _org()
    box = await _box(org_admin, tenancy="pool")
    await _beat(client, box, capabilities=[BoxCapability.ORG_WORKERS])
    await _chat(owner, machine_id=box.machine_id)
    await _beat(client, box, capabilities=[BoxCapability.ORG_WORKERS])
    routing = await client.get(ROUTING, headers=box.headers)
    assert routing.status_code == 200, routing.text
    await _worker(client, box, org)


async def test_a_box_mints_a_worker_only_for_an_org_it_holds_a_chat_for(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A pool box may be placed in any org, but it mints a worker credential
    only for an org with a live chat bound to its machine. An org with no
    chat anywhere, and an org whose chat another box holds, are the opaque
    404, on record in the refused org's lane."""
    held, owner = await _org()
    empty, _nobody = await _org()
    elsewhere, other_owner = await _org()
    box = await _box(org_admin, tenancy="pool")
    other = await _box(org_admin, tenancy="pool")
    await _beat(client, box, capabilities=[BoxCapability.ORG_WORKERS])
    await _chat(owner, machine_id=box.machine_id)
    await _chat(other_owner, machine_id=other.machine_id)

    await _worker(client, box, held)
    for org in (empty, elsewhere):
        resp = await client.post(MINT, json={"org_id": str(org)}, headers=box.headers)
        assert resp.status_code == 404, resp.text
        rows = await _decisions(org, "machine_credential")
        assert _effects(rows) == [("deny", "org_not_reached")]
        assert rows[0].payload["attrs"]["org_reached"] is False
    allowed = await _decisions(held, "machine_credential")
    assert _effects(allowed) == [("allow", "machine_self")]


@pytest.mark.parametrize(
    ("resources", "slots"),
    [
        pytest.param({"cpu_percent": 1.0}, None, id="left-out-is-not-reported"),
        pytest.param({"org_slots_free": 0}, 0, id="none-free"),
        pytest.param({"org_slots_free": 3}, 3, id="some-free"),
    ],
)
async def test_a_beat_carries_the_boxs_free_worker_slots(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    resources: dict[str, Any],
    slots: int | None,
) -> None:
    """The slots a box reports reach the placement read as sent; a box that
    leaves them out reads as not reporting, never as full."""
    box = await _box(org_admin, tenancy="pool")
    await _beat(client, box, resources=resources)
    assert placement.org_slots_free(await _row(box.machine_id)) == slots


async def test_a_beat_names_the_orgs_whose_worker_keeps_failing_and_the_next_clears_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The orgs a box serves none of reach placement as sent, and a later
    beat that names none (the worker stayed up, or the org left) clears them."""
    from alkera_core.compute.machines import failing_orgs

    box = await _box(org_admin, tenancy="pool")
    failing = str(uuid4())
    await _beat(client, box, resources={"org_workers_failing_ids": [failing]})
    assert failing_orgs(await _row(box.machine_id)) == {failing}
    await _beat(client, box, resources={"cpu_percent": 1.0})
    assert failing_orgs(await _row(box.machine_id)) == frozenset()
