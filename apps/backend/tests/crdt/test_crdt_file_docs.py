"""A text file co-edited live: the ``file`` document type against real
Postgres, real Files and real sandbox workers.

Who may read and write a file's document, what it is seeded from, and the
session rules that keep it and the file on the drive in step: an edit is
written back as a ``document_snapshot`` version under a precondition, a change
made to the file outside the session (a box pushing the agent's edit, an
upload) is merged around the live edits and never lost, a new epoch keeps the
drive's version, and a file that stops being editable closes its session.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.files import NodeId
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.files.content import ContentService
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.namespace import Namespace
from alkera_core.files.trash import Trash
from alkera_core.models import CrdtDoc, CrdtUpdate, User, WorkspaceObject
from alkera_core.models.crdt_doc import CRDT_UNSAVED_PREDICATE
from alkera_core.models.files.tree import FileNode
from backend.authz import decide_on_record
from backend.services import files
from backend.services.crdt import sweeper
from backend.services.crdt.docs import Applied, CrdtDocs, CrdtError
from backend.services.crdt.registry import DocRef
from backend.services.crdt.sessions import SOURCE_HISTORY_SECONDS
from backend.services.files.context import build_files_context
from httpx import AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.crdt_world import Peer, Person
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import (
    SOURCE,
    FileWorld,
    acting,
    drive_text,
    file_world,
    head_version,
    node_of,
    outside_write,
)
from tests.files._live_holder import MockHolder

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _row(db: AsyncSession, fw: FileWorld) -> CrdtDoc:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    return row


async def _open(docs: CrdtDocs, db: AsyncSession, fw: FileWorld, who: Person, tab: Peer) -> None:
    await docs.access(db, fw.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, fw.ref, since=None, epoch_seen=None)
    await db.commit()
    tab.receive(sync.data)


async def _write(
    docs: CrdtDocs, db: AsyncSession, fw: FileWorld, who: Person, tab: Peer, data: bytes, *, n: int
) -> Applied:
    row = await _row(db, fw)
    try:
        applied = await docs.apply(
            db,
            fw.ref,
            user=who.user,
            ent=who.ent,
            agent_id=None,
            epoch=row.epoch,
            peer=tab.peer,
            update_id=f"u-{n}",
            update=data,
            socket_peer_id=f"p:{tab.peer}",
        )
    except BaseException:
        await db.rollback()
        raise
    await db.commit()
    await docs.after_commit(fw.ref, applied)
    return applied


async def _written_back(docs: CrdtDocs, db: AsyncSession, fw: FileWorld) -> str:
    settled = await docs.sessions.write_back(db, fw.ref)
    await db.commit()
    if settled.applied is not None:
        await docs.after_commit(fw.ref, settled.applied)
    return settled.outcome


@asynccontextmanager
async def crdt_docs() -> AsyncIterator[CrdtDocs]:
    """A store whose session passes run only when a test calls them: each
    test here drives write back and merging by hand, and a background pass
    (the merge every open schedules, the write back every edit schedules)
    would race it."""
    async with lane_docs() as docs:
        docs.run_sessions = False
        yield docs


async def _last_frame(db: AsyncSession) -> int:
    value = (await db.execute(text("SELECT coalesce(max(id), 0) FROM event_outbox"))).scalar_one()
    await db.commit()
    return int(value)


async def _node_reasons(db: AsyncSession, node_id: Any, after: int) -> list[Any]:
    rows = (
        await db.execute(
            text(
                "SELECT payload FROM event_outbox WHERE id > :after "
                "AND type = 'file_node.changed' AND entity_id = :node ORDER BY id"
            ),
            {"after": after, "node": str(node_id)},
        )
    ).all()
    await db.commit()
    return [row.payload.get("reason") for row in rows]


def _tab(peer: int) -> Peer:
    return Peer(peer, container="content")


# ---------------------------------------------------------------------------
# Who may read and write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "agent", "can_write"),
    [
        pytest.param("owner", False, True, id="the-chats-owner-writes"),
        pytest.param("writer", False, True, id="can-edit-on-the-chat-writes"),
        pytest.param("commenter", False, False, id="can-comment-reads"),
        pytest.param("reader", False, False, id="can-view-reads"),
        pytest.param("owner", True, False, id="an-agent-in-the-owners-session-reads"),
    ],
)
async def test_the_files_policy_decides_who_reads_and_who_writes(
    real_session: AsyncSession, org_admin: OrgWithAdmin, who: str, agent: bool, can_write: bool
) -> None:
    fw = await file_world(real_session, org_admin)
    person: Person = getattr(fw.world, who)
    async with crdt_docs() as docs:
        access = await docs.access(
            real_session,
            fw.ref,
            user=person.user,
            ent=person.ent,
            agent_id="box-7" if agent else None,
        )
    assert (access.can_read, access.can_write) == (True, can_write)


async def test_a_member_the_chat_was_never_shared_with_cannot_find_the_file(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with crdt_docs() as docs:
        with pytest.raises(CrdtError) as excinfo:
            await docs.access(
                real_session,
                fw.ref,
                user=fw.world.stranger.user,
                ent=fw.world.stranger.ent,
                agent_id=None,
            )
    assert excinfo.value.code == "not_found"


@pytest.mark.parametrize(
    "doc_id",
    [
        pytest.param("not-a-node", id="not-a-uuid"),
        pytest.param(str(uuid.uuid4()), id="no-such-node"),
    ],
)
async def test_an_id_that_names_no_file_is_not_found(
    real_session: AsyncSession, org_admin: OrgWithAdmin, doc_id: str
) -> None:
    fw = await file_world(real_session, org_admin)
    ref = DocRef(org_id=fw.world.org_id, doc_type="file", doc_id=doc_id)
    async with crdt_docs() as docs:
        with pytest.raises(CrdtError) as excinfo:
            await docs.access(
                real_session, ref, user=fw.world.owner.user, ent=fw.world.owner.ent, agent_id=None
            )
    assert excinfo.value.code == "not_found"


async def test_a_reader_cannot_write_even_with_a_peer_of_its_own(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.reader, tab)
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, fw, fw.world.reader, tab, tab.type(0, "x"), n=1)
    assert excinfo.value.code == "forbidden"
    assert await drive_text(real_session, fw) == SOURCE


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def test_a_first_open_seeds_the_file_from_its_head_version(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.reader, tab)
    assert tab.text == SOURCE
    row = await _row(real_session, fw)
    node = await node_of(real_session, fw.node_id)
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    assert row.seeded_from == f"file_version:{node.head_version_id}:{digest}"
    assert (row.source_etag, row.source_version_id, row.source_sha256, row.source_epoch) == (
        node.etag,
        node.head_version_id,
        digest,
        1,
    )
    assert row.projection["sha256"] == digest
    assert "text" not in row.projection


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "binary", id="an-image"),
        pytest.param(b"text\x00with a nul", "binary", id="a-nul-byte"),
        pytest.param(b"\xff\xfe\xfd not utf-8", "binary", id="not-utf8"),
        pytest.param(b"x" * (1024 * 1024 + 1), "too_large", id="over-one-mib"),
    ],
)
async def test_a_file_that_is_not_editable_text_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin, content: bytes, reason: str
) -> None:
    fw = await file_world(real_session, org_admin, content=content, name="data.bin")
    async with crdt_docs() as docs:
        with pytest.raises(CrdtError) as excinfo:
            await docs.sync(real_session, fw.ref, since=None, epoch_seen=None)
    assert (excinfo.value.code, excinfo.value.reason) == ("not_editable", reason)


async def test_a_file_of_a_mib_opens_edits_and_writes_back(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Far past the sandbox's frame header (256 KiB): the seed and every
    projection travel as blobs, and the row keeps a digest, not the text."""
    big = ("x" * 1023 + "\n") * 1024
    fw = await file_world(real_session, org_admin, content=big.encode(), name="big.txt")
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "top\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
    assert await drive_text(real_session, fw) == "top\n" + big
    row = await _row(real_session, fw)
    assert row.projection["bytes"] == len(("top\n" + big).encode())
    assert "text" not in row.projection


