"""An org worker's connector leases outlive its first credential.

The supervisor mints a worker credential that lives fifteen minutes and
replaces it at ten. The worker's connections client (the one the schema cards
and every connector lease go through) must follow that replacement: here the
real CLI client talks to the real app, the clock crosses the first
credential's expiry, and the worker still lists its chat's connections and
leases their credential.
"""

from __future__ import annotations

import pytest
from alkera_cli.cloud.worker_credential import WorkerConnectionsClient, WorkerCredential
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient
from tests.conftest import OrgWithAdmin, fastapi_app
from tests.test_chat_connections_machine import SECRET, _lease_url, _owner_with_connection
from tests.test_machine_principal_routes import _beat, _box, _chat

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MINT = "/api/v1/machines/me/worker-credentials"


async def test_a_worker_leases_on_the_credential_that_replaced_its_expired_first(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    owner = await _owner_with_connection()
    with freeze_time("2026-10-05T12:00:00Z", real_asyncio=True) as frozen:
        box = await _box(org_admin, tenancy="pool")
        await _beat(client, box)
        chat_id = await _chat(owner.user, machine_id=box.machine_id)

        async def mint() -> str:
            resp = await client.post(MINT, json={"org_id": str(owner.org_id)}, headers=box.headers)
            assert resp.status_code == 201, resp.text
            token: str = resp.json()["token"]
            return token

        first = await mint()
        credential = WorkerCredential(first)
        connections = WorkerConnectionsClient(
            api_url="http://test",
            credential=credential,
            chats=lambda: [chat_id],
            transport=ASGITransport(app=fastapi_app),
        )
        bundle, _expires = await connections.lease_credential_bundle(
            owner.connection_id, chat_id=chat_id
        )
        assert bundle["primary"] == SECRET

        frozen.move_to("2026-10-05T12:10:00Z")
        await _beat(client, box)
        credential.replace(await mint())

        frozen.move_to("2026-10-05T12:16:00Z")
        await _beat(client, box)
        expired = await client.post(
            _lease_url(chat_id, owner.connection_id),
            headers={"Authorization": f"Bearer {first}"},
        )
        assert expired.status_code == 401, "the first credential has expired by now"

        listed = await connections.fetch_records()
        assert listed is not None and [r.id for r in listed] == [owner.connection_id]
        bundle, _expires = await connections.lease_credential_bundle(
            owner.connection_id, chat_id=chat_id
        )
        assert bundle["primary"] == SECRET
