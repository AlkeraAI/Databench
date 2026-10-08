"""The trash surface: what it lists, what it restores, what it purges.

Every case drives the real route through the real app, so the assertions cover
the whole chain — the flag, the idempotency dependency, the policy decision and
the library — rather than the handler body alone.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from _files_kit import NOT_FOUND, node_etag, refusal
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import namespace as namespace_module
from alkera_core.files.ids import NodeId
from alkera_core.files.trash import Trash
from alkera_core.models.event_outbox import EventOutbox
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from backend.api.routes.files.trash import TRASH_PAGE_LIMIT
from httpx import AsyncClient
from sqlalchemy import select
from tests.files.conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

#: The precondition every trash mutation carries. Every mutation requires an
#: ``If-Match``, and the two trash routes name a resource with no etag of
#: its own — the immutable trash op, and the drive root the sweep addresses —
#: so the value is a well-formed etag the route does not compare. Sending none
#: is the 428 the case below pins.
PRECONDITION = {"If-Match": "0"}


async def _trash_it(fx: FilesFixtures, org: FilesOrgFixture, node: FileNode) -> uuid.UUID:
    """Delete ``node`` the way the product does, and return its trash op id."""
    ctx = ActingContext.for_user(
        user_id=org.org.admin_id, org_id=org.org.org_id, email=org.org.admin_email
    )
    from alkera_core.files.clock import SystemClock

    trash = Trash(fx.repo, ctx, SystemClock())
    async with fx.repo.transaction():
        op = await trash.trash(NodeId(node.id), if_match=node.etag)
    await fx._session.commit()
    return uuid.UUID(str(op.id))


async def test_files_trash_list_is_dark_without_the_flag(
    files_client: AsyncClient, fx: FilesFixtures, files_off: None
) -> None:
    """With ``files_enabled`` off the surface exists but answers the opaque 404."""
    drive = await fx.drive()
    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert response.status_code == 404
    assert refusal(response) == NOT_FOUND


async def test_files_trash_lists_the_deleted_root_once(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """A folder deleted with its child is ONE entry: the trash shows deletions,
    not every node a deletion swept."""
    drive = await fx.drive()
    folder = await fx.node(b"papers", kind="folder")
    await fx.node(b"draft.txt", parent=folder)
    op_id = await _trash_it(fx, files_org, folder)

    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert response.status_code == 200
    body = response.json()
    assert [entry["trashOpId"] for entry in body["entries"]] == [str(op_id)]
    assert body["entries"][0]["item"]["name"] == "papers"


async def test_files_trash_lists_the_folder_each_root_came_from(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """``originalPath`` is the *parent's* path, rendered — the fact the view shows
    under "Original location" — and ``/`` for a root that lived at the top; the
    trashed node's own path says where it is now, which is not the question."""
    drive = await fx.drive()
    papers = await fx.node(b"papers", kind="folder")
    draft = await fx.node(b"draft.txt", parent=papers)
    loose = await fx.node(b"loose.txt")
    draft_op = await _trash_it(fx, files_org, draft)
    loose_op = await _trash_it(fx, files_org, loose)

    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert response.status_code == 200, response.text
    by_op = {entry["trashOpId"]: entry for entry in response.json()["entries"]}
    assert by_op[str(draft_op)]["originalPath"] == "/papers"
    assert by_op[str(draft_op)]["originalParentId"] == str(papers.id)
    assert by_op[str(loose_op)]["originalPath"] == "/"