# ---------------------------------------------------------------------------
# Writing back
# ---------------------------------------------------------------------------


async def test_an_edit_is_written_back_as_a_document_snapshot_by_its_author(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.writer, tab)
        await _write(
            docs, real_session, fw, fw.world.writer, tab, tab.type(len(SOURCE), "# done\n"), n=1
        )
        before = (await node_of(real_session, fw.node_id)).etag
        mark = await _last_frame(real_session)
        assert await _written_back(docs, real_session, fw) == "written"
        assert await _written_back(docs, real_session, fw) == "unchanged"
    # The tabs are told it was the document writing back (they show those
    # bytes already, and the rest re-read at a machine's rate), once.
    assert await _node_reasons(real_session, fw.node_id, mark) == ["live_doc_saved"]
    assert await drive_text(real_session, fw) == SOURCE + "# done\n"
    head = await head_version(real_session, fw)
    assert head.source == "document_snapshot"
    assert head.created_by == fw.world.writer.user.id
    node = await node_of(real_session, fw.node_id)
    assert node.etag == before + 1
    row = await _row(real_session, fw)
    assert (row.source_etag, row.source_version_id) == (node.etag, node.head_version_id)


async def test_an_unchanged_session_writes_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        before = await head_version(real_session, fw)
        assert await _written_back(docs, real_session, fw) == "unchanged"
    assert (await head_version(real_session, fw)).id == before.id


async def test_a_write_back_never_overwrites_a_change_it_did_not_know_about(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The file moved under the session (an upload) after a live edit that
    was not written back yet: the write back's precondition fails, the
    outside change is merged, and the merged text is what lands."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# live\n"), n=1)
        await outside_write(real_session, fw, SOURCE.replace("hello", "hi").encode())
        assert await _written_back(docs, real_session, fw) == "written"
    expected = "# live\n" + SOURCE.replace("hello", "hi")
    assert await drive_text(real_session, fw) == expected


async def test_a_write_back_naming_an_old_version_is_refused_and_writes_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    stale = (await node_of(real_session, fw.node_id)).etag
    await outside_write(real_session, fw, b"changed elsewhere\n")
    with pytest.raises(files.WriteBackRefusedError) as excinfo:
        await files.write_back(
            real_session, acting(fw.world.owner), fw.node_id, "the session's text\n", if_match=stale
        )
    await real_session.rollback()
    assert excinfo.value.reason == "stale"
    assert await drive_text(real_session, fw) == "changed elsewhere\n"


async def test_a_version_names_only_a_source_the_drive_knows(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)

    async def body() -> AsyncIterator[bytes]:
        yield b"x"

    node = await node_of(real_session, fw.node_id)
    context = await build_files_context(real_session, acting(fw.world.owner))
    service = ContentService(context.repo, context.ctx, context.clock, context.store)
    with pytest.raises(InvalidRequest) as excinfo:
        await service.put_version(
            NodeId(fw.node_id), body(), size_declared=1, if_match=node.etag, source="typed"
        )
    await real_session.rollback()
    assert excinfo.value.code == "files.invalid_source"
    assert await drive_text(real_session, fw) == SOURCE


async def test_a_writer_who_left_the_org_is_never_written_back_as(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.writer, tab)
        await _write(docs, real_session, fw, fw.world.writer, tab, tab.type(0, "x"), n=1)
        from alkera_core.models import User

        await real_session.execute(
            update(User).where(User.id == fw.world.writer.user.id).values(is_active=False)
        )
        await real_session.commit()
        assert await _written_back(docs, real_session, fw) == "refused"
    assert await drive_text(real_session, fw) == SOURCE


# ---------------------------------------------------------------------------
# Outside changes
# ---------------------------------------------------------------------------


