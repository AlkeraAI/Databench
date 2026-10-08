"""``GET /drives`` names the caller's OWN home, never the ``/home`` container.

``/home`` is a traversal-only container holding every member's home folder, and
an org admin sits at ``manager`` on every node of her org — so a client that
takes the root's ``home`` child for "my files" lands her on a screen listing her
colleagues' private folders, named after their addresses. The drive therefore
answers ``homeId``, and these tests pin what it points at: the caller's own
folder, distinct from the container and from anybody else's home.

The founder case is the one that makes it a route concern rather than a client
one: an org's first member never goes through ``add_member``, so her home has
never been written when she opens Files, and a route that only *looked* one up
would answer ``null`` for the very person who created the org.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from alkera_core.authz.principal import ActingContext
from alkera_core.files import acl_intern, drives
from alkera_core.files.authz.defaults import ORG_POLICY_RESTRICTED, DriveFolder, default_acl
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from alkera_core.temporal import QUEUE_FOR, WorkflowType
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


async def _home_container(fx: FilesFixtures) -> FileNode:
    """The org-wide ``/home`` container the root listing advertises."""
    drive = await fx.drive()
    async with fx.repo.transaction():
        root = await fx.repo.node(drive.root_node_id)
        assert root is not None
        found = [child for child in await fx.repo.siblings(root.id) if bytes(child.name) == b"home"]
    assert len(found) == 1
    return found[0]


async def _give_member_a_home(
    fx: FilesFixtures, files_org: FilesOrgFixture, session: AsyncSession
) -> FileNode:
    """The second member's home, written the way joining an org writes it."""
    member = files_org.member
    ctx = ActingContext.for_user(user_id=member.id, org_id=fx.org_team_id, email=member.email)
    await fx.drive()
    async with fx.repo.transaction():
        home = await drives.ensure_home_folder(fx.repo, ctx, member.id)
    await session.commit()
    return home