async def test_files_trash_lists_the_newest_deletion_first(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """The route hands the browser what was just deleted, at the top.

    With an id-ordered listing a deletion made a moment ago landed wherever its
    random op id fell, so on a drive with more trash than one page the item a
    person had just thrown away had no row to restore from.
    """
    drive = await fx.drive()
    ops = []
    for index in range(4):
        node = await fx.node(f"note-{index}.txt".encode())
        ops.append(await _trash_it(fx, files_org, node))

    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert response.status_code == 200, response.text
    listed = [entry["trashOpId"] for entry in response.json()["entries"]]
    assert listed == [str(op) for op in reversed(ops)]


async def test_files_trash_pages_backwards_through_every_deletion(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture
) -> None:
    """The marker reaches the oldest deletion without repeating or skipping one."""
    drive = await fx.drive()
    ops = []
    for index in range(5):
        node = await fx.node(f"page-{index}.txt".encode())
        ops.append(await _trash_it(fx, files_org, node))

    seen: list[str] = []
    marker: str | None = None
    with patch("backend.api.routes.files.trash.TRASH_PAGE_LIMIT", 2):
        for _page in range(5):
            query = {"marker": marker} if marker else {}
            response = await files_client.get(
                f"/api/v1/files/drives/{drive.id}/trash", params=query
            )
            assert response.status_code == 200, response.text
            body = response.json()
            seen.extend(entry["trashOpId"] for entry in body["entries"])
            marker = body["nextMarker"]
            if marker is None:
                break

    assert marker is None
    assert seen == [str(op) for op in reversed(ops)]


async def test_files_trash_restore_brings_the_subtree_back(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """Restoring un-trashes the node, and the trash no longer lists it."""
    drive = await fx.drive()
    folder = await fx.node(b"papers", kind="folder")
    op_id = await _trash_it(fx, files_org, folder)

    restored = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/{op_id}/restore",
        json={},
        headers={**idem(), **PRECONDITION},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == str(folder.id)

    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert listed.json()["entries"] == []


async def test_files_trash_restore_needs_an_idempotency_key(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """A non-GET with no key is a 428: the request becomes valid the moment the
    header is added, which is exactly what 428 means."""
    drive = await fx.drive()
    folder = await fx.node(b"papers", kind="folder")
    op_id = await _trash_it(fx, files_org, folder)

    response = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/{op_id}/restore", json={}
    )
    assert response.status_code == 428


async def test_files_trash_empty_purges_nothing_the_caller_may_not_delete(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """Emptying is decided per deletion, not per drive.

    Permanent removal sits on the owner rung of the ladder, and an org admin
    resolves to manager here — so the sweep purges nothing and, crucially, the
    entries are still listed afterwards. Drop the per-entry decision from the
    route and this test fails on the second assertion: the trash comes back
    empty for a caller who was never allowed to empty it.
    """
    drive = await fx.drive()
    first = await fx.node(b"a.txt")
    second = await fx.node(b"b.txt")
    await _trash_it(fx, files_org, first)
    await _trash_it(fx, files_org, second)

    emptied = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/empty",
        json={},
        headers={**idem(), **PRECONDITION},
    )
    assert emptied.status_code == 200, emptied.text
    body = emptied.json()
    assert body["removed"] == 0
    # And the answer says so, rather than letting the caller read a success into
    # a sweep that took nothing: the two roots are counted as left behind, with
    # the policy's own word for why.
    assert body["skipped"] == 2
    assert body["skippedReasons"] != []

    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert len(listed.json()["entries"]) == 2


async def _owned_folder(fx: FilesFixtures, files_org: FilesOrgFixture) -> FileNode:
    """A folder the caller owns, so the purge below is one they may make.

    Permanent removal sits on the owner rung and the org-admin descent floors an
    admin at manager, so a sweep with nothing granted purges nothing at all.
    """
    from alkera_core.files import acl
    from alkera_core.files.authz.grants import Principal
    from alkera_core.files.authz.ladder import ROLE_OWNER

    folder = await fx.node(b"mine", kind="folder")
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email=files_org.org.admin_email
    )
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo, ctx, folder, Principal(kind="user", id=files_org.org.admin_id), ROLE_OWNER
        )
    await fx.repo.session.commit()
    return folder


async def test_files_trash_empty_sweeps_past_the_first_page(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """More trashed roots than one listing page, all of them purged.

    The sweep used to read ONE page — the listing's page size — so a drive with
    more deletions than that answered "removed: 50" and left the rest sitting in
    a trash the person had just been told was emptied.
    """
    drive = await fx.drive()
    folder = await _owned_folder(fx, files_org)
    roots = 120
    assert roots > TRASH_PAGE_LIMIT
    for index in range(roots):
        child = await fx.node(f"doomed-{index}.txt".encode(), parent=folder)
        await _trash_it(fx, files_org, child)

    emptied = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/empty",
        json={},
        headers={**idem(), **PRECONDITION},
    )
    assert emptied.status_code == 200, emptied.text
    assert emptied.json() == {"removed": roots, "skipped": 0, "skippedReasons": []}

    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert listed.json()["entries"] == []


