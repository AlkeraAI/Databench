"""What a machine leaves behind when it stops holding a folder, through the real app.

A holder reports its tree ahead of the bytes, so a machine that goes away
leaves rows whose bytes never landed. They read ``unsynced`` for a day -- the
same machine coming back reconciles them -- and then the lease reaper trashes
the ones with no bytes at all, saying which machine they were left on. A
release that could not drain its queue says how many files it left, and the
lease row keeps the count.

Everything is driven the way it happens: the holder's double takes, reports,
lands and releases through the routes; the reaper is the janitor's own sweeper
on its own context; the lease's release is dated back rather than the clock
moved forward, so every "is the lease live" question the sweep asks is asked of
Postgres ``now()`` as it is in production.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from _live_holder import MockHolder
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import (
    REASON_LEFT_ON_MACHINE,
    UNSYNCED_MAX_NAMED_FOLDERS,
    LeaseReaper,
    SweepDeps,
)
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from worker.files_bootstrap import janitor_context

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"
MACHINE = "ana-mbp"
GRACE = settings.files_unsynced_grace_seconds


async def _held_project(
    files_client: AsyncClient, fx: Any, session: AsyncSession, idem: Any
) -> tuple[Any, Any, MockHolder]:
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    project = await fx.node(b"project", kind="folder", parent=await fx.shared())
    holder = MockHolder(files_client, drive.id, project.id, machine=MACHINE)
    taken = await holder.take(session, idem, purpose="mount", live=True)
    assert taken.status_code == 200, taken.text
    return drive, project, holder


def _file(path: str, *, size: int = 5) -> dict[str, Any]:
    return {"op": "upsert", "path": path, "kind": "file", "size": size, "mtime_ns": 1_000}


async def _row(session: AsyncSession, drive_id: uuid.UUID, name: bytes) -> Any:
    row = (
        await session.execute(
            text(
                "SELECT id, parent_id, trashed_at, trash_op_id, head_version_id, holder_size "
                "FROM file_nodes WHERE drive_id = :d AND name = :n"
            ),
            {"d": drive_id, "n": name},
        )
    ).one()
    await session.commit()
    return row


async def _lease(session: AsyncSession, node_id: uuid.UUID) -> Any:
    row = (
        await session.execute(
            text(
                "SELECT released_at, unsynced_count, unsynced_swept_at, live_seq "
                "FROM file_leases WHERE node_id = :n"
            ),
            {"n": node_id},
        )
    ).one()
    await session.commit()
    return row


async def _age(session: AsyncSession, node_id: uuid.UUID, seconds: int) -> None:
    """Date the lease's end ``seconds`` into the past."""
    await session.execute(
        text(
            "UPDATE file_leases SET released_at = now() - make_interval(secs => :s) "
            "WHERE node_id = :n"
        ),
        {"n": node_id, "s": seconds},
    )
    await session.commit()


