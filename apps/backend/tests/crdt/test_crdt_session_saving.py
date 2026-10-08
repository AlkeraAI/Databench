"""A live file session never loses what it holds, and says truthfully
whether it is saving, against real Postgres, real Files and real sandbox
workers.

The session holds edits the drive does not have yet. These pin that nothing
ends the session over them: a trash parks it until a restore saves it, a file
the agent made too large or binary first gets the session's content beside it
as a conflicted copy, and a drive over its ceiling is reported as that (and
tried once), never as "nobody may write". Saving resumed is announced by
whichever replica's write back lifts the pause the row records.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.files.quota import FROZEN_CODE, OVER_QUOTA
from alkera_core.models import CrdtDoc, CrdtUpdate, User
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.workspace_object import WorkspaceObject
from backend.services.crdt.docs import Applied, CrdtDocs
from backend.services.crdt.registry import DocRef
from backend.services.crdt.sessions import QUARANTINED_LOST, SOURCE_HISTORY_SECONDS
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Peer, Person
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import (
    SOURCE,
    FileWorld,
    drive_text,
    file_world,
    head_version,
    node_of,
    outside_write,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


@asynccontextmanager
async def _docs() -> AsyncIterator[CrdtDocs]:
    """A store whose session passes run only when a test calls them."""
    async with lane_docs() as docs:
        docs.run_sessions = False
        yield docs


def _tab(peer: int) -> Peer:
    return Peer(peer, container="content")


async def _row(db: AsyncSession, fw: FileWorld) -> CrdtDoc:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    await db.commit()
    return row


async def _open(docs: CrdtDocs, db: AsyncSession, fw: FileWorld, who: Person, tab: Peer) -> None:
    await docs.access(db, fw.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, fw.ref, since=None, epoch_seen=None)
    await db.commit()
    tab.receive(sync.data)


async def _type(
    docs: CrdtDocs, db: AsyncSession, fw: FileWorld, who: Person, tab: Peer, at: int, typed: str
) -> Applied:
    row = await _row(db, fw)
    applied = await docs.apply(
        db,
        fw.ref,
        user=who.user,
        ent=who.ent,
        agent_id=None,
        epoch=row.epoch,
        peer=tab.peer,
        update_id=uuid.uuid4().hex,
        update=tab.type(at, typed),
        socket_peer_id=f"p:{tab.peer}",
    )
    await db.commit()
    await docs.after_commit(fw.ref, applied)
    return applied


async def _written_back(docs: CrdtDocs, db: AsyncSession, ref: DocRef) -> str:
    settled = await docs.sessions.write_back(db, ref)
    await db.commit()
    return settled.outcome


async def _share(db: AsyncSession, fw: FileWorld, who: Person, role: str) -> None:
    chat = await db.get(WorkspaceObject, uuid.UUID(fw.world.ref.doc_id))
    owner = await db.get(User, fw.world.owner.user.id)
    assert chat is not None and owner is not None
    await share_chat_with(
        db, chat=chat, owner=owner, principal=Principal(kind="user", id=who.user.id), role=role
    )
    await db.commit()


@asynccontextmanager
async def _announced(docs: CrdtDocs) -> AsyncIterator[list[tuple[bool, str]]]:
    """Every saving notice ``docs`` sends, in order, as ``(paused, reason)``;
    still sent for real."""
    said: list[tuple[bool, str]] = []
    real = docs.announce_saving

    async def record(ref: DocRef, *, epoch: int, paused: bool, reason: str = "") -> None:
        said.append((paused, reason))
        await real(ref, epoch=epoch, paused=paused, reason=reason)

    docs.announce_saving = record
    yield said


async def _trash(db: AsyncSession, fw: FileWorld, *, trashed: bool) -> None:
    await db.execute(
        update(FileNode)
        .where(FileNode.id == fw.node_id)
        .values(trashed_at=func.now() if trashed else None)
    )
    await db.commit()


async def _siblings(db: AsyncSession, fw: FileWorld) -> dict[bytes, uuid.UUID]:
    node = await node_of(db, fw.node_id)
    rows = (
        await db.execute(
            select(FileNode.name, FileNode.id).where(
                FileNode.parent_id == node.parent_id,
                FileNode.id != fw.node_id,
                FileNode.trashed_at.is_(None),
            )
        )
    ).all()
    await db.commit()
    return {bytes(row.name): row.id for row in rows}


async def test_a_trash_while_the_edits_cannot_save_parks_them_until_a_restore_saves_them(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The only writer lost the right to write, so their edits wait unsaved;
    the file is trashed while their tab is open. The next pass finds the file
    gone and parks the session (it does not end it), so restoring the file
    and giving the writer their rung back lands the edits on the drive."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(6100)
    async with _docs() as docs:
        await _open(docs, real_session, fw, fw.world.writer, tab)
        await _type(docs, real_session, fw, fw.world.writer, tab, 0, "# kept\n")
        await _share(real_session, fw, fw.world.writer, ROLE_READER)
        assert await _written_back(docs, real_session, fw.ref) == "refused"
        await _trash(real_session, fw, trashed=True)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "refused"
        row = await _row(real_session, fw)
        assert (row.save_paused_reason, row.source_etag is not None) == ("gone", True)
        assert await _written_back(docs, real_session, fw.ref) == "refused"

        await _trash(real_session, fw, trashed=False)
        await _share(real_session, fw, fw.world.writer, ROLE_WRITER)
        assert await _written_back(docs, real_session, fw.ref) == "written"
    assert await drive_text(real_session, fw) == "# kept\n" + SOURCE
    assert (await _row(real_session, fw)).save_paused_reason is None


async def test_a_trashed_file_with_nothing_unsaved_still_ends_its_session(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Parking is for unsaved content only: a session whose edits are all on
    the drive ends with its file, as before."""
    fw = await file_world(real_session, org_admin)
    async with _docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, _tab(6101))
        await _trash(real_session, fw, trashed=True)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
    assert settled.outcome == "closed"
    assert (await _row(real_session, fw)).source_etag is None