async def test_files_trash_empty_counts_what_it_could_not_delete(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """A mixed drive: what the caller owns goes, what they do not is counted.

    The refused root is not silently dropped — the browser writes its notice
    from this number, and a sweep that reports only what it removed reads as a
    trash that is now empty when it is not.
    """
    drive = await fx.drive()
    folder = await _owned_folder(fx, files_org)
    mine = await fx.node(b"mine.txt", parent=folder)
    theirs = await fx.node(b"theirs.txt")
    await _trash_it(fx, files_org, mine)
    await _trash_it(fx, files_org, theirs)

    emptied = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/empty",
        json={},
        headers={**idem(), **PRECONDITION},
    )
    assert emptied.status_code == 200, emptied.text
    body = emptied.json()
    assert (body["removed"], body["skipped"]) == (1, 1)
    assert body["skippedReasons"] != []

    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert [entry["item"]["name"] for entry in listed.json()["entries"]] == ["theirs.txt"]


def _refused_root_last(monkeypatch: pytest.MonkeyPatch, refused: uuid.UUID) -> None:
    """Pin the sweep's order so the refusal is met AFTER a purge has applied.

    The listing orders by the trash op's own uuid4, so which root the sweep
    reaches first is a coin flip — and only one of the two orders can lose a
    purge, which is why the bug below was invisible half the time.
    """
    original = Trash.list_trash

    async def ordered(
        self: Trash, drive_id: Any, *, marker: str | None = None, limit: int = 50
    ) -> Any:
        page = await original(self, drive_id, marker=marker, limit=limit)
        return dataclasses.replace(
            page, entries=tuple(sorted(page.entries, key=lambda e: e.node.id == refused))
        )

    monkeypatch.setattr(Trash, "list_trash", ordered)


async def _deny_rows(org_id: uuid.UUID) -> list[str]:
    """The recorded effects of this org's ``authz.decision`` rows."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_id, EventOutbox.type == "authz.decision"
            )
        )
        return [str(row.payload["effect"]) for row in rows.scalars().all()]


async def test_files_trash_empty_keeps_a_purge_a_later_refusal_cannot_undo(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root purged before a refused one stays purged.

    The sweep decides per root inside ONE transaction, so a denial arrives after
    earlier roots have already been purged. Recording that denial used to end
    the request's transaction, which silently took the purge with it while the
    answer still said one root was removed — and the browser wrote "Removed 1"
    over a trash that had lost nothing.
    """
    drive = await fx.drive()
    folder = await _owned_folder(fx, files_org)
    mine = await fx.node(b"mine.txt", parent=folder)
    theirs = await fx.node(b"theirs.txt")
    await _trash_it(fx, files_org, mine)
    await _trash_it(fx, files_org, theirs)
    _refused_root_last(monkeypatch, theirs.id)

    emptied = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/empty",
        json={},
        headers={**idem(), **PRECONDITION},
    )
    assert emptied.status_code == 200, emptied.text
    body = emptied.json()
    assert (body["removed"], body["skipped"]) == (1, 1)

    # What the answer counted is what the drive actually holds: the purged root
    # is gone from the trash and from the tree, the refused one is untouched.
    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert [entry["item"]["name"] for entry in listed.json()["entries"]] == ["theirs.txt"]
    async with AsyncSessionLocal() as session:
        assert await session.get(FileNode, mine.id) is None
        assert await session.get(FileNode, theirs.id) is not None

    # The refusal is still on record: enforce() commits a DENY in a session of
    # its own, which is what makes sparing the sweep's transaction safe.
    assert "deny" in await _deny_rows(files_org.org.org_id)


@pytest.mark.parametrize(
    "drive_id",
    [
        pytest.param("00000000-0000-0000-0000-000000000000", id="nonexistent"),
        pytest.param(None, id="another-org"),
    ],
)
async def test_files_trash_refuses_a_drive_that_is_not_yours(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    drive_id: str | None,
) -> None:
    """A drive that does not exist and one belonging to somebody else answer the
    same opaque 404 with the same body."""
    await fx.drive()
    named = drive_id or str(uuid.uuid4())
    response = await files_client.get(f"/api/v1/files/drives/{named}/trash")
    assert response.status_code == 404
    assert refusal(response) == NOT_FOUND


# ---------------------------------------------------------------------------
# the lease fence
# ---------------------------------------------------------------------------


async def _acquire(
    client: AsyncClient, fx: FilesFixtures, drive_id: uuid.UUID, folder_id: uuid.UUID, idem: Any
) -> int:
    """Mount ``folder_id``. The precondition is the folder's own etag, read back
    rather than taken off a handle: the deletion this lease follows moved the
    counter, and a stale one would be a 412 instead of the grant."""
    granted = await client.post(
        f"/api/v1/files/drives/{drive_id}/items/{folder_id}/lease",
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": await node_etag(fx._session, folder_id)},
    )
    assert granted.status_code == 200, granted.text
    return int(granted.json()["epoch"])