async def _reap(org_team_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        await session.commit()
        deps = SweepDeps(
            repo=FilesRepo(session, OrgScope(org_team_id=org_team_id)),
            ctx=janitor_context(org_team_id),
        )
        outcome = await LeaseReaper(deps).run(now)
        await session.commit()
    return outcome.swept


async def _content(client: AsyncClient, drive_id: uuid.UUID, node_id: uuid.UUID) -> str | None:
    item = await client.get(f"{PREFIX}/drives/{drive_id}/items/{node_id}")
    assert item.status_code == 200, item.text
    return (item.json()["live"] or {}).get("content")


async def _last_outbox_id(session: AsyncSession) -> int:
    value = (
        await session.execute(text("SELECT coalesce(max(id), 0) FROM event_outbox"))
    ).scalar_one()
    await session.commit()
    return int(value)


async def _frames(session: AsyncSession, org_id: uuid.UUID, after: int) -> list[Any]:
    rows = (
        await session.execute(
            text(
                "SELECT type, entity_id, payload FROM event_outbox WHERE org_id = :org "
                "AND id > :after AND type IN ('file_node.changed', 'file_lease.changed') "
                "ORDER BY id"
            ),
            {"org": org_id, "after": after},
        )
    ).all()
    await session.commit()
    return list(rows)


# ---------------------------------------------------------------------------
# The release says what it left
# ---------------------------------------------------------------------------


async def test_a_release_records_what_it_could_not_drain_and_the_next_grant_forgets_it(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    _drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert holder.grant["live"]["releaseDrainMs"] == settings.files_release_drain_seconds * 1000

    released = await holder.release(
        real_session, idem, unsynced_count=3, unsynced_paths=["a.bin", "b/c.bin", "d.bin"]
    )
    assert released.status_code == 200, released.text
    assert (await _lease(real_session, project.id)).unsynced_count == 3

    again = await holder.take(real_session, idem, purpose="mount", live=True)
    assert again.status_code == 200, again.text
    assert (await _lease(real_session, project.id)).unsynced_count is None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        pytest.param({"unsyncedCount": -1}, 422, id="a-negative-count"),
        pytest.param({"unsyncedPaths": [f"f{i}" for i in range(201)]}, 422, id="201-paths"),
        pytest.param({"unsyncedPaths": [f"f{i}" for i in range(200)]}, 200, id="200-paths"),
    ],
)
async def test_the_release_body_bounds_what_it_reports(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    body: dict[str, Any],
    status: int,
) -> None:
    _drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    answer = await files_client.post(
        f"{holder.item}/lease/release",
        json={"epoch": holder.epoch, "instanceId": holder.instance, **body},
        headers={**idem(), "If-Match": str(await _row_etag(real_session, project.id))},
    )
    assert answer.status_code == status, answer.text
    assert ((await _lease(real_session, project.id)).released_at is not None) is (status == 200)


async def _row_etag(session: AsyncSession, node_id: uuid.UUID) -> int:
    value = (
        await session.execute(text("SELECT etag FROM file_nodes WHERE id = :n"), {"n": node_id})
    ).scalar_one()
    await session.commit()
    return int(value)


# ---------------------------------------------------------------------------
# The grace, then the trash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ended_ago", "trashed"),
    [
        pytest.param(GRACE - 60, False, id="inside-the-grace-it-reads-unsynced"),
        pytest.param(GRACE + 60, True, id="past-the-grace-it-is-trashed"),
    ],
)
async def test_a_released_holders_unlanded_rows_are_trashed_only_past_the_grace(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
    ended_ago: int,
    trashed: bool,
) -> None:
    payload = b"landed"
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (
        await holder.tree([_file("left.bin"), _file("kept.txt", size=len(payload) + 1)])
    ).status_code == 200
    kept = await _row(real_session, drive.id, b"kept.txt")
    landed = await holder.push(real_session, idem, kept.id, payload)
    assert landed.status_code in (200, 201), landed.text
    assert (await _row(real_session, drive.id, b"kept.txt")).holder_size is not None
    assert (await holder.release(real_session, idem)).status_code == 200
    await _age(real_session, project.id, ended_ago)
    left = await _row(real_session, drive.id, b"left.bin")
    assert await _content(files_client, drive.id, left.id) == "unsynced"

    await _reap(files_org.org.org_id)

    after_left = await _row(real_session, drive.id, b"left.bin")
    after_kept = await _row(real_session, drive.id, b"kept.txt")
    assert (after_left.trashed_at is not None) is trashed
    # A file with landed bytes is never trashed: it keeps its head, and past
    # the grace it only stops claiming the machine has something newer.
    assert after_kept.trashed_at is None
    assert (
        after_kept.head_version_id
        == (await _row(real_session, drive.id, b"kept.txt")).head_version_id
    )
    assert after_kept.head_version_id is not None
    assert (after_kept.holder_size is None) is trashed
    assert ((await _lease(real_session, project.id)).unsynced_swept_at is not None) is trashed