async def test_the_drive_names_the_callers_own_home_not_the_home_container(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The admin's ``homeId`` is her own folder — not ``/home``, not a teammate's."""
    drive = await fx.drive()
    container = await _home_container(fx)
    teammate_home = await _give_member_a_home(fx, files_org, real_session)

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    home_id = body["homeId"]

    assert home_id is not None
    assert home_id != str(container.id), "Home is the org-wide container, not the caller's folder"
    assert home_id != str(drive.root_node_id)
    assert home_id != str(teammate_home.id)

    item = await files_client.get(f"{BASE}/drives/{body['id']}/items/{home_id}")
    assert item.status_code == 200, item.text
    node = item.json()
    # A child of the container, which is what "the caller's own folder" means here.
    assert node["parentId"] == str(container.id)
    # The folder is the admin's own: Files stores a home's owner as the member it
    # was made for, whoever wrote the row.
    assert node["attrs"]["owner"] == str(fx.actor_id)


async def test_the_org_founder_gets_a_home_on_her_first_files_request(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """She joined no org, so nothing ever wrote her home — the drive read does."""
    container = await _home_container(fx)
    await _give_member_a_home(fx, files_org, real_session)

    async with fx.repo.transaction():
        before = await drives.find_home_folder(fx.repo, fx.actor_id)
    assert before is None, "the founder is not supposed to have a home yet"

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    home_id = response.json()["homeId"]
    assert home_id is not None

    async with fx.repo.transaction():
        after = await drives.find_home_folder(fx.repo, fx.actor_id)
        children = await fx.repo.siblings(container.id)
    assert after is not None
    assert str(after.id) == home_id
    assert after.parent_id == container.id
    # Ensured, not duplicated: a second read finds the same folder rather than
    # writing a second one beside it.
    again = await files_client.get(f"{BASE}/drives")
    assert again.json()["homeId"] == home_id
    assert len(children) == 2, [bytes(child.name) for child in children]


async def test_a_drive_read_costs_no_second_home(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    """Two founders' visits leave one folder under ``/home``, not two."""
    container = await _home_container(fx)
    first = await files_client.get(f"{BASE}/drives")
    second = await files_client.get(f"{BASE}/drives")
    assert first.json()["homeId"] == second.json()["homeId"]

    async with fx.repo.transaction():
        children = await fx.repo.siblings(container.id)
    assert [uuid.UUID(str(child.id)) for child in children] == [uuid.UUID(first.json()["homeId"])]


async def _receipt_in_the_container(fx: FilesFixtures, container: FileNode) -> FileNode:
    """A file the founder uploaded straight into ``/home``.

    The write routes never refused a file there — an org admin sits at
    ``manager`` on the container by descent — and before the drive named her
    home, ``/home`` was where the Files page landed her, so it is where her
    uploads went. The row is hers by ``created_by``, which is the one fact a
    home is found by.
    """
    return await fx.node(
        b"Receipt-2505-0695 (1).pdf", kind="file", parent=container, created_by=fx.actor_id
    )


@pytest.mark.parametrize(
    "home_written_first",
    [
        pytest.param(False, id="founder-has-no-home-yet"),
        pytest.param(True, id="founder-home-exists-beside-the-receipt"),
    ],
)
async def test_a_file_the_founder_dropped_into_the_container_is_not_her_home(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    home_written_first: bool,
) -> None:
    """``homeId`` is her FOLDER, never a file she happens to have created under ``/home``.

    The live shape: the founder's ``homeId`` came back as a receipt she had
    uploaded into the container, so Files opened a PDF as a folder — its name in
    the trail over "0 of 0 shown". Whether her folder is already there or the
    drive read writes it on this visit, the receipt is skipped and her home is
    the folder that is hers.
    """
    container = await _home_container(fx)
    receipt = await _receipt_in_the_container(fx, container)
    teammate_home = await _give_member_a_home(fx, files_org, real_session)
    written: FileNode | None = None
    if home_written_first:
        ctx = ActingContext.for_user(
            user_id=fx.actor_id, org_id=fx.org_team_id, email=files_org.org.admin_email
        )
        async with fx.repo.transaction():
            written = await drives.ensure_home_folder(fx.repo, ctx, fx.actor_id)
        await real_session.commit()

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    home_id = body["homeId"]

    assert home_id is not None
    assert home_id != str(receipt.id), "the drive took the founder's receipt for her home"
    assert home_id != str(teammate_home.id)
    if written is not None:
        assert home_id == str(written.id)

    item = await files_client.get(f"{BASE}/drives/{body['id']}/items/{home_id}")
    assert item.status_code == 200, item.text
    node = item.json()
    assert node["kind"] == "folder"
    assert node["parentId"] == str(container.id)
    assert node["attrs"]["owner"] == str(fx.actor_id)

    # One folder of hers under the container, whichever visit wrote it — and the
    # receipt is no longer beside it: it is inside it, where she will look.
    again = await files_client.get(f"{BASE}/drives")
    assert again.json()["homeId"] == home_id
    async with fx.repo.transaction():
        children = await fx.repo.siblings(container.id)
    assert sorted(str(child.id) for child in children) == sorted([str(teammate_home.id), home_id])
    moved = await files_client.get(f"{BASE}/drives/{body['id']}/items/{receipt.id}")
    assert moved.status_code == 200, moved.text
    assert moved.json()["parentId"] == home_id


def _home_name_of(user_id: uuid.UUID) -> str:
    """What a member's home is stored as: their id, spelled here on its own so
    the test does not learn the name from the code it is checking."""
    return str(user_id)


async def test_a_folder_the_founder_made_under_the_container_is_not_her_home(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The live shape: ``/home/Test`` came first, and Files opened it as Home.

    The founder had made a folder called ``Test`` straight into the container
    before her first Files visit, so the oldest folder of hers there was
    ``Test`` — the drive took it for her home, and her real one was never
    ensured because a candidate existed. Her words: "why do I still not see
    files and now just a weird Test". A home is identified by its name as well
    as its creator: ``Test`` is not her home, ``<her id>`` is ensured beside
    it, and everything she left in the container — ``Test``, the receipt — is
    moved into that home, so her Home lists them. A teammate's stray stays.
    """
    container = await _home_container(fx)
    test_folder = await fx.node(b"Test", kind="folder", parent=container, created_by=fx.actor_id)
    receipt = await _receipt_in_the_container(fx, container)
    teammate_home = await _give_member_a_home(fx, files_org, real_session)
    teammates_stray = await fx.node(
        b"theirs.txt", kind="file", parent=container, created_by=files_org.member.id
    )
    expected_name = _home_name_of(fx.actor_id)

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    home_id = body["homeId"]
    drive_id = body["id"]
    assert home_id is not None
    assert home_id != str(test_folder.id), "the drive took the founder's Test folder for her home"
    assert home_id not in {str(receipt.id), str(teammate_home.id), str(container.id)}

    home = await files_client.get(f"{BASE}/drives/{drive_id}/items/{home_id}")
    assert home.status_code == 200, home.text
    assert home.json()["kind"] == "folder"
    assert home.json()["name"] == expected_name
    assert home.json()["parentId"] == str(container.id)
    assert home.json()["attrs"]["owner"] == str(fx.actor_id)

    # Her strays now live in her home; the teammate's stray and home do not move.
    for stray in (test_folder, receipt):
        item = await files_client.get(f"{BASE}/drives/{drive_id}/items/{stray.id}")
        assert item.status_code == 200, item.text
        assert item.json()["parentId"] == home_id, item.json()["name"]
    listed = await files_client.get(f"{BASE}/drives/{drive_id}/items/{home_id}/children")
    assert listed.status_code == 200, listed.text
    assert {row["name"] for row in listed.json()["value"]} == {"Test", "Receipt-2505-0695 (1).pdf"}
    async with fx.repo.transaction():
        children = await fx.repo.siblings(container.id)
    assert sorted(str(child.id) for child in children) == sorted(
        [str(teammate_home.id), str(teammates_stray.id), home_id]
    )

    # Idempotent: a second read names the same home and moves nothing further.
    again = await files_client.get(f"{BASE}/drives")
    assert again.json()["homeId"] == home_id
    async with fx.repo.transaction():
        after = await fx.repo.siblings(container.id)
        inside = await fx.repo.siblings(uuid.UUID(home_id))
    assert sorted(str(child.id) for child in after) == sorted(str(child.id) for child in children)
    assert sorted(str(child.id) for child in inside) == sorted(
        [str(test_folder.id), str(receipt.id)]
    )


async def test_a_stray_whose_name_the_home_already_holds_is_adopted_under_another(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """A ``Test`` in the container and a ``Test`` already in her home: both end
    up inside the home, the newcomer under a conflict name, and the read is
    not refused by the collision."""
    container = await _home_container(fx)
    ctx = ActingContext.for_user(
        user_id=fx.actor_id, org_id=fx.org_team_id, email=files_org.org.admin_email
    )
    async with fx.repo.transaction():
        home = await drives.ensure_home_folder(fx.repo, ctx, fx.actor_id)
    await real_session.commit()
    kept = await fx.node(b"Test", kind="folder", parent=home, created_by=fx.actor_id)
    stray = await fx.node(b"Test", kind="folder", parent=container, created_by=fx.actor_id)

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    assert response.json()["homeId"] == str(home.id)
    drive_id = response.json()["id"]
    listed = await files_client.get(f"{BASE}/drives/{drive_id}/items/{home.id}/children")
    assert listed.status_code == 200, listed.text
    by_id = {row["id"]: row["name"] for row in listed.json()["value"]}
    assert by_id[str(kept.id)] == "Test"
    assert str(stray.id) in by_id and by_id[str(stray.id)] != "Test"
    async with fx.repo.transaction():
        left = await fx.repo.siblings(container.id)
    assert [str(row.id) for row in left] == [str(home.id)]


async def _stray_subtree(fx: FilesFixtures, container: FileNode, *, children: int) -> FileNode:
    """A folder the founder made under ``/home`` with ``children`` files in it."""
    folder = await fx.node(b"Scale test", kind="folder", parent=container, created_by=fx.actor_id)
    for index in range(children):
        await fx.node(
            f"row-{index}.txt".encode(), kind="file", parent=folder, created_by=fx.actor_id
        )
    return folder


async def _move_operations_for(session: AsyncSession, node: FileNode) -> list[FileOp]:
    rows = await session.execute(
        select(FileOp).where(FileOp.result_node_id == node.id, FileOp.kind == "move")
    )
    return list(rows.scalars().all())


async def test_a_stray_past_the_move_budget_is_adopted_through_a_queued_operation(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live shape: ``Scale test (24k)`` — a subtree past the inline move budget.

    A subtree that large is not moved inside the drive read: the library queues
    a ``move`` operation, the read answers at once, and the operation is run
    after the response by the same runner the move route hands its oversized
    moves to (``files_on`` runs operations inline, the single-process shape).
    Once it has run the folder is inside her home with its children under it.
    """
    from alkera_core.files import namespace as namespace_module

    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 3)
    container = await _home_container(fx)
    big = await _stray_subtree(fx, container, children=5)

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    home_id = response.json()["homeId"]
    drive_id = response.json()["id"]
    assert home_id is not None and home_id != str(big.id)

    queued = await _move_operations_for(real_session, big)
    assert [row.state for row in queued] == ["done"], "the subtree went the queued way, to its end"

    item = await files_client.get(f"{BASE}/drives/{drive_id}/items/{big.id}")
    assert item.status_code == 200, item.text
    assert item.json()["parentId"] == home_id
    inside = await files_client.get(f"{BASE}/drives/{drive_id}/items/{big.id}/children")
    assert inside.status_code == 200, inside.text
    assert len(inside.json()["value"]) == 5
    async with fx.repo.transaction():
        left = await fx.repo.siblings(container.id)
    assert [str(row.id) for row in left] == [home_id]


async def test_a_queued_adoption_is_not_queued_twice_while_it_waits_for_the_worker(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no inline runner the operation waits for the worker — and a second
    drive read, finding the folder still in the container, does not queue a
    second operation for the node the first one already carries."""
    from alkera_core.config import settings
    from alkera_core.files import namespace as namespace_module

    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 3)
    monkeypatch.setattr(settings, "files_inline_operations", False)
    container = await _home_container(fx)
    big = await _stray_subtree(fx, container, children=5)

    first = await files_client.get(f"{BASE}/drives")
    assert first.status_code == 200, first.text
    home_id = first.json()["homeId"]
    second = await files_client.get(f"{BASE}/drives")
    assert second.json()["homeId"] == home_id

    queued = await _move_operations_for(real_session, big)
    assert [row.state for row in queued] == ["queued"], "one operation, still waiting"
    item = await files_client.get(f"{BASE}/drives/{first.json()['id']}/items/{big.id}")
    assert item.json()["parentId"] == str(container.id), "the tree has not changed yet"


async def _queue_stale_moves(
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    session: AsyncSession,
    big: FileNode,
    home: FileNode,
    *,
    count: int,
    age: timedelta,
) -> list[FileOp]:
    """``count`` queued ``move`` operations for one node, created ``age`` ago.

    Written the way the demo got its three: the library's own routing, once per
    drive read that found the folder still in the container, with nothing
    having run any of them. Backdated so the next read sees them as abandoned
    rather than as work a runner is about to pick up.
    """
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.ids import NodeId
    from alkera_core.files.namespace import Namespace

    ctx = ActingContext.for_user(
        user_id=fx.actor_id, org_id=fx.org_team_id, email=files_org.org.admin_email
    )
    for _ in range(count):
        async with fx.repo.transaction():
            fresh = await fx.repo.node(NodeId(big.id))
            assert fresh is not None
            namespace = Namespace(fx.repo, ctx, SystemClock(), None)
            await namespace.move(NodeId(big.id), NodeId(home.id), if_match=fresh.etag)
    await session.execute(
        text(
            "UPDATE file_ops SET created_at = now() - CAST(:age AS interval) "
            "WHERE result_node_id = :node AND kind = 'move'"
        ),
        {"age": age, "node": big.id},
    )
    await session.commit()
    return await _move_operations_for(session, big)


async def test_two_overlapping_drive_reads_queue_one_operation_for_a_stray(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live shape: a client that retries a slow first drive read.

    A 24k-node subtree makes the read take seconds, so the second read starts
    while the first is still routing the move. Both find the folder in the
    container; only one may queue the operation that carries it — the other
    has to wait for the first's answer and find it, not read past it under
    READ COMMITTED and queue its own.
    """
    from alkera_core.config import settings
    from alkera_core.files import namespace as namespace_module

    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 3)
    monkeypatch.setattr(settings, "files_inline_operations", False)
    container = await _home_container(fx)
    big = await _stray_subtree(fx, container, children=5)

    first, second = await asyncio.gather(
        files_client.get(f"{BASE}/drives"), files_client.get(f"{BASE}/drives")
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["homeId"] == second.json()["homeId"]

    queued = await _move_operations_for(real_session, big)
    assert [row.state for row in queued] == ["queued"], "one operation carries the node"


async def test_stale_queued_adoptions_resolve_to_one_move_on_the_next_read(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three queued operations for one node, none of them ever run, and a read
    that comes along later: one is run to its end and the other two are closed
    as superseded, so the folder ends up in the home exactly once and nothing
    stays queued for a runner that never came."""
    from alkera_core.files import namespace as namespace_module
    from alkera_core.files.ids import NodeId

    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 3)
    container = await _home_container(fx)
    # The founder's home first, with the folder still beside it: the home is
    # ensured by a read, so make one while nothing is past the budget.
    primed = await files_client.get(f"{BASE}/drives")
    assert primed.status_code == 200, primed.text
    home_id = primed.json()["homeId"]
    drive_id = primed.json()["id"]
    async with fx.repo.transaction():
        home = await fx.repo.node(NodeId(uuid.UUID(home_id)))
        assert home is not None
    big = await _stray_subtree(fx, container, children=5)
    stale = await _queue_stale_moves(
        fx, files_org, real_session, big, home, count=3, age=timedelta(minutes=10)
    )
    assert [row.state for row in stale] == ["queued"] * 3

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text

    rows = await _move_operations_for(real_session, big)
    for row in rows:
        await real_session.refresh(row)
    assert sorted(row.state for row in rows) == ["cancelled", "cancelled", "done"]
    superseded = [row for row in rows if row.state == "cancelled"]
    assert all(any(err.get("code") == "superseded" for err in row.errors) for row in superseded), (
        "a closed duplicate says which operation carried the node instead"
    )
    item = await files_client.get(f"{BASE}/drives/{drive_id}/items/{big.id}")
    assert item.status_code == 200, item.text
    assert item.json()["parentId"] == home_id
    async with fx.repo.transaction():
        left = await fx.repo.siblings(container.id)
    assert [str(row.id) for row in left] == [home_id]


async def test_a_fresh_queued_adoption_is_left_to_the_runner_that_has_it(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconcile picks up ABANDONED work only: an operation queued moments
    ago belongs to the runner or worker that is about to claim it, and a read
    that ran it again would race that claim for nothing."""
    from alkera_core.config import settings
    from alkera_core.files import namespace as namespace_module

    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 3)
    monkeypatch.setattr(settings, "files_inline_operations", False)
    container = await _home_container(fx)
    big = await _stray_subtree(fx, container, children=5)
    first = await files_client.get(f"{BASE}/drives")
    assert first.status_code == 200, first.text

    monkeypatch.setattr(settings, "files_inline_operations", True)
    second = await files_client.get(f"{BASE}/drives")
    assert second.status_code == 200, second.text

    rows = await _move_operations_for(real_session, big)
    assert [row.state for row in rows] == ["queued"], "still the worker's, not re-run"


async def test_with_no_inline_runner_the_drive_read_nudges_the_worker(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    nudge_recorder: Any,
) -> None:
    """On the shape that leaves queued work to the worker, the read asks the
    worker to run the move NOW — keyed by the operation so a second nudge
    attaches to the run in flight — and a second read nudges nothing new."""
    from alkera_core.config import settings
    from alkera_core.files import namespace as namespace_module

    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 3)
    monkeypatch.setattr(settings, "files_inline_operations", False)
    container = await _home_container(fx)
    big = await _stray_subtree(fx, container, children=5)

    first = await files_client.get(f"{BASE}/drives")
    assert first.status_code == 200, first.text
    queued = await _move_operations_for(real_session, big)
    assert [row.state for row in queued] == ["queued"]
    op_id = queued[0].id

    nudges = nudge_recorder.for_workflow(WorkflowType.FILES_LARGE_MOVE.value)
    assert [n.workflow_id for n in nudges] == [f"files.large_move:{op_id}"]
    assert nudges[0].args == (str(op_id), str(fx.org_team_id))
    assert nudges[0].task_queue == QUEUE_FOR[WorkflowType.FILES_LARGE_MOVE].value
    assert nudges[0].signal is None, "per-entity work starts use-existing, never a signal"

    second = await files_client.get(f"{BASE}/drives")
    assert second.status_code == 200, second.text
    assert len(nudge_recorder.for_workflow(WorkflowType.FILES_LARGE_MOVE.value)) == 1


# --------------------------------------------------------------------------
# a home an earlier release ensured under the caller's address
# --------------------------------------------------------------------------


async def _old_release_home(
    fx: FilesFixtures, container: FileNode, *, name: bytes, owner: uuid.UUID
) -> FileNode:
    """The folder the previous release's ensure wrote: named after the owner's
    address, stamped by them, carrying their drive-default home grant — and no
    home subtype, which that release never set."""
    async with fx.repo.transaction():
        acl_id = await acl_intern.intern(
            fx.repo,
            default_acl(
                DriveFolder.HOME,
                org_policy=ORG_POLICY_RESTRICTED,
                org_team_id=fx.org_team_id,
                subject_id=owner,
            ),
        )
    return await fx.node(name, kind="folder", parent=container, created_by=owner, acl_id=acl_id)


async def _members_folders(
    fx: FilesFixtures, container: FileNode, owner: uuid.UUID
) -> list[FileNode]:
    async with fx.repo.transaction():
        return [
            child
            for child in await fx.repo.siblings(container.id)
            if child.created_by == owner and child.kind == "folder"
        ]


async def _names_in(fx: FilesFixtures, folder_id: uuid.UUID) -> list[bytes]:
    async with fx.repo.transaction():
        return sorted(bytes(child.name) for child in await fx.repo.siblings(folder_id))


@pytest.mark.parametrize(
    "visited_first",
    [
        pytest.param(True, id="the-id-home-already-exists"),
        pytest.param(False, id="only-the-old-release-home-exists"),
    ],
)
async def test_a_home_the_old_release_wrote_is_merged_into_the_one_home(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    visited_first: bool,
) -> None:
    """The rolling-deploy window: the schema is migrated and the member's home
    renamed to their id, and a task still on the previous release — which can
    only find a home by the address spelling — ensures a second one named after
    the address and a file lands in it. The next visit on this release ends with
    exactly one home, ``/home/<id>``, holding that file and everything that was
    already there; the address-named folder is gone, not nested."""
    container = await _home_container(fx)
    owner = fx.actor_id
    local = files_org.org.admin_email.rpartition("@")[0].encode()
    if visited_first:
        first = await files_client.get(f"{BASE}/drives")
        assert first.status_code == 200, first.text
        async with fx.repo.transaction():
            home = await fx.repo.node(uuid.UUID(first.json()["homeId"]))
        assert home is not None
        await fx.node(b"notes.md", parent=home, created_by=owner)
    old = await _old_release_home(fx, container, name=local, owner=owner)
    await fx.node(b"from-the-old-task.csv", parent=old, created_by=owner)
    await fx.node(b"notes.md", parent=old, created_by=owner)

    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    home_id = response.json()["homeId"]

    folders = await _members_folders(fx, container, owner)
    assert [str(folder.id) for folder in folders] == [home_id], "exactly one home"
    assert bytes(folders[0].name) == str(owner).encode()
    held = await _names_in(fx, uuid.UUID(home_id))
    assert b"from-the-old-task.csv" in held
    expected_notes = 2 if visited_first else 1
    assert len([name for name in held if name.startswith(b"notes")]) == expected_notes, held
    assert local not in held, "the old home is not nested inside the new one"
    async with fx.repo.transaction():
        assert await fx.repo.node(old.id) is None, "the address-named folder is gone for good"

    # Idempotent: another visit moves nothing further.
    again = await files_client.get(f"{BASE}/drives")
    assert again.json()["homeId"] == home_id
    assert await _names_in(fx, uuid.UUID(home_id)) == held


async def test_concurrent_visits_merge_the_old_release_home_once(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    container = await _home_container(fx)
    owner = fx.actor_id
    first = await files_client.get(f"{BASE}/drives")
    home_id = first.json()["homeId"]
    old = await _old_release_home(fx, container, name=b"legacy-home", owner=owner)
    await fx.node(b"only-copy.txt", parent=old, created_by=owner)

    answers = await asyncio.gather(*(files_client.get(f"{BASE}/drives") for _ in range(3)))
    assert {answer.json()["homeId"] for answer in answers} == {home_id}
    folders = await _members_folders(fx, container, owner)
    assert [str(folder.id) for folder in folders] == [home_id]
    assert (await _names_in(fx, uuid.UUID(home_id))).count(b"only-copy.txt") == 1
