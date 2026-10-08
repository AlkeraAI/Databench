"""The upload-session routes, driven through the real app.

Every test here goes over HTTP through ``ASGITransport`` against real Postgres
and a real filesystem store, so a green run means the routes, the dependencies,
the library and the store agree — not that a mock was told what to say.

The part size is driven down through the module's ``PART_SIZE`` seam so a
three-part upload is 24 bytes rather than 96 MiB; nothing else about the path
changes, and the ``partsTotal`` the route reports is computed from the same
number the parts are checked against.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import alkera_core.files.store.filesystem as driver
import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, refusal
from alkera_core.authz.enums import PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files import drives
from alkera_core.files.clock import SystemClock
from alkera_core.files.errors import Conflict, PreconditionFailed
from alkera_core.files.ids import DomainId, OperationId, OrgScope, SessionId
from alkera_core.files.leases import LeaseConflict
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sync.atomic import default_fsync_dir
from alkera_core.files.uploads import UploadCompletion
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.uploads import FileUploadSession
from backend.api.routes.files import PREFIX
from backend.api.routes.files.uploads import MAX_COMPLETE_PARTS
from backend.services.files.store import store_factory
from blake3 import blake3
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files.conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

UPLOADS = f"{PREFIX}/uploads"

#: Small enough that three parts fit in a test body, large enough that the
#: boundary cases (a part past the last one, an empty part) are still distinct.
TEST_PART_SIZE = 8

#: The content origin a download is minted onto, so a test that cares about the
#: bytes can follow the 302 back into the same app.
CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"


def digest(data: bytes) -> str:
    return blake3(data).digest().hex()


@pytest.fixture(autouse=True)
def small_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the routes at an 8-byte part size."""
    import backend.api.routes.files.uploads as routes

    monkeypatch.setattr(routes, "PART_SIZE", TEST_PART_SIZE)


@pytest_asyncio.fixture
async def root(fx: FilesFixtures) -> FileNode:
    drive = await fx.drive()
    # A drive is created with no room at all; the org's plan fills the ceilings
    # in. Give this one the product default so a 24-byte upload is not a 507.
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    # `/Shared`, not the drive root: the root is a traversal-only signpost and
    # refuses every direct write, so nothing could be uploaded into it.
    return await fx.shared()


async def open_session(
    client: AsyncClient,
    parent: FileNode,
    *,
    name: str = "report.txt",
    declared_size: int = 24,
    headers: dict[str, str] | None = None,
) -> Any:
    """``headers={}`` really means *no* Idempotency-Key, not "give me one"."""
    return await client.post(
        UPLOADS,
        json={
            "declaredSize": declared_size,
            "name": name,
            "parentId": str(parent.id),
        },
        headers={"Idempotency-Key": uuid.uuid4().hex} if headers is None else headers,
    )


async def put_part(
    client: AsyncClient, upload_id: str, part_no: int, data: bytes, *, checksum: str | None = None
) -> Any:
    return await client.put(
        f"{UPLOADS}/{upload_id}/parts/{part_no}",
        content=data,
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "X-Part-Checksum": checksum if checksum is not None else digest(data),
        },
    )


async def promote(
    session: AsyncSession,
    org: FilesOrgFixture,
    upload_id: str,
    op_id: str,
) -> None:
    """Run the commit activity the worker would run, in-process.

    The worker is not part of a route test: what the route promised is an
    operation, and this is the thing that later moves it to ``done``.
    """
    from alkera_core.files.ids import OrgScope

    repo = FilesRepo(session, OrgScope(org_team_id=org.org.org_id))
    ctx = ActingContext.for_user(
        user_id=org.org.admin_id, org_id=org.org.org_id, email=org.org.admin_email
    )
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None
    store = await store_factory(settings, clock=SystemClock()).for_domain(
        DomainId(drive.dedup_domain_id)
    )
    completion = UploadCompletion(repo, ctx, SystemClock(), store, part_size=TEST_PART_SIZE)
    await completion.promote(SessionId(uuid.UUID(upload_id)), OperationId(uuid.UUID(op_id)))


async def operation_state(session: AsyncSession, org: FilesOrgFixture, op_id: str) -> str:
    from alkera_core.files.ids import OrgScope

    repo = FilesRepo(session, OrgScope(org_team_id=org.org.org_id))
    ctx = ActingContext.for_user(
        user_id=org.org.admin_id, org_id=org.org.org_id, email=org.org.admin_email
    )
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None
    store = await store_factory(settings, clock=SystemClock()).for_domain(
        DomainId(drive.dedup_domain_id)
    )
    ops = Operations(repo, ctx, SystemClock(), store)
    return (await ops.get(OperationId(uuid.UUID(op_id)))).state


# ---------------------------------------------------------------------------
# The full path: open, three parts, complete, promote, the bytes.
# ---------------------------------------------------------------------------


async def test_open_parts_complete_promote_lands_the_bytes(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # These tests drive the promote by hand, standing in for the worker, so
    # they pin the deployment where the worker owns it — the suite otherwise
    # runs queued operations inline (FILES_INLINE_OPERATIONS).
    monkeypatch.setattr(settings, "files_inline_operations", False)
    payload = b"abcdefghijklmnopqrstuvwx"
    opened = await open_session(files_client, root, declared_size=len(payload))
    assert opened.status_code == 201, opened.text
    body = opened.json()
    assert body["partsTotal"] == 3
    assert body["partSize"] == TEST_PART_SIZE
    upload_id = body["uploadId"]

    parts = [payload[i : i + TEST_PART_SIZE] for i in range(0, len(payload), TEST_PART_SIZE)]
    for index, chunk in enumerate(parts, start=1):
        answer = await put_part(files_client, upload_id, index, chunk)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"partNo": index, "size": len(chunk), "duplicate": False}

    progress = await files_client.get(f"{UPLOADS}/{upload_id}")
    assert progress.status_code == 200
    assert progress.json()["offset"] == len(payload)
    assert progress.json()["acceptedParts"] == [1, 2, 3]
    assert progress.json()["complete"] is True

    finished = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [
                {"partNo": i, "size": len(c), "checksum": digest(c)}
                for i, c in enumerate(parts, start=1)
            ]
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert finished.status_code == 202, finished.text
    op_id = finished.json()["id"]
    assert finished.json()["state"] == "queued"

    await promote(real_session, files_org, upload_id, op_id)
    assert await operation_state(real_session, files_org, op_id) == "done"

    node = (
        await real_session.execute(
            select(FileNode).where(
                FileNode.drive_id == root.drive_id, FileNode.name == b"report.txt"
            )
        )
    ).scalar_one()
    assert node.size == len(payload)