async def test_a_trashed_row_says_which_machine_it_was_left_on(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("sub/left.bin")])).status_code == 200
    left = await _row(real_session, drive.id, b"left.bin")
    assert (await holder.release(real_session, idem)).status_code == 200
    await _age(real_session, project.id, GRACE + 60)

    await _reap(files_org.org.org_id)

    listing = await files_client.get(f"{PREFIX}/drives/{drive.id}/trash")
    assert listing.status_code == 200, listing.text
    (entry,) = [e for e in listing.json()["entries"] if e["item"]["id"] == str(left.id)]
    assert (entry["reason"], entry["reasonMachine"]) == (REASON_LEFT_ON_MACHINE, MACHINE)
    history = (
        await real_session.execute(
            text(
                "SELECT kind, after FROM file_history WHERE node_id = :n ORDER BY seq DESC LIMIT 1"
            ),
            {"n": left.id},
        )
    ).one()
    await real_session.commit()
    assert history.kind == "trash"
    assert history.after["metadata"] == {"reason": REASON_LEFT_ON_MACHINE, "machine": MACHINE}
    # The trash is an ordinary one: a restore brings the name back.
    restored = await files_client.post(
        f"{PREFIX}/drives/{drive.id}/trash/{entry['trashOpId']}/restore",
        json={},
        headers={**idem(), "If-Match": entry["item"]["etag"]},
    )
    assert restored.status_code in (200, 201), restored.text
    assert (await _row(real_session, drive.id, b"left.bin")).trashed_at is None


async def test_a_second_pass_finds_nothing_to_sweep(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    _drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("left.bin")])).status_code == 200
    assert (await holder.release(real_session, idem)).status_code == 200
    await _age(real_session, project.id, GRACE + 60)
    mark = await _last_outbox_id(real_session)
    assert await _reap(files_org.org.org_id) >= 1
    second = await _last_outbox_id(real_session)
    await _reap(files_org.org.org_id)
    assert await _frames(real_session, files_org.org.org_id, second) == []
    assert await _frames(real_session, files_org.org.org_id, mark) != []


@pytest.mark.parametrize(
    ("folders", "named"),
    [
        pytest.param(2, True, id="a-few-folders-are-each-announced"),
        pytest.param(UNSYNCED_MAX_NAMED_FOLDERS + 1, False, id="past-the-cap-one-subtree-frame"),
    ],
)
async def test_the_sweep_announces_each_folder_it_touched_or_the_subtree(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
    folders: int,
    named: bool,
) -> None:
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file(f"d{i}/f.bin") for i in range(folders)])).status_code == 200
    parents = {
        str((await _row(real_session, drive.id, f"d{i}".encode())).id) for i in range(folders)
    }
    assert (await holder.release(real_session, idem)).status_code == 200
    await _age(real_session, project.id, GRACE + 60)
    mark = await _last_outbox_id(real_session)

    await _reap(files_org.org.org_id)

    frames = await _frames(real_session, files_org.org.org_id, mark)
    nodes = {row.entity_id for row in frames if row.type == "file_node.changed"}
    leases = [row for row in frames if row.type == "file_lease.changed"]
    if named:
        assert nodes == parents
        assert leases == []
    else:
        assert nodes == set()
        assert [(row.entity_id, row.payload.get("subtree")) for row in leases] == [
            (str(project.id), True)
        ]


# ---------------------------------------------------------------------------
# The same machine comes back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ended_ago",
    [
        pytest.param(GRACE - 60, id="inside-the-grace"),
        pytest.param(GRACE + 60, id="past-the-grace-before-the-reaper-ran"),
    ],
)
async def test_the_same_machine_coming_back_turns_unsynced_to_unlanded_and_nothing_is_trashed(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
    ended_ago: int,
) -> None:
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("work.bin")])).status_code == 200
    work = await _row(real_session, drive.id, b"work.bin")
    assert (await holder.release(real_session, idem)).status_code == 200
    await _age(real_session, project.id, ended_ago)
    assert await _content(files_client, drive.id, work.id) == "unsynced"

    # The returning machine: the same instance takes the folder back and its
    # first walk reports every file it has.
    assert (await holder.take(real_session, idem, purpose="mount", live=True)).status_code == 200
    assert (await holder.tree([_file("work.bin")])).status_code == 200
    assert await _content(files_client, drive.id, work.id) == "unlanded"

    await _reap(files_org.org.org_id)

    row = await _row(real_session, drive.id, b"work.bin")
    assert (row.id, row.trashed_at, row.holder_size) == (work.id, None, 5)
    assert await _content(files_client, drive.id, work.id) == "unlanded"


