"""``FILES_INLINE_OPERATIONS``: who runs the work a 202 promised.

A Files route that queues work answers ``202`` and leaves the running to the
Temporal worker. A single-process self-hosted install has no worker, and a
route test has no Temporal at all, so the flag lets the request run the work
itself — through the same library core ``worker/tasks/files.py`` calls — after
its response has gone out.

What the tests below pin is the *difference the flag makes*, on both sides:
with it off the operation is still sitting in ``queued`` when the client's next
request arrives, with it on the same call has already reached ``done`` and the
version it promised exists. A test that only ran the flag on would pass just as
well against a route that promoted synchronously and never needed a flag.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures, FilesOrgFixture
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import OperationId, OrgScope
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from backend.api.routes.files import PREFIX
from blake3 import blake3
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

UPLOADS = f"{PREFIX}/uploads"

#: One part, so the upload is three HTTP calls rather than five. The flag is
#: about who commits the session, not about how many parts it holds.
PAYLOAD = b"inline-operations-payload"


def _checksum() -> str:
    return blake3(PAYLOAD).digest().hex()


def _parts_body() -> dict[str, Any]:
    return {"parts": [{"partNo": 1, "size": len(PAYLOAD), "checksum": _checksum()}]}


@pytest.fixture(autouse=True)
def one_part(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.api.routes.files.uploads as routes

    monkeypatch.setattr(routes, "PART_SIZE", len(PAYLOAD))


@pytest.fixture
def inline_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production SaaS shape: queued work belongs to the worker."""
    monkeypatch.setattr(settings, "files_inline_operations", False)


@pytest_asyncio.fixture
async def root(fx: FilesFixtures) -> FileNode:
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    # `/Shared`, not the drive root: the root is a traversal-only signpost and
    # refuses every direct write, so nothing could be uploaded into it.
    return await fx.shared()


async def open_and_put(client: AsyncClient, parent: FileNode, *, name: str) -> str:
    """Open a session and put its single part. Returns the upload id."""
    opened = await client.post(
        UPLOADS,
        json={"declaredSize": len(PAYLOAD), "name": name, "parentId": str(parent.id)},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert opened.status_code == 201, opened.text
    upload_id: str = opened.json()["uploadId"]
    part = await client.put(
        f"{UPLOADS}/{upload_id}/parts/1",
        content=PAYLOAD,
        headers={"Idempotency-Key": uuid.uuid4().hex, "X-Part-Checksum": _checksum()},
    )
    assert part.status_code == 200, part.text
    return upload_id


async def upload(client: AsyncClient, parent: FileNode, *, name: str) -> Any:
    """Open, put the part, and ask for the commit."""
    upload_id = await open_and_put(client, parent, name=name)
    return await client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json=_parts_body(),
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


async def operation_state(session: AsyncSession, org: FilesOrgFixture, op_id: str) -> str:
    """Read the operation row back the way the operations route does."""
    repo = FilesRepo(session, OrgScope(org_team_id=org.org.org_id))
    ctx = ActingContext.for_user(
        user_id=org.org.admin_id, org_id=org.org.org_id, email=org.org.admin_email
    )
    return (await Operations(repo, ctx, SystemClock()).get(OperationId(uuid.UUID(op_id)))).state


async def named(session: AsyncSession, root: FileNode, name: bytes) -> list[FileNode]:
    rows = await session.execute(
        select(FileNode).where(FileNode.drive_id == root.drive_id, FileNode.name == name)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# The flag's two sides, on the upload promote.
# ---------------------------------------------------------------------------


async def test_with_the_flag_on_a_completed_upload_reaches_done_and_the_version_exists(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
) -> None:
    """The 202 still says ``queued``; by the time it is read, it is not.

    Both halves matter. The wire contract does not change with the flag — a
    client writes one progress path either way — and the work has nonetheless
    happened, which is what a self-hosted install with no worker needs and what
    the CLI push chain was blocked on.
    """
    finished = await upload(files_client, root, name="inline.txt")
    assert finished.status_code == 202, finished.text
    assert finished.json()["state"] == "queued"
    op_id = finished.json()["id"]

    assert await operation_state(real_session, files_org, op_id) == "done"

    nodes = await named(real_session, root, b"inline.txt")
    assert len(nodes) == 1
    assert nodes[0].size == len(PAYLOAD)
    versions = list(
        (await real_session.execute(select(FileVersion).where(FileVersion.node_id == nodes[0].id)))
        .scalars()
        .all()
    )
    assert len(versions) == 1
    assert versions[0].size_bytes == len(PAYLOAD)


async def test_with_the_flag_off_the_completed_upload_stays_queued_and_makes_no_node(
    files_on: None,
    inline_off: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
) -> None:
    """The worker's job stays the worker's job.

    The negative twin of the test above: without it, a route that promoted
    inline unconditionally would look identical and the flag would be
    decorative.
    """
    finished = await upload(files_client, root, name="worker.txt")
    assert finished.status_code == 202, finished.text
    op_id = finished.json()["id"]

    assert await operation_state(real_session, files_org, op_id) == "queued"
    assert await named(real_session, root, b"worker.txt") == []


async def test_a_replayed_complete_does_not_promote_twice(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
) -> None:
    """A replayed ``complete`` asks for the commit zero more times.

    The hook sits inside the idempotent body, so the retry a flaky client makes
    replays the stored 202 and queues nothing. Were it outside, the second call
    would run a ``done`` operation's core again — the shape that would make a
    retried ``conflict="rename"`` leave two files.
    """
    upload_id = await open_and_put(files_client, root, name="once.txt")
    key = {"Idempotency-Key": uuid.uuid4().hex}
    first = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete", json=_parts_body(), headers=key
    )
    second = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete", json=_parts_body(), headers=key
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]

    assert await operation_state(real_session, files_org, first.json()["id"]) == "done"
    assert len(await named(real_session, root, b"once.txt")) == 1
