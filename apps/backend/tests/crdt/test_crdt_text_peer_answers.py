"""What a text peer's submit answers, and what a replica keeps for it, against
real Postgres, real Files and real sandbox workers.

A submit's answer belongs to its document (an id the holder chose is never a
key on its own), says whether the document is saving, and a version the
holder named that is not the document's is searched for without holding the
document's row. What a replica remembers per document stays bounded.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from alkera_core.models import CrdtDoc
from backend.services.crdt import sweeper
from backend.services.crdt import text_peers as text_peers_module
from backend.services.crdt.docs import CrdtDocs
from backend.services.crdt.registry import DocRef
from backend.services.crdt.text_peers import TextPeers, encode_token
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Peer
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import SOURCE, FileWorld, file_world

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

MACHINE = "7b3d2c5e-4f20-4c4d-9f7e-93c5e1b2d444"


@asynccontextmanager
async def _docs() -> AsyncIterator[CrdtDocs]:
    async with lane_docs() as docs:
        docs.run_sessions = False
        yield docs


async def _open(docs: CrdtDocs, db: AsyncSession, fw: FileWorld) -> Peer:
    who = fw.world.owner
    tab = Peer(6200, container="content")
    await docs.access(db, fw.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, fw.ref, since=None, epoch_seen=None)
    await db.commit()
    tab.receive(sync.data)
    return tab


async def _row(db: AsyncSession, fw: FileWorld) -> CrdtDoc:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    await db.commit()
    return row


async def test_one_submit_id_on_two_documents_is_answered_with_each_one_s_own_text(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The id is the holder's to choose: a reused one on another document is
    merged into that document and answered with its text, never with the
    answer the replica kept for the first."""
    first = await file_world(real_session, org_admin, name="first.py")
    second = await file_world(real_session, org_admin, content=b"other\n", name="second.py")
    submit_id = uuid.uuid4().hex
    async with _docs() as docs:
        await _open(docs, real_session, first)
        await _open(docs, real_session, second)
        one = await docs.peers.submit(
            first.ref, text=SOURCE + "# one\n", submit_id=submit_id, agent_id=MACHINE
        )
        two = await docs.peers.submit(
            second.ref, text="other\n# two\n", submit_id=submit_id, agent_id=MACHINE
        )
    assert one is not None and one.text == SOURCE + "# one\n"
    assert two is not None and two.text == "other\n# two\n"


@pytest.mark.parametrize(
    ("parked", "saved"),
    [
        pytest.param(None, True, id="saving"),
        pytest.param("no_writer", False, id="saving-paused"),
    ],
)
async def test_a_submit_says_whether_the_document_is_saving(
    real_session: AsyncSession, org_admin: OrgWithAdmin, parked: str | None, saved: bool
) -> None:
    """The holder is told the drive will not get its text while the
    document's write back is parked, so it can upload the file itself; a
    retry of the same submit says the same."""
    fw = await file_world(real_session, org_admin)
    submit_id = uuid.uuid4().hex
    async with _docs() as docs:
        await _open(docs, real_session, fw)
        if parked is not None:
            await sweeper.park(docs.session_factory, fw.ref, reason=parked, now=datetime.now(UTC))
        answer = await docs.peers.submit(
            fw.ref, text=SOURCE + "# agent\n", submit_id=submit_id, agent_id=MACHINE
        )
        retried = await TextPeers(docs).submit(
            fw.ref, text=SOURCE + "# agent\n", submit_id=submit_id, agent_id=MACHINE
        )
    assert answer is not None and answer.saved is saved
    assert retried is not None and retried.repeat and retried.saved is saved


async def test_a_named_version_the_document_lacks_is_searched_for_with_the_row_let_go(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder names a state of this epoch that holds edits the document
    never took. The submit looks for the closest version the document has
    instead, comparing it at many versions, and nobody typing waits on that:
    the document's row is free while it looks. The text still lands, adding
    what it adds and removing nothing."""
    fw = await file_world(real_session, org_admin)
    async with _docs() as docs:
        tab = await _open(docs, real_session, fw)
        epoch = (await _row(real_session, fw)).epoch
        # A tab that typed and never sent it: its state is not the document's.
        tab.type(0, "# never sent\n")
        foreign = encode_token(epoch, tab.vv)
        row_free: list[bool] = []
        real_search = docs.sessions.closest_base

        async def searching(db: AsyncSession, ref: DocRef, doc: CrdtDoc, text_: str) -> bytes:
            async with docs.session_factory() as other:
                try:
                    await other.execute(
                        text(
                            "SELECT 1 FROM crdt_docs WHERE org_id = :org AND doc_type = 'file' "
                            "AND doc_id = :doc FOR UPDATE NOWAIT"
                        ),
                        {"org": ref.org_id, "doc": ref.doc_id},
                    )
                    row_free.append(True)
                except DBAPIError:
                    row_free.append(False)
                await other.rollback()
            return await real_search(db, ref, doc, text_)

        monkeypatch.setattr(docs.sessions, "closest_base", searching)
        answer = await docs.peers.submit(
            fw.ref,
            text=SOURCE + "# agent\n",
            submit_id=uuid.uuid4().hex,
            agent_id=MACHINE,
            base_token=foreign,
        )
    assert row_free and all(row_free), row_free
    assert answer is not None and answer.text == SOURCE + "# agent\n"


async def test_what_a_replica_remembers_per_document_stays_bounded(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica that serves a long time touches every document its sockets
    ever opened. It remembers a document's holder and its last open notice
    only while they can still be used: past that they go, and however many
    documents are touched at once it keeps at most a fixed number."""
    monkeypatch.setattr(text_peers_module, "REMEMBERED_DOCS", 8)
    now = [100.0]
    async with _docs() as docs:
        docs.run_sessions = True
        peers = TextPeers(docs, notify_delay=3600.0, clock=lambda: now[0])
        refs = [DocRef(org_admin.org_id, "file", str(uuid.uuid4())) for _ in range(20)]
        try:
            for ref in refs[:5]:
                peers.opened(ref)
                await peers.notify(ref, joined_or_not=True)
            assert len(peers._opened) == 5 and len(peers._holders) == 5
            # Every one of them is past its use now.
            now[0] += text_peers_module.OPEN_NOTICE_SECONDS + 1
            peers.opened(refs[5])
            await peers.notify(refs[5], joined_or_not=True)
            assert set(peers._opened) == {refs[5].key}
            assert set(peers._holders) == {refs[5].key}
            # Many at once: never more than the bound.
            for ref in refs[6:]:
                peers.opened(ref)
                await peers.notify(ref, joined_or_not=True)
            assert len(peers._opened) == 8 and len(peers._holders) == 8
            assert refs[-1].key in peers._opened and refs[-1].key in peers._holders
        finally:
            await peers.aclose()