async def _listed_content(
    client: AsyncClient, drive_id: uuid.UUID, folder: uuid.UUID
) -> dict[str, str | None]:
    """Each row's content word as the folder's listing renders it."""
    answer = await client.get(f"{PREFIX}/drives/{drive_id}/items/{folder}/children")
    assert answer.status_code == 200, answer.text
    return {row["name"]: (row["live"] or {}).get("content") for row in answer.json()["value"]}


async def test_a_box_restart_never_calls_a_file_with_bytes_on_the_drive_unsaved(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The box restarts: it releases the folder, is gone for a while, and takes
    the same folder back. A file whose bytes reached the drive reads as the copy
    it is through all of it; only a file with no bytes on the drive is left on
    the machine, and the re-take turns that back into a file on its way."""
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    older = b"saved"
    assert (
        await holder.tree([_file("saved.txt", size=len(older) + 1), _file("left.bin")])
    ).status_code == 200
    saved = await _row(real_session, drive.id, b"saved.txt")
    left = await _row(real_session, drive.id, b"left.bin")
    # The machine moved past the bytes that landed: the drive has an older copy.
    assert (await holder.push(real_session, idem, saved.id, older)).status_code in (200, 201)
    assert await _content(files_client, drive.id, saved.id) == "behind"

    assert (await holder.release(real_session, idem)).status_code == 200

    assert await _content(files_client, drive.id, saved.id) == "behind"
    assert await _content(files_client, drive.id, left.id) == "unsynced"
    gone = await _listed_content(files_client, drive.id, project.id)
    assert (gone["saved.txt"], gone["left.bin"]) == ("behind", "unsynced")

    # The same machine comes back and its first walk reports what it has.
    assert (await holder.take(real_session, idem, purpose="mount", live=True)).status_code == 200
    assert (
        await holder.tree([_file("saved.txt", size=len(older) + 1), _file("left.bin")])
    ).status_code == 200
    back = await _listed_content(files_client, drive.id, project.id)
    assert (back["saved.txt"], back["left.bin"]) == ("behind", "unlanded")
    assert "unsynced" not in back.values()

    # Its bytes land and the report is spent: nothing about the file to say.
    assert (await holder.push(real_session, idem, saved.id, older + b"!")).status_code in (
        200,
        201,
    )
    assert await _content(files_client, drive.id, saved.id) is None


async def test_a_folder_swept_once_is_swept_again_after_its_next_holder_leaves(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    """The sweep marks the lease done; the next grant starts a new story, so
    what that holder leaves behind gets its own grace and its own sweep."""
    drive, project, holder = await _held_project(files_client, fx, real_session, idem)
    assert (await holder.tree([_file("first.bin")])).status_code == 200
    assert (await holder.release(real_session, idem)).status_code == 200
    await _age(real_session, project.id, GRACE + 60)
    await _reap(files_org.org.org_id)
    assert (await _row(real_session, drive.id, b"first.bin")).trashed_at is not None

    retaken = await holder.take(real_session, idem, purpose="mount", live=True)
    assert retaken.status_code == 200, retaken.text
    assert (await _lease(real_session, project.id)).unsynced_swept_at is None
    assert (await holder.tree([_file("second.bin")])).status_code == 200
    assert (await holder.release(real_session, idem)).status_code == 200
    await _age(real_session, project.id, GRACE + 60)
    await _reap(files_org.org.org_id)
    assert (await _row(real_session, drive.id, b"second.bin")).trashed_at is not None