async def _leased_deletion(
    client: AsyncClient, fx: FilesFixtures, org: FilesOrgFixture, idem: Any
) -> tuple[Any, uuid.UUID, int]:
    """One file trashed out of a folder, and then that folder leased.

    The lease is taken AFTER the deletion on purpose: what the restore has to
    ask is whether the folder it is putting the subtree BACK into is somebody
    else's mount right now.
    """
    drive = await fx.drive()
    folder = await fx.node(b"mounted", kind="folder")
    doc = await fx.node(b"doc.txt", parent=folder)
    op_id = await _trash_it(fx, org, doc)
    return drive, op_id, await _acquire(client, fx, drive.id, folder.id, idem)


async def test_a_restore_into_the_holders_own_mount_succeeds(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """The machine holding the folder restores into it exactly as before."""
    drive, op_id, epoch = await _leased_deletion(files_client, fx, files_org, idem)

    restored = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/{op_id}/restore",
        json={},
        headers={
            **idem(),
            **PRECONDITION,
            "X-Alkera-Lease-Epoch": str(epoch),
            "X-Alkera-Lease-Instance": "instance-a",
        },
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["name"] == "doc.txt"
    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert listed.json()["entries"] == []


@pytest.mark.parametrize(
    ("epoch_delta", "instance", "code"),
    [
        pytest.param(-1, "instance-a", "files.lease_fenced", id="a-superseded-epoch"),
        pytest.param(0, "instance-z", "files.lease_fenced", id="another-instance"),
        pytest.param(None, None, "files.leased", id="no-lease-headers-at-all"),
    ],
)
async def test_a_restore_into_somebody_elses_mount_is_refused_and_changes_nothing(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
    epoch_delta: int | None,
    instance: str | None,
    code: str,
) -> None:
    """A fenced restore leaves the deletion in the trash, whole.

    Asserting the trash still lists the op is the point: a refusal that had
    already un-stamped the subtree would put rows back under a folder whose
    holder is about to sync its own view over them.
    """
    drive, op_id, epoch = await _leased_deletion(files_client, fx, files_org, idem)

    fence: dict[str, str] = {}
    if epoch_delta is not None and instance is not None:
        fence = {
            "X-Alkera-Lease-Epoch": str(epoch + epoch_delta),
            "X-Alkera-Lease-Instance": instance,
        }
    refused = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/{op_id}/restore",
        json={},
        headers={**idem(), **PRECONDITION, **fence},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == code

    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert [entry["trashOpId"] for entry in listed.json()["entries"]] == [str(op_id)]


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param("/{op}/restore", id="restore"),
        pytest.param("/empty", id="empty"),
    ],
)
async def test_the_trash_mutations_refuse_a_request_with_no_if_match(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
    suffix: str,
) -> None:
    """Every mutation carries a precondition, POST included: a restore or an
    empty written blind is one that cannot see the drive moved under it. 428,
    not 400 — the request becomes valid the moment the header is added.
    """
    drive = await fx.drive()
    folder = await fx.node(b"papers", kind="folder")
    op_id = await _trash_it(fx, files_org, folder)

    url = f"/api/v1/files/drives/{drive.id}/trash{suffix.format(op=op_id)}"
    response = await files_client.post(url, json={}, headers=idem())
    assert response.status_code == 428, response.text
    assert response.json()["code"] == "files.if_match_required"

    # And the trash is untouched: a refusal restores and purges nothing.
    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert [entry["trashOpId"] for entry in listed.json()["entries"]] == [str(op_id)]


async def _head_version(fx: FilesFixtures, node: FileNode) -> dict[str, str]:
    """Give ``node`` a committed head version, the way an upload leaves one."""
    facts = {
        "content_hash": "b3-trash-head",
        "mime_sniffed": "text/plain",
        "scan_state": "clean",
    }
    version = FileVersion(
        id=uuid.uuid4(),
        org_team_id=node.org_team_id,
        node_id=node.id,
        seq=1,
        size_bytes=7,
        source="upload",
        **facts,
    )
    fx._session.add(version)
    await fx._session.flush()
    fresh = await fx._session.get(FileNode, node.id)
    assert fresh is not None
    fresh.head_version_id = version.id
    await fx._session.commit()
    return facts


