"""The delta feed needs no sticky session: two backend instances, one Postgres.

A client syncs against instance A, the mutation lands through instance B, and
the client resumes on A from the cursor it already held. Nothing but the
database sits between the two, so this is the proof that a fleet can put the
feed behind a round-robin load balancer — and that a resumed cursor reports the
row that landed elsewhere exactly once, never twice and never not at all.
"""

from __future__ import annotations

import uuid
from contextlib import aclosing
from typing import Any

import httpx
import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, delta_until
from alkera_core.config import settings

pytestmark = pytest.mark.asyncio

#: Nothing here is a latency claim — every request is a small page against an
#: app in this process — so the client's deadline only guards against one that
#: will never answer. It sits under the suite's own ninety seconds, so a hang is
#: still reported as the request that hung, and far above anything a loaded
#: runner does to a page this size, so load alone cannot decide the test.
TIMEOUT = httpx.Timeout(60.0)


async def _login(addr: str, email: str, password: str) -> httpx.AsyncClient:
    # Two instances on two ephemeral ports, and a browser reaching either of
    # them still says which origin it came from: the session cookie the login
    # hands back is ambient, so the origin guard refuses a mutation that shows
    # no evidence of coming from the portal this deployment serves.
    client = httpx.AsyncClient(
        base_url=f"http://{addr}",
        timeout=TIMEOUT,
        headers={"Origin": settings.frontend_base_url},
    )
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return client


async def _page(client: httpx.AsyncClient, drive_id: uuid.UUID, token: str) -> dict[str, Any]:
    """One delta read; the body carries the items and the next stored cursor."""
    response = await client.get(f"/api/v1/files/drives/{drive_id}/delta?token={token}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_files_delta_resumes_on_instance_a_after_a_mutation_through_b(
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    uvicorn_server_pair: tuple[str, str],
) -> None:
    """A cursor taken on A, a create through B, and A's resume carries that one
    row — once. Read again from the link A just handed back and the feed is
    empty, so the row is neither replayed nor lost across the two instances."""
    addr_a, addr_b = uvicorn_server_pair
    drive = await fx.drive()
    await fx._session.refresh(drive)
    # Nothing is created at the drive root: it is a traversal-only signpost that
    # refuses every direct write, so B's folder lands in the caller's home below.
    drive_id = drive.id
    # Nothing of this session's may stay in a transaction: the feed withholds
    # every row at or above the oldest in-flight snapshot in this database.
    await fx._session.rollback()

    admin = files_org.org
    async with (
        aclosing(await _login(addr_a, admin.admin_email, admin.admin_password)) as a,
        aclosing(await _login(addr_b, admin.admin_email, admin.admin_password)) as b,
    ):
        # The root is a signpost that takes no direct write, so the folder lands
        # in the caller's home — and the drive read that names the home also
        # ensures it on a first visit, which is itself an announced create. So
        # the cursor is EARNED rather than minted: `?token=latest` is the
        # boundary of what the feed may deliver, not of what has committed, and
        # while the home ensure is still behind that boundary a `latest` cursor
        # sits underneath it — leaving the setup's own row to arrive on the page
        # this test reads for the mutation. Reading on until the home has been
        # delivered puts it behind the cursor for good, whatever the cluster is
        # doing to the boundary.
        home = (await b.get("/api/v1/files/drives")).json()["homeId"]
        _, token = await delta_until(a, drive_id, carries=home)

        created = await b.post(
            f"/api/v1/files/drives/{drive_id}/items/{home}/children",
            json={"name": "landed-on-b", "kind": "folder"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert created.status_code == 201, created.text
        node_id = created.json()["id"]

        # A committed row is not yet a deliverable one, so the read is on the
        # feed's own signal: pages until A has handed back the row B wrote and
        # says it is caught up. How many that takes is the cluster's business.
        resumed, link = await delta_until(a, drive_id, carries=node_id, token=token)
        assert [item["id"] for item in resumed] == [node_id]
        assert resumed[0]["deleted"] is False
        assert resumed[0]["name"] == "landed-on-b"

        again = await _page(a, drive_id, link)
        assert again["items"] == [], "a resumed cursor never re-delivers what it already applied"
