"""Custody of a holder's files when its lease lapses, through the real app.

A box reports its folder's tree ahead of the bytes, so for a while most of an
agent's files exist only on the box: the drive lists the name and the box holds
the content. When the box's lease lapses (its beats stopped landing, as they do
through a Postgres restart) the drive cannot tell a dead machine from one cut
off for ten minutes. The invariant this file pins:

    No file the holder reported ever leaves the listing without a restorable
    trash entry. At the lapse it stays listed and reads ``unsynced`` (awaiting
    its holder); past the grace it goes to the trash under an op that names the
    machine, and the trash lists it and a restore brings it back.

The holder is driven through the routes; the reaper is the janitor's own
sweeper on its own context; and each pass runs with the clock frozen just
before and just after the lease's expiry and then the grace, so it is asked
the question at the moment the boundary is crossed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _live_holder import MockHolder
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import REASON_LEFT_ON_MACHINE, LeaseReaper, SweepDeps
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from worker.files_bootstrap import janitor_context

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"
MACHINE = "box-many"
SECOND = timedelta(seconds=1)
GRACE = timedelta(seconds=settings.files_unsynced_grace_seconds)
#: The files the agent wrote. ``landed.txt`` has its bytes on the drive; the
#: rest exist only on the box, two of them mid-upload when the beats stopped.
UPLOADING = ("f1.txt", "f2.txt")
HOLDER_ONLY = (*UPLOADING, "f3.txt")
LANDED = "landed.txt"
PAYLOAD = b"landed"


def _file(path: str, *, size: int = 3) -> dict[str, Any]:
    return {"op": "upsert", "path": path, "kind": "file", "size": size, "mtime_ns": 1_000}


async def _ids(session: AsyncSession, drive_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await session.execute(
        text("SELECT name, id FROM file_nodes WHERE drive_id = :d AND kind = 'file'"),
        {"d": drive_id},
    )
    found = {bytes(row.name).decode(): row.id for row in rows}
    await session.commit()
    return found


async def _expires_at(session: AsyncSession, node_id: uuid.UUID) -> datetime:
    value = (
        await session.execute(
            text("SELECT expires_at FROM file_leases WHERE node_id = :n"), {"n": node_id}
        )
    ).scalar_one()
    await session.commit()
    return value  # type: ignore[no-any-return]


async def _reap_at(org_team_id: uuid.UUID, when: datetime) -> None:
    """One reaper pass with the clock frozen at ``when``. Only the pass is
    frozen: the routes the test reads through check the session's expiry
    against the real clock."""
    with freeze_time(when, real_asyncio=True):
        await _reap(org_team_id)


async def _reap(org_team_id: uuid.UUID) -> None:
    """One reaper pass at the clock's now."""
    async with AsyncSessionLocal() as session:
        deps = SweepDeps(
            repo=FilesRepo(session, OrgScope(org_team_id=org_team_id)),
            ctx=janitor_context(org_team_id),
            unsynced_grace=GRACE,
        )
        await LeaseReaper(deps).run(datetime.now(UTC))
        await session.commit()


async def _hidden_without_op(session: AsyncSession, drive_id: uuid.UUID) -> list[str]:
    """Every node of the drive that is out of the listing with no trash op to
    bring it back from: what the invariant says must never exist."""
    rows = await session.execute(
        text(
            "SELECT name FROM file_nodes WHERE drive_id = :d AND trashed_at IS NOT NULL "
            "AND (trash_op_id IS NULL OR NOT EXISTS ("
            "  SELECT 1 FROM file_trash_ops o WHERE o.id = trash_op_id))"
        ),
        {"d": drive_id},
    )
    hidden = sorted(bytes(row.name).decode() for row in rows)
    await session.commit()
    return hidden


async def _listed(client: AsyncClient, drive_id: uuid.UUID, folder: uuid.UUID) -> dict[str, Any]:
    answer = await client.get(f"{PREFIX}/drives/{drive_id}/items/{folder}/children")
    assert answer.status_code == 200, answer.text
    return {row["name"]: row for row in answer.json()["value"]}


