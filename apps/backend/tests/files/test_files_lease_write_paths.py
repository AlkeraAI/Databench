"""Every write path against a leased folder, through the real routes.

A leased folder has exactly one writer. That is a claim about EVERY way this
server changes a tree, not only the three that were written with the fence in
mind, so each mutating route inside a leased subtree is driven here from a
colleague who may write the folder and from the holder itself: the colleague is
refused with ``files.leased`` and changes nothing, the holder's own fenced call
lands. A path that answers 200 to the colleague is a second writer inside
somebody's mount.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from _files_kit import node_etag, refusal
from alkera_core.authz.principal import ActingContext
from alkera_core.db.locking import advisory_key
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.models.files.history import FileConflict
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login

PREFIX = "/api/v1/files"

pytestmark = pytest.mark.usefixtures("files_on")


def _item(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


class _Leased:
    """A folder under ``/Shared`` that the admin holds and the member may write.

    The member is a genuine writer on it: the refusals asserted below are the
    lease speaking, not the policy, which is the whole point — a caller the ACL
    would let through is exactly the one a mount has to refuse.
    """

    def __init__(
        self, drive: Any, folder: FileNode, grant: dict[str, Any], colleague: AsyncClient
    ) -> None:
        self.drive = drive
        self.folder = folder
        self.grant = grant
        self.colleague = colleague

    @property
    def fence(self) -> dict[str, str]:
        return {
            "X-Alkera-Lease-Epoch": str(self.grant["epoch"]),
            "X-Alkera-Lease-Instance": "instance-a",
        }


async def _leased(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> _Leased:
    folder = await fx.node(b"mounted", kind="folder", parent=await fx.shared())
    drive = await fx.drive()
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ActingContext.for_user(
                user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
            ),
            folder,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    granted = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": await node_etag(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text
    colleague = app_client()
    await login(colleague, files_org.member.email, files_org.member_password)
    return _Leased(drive, folder, granted.json(), colleague)


async def _writable(fx: Any, files_org: Any, node: FileNode) -> FileNode:
    """Give the org's member ``writer`` on ``node`` as well.

    A batch's move and copy read a SOURCE outside the mount, so the refusal
    asserted on them has to be the lease and not the policy — the member has to
    be able to write the source for the leased destination to be the only thing
    standing in the way.
    """
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ActingContext.for_user(
                user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
            ),
            node,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    return node


async def _versioned(fx: Any, parent: FileNode) -> tuple[FileNode, Any, Any]:
    """A file with two versions, its head the newer one."""
    node = await fx.node(b"doc.txt", parent=parent)
    older = await fx.version(node, seq=1, content_hash="aa" * 32)
    newer = await fx.version(node, seq=2, content_hash="bb" * 32)
    return node, older, newer


async def _head(session: AsyncSession, node_id: uuid.UUID) -> uuid.UUID | None:
    row = (
        await session.execute(
            text("SELECT head_version_id FROM file_nodes WHERE id = :n"), {"n": node_id}
        )
    ).first()
    if row is None or row.head_version_id is None:
        return None
    return uuid.UUID(str(row.head_version_id))


async def _children(session: AsyncSession, parent_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM file_nodes WHERE parent_id = :p"), {"p": parent_id}
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Restoring a version
# ---------------------------------------------------------------------------


async def test_restoring_a_version_inside_a_leased_folder_is_refused(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A restore appends a new head, which is a content write like any other.

    It was the one content path with no fence at all, so a colleague could
    rewrite a file the holder has mounted — and the holder's next sync would
    find bytes it never wrote.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node, older, newer = await _versioned(fx, held.folder)

    refused = await held.colleague.post(
        f"{_item(held.drive.id, node.id)}/versions/{older.id}/restore",
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"
    assert await _head(real_session, node.id) == newer.id


async def test_the_holder_restores_a_version_under_its_own_fence(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The fence refuses everyone but the holder — including on this path."""
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node, older, newer = await _versioned(fx, held.folder)

    restored = await files_client.post(
        f"{_item(held.drive.id, node.id)}/versions/{older.id}/restore",
        headers={
            **idem(),
            **held.fence,
            "If-Match": await node_etag(real_session, node.id),
        },
    )

    assert restored.status_code == 200, restored.text
    assert await _head(real_session, node.id) not in (None, newer.id)


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


