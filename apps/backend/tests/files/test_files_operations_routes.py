"""The operations family through the real app.

Every case here goes through ``httpx.AsyncClient`` + ``ASGITransport`` against
real Postgres, because the contract being tested is the HTTP one: the status,
the body, and — for the "not yours" cases — the fact that three quite different
situations are indistinguishable from outside.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable

import pytest
import pytest_asyncio
from _files_kit import (
    NOT_FOUND,
    FilesFixtures,
    FilesOrgFixture,
    drive_root_etag,
    node_etag,
    refusal,
)
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.clock import SystemClock
from alkera_core.files.ops import Operations, OperationState, Progress
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.usefixtures("files_on")


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


@pytest_asyncio.fixture
async def ops(fx: FilesFixtures, files_org: FilesOrgFixture) -> Operations:
    await fx.drive()
    return Operations(fx.repo, _ctx(files_org.org), SystemClock())


async def _start(ops_handle: Operations, fx: FilesFixtures, kind: str = "move") -> OperationState:
    drive = await fx.drive()
    state = await ops_handle.start(kind, drive_id=drive.id, total=3)
    await fx._session.commit()
    return state


def _url(fx: FilesFixtures, drive_id: uuid.UUID, op_id: uuid.UUID, suffix: str = "") -> str:
    return f"/api/v1/files/drives/{drive_id}/operations/{op_id}{suffix}"


async def _commanding(
    session: AsyncSession, drive: FileDrive, key: str | None = None
) -> dict[str, str]:
    """The headers a command on an operation carries.

    An operation is not a node, so the precondition it names is the drive it is
    working in: the root's etag. Every mutation sends one — there is no method
    the header is optional on — so a client that has an etag at all can send it
    here too.
    """
    return {
        "Idempotency-Key": key or uuid.uuid4().hex,
        "If-Match": await drive_root_etag(session, drive),
    }


@pytest.mark.asyncio
async def test_get_renders_the_operation_wire_shape(
    files_client: AsyncClient, fx: FilesFixtures, ops: Operations
) -> None:
    drive = await fx.drive()
    state = await _start(ops, fx)
    response = await files_client.get(_url(fx, drive.id, state.id))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(state.id)
    assert body["driveId"] == str(drive.id)
    assert body["kind"] == "move"
    assert body["state"] == "queued"
    assert body["total"] == 3
    assert body["done"] == 0
    assert body["errors"] == []
    assert body["conflicts"] == []
    assert body["resultUrl"] is None
    assert body["cancelRequested"] is False


@pytest.mark.asyncio
async def test_progress_is_observable_from_a_second_session(
    files_client: AsyncClient, fx: FilesFixtures, ops: Operations, real_session: AsyncSession
) -> None:
    """The runner writes progress in its own session; the route reads it in
    another. Without the heartbeat write, ``done`` would stay at zero here."""
    drive = await fx.drive()
    state = await _start(ops, fx)
    assert await ops._transition(state.id, "queued", "running", heartbeat=True)
    progress = Progress(ops, state.id, every=1)
    await progress.tick(7)
    await real_session.commit()

    body = (await files_client.get(_url(fx, drive.id, state.id))).json()
    assert body["state"] == "running"
    assert body["done"] == 7


@pytest.mark.asyncio
async def test_cancelling_a_running_operation_is_observed_as_cancelled(
    files_client: AsyncClient,
    fx: FilesFixtures,
    ops: Operations,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """A running operation is asked, not killed: the body stops at its next
    poll and the route then reports ``cancelled`` — the state a client waits
    for, not merely the request flag.

    The runner works on a session of its own, as a worker does. An
    ``AsyncSession`` admits one task at a time: a runner driven on the test's
    setup session shares that session with the fixture that closes it, so any
    exit that left the runner's task behind — a failed assertion, a poll that
    gave up — had two tasks on one connection, which is a wedge nothing times
    out. The runner is also settled on every exit for the same reason.
    """
    drive = await fx.drive()
    state = await _start(ops, fx)
    headers = await _commanding(real_session, drive)
    # End the etag read's transaction: the setup session holds nothing while
    # the runner and the route work.
    await real_session.commit()

    async def body(progress: Progress) -> str:
        for _ in range(200):
            await progress.stop_if_cancelled()
            await asyncio.sleep(0.02)
        return "ran to completion"

    async with AsyncSessionLocal() as runner_session:
        runner_ops = Operations(
            FilesRepo(runner_session, fx.repo.scope), _ctx(files_org.org), SystemClock()
        )
        runner = asyncio.create_task(runner_ops.run(state.id, body))
        try:
            # Cancel only once the route can see the operation running:
            # cancelling it while still queued closes it outright, which is
            # the other test's case.
            for _ in range(500):
                polled = (await files_client.get(_url(fx, drive.id, state.id))).json()
                if polled["state"] == "running":
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("the runner never reported the operation as running")
            cancelled = await files_client.post(
                _url(fx, drive.id, state.id, "/cancel"), headers=headers
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["cancelRequested"] is True
            assert await runner is None
        finally:
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

    assert (await files_client.get(_url(fx, drive.id, state.id))).json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelling_a_settled_operation_is_a_conflict(
    files_client: AsyncClient, fx: FilesFixtures, ops: Operations, real_session: AsyncSession
) -> None:
    drive = await fx.drive()
    state = await _start(ops, fx)
    assert await ops._transition(state.id, "queued", "running", heartbeat=True)
    assert await ops._transition(state.id, "running", "done")
    await real_session.commit()

    response = await files_client.post(
        _url(fx, drive.id, state.id, "/cancel"),
        headers=await _commanding(real_session, drive),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "files.operation_settled"


@pytest.mark.asyncio
async def test_a_queued_operation_cancels_outright(
    files_client: AsyncClient,
    fx: FilesFixtures,
    ops: Operations,
    real_session: AsyncSession,
) -> None:
    drive = await fx.drive()
    state = await _start(ops, fx)
    response = await files_client.post(
        _url(fx, drive.id, state.id, "/cancel"),
        headers=await _commanding(real_session, drive),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


@pytest.mark.parametrize("suffix", ["/cancel", "/undo"], ids=["cancel", "undo"])
@pytest.mark.asyncio
async def test_a_non_get_without_an_idempotency_key_is_428(
    files_client: AsyncClient, fx: FilesFixtures, ops: Operations, suffix: str
) -> None:
    drive = await fx.drive()
    state = await _start(ops, fx)
    response = await files_client.post(_url(fx, drive.id, state.id, suffix))
    assert response.status_code == 428


@pytest.mark.parametrize("suffix", ["/cancel", "/undo"], ids=["cancel", "undo"])
@pytest.mark.asyncio
async def test_a_command_without_an_if_match_is_428(
    files_client: AsyncClient,
    fx: FilesFixtures,
    ops: Operations,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    suffix: str,
) -> None:
    """A command on an operation is a mutation like any other: with a key but
    no ``If-Match`` it is refused, and the operation is left queued."""
    drive = await fx.drive()
    state = await _start(ops, fx)
    response = await files_client.post(_url(fx, drive.id, state.id, suffix), headers=idem())
    assert response.status_code == 428, response.text
    assert response.json()["code"] == "files.if_match_required"
    still = (await files_client.get(_url(fx, drive.id, state.id))).json()
    assert still["state"] == "queued"
    assert still["cancelRequested"] is False


@pytest.mark.asyncio
async def test_undo_without_an_inverse_is_a_conflict(
    files_client: AsyncClient,
    fx: FilesFixtures,
    ops: Operations,
    real_session: AsyncSession,
) -> None:
    drive = await fx.drive()
    state = await _start(ops, fx)
    assert await ops._transition(state.id, "queued", "running", heartbeat=True)
    assert await ops._transition(state.id, "running", "done")
    await real_session.commit()

    response = await files_client.post(
        _url(fx, drive.id, state.id, "/undo"),
        headers=await _commanding(real_session, drive),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "files.not_undoable"


@pytest.mark.asyncio
async def test_undo_answers_a_new_forward_operation(
    files_client: AsyncClient,
    fx: FilesFixtures,
    ops: Operations,
    real_session: AsyncSession,
) -> None:
    """Undo is not a rewind: the answer is a different operation, of kind
    ``undo``, which is itself a row a later undo can act on."""
    from alkera_core.files.ops import set_attrs

    drive = await fx.drive()
    node = await fx.node(b"attrs.txt", mode=0o100644)
    async with ops.perform("move", drive_id=drive.id) as performed:
        async with fx.repo.transaction():
            await set_attrs(fx.repo, ops._ctx, node.id, {"mode": 0o100600}, op=ops)
    await real_session.commit()

    response = await files_client.post(
        _url(fx, drive.id, performed.id, "/undo"),
        headers=await _commanding(real_session, drive),
    )
    assert response.status_code == 202
    body = response.json()
    assert body["id"] != str(performed.id)
    assert body["kind"] == "undo"
    assert body["state"] == "done"

    await real_session.refresh(node)
    assert node.mode == 0o100644


@pytest.mark.asyncio
async def test_the_three_not_yours_classes_are_indistinguishable(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: FilesFixtures,
    ops: Operations,
    real_session: AsyncSession,
) -> None:
    """A never-minted id, an id minted in another org, and one this caller may
    not read must answer the same 404 with the same body."""
    drive = await fx.drive()
    org_row, admin_row = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.local",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    other = OrgWithAdmin(
        org_id=org_row.id,
        admin_id=admin_row.id,
        admin_email=admin_row.email,
        admin_password="other-pass-12345",
    )
    other_fx = FilesFixtures(real_session, other.org_id, other.admin_id)
    other_drive = await other_fx.drive()
    other_ops = Operations(other_fx.repo, _ctx(other), ops._clock)
    foreign = await other_ops.start("move", drive_id=other_drive.id)
    await real_session.commit()

    nonexistent = await files_client.get(_url(fx, drive.id, uuid.uuid4()))
    other_org = await files_client.get(_url(fx, drive.id, foreign.id))
    # The same id, asked for under the other org's own drive: the drive is not
    # this caller's either, so the drive check answers before the operation.
    other_drive_probe = await files_client.get(_url(fx, other_drive.id, foreign.id))

    for response in (nonexistent, other_org, other_drive_probe):
        assert response.status_code == 404
        assert refusal(response) == NOT_FOUND


@pytest.mark.asyncio
async def test_a_download_names_the_bytes_its_archive_will_hold(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """``bytes`` is the size of the work, not a placeholder.

    A download settles its whole walk before answering, so its count is final
    the moment the operation is readable — which makes it the one family whose
    bytes can be asserted without racing a runner.
    """
    drive = await fx.drive()
    folder = await fx.shared()
    first = await fx.node(b"one.txt", parent=folder, size=4096)
    await fx.version(first, size_bytes=4096)
    second = await fx.node(b"two.txt", parent=folder, size=1234)
    await fx.version(second, size_bytes=1234)

    started = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{folder.id}/download",
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "If-Match": await node_etag(real_session, folder.id),
        },
    )
    assert started.status_code == 202, started.text
    assert started.json()["bytes"] == 4096 + 1234

    polled = await files_client.get(_url(fx, drive.id, uuid.UUID(started.json()["id"])))
    assert polled.status_code == 200
    assert polled.json()["bytes"] == 4096 + 1234


@pytest.mark.asyncio
async def test_the_wire_reports_the_bytes_the_operation_recorded(
    files_client: AsyncClient, fx: FilesFixtures, ops: Operations, real_session: AsyncSession
) -> None:
    """Every family records its bytes in the same place the download does.

    Written here through the progress column the runner beats on, so the case
    stands for a copy or a move counting bytes as it goes, not only for the one
    family that settles its count up front.
    """
    drive = await fx.drive()
    state = await _start(ops, fx)
    await real_session.execute(
        text("UPDATE file_ops SET progress = progress || :recorded WHERE id = :id"),
        {"id": state.id, "recorded": '{"bytes": 9001}'},
    )
    await real_session.commit()

    body = (await files_client.get(_url(fx, drive.id, state.id))).json()
    assert body["bytes"] == 9001


@pytest.mark.asyncio
async def test_the_wire_says_whether_an_undo_would_be_accepted(
    files_client: AsyncClient,
    fx: FilesFixtures,
    ops: Operations,
    real_session: AsyncSession,
) -> None:
    """``undoable`` is the fact the undo route enforces, published.

    A client that guessed from the kind offered an undo for a copy — an
    operation that records no inverse — and the press did nothing. So the flag
    and the route's own answer are pinned to each other here."""
    from alkera_core.files.ops import set_attrs

    drive = await fx.drive()

    # An operation that put nothing on record to reverse.
    bare = await _start(ops, fx)
    assert await ops._transition(bare.id, "queued", "running", heartbeat=True)
    assert await ops._transition(bare.id, "running", "done")
    await real_session.commit()
    assert (await files_client.get(_url(fx, drive.id, bare.id))).json()["undoable"] is False
    refused = await files_client.post(
        _url(fx, drive.id, bare.id, "/undo"),
        headers=await _commanding(real_session, drive),
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "files.not_undoable"

    # One that recorded an inverse.
    node = await fx.node(b"undoable.txt", mode=0o100644)
    async with ops.perform("move", drive_id=drive.id) as performed:
        async with fx.repo.transaction():
            await set_attrs(fx.repo, ops._ctx, node.id, {"mode": 0o100600}, op=ops)
    await real_session.commit()
    assert (await files_client.get(_url(fx, drive.id, performed.id))).json()["undoable"] is True
    accepted = await files_client.post(
        _url(fx, drive.id, performed.id, "/undo"),
        headers=await _commanding(real_session, drive),
    )
    assert accepted.status_code == 202
    assert accepted.json()["undoable"] is True