@pytest.mark.parametrize(
    "upload",
    [
        pytest.param(b"x" * (1024 * 1024 + 1), id="past-what-a-session-opens"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, id="binary"),
    ],
)
async def test_an_upload_a_session_cannot_hold_keeps_the_unsaved_typing_beside_the_file(
    real_session: AsyncSession, org_admin: OrgWithAdmin, upload: bytes
) -> None:
    """Someone typed, and before the write back the file was replaced by
    bytes a session cannot hold. The session ends (the file is not text it
    can edit any more), but only after its content is kept beside the file as
    a conflicted copy, written as the person who typed it."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(6102)
    async with _docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _type(docs, real_session, fw, fw.world.owner, tab, 0, "# typed\n")
        await outside_write(real_session, fw, upload)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
    assert settled.outcome == "closed"
    copies = await _siblings(real_session, fw)
    assert len(copies) == 1, copies
    (name, copy_id) = next(iter(copies.items()))
    assert b"conflicted copy" in name and name.endswith(b".py")
    kept = FileWorld(world=fw.world, ref=fw.ref, node_id=copy_id)
    assert await drive_text(real_session, kept) == "# typed\n" + SOURCE
    assert (await head_version(real_session, kept)).created_by == fw.world.owner.user.id


async def test_a_drive_over_its_ceiling_is_said_as_that_after_one_try(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two people typed and the drive went over its ceiling (it takes no new
    bytes until something is deleted). That refuses whoever writes, so the
    session parks with the drive's own refusal, what a person can act on,
    after one try: never as "nobody may write" after trying everyone."""
    fw = await file_world(real_session, org_admin)
    mine, theirs = _tab(6103), _tab(6104)
    async with _docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, mine)
        await _open(docs, real_session, fw, fw.world.writer, theirs)
        await _type(docs, real_session, fw, fw.world.owner, mine, 0, "# owner\n")
        theirs.receive(mine.since(theirs.vv))
        await _type(docs, real_session, fw, fw.world.writer, theirs, 0, "# writer\n")
        node = await node_of(real_session, fw.node_id)
        drive = await real_session.get(FileDrive, node.drive_id)
        assert drive is not None
        drive.frozen_reason = OVER_QUOTA
        await real_session.commit()
        with capture_logs() as logged:
            assert await _written_back(docs, real_session, fw.ref) == "refused"
        tries = [e for e in logged if e["event"] == "crdt.session.write_back_refused"]
    assert (await _row(real_session, fw)).save_paused_reason == FROZEN_CODE
    assert len(tries) == 1, tries
    assert await drive_text(real_session, fw) == SOURCE


async def test_a_resume_another_replica_saves_is_announced_by_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """One replica parks the session and tells the tabs saving paused;
    another replica's write back is the one that lands. That replica lifts
    the pause the row records and tells the tabs saving resumed, though it
    never paused anything itself."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(6105)
    async with _docs() as first, _announced(first) as said_first:
        await _open(first, real_session, fw, fw.world.writer, tab)
        await _type(first, real_session, fw, fw.world.writer, tab, 0, "# mine\n")
        await _share(real_session, fw, fw.world.writer, ROLE_READER)
        assert await _written_back(first, real_session, fw.ref) == "refused"
        assert said_first == [(True, "no_writer")]
        await _share(real_session, fw, fw.world.writer, ROLE_WRITER)
        async with _docs() as second, _announced(second) as said_second:
            assert await _written_back(second, real_session, fw.ref) == "written"
        assert said_second == [(False, "")]
        assert said_first == [(True, "no_writer")]
    assert await drive_text(real_session, fw) == "# mine\n" + SOURCE


async def test_an_outside_change_made_on_a_version_long_out_of_the_window_keeps_every_edit(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """An agent read the file, then the session wrote back a dozen times
    over more than its history window, and the agent's write naming the
    version it read arrives. That version is gone from the session's own
    history, but the drive still records where in the document each write
    back came from: the change is merged from the version named, and none of
    the person's later lines reads as removed."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(6106)
    now = [2_000_000.0]
    async with _docs() as docs:
        docs.sessions.clock = lambda: now[0]
        await _open(docs, real_session, fw, fw.world.owner, tab)
        lines = [f"# line {n}\n" for n in range(12)]
        read_by_agent, read_at = "", 0
        for n, line in enumerate(lines):
            await _type(docs, real_session, fw, fw.world.owner, tab, 0, line)
            assert await _written_back(docs, real_session, fw.ref) == "written"
            if n == 0:
                read_by_agent = await drive_text(real_session, fw)
                read_at = (await node_of(real_session, fw.node_id)).etag
            now[0] += SOURCE_HISTORY_SECONDS / 6
        await outside_write(real_session, fw, (read_by_agent + "# agent\n").encode())
        version = await head_version(real_session, fw)
        version.version_metadata = {
            **version.version_metadata,
            "based_on_etag": read_at,
            "machine_id": "box-7",
        }
        await real_session.commit()
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    assert tab.text == "".join(reversed(lines)) + SOURCE + "# agent\n"