async def test_setting_attributes_inside_a_leased_folder_is_refused(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """``PATCH`` with only ``attrs`` took no fence at all.

    A rename and a move on the same route are fenced; the stat block was not,
    so a colleague could chmod and setxattr every node in a mount the holder is
    about to write back — and a mode the holder never set would land on the
    box's disk on its next pull.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node = await fx.node(b"script.sh", parent=held.folder, mode=0o100644)

    refused = await held.colleague.patch(
        _item(held.drive.id, node.id),
        json={"attrs": {"mode": 0o104755}},
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"
    mode = (
        await real_session.execute(
            text("SELECT mode FROM file_nodes WHERE id = :n"), {"n": node.id}
        )
    ).scalar_one()
    assert mode == 0o100644


async def test_the_holder_sets_attributes_under_its_own_fence(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node = await fx.node(b"script.sh", parent=held.folder, mode=0o100644)

    patched = await files_client.patch(
        _item(held.drive.id, node.id),
        json={"attrs": {"mode": 0o104755}},
        headers={**idem(), **held.fence, "If-Match": await node_etag(real_session, node.id)},
    )

    assert patched.status_code == 200, patched.text
    mode = (
        await real_session.execute(
            text("SELECT mode FROM file_nodes WHERE id = :n"), {"n": node.id}
        )
    ).scalar_one()
    assert mode == 0o104755


# ---------------------------------------------------------------------------
# Copy and duplicate
# ---------------------------------------------------------------------------


async def test_copying_into_a_leased_folder_is_refused(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A copy plants a whole tree in the destination, so it is a write into it.

    Neither copy route named the lease, so the one verb that can put the most
    bytes into somebody's mount was the one verb the mount could not refuse.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    source = await fx.node(b"source.txt", parent=await fx.shared())

    refused = await held.colleague.post(
        f"{_item(held.drive.id, source.id)}/copy",
        json={"parentId": str(held.folder.id)},
        headers=idem(),
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"
    assert await _children(real_session, held.folder.id) == 0


async def test_duplicating_into_a_leased_folder_is_refused(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The same hole through the synchronous copy verb the UI's duplicate uses."""
    held = await _leased(files_client, fx, files_org, real_session, idem)
    source = await fx.node(b"source.txt", parent=await fx.shared())

    refused = await held.colleague.post(
        f"{_item(held.drive.id, source.id)}/duplicate",
        json={"destinationId": str(held.folder.id)},
        headers=idem(),
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"
    assert await _children(real_session, held.folder.id) == 0


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------


async def test_resolving_a_conflict_inside_a_leased_folder_is_refused(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Resolving promotes a version onto the node, which is a head swap.

    The conflicts pane is where a fenced holder's divergent work lands, so its
    resolve route is the last place a second writer should be able to reach
    into a live mount — and it named no lease at all.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node, older, newer = await _versioned(fx, held.folder)
    row = FileConflict(
        id=uuid.uuid4(),
        org_team_id=fx.org_team_id,
        node_id=node.id,
        base_version_id=None,
        theirs_version_id=older.id,
        mine_version_id=newer.id,
        actor=fx.actor_id,
        state="open",
    )
    real_session.add(row)
    await real_session.commit()

    refused = await held.colleague.post(
        f"{PREFIX}/drives/{held.drive.id}/conflicts/{row.id}/resolve",
        json={"keep": "theirs"},
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"
    assert await _head(real_session, node.id) == newer.id
    state = (
        await real_session.execute(
            text("SELECT state FROM file_conflicts WHERE id = :c"), {"c": row.id}
        )
    ).scalar_one()
    assert state == "open"


async def test_the_holder_resolves_a_conflict_keeping_both_under_its_fence(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """``both`` promotes a side AND creates a sibling beside the node.

    The sibling is a create into the leased folder, so it is fenced on its own —
    and it is the holder's own write, which means the holder's epoch has to
    reach it. Without that the one resolution that loses nothing was the one
    resolution nobody could run inside a mount.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node, older, newer = await _versioned(fx, held.folder)
    row = FileConflict(
        id=uuid.uuid4(),
        org_team_id=fx.org_team_id,
        node_id=node.id,
        base_version_id=None,
        theirs_version_id=older.id,
        mine_version_id=newer.id,
        actor=fx.actor_id,
        state="open",
    )
    real_session.add(row)
    await real_session.commit()

    resolved = await files_client.post(
        f"{PREFIX}/drives/{held.drive.id}/conflicts/{row.id}/resolve",
        json={"keep": "both"},
        headers={**idem(), **held.fence, "If-Match": await node_etag(real_session, node.id)},
    )

    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["copyNodeId"] is not None
    assert await _children(real_session, held.folder.id) == 2


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def _batch(
    folder_id: uuid.UUID, folder_etag: int, source_id: uuid.UUID, source_etag: int
) -> dict[str, Any]:
    """One item of each structural verb, all aimed at the leased folder."""
    return {
        "items": [
            {
                "id": "a",
                "op": "createFolder",
                "parentId": str(folder_id),
                "name": "new",
                "ifMatch": folder_etag,
            },
            {
                "id": "b",
                "op": "move",
                "itemId": str(source_id),
                "parentId": str(folder_id),
                "ifMatch": source_etag,
            },
            {"id": "c", "op": "copy", "itemId": str(source_id), "parentId": str(folder_id)},
        ]
    }


async def test_a_batch_cannot_reach_into_a_leased_folder(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Every structural verb a batch carries is the single-item verb it batches.

    Only ``trash`` was fenced. ``createFolder`` and ``move`` were refused by the
    library on their own, which is fail-closed but never reached the holder
    either; ``copy`` queued an operation with no check at all, so a batch was a
    way to plant a tree inside a mount that the same request sent on its own
    could not.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    source = await _writable(fx, files_org, await fx.node(b"source.txt", parent=await fx.shared()))

    answered = await held.colleague.post(
        f"{PREFIX}/drives/{held.drive.id}/bulk",
        json=_batch(
            held.folder.id,
            int(await node_etag(real_session, held.folder.id)),
            source.id,
            int(await node_etag(real_session, source.id)),
        ),
        headers=idem(),
    )

    assert answered.status_code == 200, answered.text
    rows = {row["id"]: row for row in answered.json()["responses"]}
    for key in ("a", "b", "c"):
        assert rows[key]["status"] == 409, rows[key]
        assert rows[key]["body"]["code"] == "files.leased", rows[key]
    assert await _children(real_session, held.folder.id) == 0


async def test_the_holder_runs_a_batch_under_its_own_fence(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The holder's own batch lands, item by item, inside its own mount."""
    held = await _leased(files_client, fx, files_org, real_session, idem)
    source = await fx.node(b"source.txt", parent=await fx.shared())

    answered = await files_client.post(
        f"{PREFIX}/drives/{held.drive.id}/bulk",
        json=_batch(
            held.folder.id,
            int(await node_etag(real_session, held.folder.id)),
            source.id,
            int(await node_etag(real_session, source.id)),
        ),
        headers={**idem(), **held.fence},
    )

    assert answered.status_code == 200, answered.text
    rows = {row["id"]: row for row in answered.json()["responses"]}
    assert rows["a"]["status"] == 201, rows["a"]
    assert rows["b"]["status"] == 200, rows["b"]
    assert rows["c"]["status"] == 202, rows["c"]


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


async def _trashed(
    client: AsyncClient,
    held: _Leased,
    node: FileNode,
    session: AsyncSession,
    idem: Any,
) -> uuid.UUID:
    """The holder trashes one of its own nodes, and the operation it can undo."""
    binned = await client.delete(
        _item(held.drive.id, node.id),
        headers={**idem(), **held.fence, "If-Match": await node_etag(session, node.id)},
    )
    assert binned.status_code == 200, binned.text
    return uuid.UUID(str(binned.json()["id"]))


async def _is_trashed(session: AsyncSession, node_id: uuid.UUID) -> bool:
    return (
        await session.execute(
            text("SELECT trashed_at IS NOT NULL FROM file_nodes WHERE id = :n"), {"n": node_id}
        )
    ).scalar_one()


async def test_the_holder_undoes_its_own_delete_under_its_fence(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Undo runs an inverse through the real services, so it meets the fence.

    The route already took the lease headers and threw them away, so every
    inverse ran with no lease at all — which the library refused, correctly for
    a stranger and wrongly for the holder: the one caller allowed to write
    inside the folder was the one caller that could not take a write back, and
    the undo toast on a mounted folder never worked.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node = await fx.node(b"doomed.txt", parent=held.folder)
    op_id = await _trashed(files_client, held, node, real_session, idem)
    assert await _is_trashed(real_session, node.id)

    undone = await files_client.post(
        f"{PREFIX}/drives/{held.drive.id}/operations/{op_id}/undo",
        headers={**idem(), **held.fence, "If-Match": "1"},
    )

    assert undone.status_code == 202, undone.text
    assert not await _is_trashed(real_session, node.id)


async def test_a_colleague_cannot_undo_inside_someone_elses_mount(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The holder's undo handle is not a way in for anybody else.

    Undoing a trash is decided as restoring it, so a colleague who may write
    the folder gets exactly what the restore route gives them: the lease
    refuses, because the folder is mounted by someone else, and the inverse
    never runs. Threading the holder's epoch through undo must not turn this
    into an undo anybody holding the operation id can drive.
    """
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node = await fx.node(b"doomed.txt", parent=held.folder)
    op_id = await _trashed(files_client, held, node, real_session, idem)

    undone = await held.colleague.post(
        f"{PREFIX}/drives/{held.drive.id}/operations/{op_id}/undo",
        headers={**idem(), "If-Match": "1"},
    )
    trash = await held.colleague.get(f"{PREFIX}/drives/{held.drive.id}/trash")
    assert trash.status_code == 200, trash.text
    [trash_op] = [
        entry["trashOpId"]
        for entry in trash.json()["entries"]
        if entry["item"]["id"] == str(node.id)
    ]
    restored = await held.colleague.post(
        f"{PREFIX}/drives/{held.drive.id}/trash/{trash_op}/restore",
        json={},
        headers={**idem(), "If-Match": "1"},
    )

    assert undone.status_code == 409, undone.text
    assert (undone.status_code, refusal(undone)) == (restored.status_code, refusal(restored))
    assert refusal(undone)["code"] == "files.leased"
    assert await _is_trashed(real_session, node.id)


async def _waiting_on_the_holder(
    session: AsyncSession, holder: AsyncSession, deadline: float
) -> None:
    """Return once some backend of this database waits on a lock ``holder``
    holds, whichever statement it is waiting in: the request under test has
    reached it."""
    holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    while True:
        waiting = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND :holder = ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            )
        ).scalar_one()
        await session.rollback()
        if waiting:
            return
        assert time.monotonic() < deadline, "the request never reached the held lock"
        await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        pytest.param("/children", {"name": "made", "kind": "folder"}, id="create-a-child"),
        pytest.param("/tree", {"paths": ["made/inner"]}, id="create-a-tree"),
    ],
)
async def test_a_holders_create_takes_the_namespace_before_the_leased_folder(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
    suffix: str,
    body: dict[str, Any],
) -> None:
    """Every Files writer takes the drive's namespace before the leased folder.
    A create that fenced first held the folder while waiting for the namespace,
    and a tree report or a move holding the namespace while waiting for the
    folder deadlocked with it.

    The namespace is held exclusively from outside, as a move holds it, while
    the holder's create runs: the create must be waiting on it with the folder
    still free, so the mover can take the folder and finish instead of
    deadlocking."""
    held = await _leased(files_client, fx, files_org, real_session, idem)
    await real_session.commit()
    namespace = advisory_key("files-namespace", fx.org_team_id, held.drive.id)
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as probe:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": namespace.value})
        made = asyncio.ensure_future(
            files_client.post(
                f"{_item(held.drive.id, held.folder.id)}{suffix}",
                json=body,
                headers={**idem(), **held.fence},
            )
        )
        try:
            await _waiting_on_the_holder(probe, holder, time.monotonic() + 30)
            await probe.execute(
                text("SELECT id FROM file_nodes WHERE id = :n FOR NO KEY UPDATE NOWAIT"),
                {"n": held.folder.id},
            )
            await probe.rollback()
        finally:
            await holder.rollback()
            answer = await made
    assert answer.status_code == 201, answer.text


async def test_a_holders_content_write_takes_the_drive_before_the_leased_folder(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A content write fenced the leased folder and then took the drive for its
    upload session's quota hold, inside one transaction; a promote or a create
    holding the drive and waiting for the folder deadlocked with it (Postgres
    logged it on file_drives, and two box promotes failed). With the drive held
    from outside, the write must be waiting on the drive with the folder free."""
    held = await _leased(files_client, fx, files_org, real_session, idem)
    node, _older, _newer = await _versioned(fx, held.folder)
    etag = await node_etag(real_session, node.id)
    await real_session.commit()
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as probe:
        await holder.execute(
            text("SELECT id FROM file_drives WHERE id = :d FOR UPDATE"), {"d": held.drive.id}
        )
        written = asyncio.ensure_future(
            files_client.put(
                f"{_item(held.drive.id, node.id)}/content",
                content=b"from the box\n",
                headers={
                    **idem(),
                    **held.fence,
                    "If-Match": etag,
                    "Content-Type": "application/octet-stream",
                },
            )
        )
        try:
            await _waiting_on_the_holder(probe, holder, time.monotonic() + 30)
            await probe.execute(
                text("SELECT id FROM file_nodes WHERE id = :n FOR NO KEY UPDATE NOWAIT"),
                {"n": held.folder.id},
            )
            await probe.rollback()
        finally:
            await holder.rollback()
            answer = await written
    assert answer.status_code in (200, 201), answer.text


async def _node_events(session: AsyncSession, node_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT payload FROM event_outbox WHERE type = 'file_node.changed' "
                "AND entity_id = :n ORDER BY id"
            ),
            {"n": str(node_id)},
        )
    ).scalars()
    return [dict(row) for row in rows]


@pytest.mark.parametrize(
    ("holder", "reason"),
    [
        pytest.param(True, "live_saved", id="the-holder-is-a-machine-save"),
        pytest.param(False, None, id="a-person-is-a-persons-change"),
    ],
)
async def test_an_attributes_write_names_its_folder_and_who_made_it(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
    holder: bool,
    reason: str | None,
) -> None:
    """After each file it saves, a box restores the file's mode and mtime with
    an attributes write. Announced with no reason and no folder, it read as a
    person's change, and every open portal refetched its chat list, every folder
    listing and sharing after every box save. The holder's write is now a
    machine save (``live_saved``, the reason a reader refreshes the file and its
    folder for and nothing else); anyone else's stays a person's change."""
    if holder:
        held = await _leased(files_client, fx, files_org, real_session, idem)
        parent, drive_id, fence = held.folder, held.drive.id, held.fence
    else:
        parent = await fx.node(b"plain", kind="folder", parent=await fx.shared())
        drive_id, fence = (await fx.drive()).id, {}
    node = await fx.node(b"script.sh", parent=parent, mode=0o100644)
    await real_session.commit()
    seen = len(await _node_events(real_session, node.id))

    patched = await files_client.patch(
        _item(drive_id, node.id),
        json={"attrs": {"mode": 0o100755}},
        headers={**idem(), **fence, "If-Match": await node_etag(real_session, node.id)},
    )

    assert patched.status_code == 200, patched.text
    new = (await _node_events(real_session, node.id))[seen:]
    assert len(new) == 1, new
    (event,) = new
    assert event["reason"] == reason
    assert event["parent_id"] == str(parent.id)