async def _content(client: AsyncClient, drive_id: uuid.UUID, node_id: uuid.UUID) -> str | None:
    item = await client.get(f"{PREFIX}/drives/{drive_id}/items/{node_id}")
    assert item.status_code == 200, item.text
    return (item.json()["live"] or {}).get("content")


async def _lapsed_holder(
    files_client: AsyncClient, fx: Any, session: AsyncSession, idem: Any
) -> tuple[Any, Any, MockHolder, dict[str, uuid.UUID]]:
    """A box that took a folder, reported what its agent wrote, landed the
    bytes of one file and then went silent: no beat, no release."""
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    project = await fx.node(b"many", kind="folder", parent=await fx.shared())
    holder = MockHolder(files_client, drive.id, project.id, machine=MACHINE)
    taken = await holder.take(session, idem, purpose="mount", live=True)
    assert taken.status_code == 200, taken.text
    reported = await holder.tree(
        [_file(name) for name in HOLDER_ONLY] + [_file(LANDED, size=len(PAYLOAD))]
    )
    assert reported.status_code == 200, reported.text
    ids = await _ids(session, drive.id)
    in_flight = await holder.report(
        [{"nodeId": str(ids[name]), "state": "uploading"} for name in UPLOADING]
    )
    assert in_flight.status_code == 200, in_flight.text
    landed = await holder.push(session, idem, ids[LANDED], PAYLOAD)
    assert landed.status_code in (200, 201), landed.text
    return drive, project, holder, ids


async def test_a_lapse_leaves_every_holder_only_file_listed_and_awaiting_its_holder(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    drive, project, _holder, ids = await _lapsed_holder(files_client, fx, real_session, idem)
    expires_at = await _expires_at(real_session, project.id)

    await _reap_at(files_org.org.org_id, expires_at - SECOND)
    await _reap_at(files_org.org.org_id, expires_at + SECOND)

    assert await _hidden_without_op(real_session, drive.id) == []
    listed = await _listed(files_client, drive.id, project.id)
    assert sorted(listed) == sorted((*HOLDER_ONLY, LANDED))
    for name in HOLDER_ONLY:
        assert await _content(files_client, drive.id, ids[name]) == "unsynced", name


async def test_past_the_grace_the_holders_files_are_in_the_trash_and_restore(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any, files_org: Any
) -> None:
    drive, project, _holder, ids = await _lapsed_holder(files_client, fx, real_session, idem)
    expires_at = await _expires_at(real_session, project.id)

    lapsed = expires_at + SECOND
    await _reap_at(files_org.org.org_id, lapsed)
    await _reap_at(files_org.org.org_id, lapsed + GRACE - SECOND)
    assert sorted(await _listed(files_client, drive.id, project.id)) == sorted(
        (*HOLDER_ONLY, LANDED)
    )
    await _reap_at(files_org.org.org_id, lapsed + GRACE + SECOND)

    assert await _hidden_without_op(real_session, drive.id) == []
    assert sorted(await _listed(files_client, drive.id, project.id)) == [LANDED]
    trash = await files_client.get(f"{PREFIX}/drives/{drive.id}/trash")
    assert trash.status_code == 200, trash.text
    entries = {e["item"]["id"]: e for e in trash.json()["entries"]}
    for name in HOLDER_ONLY:
        entry = entries[str(ids[name])]
        assert (entry["reason"], entry["reasonMachine"]) == (REASON_LEFT_ON_MACHINE, MACHINE)

    entry = entries[str(ids["f1.txt"])]
    restored = await files_client.post(
        f"{PREFIX}/drives/{drive.id}/trash/{entry['trashOpId']}/restore",
        json={},
        headers={**idem(), "If-Match": entry["item"]["etag"]},
    )
    assert restored.status_code in (200, 201), restored.text
    assert "f1.txt" in await _listed(files_client, drive.id, project.id)