async def test_the_worker_hand_off_names_the_promote_its_row_can_run(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
    nudge_recorder: Any,
) -> None:
    """On the worker-run shape the route's whole job is the hand-off: it starts
    ``files.promote`` with positional args across a process boundary. Bind what
    it sent to the workflow's own ``run`` by name, then run the promote from
    those values alone -- the row names the session -- which is exactly what the
    workflow, and a recovery of a lost nudge, does. A drift between the route,
    the nudge and the workflow input fails here, not on the first upload."""
    import inspect

    from alkera_core.temporal import WorkflowType
    from worker.workflows.files import FilesPromote

    monkeypatch.setattr(settings, "files_inline_operations", False)
    payload = b"handed to the worker"
    opened = await open_session(files_client, root, declared_size=len(payload), name="w.txt")
    upload_id = opened.json()["uploadId"]
    assert (await put_part(files_client, upload_id, 1, payload[:8])).status_code == 200
    assert (await put_part(files_client, upload_id, 2, payload[8:16])).status_code == 200
    assert (await put_part(files_client, upload_id, 3, payload[16:])).status_code == 200
    parts = [payload[:8], payload[8:16], payload[16:]]
    finished = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [
                {"partNo": i, "size": len(c), "checksum": digest(c)}
                for i, c in enumerate(parts, start=1)
            ]
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert finished.status_code == 202, finished.text
    op_id = finished.json()["id"]

    (nudge,) = nudge_recorder.for_workflow(WorkflowType.FILES_PROMOTE.value)
    bound = inspect.signature(FilesPromote.run).bind(object(), *nudge.args)
    assert bound.arguments["op_id"] == op_id
    assert bound.arguments["org_team_id"] == str(files_org.org.org_id)
    assert set(bound.arguments) == {"self", "op_id", "org_team_id"}

    repo = FilesRepo(real_session, OrgScope(org_team_id=files_org.org.org_id))
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id,
        org_id=files_org.org.org_id,
        email=files_org.org.admin_email,
    )
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None
    store = await store_factory(settings, clock=SystemClock()).for_domain(
        DomainId(drive.dedup_domain_id)
    )
    completion = UploadCompletion(repo, ctx, SystemClock(), store, part_size=TEST_PART_SIZE)
    await completion.promote_operation(OperationId(uuid.UUID(bound.arguments["op_id"])))
    assert await operation_state(real_session, files_org, op_id) == "done"
    node = (
        await real_session.execute(
            select(FileNode).where(FileNode.drive_id == root.drive_id, FileNode.name == b"w.txt")
        )
    ).scalar_one()
    assert node.size == len(payload)


# ---------------------------------------------------------------------------
# The status contract, one param per status.
# ---------------------------------------------------------------------------


async def test_open_over_quota_is_507(
    files_on: None,
    files_client: AsyncClient,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_core.models.files.stores import FileDrive
    from alkera_core.org_entitlements import org_entitlements

    async def _no_plan_figure(db: object, org_id: object) -> None:
        return None

    # The drive row is the ceiling only for a tier the plan sets no figure for;
    # a plan figure would replace the 4 bytes written below.
    monkeypatch.setattr(org_entitlements(), "plan_storage_bytes", _no_plan_figure)
    drive = await real_session.get(FileDrive, root.drive_id)
    assert drive is not None
    drive.quota_bytes = 4
    await real_session.commit()
    answer = await open_session(files_client, root, declared_size=1024)
    assert answer.status_code == 507
    assert answer.json()["code"] == "files.quota_bytes"


async def test_open_with_too_many_live_sessions_is_409(
    files_on: None,
    files_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    root: FileNode,
) -> None:
    first = await open_session(files_client, root)
    assert first.status_code == 201
    monkeypatch.setattr(settings, "files_max_open_sessions_per_org", 1)
    second = await open_session(files_client, root)
    assert second.status_code == 409
    assert second.json()["code"] == "files.too_many_sessions"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("a/b", id="separator"),
        pytest.param("..", id="dotdot"),
    ],
)
async def test_open_with_a_refused_name_is_422(
    files_on: None, files_client: AsyncClient, root: FileNode, name: str
) -> None:
    answer = await open_session(files_client, root, name=name)
    assert answer.status_code == 422
    assert answer.json()["code"].startswith("files.invalid_name.")