async def test_the_trash_listing_carries_the_head_facts_and_the_callers_star(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """A trashed row is rendered by the same ``to_item`` a live read uses, so it
    owes the same two facts: the head version's hash / mime type / scan state,
    and whether *this* caller starred the node.

    The trash view is where a person decides what to restore, and it decides it
    from those — "is this the copy I want back" is the content hash, and the
    star is how they find it. Both were schema defaults here because the route
    handed ``to_item`` neither.
    """
    drive = await fx.drive()
    node = await fx.node(b"starred-and-binned.txt")
    item_url = f"/api/v1/files/drives/{drive.id}/items/{node.id}"

    fetched = (await files_client.get(item_url)).json()
    starred = await files_client.put(
        f"{item_url}/star", headers={**idem(), "If-Match": str(fetched["etag"])}
    )
    assert starred.status_code == 200, starred.text
    version = await _head_version(fx, node)

    fresh = await fx._session.get(FileNode, node.id)
    assert fresh is not None
    await _trash_it(fx, files_org, fresh)

    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")
    assert listed.status_code == 200, listed.text
    item = listed.json()["entries"][0]["item"]
    assert item["starred"] is True
    assert item["file"]["content_hash"] == version["content_hash"]
    assert item["file"]["mime_type"] == version["mime_sniffed"]
    assert item["file"]["scan_state"] == version["scan_state"]


async def test_a_restore_whose_reparent_is_too_large_hands_back_the_operation(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore that has to re-parent more nodes than a request may move is the
    same 202 an oversized move is.

    The library hands the route an ``OperationState`` instead of the node in
    exactly that case. Answering with the node would say the subtree was back
    when the runner has not touched it yet, so the wire is the queued move's:
    the ``Operation`` body and a ``Location`` naming where its progress is read.
    """
    # The suite runs queued operations inline; this case is about the shape of
    # the answer, so the runner is pinned off and the tree is asserted unmoved.
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 0)
    drive = await fx.drive()
    home = await fx.node(b"attic", kind="folder")
    elsewhere = await fx.node(b"elsewhere", kind="folder")
    folder = await fx.node(b"papers", kind="folder", parent=home)
    await fx.node(b"note.txt", parent=folder)
    op_id = await _trash_it(fx, files_org, folder)

    # A restore to a NAMED parent is the one that re-parents, and re-parenting
    # is what can exceed the inline cap; a restore to the original place moves
    # nothing and never reaches the branch.
    restored = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/{op_id}/restore",
        json={"parent_id": str(elsewhere.id)},
        headers={**idem(), **PRECONDITION},
    )

    assert restored.status_code == 202, restored.text
    body = restored.json()
    assert uuid.UUID(body["id"])
    assert body["driveId"] == str(drive.id)
    assert restored.headers["Location"] == (
        f"/api/v1/files/drives/{drive.id}/operations/{body['id']}"
    )
    # The runner owns the re-parent, so the subtree is still under its old
    # parent rather than under the folder the caller named.
    landed = await files_client.get(f"/api/v1/files/drives/{drive.id}/items/{folder.id}")
    assert landed.json()["parentId"] == str(home.id)


async def test_the_trash_list_renders_the_lease_of_the_leased_ancestor(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """A trashed node under a live mount says so, exactly as the listing does.

    The main listing named the holder while the trash said the very same file
    was under no lease at all, which a customer reads as the bin having lost
    track of their mount.
    """
    drive, _op_id, _epoch = await _leased_deletion(files_client, fx, files_org, idem)

    listed = await files_client.get(f"/api/v1/files/drives/{drive.id}/trash")

    assert listed.status_code == 200, listed.text
    entries = listed.json()["entries"]
    assert len(entries) == 1
    lease = entries[0]["item"]["lease"]
    assert lease is not None, "a trashed node under a live mount rendered no lease"
    assert lease["machine"] == "machine-a"
    assert lease["mine"] is True


async def test_a_restore_answers_with_the_mount_it_landed_under(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """The node a restore hands back carries the mount it went back into."""
    drive, op_id, epoch = await _leased_deletion(files_client, fx, files_org, idem)

    restored = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/trash/{op_id}/restore",
        json={},
        headers={
            **idem(),
            **PRECONDITION,
            "X-Alkera-Lease-Epoch": str(epoch),
            "X-Alkera-Lease-Instance": "instance-a",
        },
    )

    assert restored.status_code == 200, restored.text
    lease = restored.json()["lease"]
    assert lease is not None, "a restored node under a live mount rendered no lease"
    assert lease["machine"] == "machine-a"