async def _corrupt_update(db: AsyncSession, fw: FileWorld, log_seq: int) -> None:
    """One logged update's bytes, as storage that went bad would leave them."""
    await db.execute(
        update(CrdtUpdate)
        .where(
            CrdtUpdate.doc_type == "file",
            CrdtUpdate.doc_id == fw.ref.doc_id,
            CrdtUpdate.log_seq == log_seq,
        )
        .values(data=b"\x00not a loro update\xff")
    )
    await db.commit()


async def test_a_quarantine_keeps_beside_the_file_what_the_broken_history_still_holds(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Edits were typed after the last write back and one logged update went
    bad, so the document is quarantined and restarts from the drive. Before
    it does, its history is rebuilt as far as it goes (every update that
    still applies, the bad one skipped), and that text is kept beside the
    file as a conflicted copy: the good edits since the last write back
    survive, the drive's own head is untouched."""
    fw = await file_world(real_session, org_admin)
    mine, theirs = _tab(6110), _tab(6111)
    async with _docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, mine)
        await _type(docs, real_session, fw, fw.world.owner, mine, 0, "# kept\n")
        assert await _written_back(docs, real_session, fw.ref) == "written"
        await _type(docs, real_session, fw, fw.world.owner, mine, 0, "# a\n")
        await _open(docs, real_session, fw, fw.world.writer, theirs)
        bad = await _type(docs, real_session, fw, fw.world.owner, mine, 0, "# b\n")
        # The writer's edit, made without the bad one: it still applies.
        await _type(docs, real_session, fw, fw.world.writer, theirs, len(theirs.text), "# c\n")
        await _corrupt_update(real_session, fw, bad.log_seq)
        with capture_logs() as logged:
            await docs.restart(real_session, fw.ref, reason="test", quarantine=True)
            await real_session.commit()
    events = [entry["event"] for entry in logged]
    assert "crdt.doc.quarantine_rescued" in events
    assert "crdt.doc.quarantine_lost_edits" not in events
    assert await drive_text(real_session, fw) == "# kept\n" + SOURCE
    copies = await _siblings(real_session, fw)
    assert len(copies) == 1, copies
    kept = FileWorld(world=fw.world, ref=fw.ref, node_id=next(iter(copies.values())))
    assert await drive_text(real_session, kept) == "# a\n# kept\n" + SOURCE + "# c\n"


async def test_a_quarantine_that_cannot_rebuild_says_the_edits_since_the_last_save_are_lost(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The stored snapshot itself went bad, so nothing after it can be
    rebuilt. The document restarts from the drive's head (kept as it was),
    the loss is logged where it can be alarmed on, and the open tabs are told
    with a reason of its own, which the row keeps until the next save."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(6112)
    async with _docs() as docs, _announced(docs) as said:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _type(docs, real_session, fw, fw.world.owner, tab, 0, "# saved\n")
        assert await _written_back(docs, real_session, fw.ref) == "written"
        await _type(docs, real_session, fw, fw.world.owner, tab, 0, "# lost\n")
        await real_session.execute(
            update(CrdtDoc)
            .where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
            .values(snapshot=b"\x00not a loro snapshot\xff")
        )
        await real_session.commit()
        with capture_logs() as logged:
            await docs.restart(real_session, fw.ref, reason="test", quarantine=True)
            await real_session.commit()
        fresh = _tab(6113)
        await _open(docs, real_session, fw, fw.world.owner, fresh)
    assert "crdt.doc.quarantine_lost_edits" in [entry["event"] for entry in logged]
    assert said == [(True, QUARANTINED_LOST)]
    assert QUARANTINED_LOST == "quarantined_lost"
    assert (await _row(real_session, fw)).save_paused_reason == QUARANTINED_LOST
    assert await drive_text(real_session, fw) == "# saved\n" + SOURCE
    assert fresh.text == "# saved\n" + SOURCE
    assert await _siblings(real_session, fw) == {}