async def test_an_outside_change_is_merged_into_the_live_session_around_its_edits(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(
            docs, real_session, fw, fw.world.owner, tab, tab.type(len(SOURCE), "# tail\n"), n=1
        )
        await outside_write(real_session, fw, ("# agent\n" + SOURCE).encode())
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        assert tab.text == "# agent\n" + SOURCE + "# tail\n"
        update_row = (
            await real_session.execute(
                select(CrdtUpdate).where(
                    CrdtUpdate.doc_id == fw.ref.doc_id,
                    CrdtUpdate.log_seq == settled.applied.log_seq,
                )
            )
        ).scalar_one()
        assert update_row.author_user_id is None
        # Nothing new on the drive: the second pass finds the source current.
        assert (await docs.sessions.merge_outside(real_session, fw.ref)).outcome == "unchanged"
        await real_session.commit()
        assert await _written_back(docs, real_session, fw) == "written"
    assert await drive_text(real_session, fw) == "# agent\n" + SOURCE + "# tail\n"


async def _sent_by_a_box(db: AsyncSession, fw: FileWorld) -> None:
    """Mark the head version as the folder's lease holder's upload (what the
    content route records for a box's hand-back): a box's named version is a
    hint, where a person's or an app's is the version it read."""
    version = await head_version(db, fw)
    version.version_metadata = {**version.version_metadata, "machine_id": "box-7"}
    await db.commit()


async def test_an_outside_change_made_on_an_older_version_keeps_the_newer_live_edits(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The agent read the file before the session's last write back reached
    the box, and wrote it after: its text is the OLDER version plus its own
    change. Merged from the latest version, that would read as the person's
    newer edit being removed; it is merged from the version it was made on."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# person\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        assert await drive_text(real_session, fw) == "# person\n" + SOURCE
        # The agent's write: the version before the person's line, plus its own.
        await outside_write(real_session, fw, (SOURCE + "# agent\n").encode())
        await _sent_by_a_box(real_session, fw)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        assert tab.text == "# person\n" + SOURCE + "# agent\n"
        assert await _written_back(docs, real_session, fw) == "written"
    assert await drive_text(real_session, fw) == "# person\n" + SOURCE + "# agent\n"


async def test_a_version_records_the_etag_its_writer_made_it_on(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The writer's if-match is kept on the version it wrote: what a live
    session later merges that version from."""
    fw = await file_world(real_session, org_admin)
    before = (await node_of(real_session, fw.node_id)).etag
    await outside_write(real_session, fw, b"# replaced\n")
    version = await head_version(real_session, fw)
    assert version.version_metadata["based_on_etag"] == before


async def test_an_outside_change_made_many_write_backs_ago_keeps_every_live_edit(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A slow writer (an agent mid-turn, an editor left open) read the file a
    dozen write backs ago. The session still remembers that version, so the
    writer's change is merged from it and none of the person's later lines
    reads as removed; a history kept by count (eight) had lost it."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        lines = [f"# line {n}\n" for n in range(12)]
        read_by_agent = ""
        for n, line in enumerate(lines):
            await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, line), n=n + 1)
            assert await _written_back(docs, real_session, fw) == "written"
            if n == 0:
                read_by_agent = await drive_text(real_session, fw)
        await outside_write(real_session, fw, (read_by_agent + "# agent\n").encode())
        await _sent_by_a_box(real_session, fw)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    assert tab.text == "".join(reversed(lines)) + SOURCE + "# agent\n"


async def test_a_writer_naming_a_version_older_than_its_text_never_duplicates_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A box names the version it last agreed with the drive, which can be
    older than what its file holds (its own upload not agreed yet). Merged
    from that older version, every line written since would read as added
    again and the file would hold them twice; the version its text is
    closest to is the one it is merged from."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        named = (await node_of(real_session, fw.node_id)).etag
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# one\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        await outside_write(real_session, fw, ("# one\n" + SOURCE + "# agent\n").encode())
        version = await head_version(real_session, fw)
        version.version_metadata = {
            **version.version_metadata,
            "based_on_etag": named,
            "machine_id": "box-7",
        }
        await real_session.commit()
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    assert tab.text == "# one\n" + SOURCE + "# agent\n"


async def test_the_source_history_is_kept_by_time(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Every version that stopped being the latest within the window stays,
    however many there are; one older than the window goes."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    now = [1_000_000.0]
    async with crdt_docs() as docs:
        docs.sessions.clock = lambda: now[0]
        await _open(docs, real_session, fw, fw.world.owner, tab)
        etags = [(await _row(real_session, fw)).source_etag]
        for n in range(3):
            await _write(
                docs, real_session, fw, fw.world.owner, tab, tab.type(0, f"{n}\n"), n=n + 1
            )
            assert await _written_back(docs, real_session, fw) == "written"
            etags.append((await _row(real_session, fw)).source_etag)
            now[0] += 10.0
        kept = [record["etag"] for record in (await _row(real_session, fw)).source_history]
        assert kept == [etags[2], etags[1], etags[0]]
        # The seed's version stopped being the latest at the first write back;
        # a window later (and a write back to notice it) it has aged out.
        now[0] = 1_000_000.0 + SOURCE_HISTORY_SECONDS + 5.0
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "x\n"), n=9)
        assert await _written_back(docs, real_session, fw) == "written"
        kept = [record["etag"] for record in (await _row(real_session, fw)).source_history]
        assert kept == [etags[3], etags[2], etags[1]]


async def test_a_file_that_stops_being_text_closes_its_session_and_reopens_when_it_is_again(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await outside_write(real_session, fw, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "closed"
        row = await _row(real_session, fw)
        assert (row.seeded_from, row.source_etag, row.epoch) == ("closed", None, 2)
        with pytest.raises(CrdtError) as excinfo:
            await docs.sync(real_session, fw.ref, since=None, epoch_seen=None)
        await real_session.rollback()
        assert excinfo.value.code == "not_editable"
        await outside_write(real_session, fw, b"text again\n")
        again = _tab(5001)
        await _open(docs, real_session, fw, fw.world.owner, again)
    assert again.text == "text again\n"
    row = await _row(real_session, fw)
    assert row.epoch == 3 and row.seeded_from.startswith("file_version:")


async def test_a_deleted_file_closes_its_session(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await real_session.execute(
            update(FileNode).where(FileNode.id == fw.node_id).values(trashed_at=func.now())
        )
        await real_session.commit()
        assert (await docs.sessions.merge_outside(real_session, fw.ref)).outcome == "closed"
        await real_session.commit()


# ---------------------------------------------------------------------------
# New epochs
# ---------------------------------------------------------------------------


async def test_a_new_epoch_keeps_the_drives_version_so_a_later_outside_change_still_merges(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A rotation while a live edit is not written back: the new epoch holds
    the live text, and a version of it holding the drive's text, so an
    outside change made afterwards lands around the live edit instead of
    reverting it."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# live\n"), n=1)
        epoch = await docs.restart(real_session, fw.ref, reason="test", quarantine=False)
        await real_session.commit()
        row = await _row(real_session, fw)
        assert (epoch, row.source_epoch, row.seeded_from) == (2, 2, "rotation")
        await outside_write(real_session, fw, SOURCE.replace("name)", "who)").encode())
        assert await _written_back(docs, real_session, fw) == "written"
    assert await drive_text(real_session, fw) == "# live\n" + SOURCE.replace("name)", "who)")


async def test_a_quarantined_file_is_reseeded_from_the_drive(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "lost "), n=1)
        await docs.restart(real_session, fw.ref, reason="test", quarantine=True)
        await real_session.commit()
        fresh = _tab(5001)
        await _open(docs, real_session, fw, fw.world.owner, fresh)
    assert fresh.text == SOURCE
    row = await _row(real_session, fw)
    node = await node_of(real_session, fw.node_id)
    assert (row.seeded_from, row.source_etag, row.quarantined_at is not None) == (
        "quarantine",
        node.etag,
        True,
    )


# ---------------------------------------------------------------------------
# A box holding the chat folder
# ---------------------------------------------------------------------------


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _holder(
    db: AsyncSession, client: AsyncClient, fw: FileWorld, *, purpose: str, inbound: bool
) -> MockHolder:
    """A holder taking the chat's folder, through the routes a box takes it
    by. The chat folder is the node whose object is the chat."""
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    folder = (
        await db.execute(
            select(FileNode).where(FileNode.target_object_id == uuid.UUID(fw.world.ref.doc_id))
        )
    ).scalar_one()
    holder = MockHolder(client, folder.drive_id, folder.id)
    taken = await holder.take(db, _idem, purpose=purpose, inbound=inbound, live=inbound)
    assert taken.status_code == 200, taken.text
    return holder


async def test_under_a_box_the_write_back_is_an_inbound_write_the_box_applies(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """The box holds the chat folder: the write back lands as an inbound
    write under ``scratch/``, which the box is offered through its existing
    inbound drain (an old box applies it unchanged). Then the box sends the
    agent's own edit up, and the session merges it around the person's."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        access = await docs.access(
            real_session, fw.ref, user=fw.world.owner.user, ent=fw.world.owner.ent, agent_id=None
        )
        assert access.can_write is True
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# person\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        assert (str(fw.node_id), "inbound") in await holder.owed()
        assert await drive_text(real_session, fw) == "# person\n" + SOURCE
        assert (await holder.settle(fw.node_id, "applied")).status_code == 200
        sent = await holder.push(
            real_session, _idem, fw.node_id, ("# person\n" + SOURCE + "# agent\n").encode()
        )
        assert sent.status_code in (200, 201), sent.text
        # The person types under their first line, not at the end where the
        # agent appended: two inserts at one position are ordered by their
        # peers' numbers, which come from a sequence every earlier test moves.
        await _write(
            docs,
            real_session,
            fw,
            fw.world.owner,
            tab,
            tab.type(len("# person\n"), "# more\n"),
            n=2,
        )
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        assert tab.text == "# person\n# more\n" + SOURCE + "# agent\n"


async def test_a_box_edit_over_a_session_write_back_is_merged_with_no_copy(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """The agent edited the file on the box while people typed: the box sends
    its bytes up on the version it last agreed, older than the session's
    latest write back. The drive keeps that write back in the file's history
    only, with no conflicted copy beside the file, and the session merges the
    agent's edit around the person's: nothing is lost and nobody has a copy
    to reconcile."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    agreed = (await node_of(real_session, fw.node_id)).etag
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# person\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        sent = await holder.push(
            real_session, _idem, fw.node_id, (SOURCE + "# agent\n").encode(), base=agreed
        )
        assert sent.status_code in (200, 201), sent.text
        copies = (
            await real_session.execute(
                select(FileNode.name).where(
                    FileNode.parent_id == (await node_of(real_session, fw.node_id)).parent_id,
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalars()
        assert [bytes(name) for name in copies if b"conflicted copy" in bytes(name)] == []
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        assert tab.text == "# person\n" + SOURCE + "# agent\n"
    # The merge is the agent's edit, written as the machine that sent it; the
    # person's keystrokes stay theirs.
    rows = (
        await real_session.execute(
            select(CrdtUpdate.update_id, CrdtUpdate.agent_id).where(
                CrdtUpdate.doc_id == fw.ref.doc_id
            )
        )
    ).all()
    await real_session.commit()
    by_id = {str(row.update_id): row.agent_id for row in rows}
    assert [agent for update, agent in by_id.items() if update.startswith("source-")] == [
        holder.machine
    ]
    assert {agent for update, agent in by_id.items() if not update.startswith("source-")} == {None}


@pytest.mark.parametrize("write_backs", [1, 30])
@pytest.mark.parametrize("known_base", [True, False])
async def test_a_box_that_stalled_through_many_write_backs_loses_nobody_s_text(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    write_backs: int,
    known_base: bool,
) -> None:
    """A box whose sync stalled while people typed and the session wrote back
    again and again: it took none of those write backs, and when it comes
    back it sends the agent's edit made on the version it last agreed, many
    versions behind. Whatever the session has to search to find that base,
    the merge adds the agent's line and removes nothing a person typed.

    A box that restarted may no longer know which version that was. It then
    says its bytes are made on no version the drive has (``UNKNOWN_BASE``),
    never on the head: claimed on the head, everything typed since reads as
    removed by the agent."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    agreed = (await node_of(real_session, fw.node_id)).etag
    tab = _tab(5000)
    typed: list[str] = []
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        for n in range(write_backs):
            token = f"p{n}"
            first_line_end = tab.text.index("\n")
            await _write(
                docs,
                real_session,
                fw,
                fw.world.owner,
                tab,
                tab.type(first_line_end, f" {token}"),
                n=n + 1,
            )
            typed.append(token)
            assert await _written_back(docs, real_session, fw) == "written"
        sent = await holder.push(
            real_session,
            _idem,
            fw.node_id,
            (SOURCE + "# agent\n").encode(),
            base=agreed if known_base else 0,
        )
        assert sent.status_code in (200, 201), sent.text
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    words = tab.text.split()
    assert {token: words.count(token) for token in typed} == {token: 1 for token in typed}
    assert words.count("agent") == 1


async def test_an_old_box_that_claims_the_head_loses_nobody_s_text(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """A box from before boxes named their base restarts, no longer knows
    which version its file was made on, and fences the agent's edit on the
    drive's head. People typed through many write backs it never took. Its
    bytes are merged as made on an unknown version: from the closest of all
    the versions, removing nothing, so every person's line stays and the
    agent's line lands once. (Taken at its word, the same upload is merged
    from versions near the head, and the lines typed between the box's real
    base and those read as removed.)"""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    holder.names_its_base = False
    tab = _tab(5000)
    typed: list[str] = []
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        for n in range(30):
            token = f"p{n}"
            await _write(
                docs,
                real_session,
                fw,
                fw.world.owner,
                tab,
                tab.type(tab.text.index("\n"), f" {token}"),
                n=n + 1,
            )
            typed.append(token)
            assert await _written_back(docs, real_session, fw) == "written"
        head = (await node_of(real_session, fw.node_id)).etag
        sent = await holder.push(
            real_session, _idem, fw.node_id, (SOURCE + "# agent\n").encode(), base=head
        )
        assert sent.status_code in (200, 201), sent.text
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    words = tab.text.split()
    assert words.count("agent") == 1
    assert {token: words.count(token) for token in typed} == {token: 1 for token in typed}


async def test_restoring_a_version_while_the_file_is_open_lands_in_the_document(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Someone restores the file's first version while people have it open
    live. The restored text becomes the document's (the editors show it, and
    the next write back keeps it), written as the person who restored it; a
    person's edit made after the restore stays. It used to be merged as a
    change from the version whose text it is, which is no change at all, and
    the next write back silently put the newer text back."""
    fw = await file_world(real_session, org_admin)
    first = (await head_version(real_session, fw)).id
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# draft\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        assert await drive_text(real_session, fw) == "# draft\n" + SOURCE

        context = await build_files_context(real_session, acting(fw.world.owner))
        service = ContentService(context.repo, context.ctx, context.clock, context.store)
        async with context.repo.transaction():
            await service.restore_version(NodeId(fw.node_id), first)
        await real_session.commit()
        assert await drive_text(real_session, fw) == SOURCE

        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        assert tab.text == SOURCE
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# after\n"), n=2)
        assert await _written_back(docs, real_session, fw) == "written"
    assert await drive_text(real_session, fw) == "# after\n" + SOURCE
    restored_by = await real_session.scalar(
        select(CrdtUpdate.author_user_id).where(
            CrdtUpdate.doc_id == fw.ref.doc_id, CrdtUpdate.update_id.like("source-%")
        )
    )
    await real_session.commit()
    assert restored_by == fw.world.owner.user.id


async def test_a_restore_under_a_box_with_the_file_open_reaches_the_box_and_the_editors(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """The box holds the chat's folder and people have the file open when
    someone restores its first version. The editors show the restored text
    and the next write back keeps it; the box is owed the restore, so it
    takes those bytes instead of sending its replaced copy back up over them
    (which kept the restore only as a conflicted copy beside the file)."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    first = (await head_version(real_session, fw)).id
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# draft\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        assert (await holder.settle(fw.node_id, "applied")).status_code == 200
        assert await holder.owed() == []

        context = await build_files_context(real_session, acting(fw.world.owner))
        service = ContentService(context.repo, context.ctx, context.clock, context.store)
        async with context.repo.transaction():
            await service.restore_version(NodeId(fw.node_id), first)
        await real_session.commit()
        assert await holder.owed() == [(str(fw.node_id), "inbound")]

        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        assert tab.text == SOURCE
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# after\n"), n=2)
        assert await _written_back(docs, real_session, fw) == "written"
    assert await drive_text(real_session, fw) == "# after\n" + SOURCE


async def test_a_whole_file_save_on_the_current_version_replaces_what_it_replaced(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Someone saves the whole file through the API, naming the version they
    read: the version the live editor last wrote back. What they left out
    they removed: the document becomes their text, and nothing already
    written back comes back. (Merged from the closest older version instead,
    the editor's earlier edit returned.)"""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "X"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        assert await drive_text(real_session, fw) == "X" + SOURCE
        await outside_write(real_session, fw, b"BBB\n")
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    assert tab.text == "BBB\n"


async def _rename(db: AsyncSession, fw: FileWorld, name: bytes) -> None:
    """Move the file's etag on with no new version: a metadata change."""
    node = await node_of(db, fw.node_id)
    context = await build_files_context(db, acting(fw.world.owner))
    async with context.repo.transaction():
        await Namespace(context.repo, context.ctx, context.clock).rename(
            NodeId(fw.node_id), name, if_match=node.etag
        )
    await db.commit()


@pytest.mark.parametrize("passes", [1, 2])
async def test_a_whole_file_save_merged_once_is_never_merged_again_when_only_the_etag_moves(
    real_session: AsyncSession, org_admin: OrgWithAdmin, passes: int
) -> None:
    """A REST save replaced the file while it was open live, and the session
    merged it. Then the file's etag moved again with the same version at its
    head (a rename, a box handing back the bytes it was just sent). Each pass
    that sees the moved etag reads the same version again: it is the content
    the session already holds, never a second edit. Merged a second time from
    the version the save named, the new text was typed twice (``BBBBBB``)."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "X"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        await outside_write(real_session, fw, b"BBB\n")
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        for n in range(passes):
            await _rename(real_session, fw, f"renamed-{n}.py".encode())
            again = await docs.sessions.merge_outside(real_session, fw.ref)
            await real_session.commit()
            assert again.outcome == "unchanged" and again.applied is None
        assert tab.text == "BBB\n"
        assert await _written_back(docs, real_session, fw) == "unchanged"
        # The session matches the drive at its new etag: a later save is
        # merged from there.
        row = await _row(real_session, fw)
        assert row.source_etag == (await node_of(real_session, fw.node_id)).etag
    assert await drive_text(real_session, fw) == "BBB\n"


async def test_a_rest_save_under_a_box_that_hands_the_bytes_back_lands_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """The live round trip of a REST replace while a box holds the folder:
    the save lands as an inbound write, the session merges it, the box applies
    it and sends the same bytes back. The file and the editors hold the new
    text exactly once."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "X"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        assert (await holder.settle(fw.node_id, "applied")).status_code == 200
        await outside_write(real_session, fw, b"BBB\n")
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
        assert (await holder.settle(fw.node_id, "applied")).status_code == 200
        before = (await node_of(real_session, fw.node_id)).etag
        sent = await holder.push(real_session, _idem, fw.node_id, b"BBB\n", final=True)
        assert sent.status_code in (200, 201), sent.text
        await _rename(real_session, fw, b"touched.py")
        assert (await node_of(real_session, fw.node_id)).etag > before
        again = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        if again.applied is not None:
            tab.receive(again.applied.delta)
        assert tab.text == "BBB\n"
        assert await _written_back(docs, real_session, fw) in ("unchanged", "written")
    assert await drive_text(real_session, fw) == "BBB\n"


async def test_a_box_never_makes_a_conflicted_copy_of_its_own_upload(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """A box's live plane and its checkpoint push each send the file from the
    same disk, the second on the version the first had not reported yet. The
    head it displaces is its own: the newer bytes take the name, the older
    stay in the history, and no copy appears beside the file. A head somebody
    else wrote is still kept beside it."""
    fw = await file_world(real_session, org_admin, name="bg.log")
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    agreed = (await node_of(real_session, fw.node_id)).etag
    first = await holder.push(real_session, _idem, fw.node_id, b"1\n", base=agreed)
    assert first.status_code in (200, 201), first.text
    second = await holder.push(real_session, _idem, fw.node_id, b"1\n2\n", base=agreed)
    assert second.status_code in (200, 201), second.text
    assert await drive_text(real_session, fw) == "1\n2\n"
    names = (
        await real_session.execute(
            select(FileNode.name).where(
                FileNode.parent_id == (await node_of(real_session, fw.node_id)).parent_id,
                FileNode.trashed_at.is_(None),
            )
        )
    ).scalars()
    assert [bytes(name) for name in names if b"conflicted copy" in bytes(name)] == []

    await outside_write(real_session, fw, b"a person's text\n")
    third = await holder.push(real_session, _idem, fw.node_id, b"1\n2\n3\n", base=agreed)
    assert third.status_code in (200, 201), third.text
    names = (
        await real_session.execute(
            select(FileNode.name).where(
                FileNode.parent_id == (await node_of(real_session, fw.node_id)).parent_id,
                FileNode.trashed_at.is_(None),
            )
        )
    ).scalars()
    assert len([bytes(name) for name in names if b"conflicted copy" in bytes(name)]) == 1


async def test_a_flush_writes_back_what_a_file_or_a_folder_s_files_hold_unsaved(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Before a trash: every live session at or under the trashed node writes
    back what it holds, and says which files moved and from which etag to
    which; a session with nothing unsaved is left alone."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        docs.run_sessions = False
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# acked\n"), n=1)
        before = (await node_of(real_session, fw.node_id)).etag
        folder = (await node_of(real_session, fw.node_id)).parent_id
        moved = await docs.flush_unsaved(fw.ref.org_id, folder)
        assert moved == {fw.ref.doc_id: (before, before + 1)}
        assert await drive_text(real_session, fw) == "# acked\n" + SOURCE
        assert await docs.flush_unsaved(fw.ref.org_id, fw.node_id) == {}


async def test_trashing_a_file_inside_the_write_back_delay_keeps_the_acked_edit(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person's edit was acknowledged and the file is trashed before the
    session wrote it back. The trash writes it back first and is fenced on the
    version that produced, so the caller's etag still holds, and restoring the
    file brings the edit back with it."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    node = await node_of(real_session, fw.node_id)
    async with crdt_docs() as docs:
        docs.run_sessions = False
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# acked\n"), n=1)
    # The app as a replica runs it: with its live lane (no listener needed).
    from alkera_core.config import settings
    from backend.app_factory import process_app

    app = process_app()
    from backend.services.realtime import runtime

    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    started = await runtime.start(app, decide=decide_on_record)
    try:
        trashed = await client.delete(
            f"/api/v1/files/drives/{node.drive_id}/items/{fw.node_id}",
            headers={**_idem(), "If-Match": str(node.etag)},
        )
    finally:
        await runtime.stop(app, started)
    assert trashed.status_code == 200, trashed.text
    assert await drive_text(real_session, fw) == "# acked\n" + SOURCE


async def _doc_rows(db: AsyncSession, fw: FileWorld) -> dict[str, int]:
    counts = {}
    for table in ("crdt_docs", "crdt_updates", "crdt_peers"):
        counts[table] = int(
            await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE doc_type = 'file' AND doc_id = :doc"),
                {"doc": fw.ref.doc_id},
            )
            or 0
        )
    await db.commit()
    return counts


async def test_purging_a_file_deletes_its_live_document_with_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A file deleted for good takes its live document's text, every edit's
    history and who held a peer on it in the same transaction; a file next to
    it keeps its own."""
    fw = await file_world(real_session, org_admin)
    other = await file_world(real_session, org_admin, name="kept.py")
    tab, beside = _tab(5000), _tab(5001)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# secret\n"), n=1)
        assert await _written_back(docs, real_session, fw) == "written"
        await _open(docs, real_session, other, other.world.owner, beside)
    assert all(await _doc_rows(real_session, fw))
    context = await build_files_context(real_session, acting(fw.world.owner))
    async with context.repo.transaction():
        await Trash(context.repo, context.ctx, context.clock).trash(
            NodeId(fw.node_id), if_match=(await node_of(real_session, fw.node_id)).etag
        )
    async with context.repo.transaction():
        await Trash(context.repo, context.ctx, context.clock).purge(NodeId(fw.node_id))
    await real_session.commit()
    assert await _doc_rows(real_session, fw) == {
        "crdt_docs": 0,
        "crdt_updates": 0,
        "crdt_peers": 0,
    }
    assert (await _doc_rows(real_session, other))["crdt_docs"] == 1


async def test_sessions_nobody_needs_are_deleted_and_the_rest_kept(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Untouched for a month with nothing unsaved: deleted. Untouched as long
    but holding unsaved edits, or held open by a socket now: kept. Closed (the
    file stopped being editable) for a day: deleted."""
    saved, unsaved, held, closed = [
        await file_world(real_session, org_admin, name=f"{name}.py")
        for name in ("saved", "unsaved", "held", "closed")
    ]
    async with crdt_docs() as docs:
        docs.run_sessions = False
        for n, world in enumerate((saved, unsaved, held, closed)):
            tab = _tab(5000 + n)
            await _open(docs, real_session, world, world.world.owner, tab)
            await _write(docs, real_session, world, world.world.owner, tab, tab.type(0, "x\n"), n=1)
            if world is not unsaved:
                assert await _written_back(docs, real_session, world) == "written"
    now = datetime.now(UTC)
    old = now - timedelta(days=sweeper.DORMANT_DAYS + 1)
    for world in (saved, unsaved, held):
        await real_session.execute(
            update(CrdtDoc).where(CrdtDoc.doc_id == world.ref.doc_id).values(updated_at=old)
        )
    await real_session.execute(
        update(CrdtDoc)
        .where(CrdtDoc.doc_id == closed.ref.doc_id)
        .values(
            source_etag=None,
            source_version_id=None,
            source_sha256=None,
            source_vv=None,
            source_epoch=None,
            updated_at=now - timedelta(days=sweeper.CLOSED_DAYS, hours=1),
        )
    )
    # A socket holds a peer on it right now.
    await real_session.execute(
        text(
            "INSERT INTO crdt_peers (org_id, doc_type, doc_id, user_id, held_by, held_until) "
            "VALUES (:org, 'file', :doc, :user, 'replica:p:1', :later)"
        ),
        {
            "org": held.ref.org_id,
            "doc": held.ref.doc_id,
            "user": held.world.owner.user.id,
            "later": now + timedelta(minutes=5),
        },
    )
    await real_session.commit()
    expired = await sweeper.expire_dormant(real_session, now=now)
    await real_session.commit()
    assert expired >= 2
    named = {"saved": saved, "unsaved": unsaved, "held": held, "closed": closed}
    left = {
        name: (await _doc_rows(real_session, world))["crdt_docs"] for name, world in named.items()
    }
    assert left == {"saved": 0, "unsaved": 1, "held": 1, "closed": 0}


async def test_a_write_back_and_a_merge_run_on_the_rebuild_budget_not_an_updates(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """A write back's read, the base search and the merge are work the server
    started, on a document that may hold hours of history: they run on the
    budget a rebuild gets. Held to the one a single keystroke is judged on,
    a large session's merge timed out on every try, killing the worker each
    time, and the session could never write back again."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    agreed = (await node_of(real_session, fw.node_id)).etag
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# person\n"), n=1)
        # No keystroke could be judged in this budget; the server's own work
        # must not depend on it.
        docs.validate_budget = 0.000_001
        assert await _written_back(docs, real_session, fw) == "written"
        sent = await holder.push(
            real_session, _idem, fw.node_id, (SOURCE + "# agent\n").encode(), base=agreed
        )
        assert sent.status_code in (200, 201), sent.text
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    assert tab.text == "# person\n" + SOURCE + "# agent\n"


async def test_under_a_lease_that_takes_no_inbound_write_nobody_writes_the_file(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """A folder mounted by a holder that does not take drops: a session could
    not write back, so nobody is told they may type, and a write back that is
    tried anyway is refused without touching the file."""
    fw = await file_world(real_session, org_admin)
    await _holder(real_session, client, fw, purpose="mount", inbound=False)
    async with crdt_docs() as docs:
        access = await docs.access(
            real_session, fw.ref, user=fw.world.owner.user, ent=fw.world.owner.ent, agent_id=None
        )
        assert (access.can_read, access.can_write) == (True, False)
    stamp = (await node_of(real_session, fw.node_id)).etag
    with pytest.raises(files.WriteBackRefusedError) as excinfo:
        await files.write_back(
            real_session, acting(fw.world.owner), fw.node_id, "x", if_match=stamp
        )
    await real_session.rollback()
    assert excinfo.value.reason == "leased"
    assert await drive_text(real_session, fw) == SOURCE


# ---------------------------------------------------------------------------
# Who a write back is written as, and saying when it cannot be
# ---------------------------------------------------------------------------


async def _demote(db: AsyncSession, fw: FileWorld, who: Person) -> None:
    """Narrow ``who``'s share of the chat to Can view."""
    chat = await db.get(WorkspaceObject, uuid.UUID(fw.world.ref.doc_id))
    owner = await db.get(User, fw.world.owner.user.id)
    assert chat is not None and owner is not None
    await share_chat_with(
        db,
        chat=chat,
        owner=owner,
        principal=Principal(kind="user", id=who.user.id),
        role=ROLE_READER,
    )


@asynccontextmanager
async def _announced(docs: CrdtDocs) -> AsyncIterator[list[tuple[bool, str]]]:
    """Every saving notice the store sends, in order, as ``(paused, reason)``;
    still sent for real."""
    said: list[tuple[bool, str]] = []
    real = docs.announce_saving

    async def record(ref: DocRef, *, epoch: int, paused: bool, reason: str = "") -> None:
        said.append((paused, reason))
        await real(ref, epoch=epoch, paused=paused, reason=reason)

    docs.announce_saving = record
    yield said


async def test_a_last_editor_who_lost_writing_is_succeeded_by_one_who_still_may(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The person who typed last was narrowed to Can view: the write back is
    written as someone else in the session who may still write the file,
    rather than refused until somebody types again."""
    fw = await file_world(real_session, org_admin)
    mine, theirs = _tab(5000), _tab(5001)
    async with crdt_docs() as docs, _announced(docs) as said:
        await _open(docs, real_session, fw, fw.world.owner, mine)
        await _open(docs, real_session, fw, fw.world.writer, theirs)
        await _write(docs, real_session, fw, fw.world.owner, mine, mine.type(0, "# owner\n"), n=1)
        theirs.receive(mine.since(theirs.vv))
        await _write(
            docs, real_session, fw, fw.world.writer, theirs, theirs.type(0, "# writer\n"), n=2
        )
        await _demote(real_session, fw, fw.world.writer)
        assert await _written_back(docs, real_session, fw) == "written"
    version = await head_version(real_session, fw)
    assert version.created_by == fw.world.owner.user.id
    assert await drive_text(real_session, fw) == "# writer\n# owner\n" + SOURCE
    assert said == []


async def test_with_nobody_left_who_may_write_saving_is_paused_and_said(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    tab = _tab(5001)
    async with crdt_docs() as docs, _announced(docs) as said:
        await _open(docs, real_session, fw, fw.world.writer, tab)
        await _write(docs, real_session, fw, fw.world.writer, tab, tab.type(0, "# mine\n"), n=1)
        await _demote(real_session, fw, fw.world.writer)
        assert await _written_back(docs, real_session, fw) == "refused"
        assert said == [(True, "no_writer")]
    assert await drive_text(real_session, fw) == SOURCE


async def test_a_session_that_cannot_save_is_parked_on_a_widening_wait_and_said_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """A folder taken by a machine that takes no outside write: every try is
    refused. The session is parked with the reason, the sweep leaves it alone
    until its wait is up, each refusal doubles the wait, readers are told once,
    and the write back that finally lands clears all of it."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs, _announced(docs) as said:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# typed\n"), n=1)
        holder = await _holder(real_session, client, fw, purpose="mount", inbound=False)
        docs.sweep_orgs = frozenset({fw.ref.org_id})

        assert await _written_back(docs, real_session, fw) == "refused"
        first = await _row(real_session, fw)
        assert (first.save_paused_reason, first.save_failures) == ("leased", 1)
        assert first.save_retry_at is not None
        first_wait = first.save_retry_at
        # Not due: the sweep takes nothing.
        assert await docs.sweep_unsaved() == 0

        assert await _written_back(docs, real_session, fw) == "refused"
        second = await _row(real_session, fw)
        assert second.save_failures == 2
        assert second.save_retry_at is not None and second.save_retry_at > first_wait
        assert said == [(True, "leased")]

        # Its wait up, the sweep takes it again.
        due = second.save_retry_at + timedelta(seconds=1)
        found = await sweeper.unsaved(
            docs.session_factory, docs.registry, limit=10, now=due, orgs=docs.sweep_orgs
        )
        assert found == [fw.ref]

        released = await holder.release(real_session, _idem)
        assert released.status_code in (200, 204), released.text
        assert await _written_back(docs, real_session, fw) == "written"
    landed = await _row(real_session, fw)
    assert (landed.save_paused_reason, landed.save_failures, landed.save_retry_at) == (
        None,
        0,
        None,
    )


async def test_the_sweep_takes_the_oldest_first_and_never_the_same_session_twice(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Three unsaved sessions, a sweep that takes two: the two least recently
    touched go, and a second sweep (another replica) takes only the third:
    what a sweep takes is its replica's until the claim runs out."""
    worlds = [await file_world(real_session, org_admin, name=f"f{n}.py") for n in range(3)]
    tabs = [_tab(5000 + n) for n in range(3)]
    async with crdt_docs() as docs:
        docs.run_sessions = False
        for n, (world, tab) in enumerate(zip(worlds, tabs, strict=True)):
            await _open(docs, real_session, world, world.world.owner, tab)
            await _write(
                docs, real_session, world, world.world.owner, tab, tab.type(0, f"# {n}\n"), n=1
            )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Oldest first: f2, then f0, then f1.
    for world, at in zip(
        worlds, [base + timedelta(minutes=1), base + timedelta(minutes=2), base], strict=True
    ):
        await real_session.execute(
            update(CrdtDoc)
            .where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == world.ref.doc_id)
            .values(updated_at=at)
        )
    await real_session.commit()
    async with lane_docs() as replica:
        orgs = frozenset({org_admin.org_id})
        now = datetime.now(UTC)
        first = await sweeper.unsaved(
            replica.session_factory, replica.registry, limit=2, now=now, orgs=orgs
        )
        second = await sweeper.unsaved(
            replica.session_factory, replica.registry, limit=2, now=now, orgs=orgs
        )
    assert first == [worlds[2].ref, worlds[0].ref]
    assert second == [worlds[1].ref]


async def test_the_sweeps_question_is_answered_from_its_index(real_session: AsyncSession) -> None:
    """The sweep runs on every replica every few seconds over a table every
    live session writes to: its predicate is the partial index's, so it never
    scans the table."""
    await real_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        str(row[0])
        for row in await real_session.execute(
            text(
                "EXPLAIN SELECT org_id FROM crdt_docs WHERE doc_type IN ('file') AND "
                + CRDT_UNSAVED_PREDICATE
                + " AND (save_retry_at IS NULL OR save_retry_at <= now()) "
                "ORDER BY save_retry_at NULLS FIRST, updated_at LIMIT 200"
            )
        )
    )
    await real_session.rollback()
    assert "ix_crdt_docs_unsaved" in plan, plan


async def test_a_machine_holding_the_folder_pauses_saving_until_it_lets_go(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """A folder taken by a machine that takes no outside write (``alkera
    files mount``) before the person's edits were written back: saving is
    paused and says why (every tab re-reads its grant on it, and may no
    longer type), the edits stay in the document, and they land once the
    machine lets go."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs, _announced(docs) as said:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# typed\n"), n=1)
        holder = await _holder(real_session, client, fw, purpose="mount", inbound=False)
        assert await _written_back(docs, real_session, fw) == "refused"
        assert said == [(True, "leased")]
        assert await drive_text(real_session, fw) == SOURCE
        released = await holder.release(real_session, _idem)
        assert released.status_code in (200, 204), released.text
        assert await _written_back(docs, real_session, fw) == "written"
        assert said == [(True, "leased"), (False, "")]
        # Said once: a later pass that lands says nothing more.
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# more\n"), n=2)
        assert await _written_back(docs, real_session, fw) == "written"
        assert said == [(True, "leased"), (False, "")]
    assert await drive_text(real_session, fw) == "# more\n# typed\n" + SOURCE


# ---------------------------------------------------------------------------
# Nothing waits on a write back
# ---------------------------------------------------------------------------


async def test_typing_is_never_held_up_by_a_write_back_in_flight(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write back waiting on the drive (a slow store, a busy database)
    holds nothing people typing need: their edits commit at once rather than
    queueing on the document's row until the drive answers, and the write
    back then lands what it read."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    in_flight = asyncio.Event()
    release = asyncio.Event()
    real_write = files.write_back

    async def slow_write(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        await release.wait()
        return await real_write(*args, **kwargs)

    monkeypatch.setattr(files, "write_back", slow_write)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# one\n"), n=1)

        async def back() -> str:
            async with docs.session_factory() as db:
                settled = await docs.sessions.write_back(db, fw.ref)
                await db.commit()
                return settled.outcome

        pass_ = asyncio.create_task(back())
        await asyncio.wait_for(in_flight.wait(), 10)
        # Typed while the write back waits on the drive: committed promptly.
        async with docs.session_factory() as db:
            row = await docs.row(db, fw.ref)
            assert row is not None
            applied = await asyncio.wait_for(
                docs.apply(
                    db,
                    fw.ref,
                    user=fw.world.owner.user,
                    ent=fw.world.owner.ent,
                    agent_id=None,
                    epoch=row.epoch,
                    peer=tab.peer,
                    update_id="u-2",
                    update=tab.type(0, "# two\n"),
                    socket_peer_id="p:5000",
                ),
                2,
            )
            await db.commit()
        assert applied.changed
        release.set()
        assert await asyncio.wait_for(pass_, 20) == "written"
    # The write back wrote what it had read; the edit made meanwhile is the
    # next pass's.
    assert await drive_text(real_session, fw) == "# one\n" + SOURCE
    async with crdt_docs() as docs:
        assert await _written_back(docs, real_session, fw) == "written"
    assert await drive_text(real_session, fw) == "# two\n# one\n" + SOURCE


async def test_a_write_back_cut_off_before_it_recorded_itself_is_never_merged_again(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy cancels a write back after the drive took the version and
    before the session recorded it. The next pass finds a version it did not
    record: it is the document's own, so it is recorded, never merged in as
    an outside change (which would repeat every edit since the last record),
    and the file ends with each edit exactly once."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# one\n"), n=1)
        real_write = docs.sessions._write

        async def cut_off(*args: Any, **kwargs: Any) -> Any:
            await real_write(*args, **kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(docs.sessions, "_write", cut_off)
        with pytest.raises(asyncio.CancelledError):
            await docs.sessions.write_back(real_session, fw.ref)
        await real_session.rollback()
        monkeypatch.setattr(docs.sessions, "_write", real_write)
        assert await drive_text(real_session, fw) == "# one\n" + SOURCE

        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# two\n"), n=2)
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "unchanged" and settled.applied is None
        assert await _written_back(docs, real_session, fw) == "written"
    assert tab.text == "# two\n# one\n" + SOURCE
    assert await drive_text(real_session, fw) == "# two\n# one\n" + SOURCE


async def test_a_box_edit_on_a_cut_off_write_back_is_merged_from_it_not_before_it(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write back lands (etag E+1) and is cut off before the session records
    it; the box takes it and sends the agent's edit on top (E+2). The session
    knows nothing past E, but the drive's own version says which point of the
    document E+1 was: the agent's edit is merged from there, so the person's
    text is not read as added twice."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, purpose="chat", inbound=True)
    tab = _tab(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# one\n"), n=1)
        real_write = docs.sessions._write

        async def cut_off(*args: Any, **kwargs: Any) -> Any:
            await real_write(*args, **kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(docs.sessions, "_write", cut_off)
        with pytest.raises(asyncio.CancelledError):
            await docs.sessions.write_back(real_session, fw.ref)
        await real_session.rollback()
        monkeypatch.setattr(docs.sessions, "_write", real_write)
        written = (await node_of(real_session, fw.node_id)).etag
        assert await drive_text(real_session, fw) == "# one\n" + SOURCE
        assert (await holder.settle(fw.node_id, "applied")).status_code == 200

        sent = await holder.push(
            real_session,
            _idem,
            fw.node_id,
            ("# one\n" + SOURCE + "# agent\n").encode(),
            base=written,
        )
        assert sent.status_code in (200, 201), sent.text
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "merged" and settled.applied is not None
        tab.receive(settled.applied.delta)
    assert tab.text == "# one\n" + SOURCE + "# agent\n"


async def test_edits_a_stopped_process_left_unwritten_are_written_by_the_next(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A process takes an edit and stops before its write back (a deploy, a
    crash). Nobody types again. The next process finds the session by its row
    alone and writes it back within seconds; a session already written back is
    left alone."""
    fw = await file_world(real_session, org_admin)
    saved = await file_world(real_session, org_admin, name="saved.py")
    tab, other = _tab(5000), _tab(5001)
    async with crdt_docs() as stopped:
        await _open(stopped, real_session, fw, fw.world.owner, tab)
        await _write(stopped, real_session, fw, fw.world.owner, tab, tab.type(0, "# kept\n"), n=1)
        await _open(stopped, real_session, saved, saved.world.owner, other)
        await _write(
            stopped, real_session, saved, saved.world.owner, other, other.type(0, "# x\n"), n=1
        )
        assert await _written_back(stopped, real_session, saved) == "written"
    assert await drive_text(real_session, fw) == SOURCE
    async with lane_docs() as restarted:
        # A sweep scoped to orgs that hold none of it finds nothing: a test's
        # backend never reaches the sessions earlier tests left behind.
        restarted.sweep_orgs = frozenset({uuid.uuid4()})
        assert await restarted.sweep_unsaved() == 0
        assert await drive_text(real_session, fw) == SOURCE
        restarted.sweep_orgs = frozenset({fw.ref.org_id})
        found = await restarted.sweep_unsaved()
        assert found == 1
        for _ in range(80):
            if await drive_text(real_session, fw) == "# kept\n" + SOURCE:
                break
            await asyncio.sleep(0.1)
        assert await drive_text(real_session, fw) == "# kept\n" + SOURCE
    assert await drive_text(real_session, saved) == "# x\n" + SOURCE
    # Written back, the session is not one a sweep finds again.
    row = await _row(real_session, fw)
    assert row.projection["sha256"] == row.source_sha256


async def test_a_slow_drive_holds_no_admission_to_the_sandbox(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane admits only so many requests to the sandbox at once. A write
    back waiting on a slow store holds none of those admissions, so typing
    into every other document goes on while it waits."""
    fw = await file_world(real_session, org_admin)
    tab = _tab(5000)
    in_flight = asyncio.Event()
    release = asyncio.Event()
    real_write = files.write_back

    async def slow_write(*args: Any, **kwargs: Any) -> Any:
        in_flight.set()
        await release.wait()
        return await real_write(*args, **kwargs)

    monkeypatch.setattr(files, "write_back", slow_write)
    async with crdt_docs() as docs:
        await _open(docs, real_session, fw, fw.world.owner, tab)
        await _write(docs, real_session, fw, fw.world.owner, tab, tab.type(0, "# one\n"), n=1)
        docs._admission = asyncio.Semaphore(1)
        docs.run_sessions = True
        docs._schedule(fw.ref, "write_back", delay=0.0)
        await asyncio.wait_for(in_flight.wait(), 10)

        async def admitted() -> None:
            async with docs.admitted():
                pass

        await asyncio.wait_for(admitted(), 2)
        release.set()
        for _ in range(80):
            if await drive_text(real_session, fw) == "# one\n" + SOURCE:
                break
            await asyncio.sleep(0.1)
    assert await drive_text(real_session, fw) == "# one\n" + SOURCE