async def test_open_without_an_idempotency_key_is_428(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    answer = await open_session(files_client, root, headers={})
    assert answer.status_code == 428
    assert answer.json()["code"] == "files.idempotency_key_required"


async def test_open_replayed_under_one_key_returns_the_same_session(
    files_on: None, files_client: AsyncClient, root: FileNode, real_session: AsyncSession
) -> None:
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    first = await open_session(files_client, root, headers=headers)
    second = await open_session(files_client, root, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    from alkera_core.models.files.uploads import FileUploadSession

    sessions = (
        await real_session.execute(
            select(FileUploadSession.id).where(FileUploadSession.parent_id == root.id)
        )
    ).all()
    assert len(sessions) == 1, "a replay must not open a second session"


async def test_a_zero_byte_file_is_opened_sent_and_read_back_empty(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty file is a first-class upload, not a size the API cannot express.

    The whole point is that it goes through the *same* session a 24-byte file
    does: one published part, one PUT, one completion — so a client that cuts
    every size the same way never meets a special case, and the object the
    store ends up holding is a real empty object it can serve back.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)
    opened = await open_session(files_client, root, name="empty.txt", declared_size=0)
    assert opened.status_code == 201, opened.text
    body = opened.json()
    assert body["partsTotal"] == 1, "a zero-byte file is published as one part, never none"
    upload_id = body["uploadId"]

    answer = await put_part(files_client, upload_id, 1, b"")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"partNo": 1, "size": 0, "duplicate": False}

    progress = await files_client.get(f"{UPLOADS}/{upload_id}")
    assert progress.status_code == 200
    assert (progress.json()["offset"], progress.json()["length"]) == (0, 0)
    assert progress.json()["acceptedParts"] == [1]
    assert progress.json()["complete"] is True, "nothing is left to send"

    finished = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={"parts": [{"partNo": 1, "size": 0, "checksum": digest(b"")}]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert finished.status_code == 202, finished.text
    op_id = finished.json()["id"]

    await promote(real_session, files_org, upload_id, op_id)
    assert await operation_state(real_session, files_org, op_id) == "done"

    node = (
        await real_session.execute(
            select(FileNode).where(
                FileNode.drive_id == root.drive_id, FileNode.name == b"empty.txt"
            )
        )
    ).scalar_one()
    assert node.size == 0

    minted = await files_client.get(f"{PREFIX}/drives/{node.drive_id}/items/{node.id}/content")
    assert minted.status_code == 302, minted.text
    location = str(minted.headers["location"])
    read = await files_client.get(
        location.removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST}
    )
    assert read.status_code == 200, read.text
    assert read.content == b"", "the empty object is served back, not a 404 or a stub"


async def test_bytes_are_refused_on_a_session_that_declared_none(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """A zero-byte session reserved no quota, so it may not be fed any bytes."""
    opened = await open_session(files_client, root, name="empty.txt", declared_size=0)
    answer = await put_part(files_client, opened.json()["uploadId"], 1, b"sneaky")
    assert answer.status_code == 422
    assert answer.json()["code"] == "files.size_mismatch"


async def test_part_out_of_range_is_422(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    answer = await put_part(files_client, upload_id, 2, b"12345678")
    assert answer.status_code == 422
    assert answer.json()["code"] == "files.part_out_of_range"


async def test_empty_part_is_422(files_on: None, files_client: AsyncClient, root: FileNode) -> None:
    """The emptiness rule is the session's: a session that declared bytes
    cannot be satisfied with none, whatever ``partsTotal`` it published."""
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    answer = await put_part(files_client, upload_id, 1, b"")
    assert answer.status_code == 422
    assert answer.json()["code"] == "files.empty_part"


async def test_a_part_still_streaming_holds_up_no_other_part(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """The parts of one upload go up side by side, as a large upload sends them.

    Part 1's body is held mid-stream. By then the session must already be
    claimed and committed — the body streams after the claim, not into memory
    ahead of it — and part 2 must be answered meanwhile. The route used to read
    the whole body to fingerprint it and then hold the claim's lock on the
    session row until the response, so the other parts of a 2 GB upload queued
    behind it until they timed out as 503s.
    """
    first_bytes, second_bytes = b"abcdefgh", b"ijklmnop"
    opened = await open_session(files_client, root, declared_size=16)
    assert opened.status_code == 201, opened.text
    upload_id = opened.json()["uploadId"]
    started, release = asyncio.Event(), asyncio.Event()

    async def held_open() -> AsyncIterator[bytes]:
        yield first_bytes[:4]
        started.set()
        await release.wait()
        yield first_bytes[4:]

    first = asyncio.create_task(
        files_client.put(
            f"{UPLOADS}/{upload_id}/parts/1",
            content=held_open(),
            headers={
                "Idempotency-Key": uuid.uuid4().hex,
                "X-Part-Checksum": digest(first_bytes),
                "Content-Length": str(len(first_bytes)),
            },
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 10)
        during = await files_client.get(f"{UPLOADS}/{upload_id}")
        assert during.json()["state"] == "uploading", "the claim had not committed"
        second = await asyncio.wait_for(put_part(files_client, upload_id, 2, second_bytes), 10)
        assert second.status_code == 200, second.text
        assert not first.done(), "part 1 answered before its body was released"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    assert first.result().status_code == 200, first.result().text

    progress = await files_client.get(f"{UPLOADS}/{upload_id}")
    assert progress.json()["acceptedParts"] == [1, 2]
    assert progress.json()["offset"] == 16


async def test_part_resent_with_other_bytes_is_409(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    assert (await put_part(files_client, upload_id, 1, b"abcdefgh")).status_code == 200
    answer = await put_part(files_client, upload_id, 1, b"ABCDEFGH")
    assert answer.status_code == 409
    assert answer.json()["code"] == "files.part_mismatch"


async def test_part_resent_identically_is_a_no_op(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    assert (await put_part(files_client, upload_id, 1, b"abcdefgh")).status_code == 200
    again = await put_part(files_client, upload_id, 1, b"abcdefgh")
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    progress = await files_client.get(f"{UPLOADS}/{upload_id}")
    assert progress.json()["offset"] == 8


async def test_part_into_an_aborted_session_is_409(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    killed = await files_client.request(
        "DELETE",
        f"{UPLOADS}/{upload_id}",
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert killed.status_code == 204
    answer = await put_part(files_client, upload_id, 1, b"abcdefgh")
    assert answer.status_code == 409
    assert answer.json()["code"] == "files.session_state"


async def test_abort_twice_is_409(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    for _ in range(1):
        killed = await files_client.request(
            "DELETE", f"{UPLOADS}/{upload_id}", headers={"Idempotency-Key": uuid.uuid4().hex}
        )
        assert killed.status_code == 204
    again = await files_client.request(
        "DELETE", f"{UPLOADS}/{upload_id}", headers={"Idempotency-Key": uuid.uuid4().hex}
    )
    assert again.status_code == 409
    assert again.json()["code"] == "files.session_state"


async def test_complete_is_always_202_whatever_the_bytes(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same content twice: both completions answer 202 before a promote runs.

    If ``complete`` decided dedup inline, the second call would be able to
    answer differently — and a client could time the difference.
    """
    # These tests drive the promote by hand, standing in for the worker, so
    # they pin the deployment where the worker owns it — the suite otherwise
    # runs queued operations inline (FILES_INLINE_OPERATIONS).
    monkeypatch.setattr(settings, "files_inline_operations", False)
    payload = b"abcdefgh"
    answers = []
    uploads = []
    for name in ("one.txt", "two.txt"):
        opened = await open_session(files_client, root, name=name, declared_size=len(payload))
        upload_id = opened.json()["uploadId"]
        await put_part(files_client, upload_id, 1, payload)
        finished = await files_client.post(
            f"{UPLOADS}/{upload_id}/complete",
            json={"parts": [{"partNo": 1, "size": len(payload), "checksum": digest(payload)}]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        answers.append(finished)
        uploads.append((upload_id, finished.json()["id"]))
    assert [a.status_code for a in answers] == [202, 202]
    assert [a.json()["state"] for a in answers] == ["queued", "queued"]
    for upload_id, op_id in uploads:
        await promote(real_session, files_org, upload_id, op_id)
        assert await operation_state(real_session, files_org, op_id) == "done"


async def test_complete_with_more_parts_than_a_session_can_hold_is_422(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """The part list is the one field of this body that grows without bound, and
    FastAPI parses the whole thing into an object tree before any dependency
    runs. `max_length` refuses the list itself, so a caller cannot make the
    process build one descriptor past what a session could ever hold."""
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    parts = [
        {"partNo": n, "size": 8, "checksum": digest(b"abcdefgh")}
        for n in range(1, MAX_COMPLETE_PARTS + 2)
    ]
    answer = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={"parts": parts},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert answer.status_code == 422
    assert MAX_COMPLETE_PARTS == settings.files_max_upload_parts


async def test_complete_with_a_part_list_that_disagrees_is_409(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    await put_part(files_client, upload_id, 1, b"abcdefgh")
    answer = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={"parts": [{"partNo": 1, "size": 8, "checksum": digest(b"ABCDEFGH")}]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert answer.status_code == 409
    assert answer.json()["code"].startswith("files.part")


# ---------------------------------------------------------------------------
# No oracles: the three not-yours classes over GET /uploads/{id}.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def foreign_session_id(
    files_on: None, client: AsyncClient, real_session: AsyncSession
) -> AsyncIterator[str]:
    """An upload session opened by a *different* org's admin."""
    from datetime import UTC, datetime

    from backend.services.org import teams as team_service
    from tests.conftest import login

    email = f"stranger-{uuid.uuid4().hex[:8]}@example.com"
    password = "stranger-pass-12345"
    org, admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"stranger-{uuid.uuid4().hex[:8]}",
        admin_email=email,
        admin_first_name="Other",
        admin_last_name="Org",
        admin_password=password,
    )
    admin.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    stranger = await login(client, email, password)
    from alkera_core.models.files.stores import FileDrive

    # The drive is created by the first Files request this org makes, so make
    # one (it is refused: the parent does not exist) before reading the row.
    warmed = await stranger.post(
        UPLOADS,
        json={"declaredSize": 8, "name": "probe.txt", "parentId": str(uuid.uuid4())},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert warmed.status_code == 404
    other_drive = await real_session.get(FileDrive, await _drive_id_of(real_session, org.id))
    assert other_drive is not None
    other_drive.quota_bytes = settings.files_quota_default_bytes
    other_drive.quota_nodes = settings.files_quota_default_nodes
    await real_session.commit()
    drive_root = await _root_of(real_session, org.id)
    opened = await stranger.post(
        UPLOADS,
        json={"declaredSize": 8, "name": "theirs.txt", "parentId": str(drive_root)},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert opened.status_code == 201, opened.text
    yield opened.json()["uploadId"]


async def _drive_id_of(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    from alkera_core.files.ids import OrgScope

    repo = FilesRepo(session, OrgScope(org_team_id=org_id))
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None
    return drive.id


async def _root_of(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    """A folder in that org's drive a caller could write into, if it were theirs.

    ``/Shared`` rather than the drive root, because the root is a traversal-only
    signpost and refuses every direct write: aimed there the open would answer
    ``files.container_readonly`` whoever asked, and the case would stop proving
    that a stranger's folder is the same opaque 404 as one that never existed.
    """
    from alkera_core.files.ids import OrgScope
    from alkera_core.models.files.tree import FileNode as Node

    repo = FilesRepo(session, OrgScope(org_team_id=org_id))
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None and drive.root_node_id is not None
    shared = (
        await session.execute(
            select(Node).where(
                Node.parent_id == drive.root_node_id,
                Node.name == drives.SHARED_NAME,
                Node.trashed_at.is_(None),
            )
        )
    ).scalar_one()
    return uuid.UUID(str(shared.id))


async def test_the_three_not_yours_classes_are_one_answer(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    foreign_session_id: str,
    client: AsyncClient,
) -> None:
    """Nonexistent, other-org and same-org-unreadable answer byte-identically."""
    from tests.conftest import login

    # The stranger fixture logged the shared client in as another org, so this
    # org's admin signs back in before asking anything.
    admin = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    nonexistent = await admin.get(f"{UPLOADS}/{uuid.uuid4()}")
    other_org = await admin.get(f"{UPLOADS}/{foreign_session_id}")

    mine = await open_session(admin, root)
    assert mine.status_code == 201, mine.text
    unreadable_id = mine.json()["uploadId"]
    member = await login(client, files_org.member.email, files_org.member_password)
    unreadable = await member.get(f"{UPLOADS}/{unreadable_id}")

    bodies = {r.status_code: r.json() for r in (nonexistent, other_org, unreadable)}
    assert list(bodies) == [404], [
        (r.status_code, r.text) for r in (nonexistent, other_org, unreadable)
    ]
    assert refusal(nonexistent) == refusal(other_org) == refusal(unreadable) == NOT_FOUND


async def test_a_dark_deployment_answers_the_same_404(
    files_client: AsyncClient, files_off: None, root: FileNode
) -> None:
    """``files_enabled`` off: the route is mounted and answers not-found.

    The id is real rather than invented, so the 404 is the kill switch rather
    than the miss a nonexistent upload would earn on a live deployment too."""
    answer = await files_client.get(f"{UPLOADS}/{uuid.uuid4()}")
    assert answer.status_code == 404
    assert refusal(answer) == NOT_FOUND


async def test_the_upload_routes_are_in_the_openapi_surface_while_dark() -> None:
    """The SDKs carry the surface even when production ships it dark."""
    from backend.app_factory import create_app

    paths = create_app().openapi()["paths"]
    assert f"{PREFIX}/uploads" in paths
    assert f"{PREFIX}/uploads/{{session_id}}/complete" in paths


async def test_a_part_over_the_proxied_cap_is_422(
    files_on: None, files_client: AsyncClient, root: FileNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published cap is a typed 422, never a timeout or a truncated body."""
    import backend.api.routes.files.uploads as routes

    monkeypatch.setattr(routes, "MAX_PROXIED_PART_BYTES", 4)
    opened = await open_session(files_client, root, declared_size=8)
    answer = await put_part(files_client, opened.json()["uploadId"], 1, b"abcdefgh")
    assert answer.status_code == 422
    assert answer.json()["code"] == "files.part_too_large"


async def test_a_part_whose_bytes_disagree_with_its_checksum_is_422(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """A flipped bit in transit is the caller's failure, told in the Files
    vocabulary. Left untranslated the driver's own ``ChecksumMismatch`` is not
    a ``FilesError``, so it escapes to the platform catch-all as a 500."""
    opened = await open_session(files_client, root, declared_size=8)

    answer = await put_part(
        files_client, opened.json()["uploadId"], 1, b"abcdefgh", checksum=digest(b"ABCDEFGH")
    )

    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "files.part_checksum_mismatch"


async def test_the_part_checksum_is_blake3_and_a_sha256_of_the_same_bytes_is_refused(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """The header names an algorithm, not just "some stable hex digest".

    The store re-hashes the streamed bytes with BLAKE3, so a client that hashes
    the RIGHT bytes with the WRONG function is refused exactly like a flipped
    bit — which is how every browser upload died on a live stack while the
    mocked tiers, which inject their own digest, stayed green. Pinning both
    answers here means an algorithm change on either side is a red test rather
    than a 422 nobody can explain.
    """
    body = b"abcdefgh"
    upload_id = (await open_session(files_client, root, declared_size=8)).json()["uploadId"]

    refused = await put_part(
        files_client, upload_id, 1, body, checksum=hashlib.sha256(body).hexdigest()
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.part_checksum_mismatch"

    accepted = await put_part(files_client, upload_id, 1, body, checksum=blake3(body).hexdigest())
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"partNo": 1, "size": 8, "duplicate": False}


async def test_a_store_outage_on_a_part_is_503_not_a_platform_500(
    files_on: None,
    files_client: AsyncClient,
    root: FileNode,
    files_store: Path,
) -> None:
    """A throttled or unreachable store is a typed 503: the class of failure a
    client retries, not an internal fault in the platform envelope."""
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.store.scoped import FilesystemScoped
    from alkera_test_support.files.faulty_store import Fault, FaultSchedule, FaultyStore
    from backend.services.files.store import set_store_factory

    opened = await open_session(files_client, root, declared_size=8)
    inner = FilesystemScoped(files_store, clock=SystemClock())

    class _Unavailable:
        """Every handle the request path takes answers "unavailable"."""

        def __init__(self) -> None:
            self._schedule = FaultSchedule([Fault(kind="unavailable", count=1000)])

        async def for_domain(self, domain_id: Any) -> Any:
            return FaultyStore(await inner.for_domain(domain_id), self._schedule)

        def admin(self) -> Any:
            return inner.admin()

    set_store_factory(_Unavailable())
    try:
        answer = await put_part(files_client, opened.json()["uploadId"], 1, b"abcdefgh")
    finally:
        set_store_factory(None)

    assert answer.status_code == 503, answer.text
    assert answer.json()["code"] == "files.store_unavailable"


async def test_a_part_without_its_checksum_header_is_422(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    opened = await open_session(files_client, root, declared_size=8)
    answer = await files_client.put(
        f"{UPLOADS}/{opened.json()['uploadId']}/parts/1",
        content=b"abcdefgh",
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert answer.status_code == 422
    assert answer.json()["code"] == "files.part_checksum_required"


# ---------------------------------------------------------------------------
# ``replace``: the wire behaviour a file over the single-call PUT cap needs.
# ---------------------------------------------------------------------------


async def _land(
    files_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    root: FileNode,
    payload: bytes,
    *,
    name: str = "big.bin",
    conflict: str | None = None,
    if_match: int | None = None,
) -> tuple[str, str]:
    """Open, send ``payload`` as one part, complete — answer the op and session."""
    opened = await open_session(files_client, root, name=name, declared_size=len(payload))
    assert opened.status_code == 201, opened.text
    upload_id = opened.json()["uploadId"]
    sent = await put_part(files_client, upload_id, 1, payload)
    assert sent.status_code == 200, sent.text

    body: dict[str, Any] = {
        "parts": [{"partNo": 1, "size": len(payload), "checksum": digest(payload)}]
    }
    if conflict is not None:
        body["conflictBehavior"] = conflict
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    if if_match is not None:
        headers["If-Match"] = f'"{if_match}"'
    finished = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete", json=body, headers=headers
    )
    assert finished.status_code == 202, finished.text
    return str(finished.json()["id"]), str(upload_id)


async def _named(real_session: AsyncSession, drive_id: Any, name: bytes) -> Any:
    """``(id, size, etag)`` of the live node under ``name``, read off the row.

    Deliberately not the ORM: the promote commits through a second repo on this
    same session, and an identity-mapped ``FileNode`` would answer the
    pre-commit values — a replaced node would look untouched whether or not it
    was. One statement, no cache.
    """
    return (
        await real_session.execute(
            text(
                "SELECT id, size, etag FROM file_nodes "
                "WHERE drive_id = :drive AND name = :name AND trashed_at IS NULL"
            ),
            {"drive": drive_id, "name": name},
        )
    ).one()


async def test_a_replace_commit_writes_a_version_onto_the_node_that_holds_the_name(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route admits ``replace`` and the commit lands a second version."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    drive_id = root.drive_id
    op_id, upload_id = await _land(files_client, real_session, files_org, root, b"aaaaaaaa")
    await promote(real_session, files_org, upload_id, op_id)
    before = await _named(real_session, drive_id, b"big.bin")
    node_id, etag = before.id, before.etag

    op_id, upload_id = await _land(
        files_client,
        real_session,
        files_org,
        root,
        b"bbbbbbbbbbbbbbbb",
        conflict="replace",
        if_match=etag,
    )
    await promote(real_session, files_org, upload_id, op_id)
    assert await operation_state(real_session, files_org, op_id) == "done"

    after = await _named(real_session, drive_id, b"big.bin")
    assert after.id == node_id, "replace adopted the node instead of making a second one"
    assert after.size == 16
    assert after.etag > etag


async def test_a_replace_agreed_against_a_stale_etag_never_lands(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node somebody else moved on is refused, not clobbered."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    drive_id = root.drive_id
    op_id, upload_id = await _land(files_client, real_session, files_org, root, b"aaaaaaaa")
    await promote(real_session, files_org, upload_id, op_id)
    before = await _named(real_session, drive_id, b"big.bin")
    seen = before.etag

    op_id, upload_id = await _land(
        files_client,
        real_session,
        files_org,
        root,
        b"bbbbbbbbbbbbbbbb",
        conflict="replace",
        if_match=seen - 1,
    )
    with pytest.raises(PreconditionFailed):
        await promote(real_session, files_org, upload_id, op_id)
    assert await operation_state(real_session, files_org, op_id) == "failed"

    after = await _named(real_session, drive_id, b"big.bin")
    assert after.size == 8, "the refused commit left the head version alone"


async def test_the_complete_body_still_refuses_a_behaviour_nobody_defined(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """Widening the literal to three values did not widen it to anything."""
    opened = await open_session(files_client, root, declared_size=8)
    upload_id = opened.json()["uploadId"]
    assert (await put_part(files_client, upload_id, 1, b"abcdefgh")).status_code == 200
    answer = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [{"partNo": 1, "size": 8, "checksum": digest(b"abcdefgh")}],
            "conflictBehavior": "clobber",
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert answer.status_code == 422, answer.text


class _Refusing:
    """A driver whose failures leave as the *base* ``StoreError``.

    Expired scoped credentials, a denied bucket and a bucket that was never
    created all normalize onto classes the library does not name one by one.
    Raising the base class is the strongest pin available: it proves the catch
    is on ``StoreError`` itself, not on a subclass somebody happened to list.
    """

    def __init__(self, inner: Any, *methods: str) -> None:
        self._inner = inner
        self._methods = frozenset(methods)

    def __getattr__(self, name: str) -> Any:
        if name in self._methods:

            async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
                from alkera_core.files.store.errors import StoreError

                raise StoreError(f"{name}: the credential this deployment holds expired")

            return _refuse
        return getattr(self._inner, name)


def _refusing_factory(inner: Any, *methods: str) -> Any:
    class _Factory:
        async def for_domain(self, domain_id: Any) -> Any:
            return _Refusing(await inner.for_domain(domain_id), *methods)

        def admin(self) -> Any:
            return inner.admin()

    return _Factory()


async def test_an_unmapped_store_refusal_on_a_part_is_a_typed_files_error(
    files_on: None,
    files_client: AsyncClient,
    root: FileNode,
    files_store: Path,
) -> None:
    """Only four ``StoreError`` subclasses were translated; every other one —
    expired credentials, a denied bucket, a bucket that is not there — escaped
    to the platform catch-all as a 500 in the wrong envelope."""
    from alkera_core.files.store.scoped import FilesystemScoped
    from backend.services.files.store import set_store_factory

    opened = await open_session(files_client, root, declared_size=8)
    inner = FilesystemScoped(files_store, clock=SystemClock())

    set_store_factory(_refusing_factory(inner, "put"))
    try:
        answer = await put_part(files_client, opened.json()["uploadId"], 1, b"abcdefgh")
    finally:
        set_store_factory(None)

    assert answer.status_code == 503, answer.text
    assert answer.json()["code"] == "files.store_unavailable"


async def test_a_replace_completion_without_if_match_is_428(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """``replace`` writes onto a node that already exists, so the caller has to
    name the version it believes it is replacing. Without the header the commit
    fenced against whatever the etag happened to be at promote time, which is a
    silent clobber of whoever wrote in between."""
    opened = await open_session(files_client, root, name="big.bin", declared_size=8)
    upload_id = opened.json()["uploadId"]
    assert (await put_part(files_client, upload_id, 1, b"aaaaaaaa")).status_code == 200

    answer = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [{"partNo": 1, "size": 8, "checksum": digest(b"aaaaaaaa")}],
            "conflictBehavior": "replace",
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert answer.status_code == 428, answer.text
    assert answer.json()["code"] == "files.if_match_required"


@pytest.mark.parametrize("conflict", ["fail", "rename"])
async def test_a_create_completion_stays_header_optional(
    files_on: None, files_client: AsyncClient, root: FileNode, conflict: str
) -> None:
    """The 428 is scoped to ``replace``: a completion that creates a node names
    no version, so demanding a precondition of it would be nonsense."""
    opened = await open_session(files_client, root, name="fresh.bin", declared_size=8)
    upload_id = opened.json()["uploadId"]
    assert (await put_part(files_client, upload_id, 1, b"aaaaaaaa")).status_code == 200

    answer = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [{"partNo": 1, "size": 8, "checksum": digest(b"aaaaaaaa")}],
            "conflictBehavior": conflict,
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert answer.status_code == 202, answer.text


class _RunAt:
    """A checkpoint seam that runs ``action`` the first time ``name`` is reached."""

    def __init__(self, name: str, action: Any) -> None:
        self._name = name
        self._action = action
        self._fired = False

    async def reach(self, name: str) -> None:
        if name == self._name and not self._fired:
            self._fired = True
            await self._action()

    def reach_sync(self, name: str) -> None:
        return None

    def as_hook(self) -> Any:
        return self.reach_sync

    @property
    def fired(self) -> bool:
        return self._fired


async def test_a_bump_between_the_completion_and_the_version_refuses_the_replace(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The precondition has to reach the write, not just the adoption.

    ``_adopt`` agrees the commit against the etag the caller saw, and commits.
    ``put_version`` then ran a moment later against whatever the etag was *by
    then* — so a writer who landed a version in that window was clobbered with
    no 412. The bump here happens exactly in that window.
    """
    from alkera_core.files.ids import OrgScope

    monkeypatch.setattr(settings, "files_inline_operations", False)
    drive_id = root.drive_id
    op_id, upload_id = await _land(files_client, real_session, files_org, root, b"aaaaaaaa")
    await promote(real_session, files_org, upload_id, op_id)
    before = await _named(real_session, drive_id, b"big.bin")

    op_id, upload_id = await _land(
        files_client,
        real_session,
        files_org,
        root,
        b"bbbbbbbbbbbbbbbb",
        conflict="replace",
        if_match=before.etag,
    )

    async def bump() -> None:
        """Somebody else lands a version while the parts are being committed."""
        await real_session.execute(
            text("UPDATE file_nodes SET etag = etag + 1 WHERE id = :id"), {"id": before.id}
        )
        await real_session.commit()

    checkpoints = _RunAt("uploads.before_promote_bytes", bump)
    repo = FilesRepo(real_session, OrgScope(org_team_id=files_org.org.org_id))
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id,
        org_id=files_org.org.org_id,
        email=files_org.org.admin_email,
    )
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None
    store = await store_factory(settings, clock=SystemClock()).for_domain(
        DomainId(drive.dedup_domain_id)
    )
    completion = UploadCompletion(
        repo, ctx, SystemClock(), store, part_size=TEST_PART_SIZE, checkpoints=checkpoints
    )

    with pytest.raises(PreconditionFailed):
        await completion.promote(SessionId(uuid.UUID(upload_id)), OperationId(uuid.UUID(op_id)))
    assert checkpoints.fired, "the window this test aims at was never entered"

    after = await _named(real_session, drive_id, b"big.bin")
    assert after.size == 8, "the refused commit left the concurrent writer's head alone"


# ---- the published ceilings (F-347 / F-213) --------------------------------


async def test_the_published_limits_are_the_deployment_s_own_settings(
    files_on: None, files_client: AsyncClient, root: FileNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every number in ``limits`` is read off the settings, not a constant.

    A self-hosted install that lowers a cap must publish the cap it enforces:
    the four fields move when the settings move, or the block is a promise the
    commit breaks.
    """
    import backend.api.routes.files.uploads as routes

    monkeypatch.setattr(settings, "files_max_file_bytes", 4096)
    monkeypatch.setattr(settings, "files_single_put_max_bytes", 512)
    monkeypatch.setattr(settings, "files_max_upload_parts", 7)
    monkeypatch.setattr(routes, "MAX_PROXIED_PART_BYTES", 64)
    opened = await open_session(files_client, root, declared_size=24)
    assert opened.status_code == 201
    assert opened.json()["limits"] == {
        "maxFileBytes": 4096,
        "singlePutMaxBytes": 512,
        "maxPartBytes": 64,
        "maxParts": 7,
    }


async def test_a_declared_size_over_the_file_cap_is_413_in_the_reader_s_own_words(
    files_on: None, files_client: AsyncClient, root: FileNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal lands at open, before the client has spent a transfer, and
    the message is what a surface shows a reader verbatim — the ceiling in the
    unit the product states it in, no byte count and no code in the sentence.
    The typed code is still there for a client that routes on it."""
    monkeypatch.setattr(settings, "files_max_file_bytes", 1_000_000_000_000)
    answer = await open_session(files_client, root, declared_size=1_000_000_000_001)
    assert answer.status_code == 413
    body = answer.json()
    assert body["code"] == "files.too_large"
    assert body["message"] == "exceeded the maximum upload size of 1000 GB"


async def test_a_declared_size_at_the_file_cap_is_admitted(
    files_on: None, files_client: AsyncClient, root: FileNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is inclusive: the published number is a size you may send."""
    monkeypatch.setattr(settings, "files_max_file_bytes", 24)
    answer = await open_session(files_client, root, declared_size=24)
    assert answer.status_code == 201


async def test_a_declared_size_needing_more_parts_than_the_cap_is_413(
    files_on: None, files_client: AsyncClient, root: FileNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``maxParts`` is enforced where it is knowable - at open, from the size.

    Refusing here is the whole point: the alternative is a session the client
    fills part by part until one lands outside the range and the transfer is
    wasted.
    """
    monkeypatch.setattr(settings, "files_max_upload_parts", 2)
    answer = await open_session(files_client, root, declared_size=24)
    assert answer.status_code == 413
    body = answer.json()
    assert body["code"] == "files.too_large"
    assert "3 parts" in body["message"] and "limit of 2" in body["message"]


async def test_a_part_beyond_the_session_s_parts_is_a_files_error_not_a_500(
    files_on: None, files_client: AsyncClient, root: FileNode
) -> None:
    """A part past the last one the session has is typed, never an unhandled 500."""
    opened = await open_session(files_client, root, declared_size=8)
    answer = await put_part(files_client, opened.json()["uploadId"], 9999, b"12345678")
    assert answer.status_code == 422
    assert answer.json()["code"] == "files.part_out_of_range"


async def test_the_part_cap_refusal_names_the_size_and_the_limit(
    files_on: None, files_client: AsyncClient, root: FileNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that reads the message learns what it may resend."""
    import backend.api.routes.files.uploads as routes

    monkeypatch.setattr(routes, "MAX_PROXIED_PART_BYTES", 4)
    opened = await open_session(files_client, root, declared_size=8)
    answer = await put_part(files_client, opened.json()["uploadId"], 1, b"abcdefgh")
    assert answer.status_code == 422
    message = answer.json()["message"]
    assert "8 bytes" in message and "at most 4" in message


# ---------------------------------------------------------------------------
# The commit on a platform with no directory fsync.
# ---------------------------------------------------------------------------


def _as_windows(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Both halves of what Windows does to a directory handle.

    Faking ``sys.platform`` alone is inert on a POSIX host — the guard would be
    taken but the call it guards would have succeeded anyway — so the real
    ``default_fsync_dir`` is driven with ``os.open`` refusing a directory the
    way Windows does, with ``EACCES``. The refusal is armed around THAT call
    only: a process-wide one also breaks the store's containment walk, which
    opens the store root for a reason that has nothing to do with durability,
    and the commit would then fail for a reason the platform never had.
    """
    real_open = os.open

    def refuse_a_directory(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, str | bytes | os.PathLike) and os.path.isdir(path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    flushed: list[Path] = []

    def fsync_dir_without_a_directory_handle(directory: Path) -> None:
        monkeypatch.setattr(os, "open", refuse_a_directory)
        try:
            default_fsync_dir(directory)
        finally:
            monkeypatch.setattr(os, "open", real_open)
        flushed.append(directory)

    monkeypatch.setattr(driver, "default_fsync_dir", fsync_dir_without_a_directory_handle)
    monkeypatch.setattr(sys, "platform", "win32")
    # The caller asserts this is not empty: a publish that stopped renaming
    # would take the durability step with it, and the test would pass proving
    # nothing.
    return flushed


async def test_an_upload_past_the_inline_ceiling_lands_without_a_directory_fsync(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session's bytes reach the node on a platform that cannot fsync a directory.

    Past the inline ceiling the commit stages the assembled stream as an object
    and *renames* it onto its content key, which is the only step in the path
    that flushes a directory entry. Where that flush cannot be taken the rename
    still has to stand: otherwise the node is adopted, the version never lands,
    and the file reads back as the zero bytes the adoption created it with.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    # The production trigger is a file past ``files_inline_max_bytes``; driving
    # the ceiling to zero puts a 24-byte upload on the same staged path.
    monkeypatch.setattr(settings, "files_inline_max_bytes", 0)
    payload = b"abcdefghijklmnopqrstuvwx"
    opened = await open_session(
        files_client, root, name="checkpoint.bin", declared_size=len(payload)
    )
    assert opened.status_code == 201, opened.text
    upload_id = opened.json()["uploadId"]

    parts = [payload[i : i + TEST_PART_SIZE] for i in range(0, len(payload), TEST_PART_SIZE)]
    for index, chunk in enumerate(parts, start=1):
        answer = await put_part(files_client, upload_id, index, chunk)
        assert answer.status_code == 200, answer.text

    finished = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [
                {"partNo": i, "size": len(c), "checksum": digest(c)}
                for i, c in enumerate(parts, start=1)
            ]
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert finished.status_code == 202, finished.text

    session_row = (
        await real_session.execute(
            select(FileUploadSession).where(FileUploadSession.id == uuid.UUID(upload_id))
        )
    ).scalar_one()
    assert session_row.bytes_received == len(payload), "every part was counted"

    flushed = _as_windows(monkeypatch)
    await promote(real_session, files_org, upload_id, finished.json()["id"])
    assert flushed, "the commit never reached the directory flush the platform cannot take"

    node = (
        await real_session.execute(
            select(FileNode).where(
                FileNode.drive_id == root.drive_id, FileNode.name == b"checkpoint.bin"
            )
        )
    ).scalar_one()
    assert node.size == len(payload), "the staged object was published, not lost"


# ---------------------------------------------------------------------------
# An upload under a folder lease: two halves, two principals
# ---------------------------------------------------------------------------

#: The platform's own principal id, as ``worker.files_bootstrap`` spells it.
#: Repeated rather than imported because the backend never imports the worker
#: package; what the fence sees is a SERVICE principal that holds nothing,
#: which is what every deployment with a worker promotes as.
JANITOR_PRINCIPAL_ID = "00000000-0000-4000-8000-0000000f11e5"

LEASE_INSTANCE = "instance-a"


async def _lease_folder(
    client: AsyncClient, session: AsyncSession, folder: FileNode
) -> dict[str, str]:
    """Take ``folder``'s lease through the route; return the fencing headers."""
    etag = (
        await session.execute(select(FileNode.etag).where(FileNode.id == folder.id))
    ).scalar_one()
    granted = await client.post(
        f"{PREFIX}/drives/{folder.drive_id}/items/{folder.id}/lease",
        json={"instanceId": LEASE_INSTANCE, "machineId": "laptop-a", "purpose": "mount"},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(etag)},
    )
    assert granted.status_code in {200, 201}, granted.text
    return {
        "X-Alkera-Lease-Epoch": str(granted.json()["epoch"]),
        "X-Alkera-Lease-Instance": LEASE_INSTANCE,
    }


async def _promote_as_the_worker(
    session: AsyncSession, org: FilesOrgFixture, upload_id: str, op_id: str
) -> None:
    """``promote`` exactly as ``worker.tasks.files`` runs it: the same library
    entry point, on the platform's own context rather than the holder's."""
    repo = FilesRepo(session, OrgScope(org_team_id=org.org.org_id))
    ctx = ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.SERVICE,
            id=JANITOR_PRINCIPAL_ID,
            org_id=org.org.org_id,
            label="files-janitor",
            credential=None,
        )
    )
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None
    store = await store_factory(settings, clock=SystemClock()).for_domain(
        DomainId(drive.dedup_domain_id)
    )
    completion = UploadCompletion(repo, ctx, SystemClock(), store, part_size=TEST_PART_SIZE)
    await completion.promote(SessionId(uuid.UUID(upload_id)), OperationId(uuid.UUID(op_id)))


async def _upload_under_lease(
    client: AsyncClient, folder: FileNode, fence: dict[str, str], *, name: str, payload: bytes
) -> tuple[str, str]:
    """Open, part and complete one upload carrying ``fence``; return the ids."""
    opened = await client.post(
        UPLOADS,
        json={"declaredSize": len(payload), "name": name, "parentId": str(folder.id)},
        headers={"Idempotency-Key": uuid.uuid4().hex, **fence},
    )
    assert opened.status_code == 201, opened.text
    upload_id: str = opened.json()["uploadId"]
    parts = [payload[i : i + TEST_PART_SIZE] for i in range(0, len(payload), TEST_PART_SIZE)]
    for index, chunk in enumerate(parts, start=1):
        answered = await put_part(client, upload_id, index, chunk)
        assert answered.status_code == 200, answered.text
    finished = await client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={
            "parts": [
                {"partNo": i, "size": len(c), "checksum": digest(c)}
                for i, c in enumerate(parts, start=1)
            ]
        },
        headers={"Idempotency-Key": uuid.uuid4().hex, **fence},
    )
    assert finished.status_code == 202, finished.text
    return upload_id, str(finished.json()["id"])


async def test_the_holders_upload_is_promoted_by_a_worker_that_holds_nothing(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The open is fenced in the holder's request; the commit is not their call.

    In a deployment with a worker the two halves of an upload run as two
    different principals: the holder's browser or daemon opens the session
    under its lease headers, and the Temporal worker promotes it later as the
    platform. A fence that asked who is promoting would refuse the holder their
    own file — the bytes staged, the operation stuck, nothing ever landing.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    fence = await _lease_folder(files_client, real_session, root)
    payload = b"a-held-folder-upload-abc"

    upload_id, op_id = await _upload_under_lease(
        files_client, root, fence, name="held.txt", payload=payload
    )
    await _promote_as_the_worker(real_session, files_org, upload_id, op_id)

    assert await operation_state(real_session, files_org, op_id) == "done"
    node = (
        await real_session.execute(
            select(FileNode).where(FileNode.drive_id == root.drive_id, FileNode.name == b"held.txt")
        )
    ).scalar_one()
    assert node.size == len(payload)


async def test_a_worker_promote_is_refused_once_the_folder_has_changed_hands(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carrying the holder past the request is not dropping the fence.

    Same worker, same session — but the folder was handed to a second machine
    while the parts were in flight, so the epoch the session recorded is no
    longer the live one. The commit is refused and the folder never grows the
    node, which is exactly what a fence that had merely stopped comparing would
    fail to do.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    # Read off the handle before the refusal: the fenced promote rolls its
    # session back, which expires every instance the fixture is holding, and a
    # lazy re-load under asyncio is an error rather than a query.
    drive_id = root.drive_id
    fence = await _lease_folder(files_client, real_session, root)
    upload_id, op_id = await _upload_under_lease(
        files_client, root, fence, name="handed.txt", payload=b"handed-on-bytes-abcd"
    )

    etag = (
        await real_session.execute(select(FileNode.etag).where(FileNode.id == root.id))
    ).scalar_one()
    released = await files_client.post(
        f"{PREFIX}/drives/{root.drive_id}/items/{root.id}/lease/release",
        json={
            "epoch": int(fence["X-Alkera-Lease-Epoch"]),
            "instanceId": LEASE_INSTANCE,
            "final": None,
        },
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(etag)},
    )
    assert released.status_code == 200, released.text
    again = await _lease_folder(files_client, real_session, root)
    assert int(again["X-Alkera-Lease-Epoch"]) > int(fence["X-Alkera-Lease-Epoch"])

    with pytest.raises(LeaseConflict) as refused:
        await _promote_as_the_worker(real_session, files_org, upload_id, op_id)
    assert refused.value.code == "files.lease_fenced"

    assert (
        await real_session.execute(
            select(FileNode.id).where(FileNode.drive_id == drive_id, FileNode.name == b"handed.txt")
        )
    ).scalar_one_or_none() is None


async def test_sixty_sessions_opened_at_once_all_succeed(
    files_on: None, files_client: AsyncClient, root: FileNode, real_session: AsyncSession
) -> None:
    """RED before the lock order was fixed.

    A browser uploading a folder opens every session at once, and each one
    inserted its row — taking ``FOR KEY SHARE`` on the drive for the foreign
    key — before upgrading that same row to ``FOR UPDATE`` for its quota hold.
    Two openers each held the share the other needed, Postgres killed one as a
    deadlock, and the route carried no replay ladder to answer it, so the file
    was dropped on the floor with a 500 nobody could retry.

    In flight at once is capped the way a browser caps its own connections: a
    request holds a database connection for its whole life, so putting all sixty
    on the wire at once exhausts the pool and the test would measure the pool's
    ceiling instead of this route's.
    """
    at_once = asyncio.Semaphore(12)

    async def opened(index: int) -> Any:
        async with at_once:
            return await open_session(files_client, root, name=f"bulk-{index:03d}.txt")

    answers = await asyncio.gather(*(opened(i) for i in range(60)))

    faults = [a for a in answers if a.status_code >= 500]
    assert not faults, [(a.status_code, a.text) for a in faults]
    assert [a.status_code for a in answers] == [201] * 60
    ids = {a.json()["uploadId"] for a in answers}
    assert len(ids) == 60, "two openers were handed the same session"
    rows = await real_session.execute(
        select(FileUploadSession.id).where(FileUploadSession.parent_id == root.id)
    )
    assert len(rows.scalars().all()) == 60, "a session answered 201 with no row behind it"


async def test_a_deadlocked_open_is_replayed_rather_than_answered_500(
    files_on: None,
    files_client: AsyncClient,
    root: FileNode,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ladder itself, on a transaction Postgres really aborted.

    The lock order is what stops the collision happening at all; this pins what
    the route does when Postgres rules against it anyway — the losing attempt is
    replayed under the same key rather than reaching the client as a 500, and
    the key is spent exactly once.
    """
    import alkera_core.files.uploads as library
    from asyncpg.exceptions import DeadlockDetectedError
    from sqlalchemy.exc import DBAPIError

    real_open = library.UploadService.open
    calls = 0

    async def fails_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            return await real_open(self, *args, **kwargs)
        try:
            await self._repo.session.execute(text("SELECT 1 / 0"))
        except DBAPIError:
            pass
        raise DBAPIError(
            "INSERT INTO file_upload_sessions", {}, DeadlockDetectedError("deadlock detected")
        )

    monkeypatch.setattr(library.UploadService, "open", fails_once)
    key = uuid.uuid4().hex

    answer = await open_session(files_client, root, headers={"Idempotency-Key": key})

    assert answer.status_code == 201, answer.text
    assert calls == 2, "the losing attempt was not replayed"
    spent = (
        await real_session.execute(
            text("SELECT count(*) FROM file_idempotency_keys WHERE key = :key"), {"key": key}
        )
    ).scalar_one()
    assert int(spent) == 1, f"the key was spent {spent} times, not once"


# ---------------------------------------------------------------------------
# What the poll learns: a queued commit is answered 202, so the operation is
# the only thing that can name what the bytes became.
# ---------------------------------------------------------------------------


async def _operation(files_client: AsyncClient, drive_id: Any, op_id: str) -> dict[str, Any]:
    answer = await files_client.get(f"{PREFIX}/drives/{drive_id}/operations/{op_id}")
    assert answer.status_code == 200, answer.text
    body: dict[str, Any] = answer.json()
    return body


async def test_a_committed_upload_names_the_node_and_version_it_created(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this the client cannot stamp the laptop's mtime on what it sent."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    op_id, upload_id = await _land(files_client, real_session, files_org, root, b"abcdefgh")
    await promote(real_session, files_org, upload_id, op_id)

    body = await _operation(files_client, root.drive_id, op_id)
    landed = await _named(real_session, root.drive_id, b"big.bin")
    assert body["state"] == "done"
    assert body["resultNodeId"] == str(landed.id)
    assert body["resultUnchanged"] is False
    version = (
        await real_session.execute(
            text("SELECT id FROM file_versions WHERE node_id = :node ORDER BY seq DESC LIMIT 1"),
            {"node": landed.id},
        )
    ).scalar_one()
    assert body["resultVersionId"] == str(version)


async def test_a_replace_of_the_same_bytes_says_the_drive_already_held_them(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count of files a drop did not have to store is read off the poll."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    payload = b"identical"
    op_id, upload_id = await _land(files_client, real_session, files_org, root, payload)
    await promote(real_session, files_org, upload_id, op_id)
    first = await _named(real_session, root.drive_id, b"big.bin")

    op_id, upload_id = await _land(
        files_client,
        real_session,
        files_org,
        root,
        payload,
        conflict="replace",
        if_match=first.etag,
    )
    await promote(real_session, files_org, upload_id, op_id)

    body = await _operation(files_client, root.drive_id, op_id)
    assert body["resultNodeId"] == str(first.id)
    assert body["resultUnchanged"] is True


async def test_a_taken_name_fails_the_commit_with_the_code_a_client_branches_on(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collision decided inside the commit has to arrive as a code.

    The prose is the library's and may be reworded; the client that asks the
    person "keep both or replace?" branches on ``files.exists`` and on nothing
    else, so a stringified exception whose ``code`` is the sentence is a
    collision the tray cannot recognise.

    Both completions are accepted before either commit runs: a name already
    taken is refused on the completion itself, so the collision the commit has
    to decide is the race between two uploads of one name.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    # The refused commit rolls the session back, which expires the ORM row.
    drive_id = root.drive_id
    first_op, first_upload = await _land(files_client, real_session, files_org, root, b"first")
    op_id, upload_id = await _land(files_client, real_session, files_org, root, b"second")
    await promote(real_session, files_org, first_upload, first_op)

    with pytest.raises(Conflict):
        await promote(real_session, files_org, upload_id, op_id)

    body = await _operation(files_client, drive_id, op_id)
    assert body["state"] == "failed"
    assert [row["code"] for row in body["errors"]] == ["files.exists"]
    assert body["errors"][0]["message"] != "files.exists", "the prose was dropped"
    assert body["resultNodeId"] is None


@pytest.mark.parametrize(
    "taken", [pytest.param(True, id="name-taken"), pytest.param(False, id="name-free")]
)
async def test_a_completion_onto_a_taken_name_is_refused_on_the_call_and_keeps_the_bytes(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
    taken: bool,
) -> None:
    """ "Keep both" is a second completion of the bytes already sent.

    Refused inside the queued commit, the collision released the session and
    the person's answer had to send the whole file again from 0%. Refused on
    the completion itself, the session is still ``uploading`` with its part,
    and completing it again with ``rename`` lands the file beside the other
    one — no part sent twice.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    if taken:
        first_op, first_upload = await _land(files_client, real_session, files_org, root, b"one")
        await promote(real_session, files_org, first_upload, first_op)

    payload = b"second!"
    opened = await open_session(files_client, root, name="big.bin", declared_size=len(payload))
    assert opened.status_code == 201, opened.text
    upload_id = opened.json()["uploadId"]
    assert (await put_part(files_client, upload_id, 1, payload)).status_code == 200
    parts = [{"partNo": 1, "size": len(payload), "checksum": digest(payload)}]

    answer = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={"parts": parts},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    if not taken:
        assert answer.status_code == 202, answer.text
        return
    assert answer.status_code == 409, answer.text
    assert answer.json()["code"] == "files.exists"

    kept = await files_client.post(
        f"{UPLOADS}/{upload_id}/complete",
        json={"parts": parts, "conflictBehavior": "rename"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert kept.status_code == 202, kept.text
    op_id = str(kept.json()["id"])
    await promote(real_session, files_org, upload_id, op_id)
    body = await _operation(files_client, root.drive_id, op_id)
    assert body["state"] == "done", body
    names = (
        (
            await real_session.execute(
                text(
                    "SELECT name_display FROM file_nodes "
                    "WHERE parent_id = :p AND trashed_at IS NULL"
                ),
                {"p": root.id},
            )
        )
        .scalars()
        .all()
    )
    await real_session.commit()
    assert sorted(names) == ["big (1).bin", "big.bin"]


async def test_a_commit_that_fails_for_no_named_reason_says_so_without_its_words(
    files_on: None,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    root: FileNode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untyped failure names no row, no path and no driver internals."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    drive_id = root.drive_id
    op_id, upload_id = await _land(files_client, real_session, files_org, root, b"payload!")

    import alkera_core.files.uploads as library

    async def blow_up(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"store /var/lib/alkera/{drive_id} is on fire")

    monkeypatch.setattr(library.UploadCompletion, "_land", blow_up)
    with pytest.raises(RuntimeError):
        await promote(real_session, files_org, upload_id, op_id)

    body = await _operation(files_client, drive_id, op_id)
    assert body["state"] == "failed"
    assert [row["code"] for row in body["errors"]] == ["files.operation_failed"]
    assert "/var/lib/alkera" not in body["errors"][0]["message"]
