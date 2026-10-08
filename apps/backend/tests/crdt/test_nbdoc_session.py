"""A notebook co-edited live: the ``notebook`` document type against real
Postgres, real Files, real sandbox workers and the real format API.

A person's typing over the socket, an agent's operations through
``NotebookPeers``, the view at a frontier a client holds, the file written
back to the drive, normalization as the server peer, and a file the session
cannot hold (not a notebook, a newer major format)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from alkera_core.models import CrdtDoc, CrdtUpdate, WorkspaceObject
from alkera_core.notebooks.models import NotebookEdit
from alkera_notebook.engine.models import GraphSummary
from backend.services.crdt.docs import Applied, CrdtDocs, CrdtError
from backend.services.crdt.notebook_peers import NotebookOpError
from backend.services.crdt.registry import DocRef
from backend.services.crdt.sandbox import notebook as nb
from backend.services.crdt.text_peers import encode_token
from loro import ExportMode, LoroDoc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Person
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import FileWorld, drive_text, file_world, outside_write
from tests.crdt.test_nbdoc_real_format import FULL

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

WEEKLY = "s7t8v9w0x1"
ORDERS = "m2n3p4q5r6"


@asynccontextmanager
async def crdt_docs() -> AsyncIterator[CrdtDocs]:
    async with lane_docs() as docs:
        docs.run_sessions = False
        yield docs


async def notebook_world(
    db: AsyncSession,
    org: OrgWithAdmin,
    text: str = FULL,
    *,
    workspace: WorkspaceObject | None = None,
) -> FileWorld:
    fw = await file_world(
        db, org, content=text.encode(), name="weekly.alknb.py", workspace=workspace
    )
    fw.ref = DocRef(org_id=fw.ref.org_id, doc_type="notebook", doc_id=fw.ref.doc_id)
    return fw


class Tab:
    """A browser tab holding the notebook document."""

    def __init__(self, peer: int) -> None:
        self.doc = LoroDoc()  # type: ignore[no-untyped-call]
        self.doc.peer_id = peer
        self.peer = peer

    def type(self, cell_id: str, at: int, text: str) -> bytes:
        before = self.doc.oplog_vv
        source = nb.child(nb.child(self.doc.get_map("cells"), cell_id), "source")
        source.insert(at, text)
        self.doc.commit()
        return bytes(self.doc.export(ExportMode.Updates(before)))

    def erase(self, cell_id: str, at: int, length: int) -> bytes:
        before = self.doc.oplog_vv
        source = nb.child(nb.child(self.doc.get_map("cells"), cell_id), "source")
        source.delete(at, length)
        self.doc.commit()
        return bytes(self.doc.export(ExportMode.Updates(before)))

    def place_again(self, cell_id: str) -> bytes:
        """Put ``cell_id`` in the order a second time (two tabs placing one
        cell), as the update a tab sends."""
        before = self.doc.oplog_vv
        self.doc.get_movable_list("order").push(cell_id)
        self.doc.commit()
        return bytes(self.doc.export(ExportMode.Updates(before)))

    @property
    def vv(self) -> bytes:
        return bytes(self.doc.oplog_vv.encode())


async def _open(docs: CrdtDocs, db: AsyncSession, fw: FileWorld, who: Person, tab: Tab) -> None:
    await docs.access(db, fw.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, fw.ref, since=None, epoch_seen=None)
    await db.commit()
    tab.doc.import_(sync.data)


async def _row(db: AsyncSession, fw: FileWorld) -> CrdtDoc:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == "notebook", CrdtDoc.doc_id == fw.ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    return row


async def _write(
    docs: CrdtDocs, db: AsyncSession, fw: FileWorld, who: Person, tab: Tab, data: bytes, n: int
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
        update_id=f"u-{n}",
        update=data,
        socket_peer_id=f"p:{tab.peer}",
    )
    await db.commit()
    await docs.after_commit(fw.ref, applied)
    return applied


def _source(view: dict[str, object], cell_id: str) -> str:
    cells = view["cells"]
    assert isinstance(cells, list)
    return str(next(c for c in cells if c["id"] == cell_id)["source"])


async def test_a_persons_typing_is_recorded_per_cell_and_written_back_as_the_file(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        await _write(docs, real_session, fw, owner, tab, tab.type(WEEKLY, 0, "# by region\n"), 1)
        settled = await docs.sessions.write_back(real_session, fw.ref)
        await real_session.commit()
    assert settled.outcome == "written"
    written = await drive_text(real_session, fw)
    assert "    # by region\n    weekly = orders" in written
    assert 'alkera_id="s7t8v9w0x1"' in written
    edits = (
        (await real_session.execute(select(NotebookEdit).where(NotebookEdit.item_id == fw.node_id)))
        .scalars()
        .all()
    )
    assert [(e.cell_id, e.actor_key, e.actor_kind) for e in edits] == [
        (WEEKLY, f"user:{owner.user.id}", "person")
    ]


async def test_an_agents_operations_merge_with_typing_since_its_token_and_are_idempotent(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        read = await docs.notebooks.view(fw.ref)
        await _write(docs, real_session, fw, owner, tab, tab.type(WEEKLY, 0, "# live\n"), 1)
        ops = [
            {
                "op": "edit",
                "cell_id": WEEKLY,
                "edits": [{"old": ".sum()", "new": '.sum().alias("total")'}],
            }
        ]
        first = await docs.notebooks.apply(
            fw.ref,
            ops=ops,
            base_token=read.token,
            submit_id="agent-sub-0001",
            agent_id="agent:analyst",
            author=None,
        )
        again = await docs.notebooks.apply(
            fw.ref,
            ops=ops,
            base_token=read.token,
            submit_id="agent-sub-0001",
            agent_id="agent:analyst",
            author=None,
        )
        view = await docs.notebooks.view(fw.ref)
    assert first.repeat is False and again == first
    assert first.caret is not None and first.touched == [WEEKLY]
    assert _source(view.view, WEEKLY).startswith("# live\n")
    assert '.alias("total")' in _source(view.view, WEEKLY)
    logged = (
        (
            await real_session.execute(
                select(CrdtUpdate.agent_id).where(
                    CrdtUpdate.doc_id == fw.ref.doc_id,
                    CrdtUpdate.update_id == "nbops-agent-sub-0001",
                )
            )
        )
        .scalars()
        .all()
    )
    assert logged == ["agent:analyst"]


async def test_a_retry_on_another_replica_is_recognised_in_the_log(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    ops = [{"op": "replace", "cell_id": ORDERS, "source": "SELECT 1"}]
    async with crdt_docs() as docs:
        await docs.notebooks.apply(
            fw.ref, ops=ops, base_token=None, submit_id="retry-0000001", agent_id="a", author=None
        )
    async with crdt_docs() as other_replica:
        again = await other_replica.notebooks.apply(
            fw.ref, ops=ops, base_token=None, submit_id="retry-0000001", agent_id="a", author=None
        )
    assert again.repeat is True
    count = (
        (
            await real_session.execute(
                select(CrdtUpdate).where(
                    CrdtUpdate.doc_id == fw.ref.doc_id,
                    CrdtUpdate.update_id == "nbops-retry-0000001",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(count) == 1


async def test_a_refused_operation_names_its_index_and_writes_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    async with crdt_docs() as docs:
        with pytest.raises(NotebookOpError) as refused:
            await docs.notebooks.apply(
                fw.ref,
                ops=[
                    {"op": "replace", "cell_id": ORDERS, "source": "SELECT 2"},
                    {"op": "edit", "cell_id": WEEKLY, "edits": [{"old": "nothing", "new": "x"}]},
                ],
                base_token=None,
                submit_id=None,
                agent_id="a",
                author=None,
            )
        view = await docs.notebooks.view(fw.ref)
    assert (refused.value.index, refused.value.op_code) == (1, "edit_not_found")
    assert _source(view.view, ORDERS).startswith("SELECT order_id")


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("token", id="a-token-the-store-handed-out"),
        pytest.param("base64", id="an-editors-base64-version-vector"),
    ],
)
async def test_a_run_waits_for_the_frontier_so_text_typed_just_before_runs(
    real_session: AsyncSession, org_admin: OrgWithAdmin, spelling: str
) -> None:
    """Type, then run within 10 ms: the run's view waits for the typing to
    reach the document and takes the typed text."""
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        row = await _row(real_session, fw)
        update = tab.type(ORDERS, 0, "-- typed\n")
        frontier = (
            encode_token(row.epoch, tab.vv)
            if spelling == "token"
            else base64.b64encode(tab.vv).decode()
        )

        async def typing_lands() -> None:
            await asyncio.sleep(0.01)
            async with docs.session_factory() as db:
                await _write(docs, db, fw, owner, tab, update, 1)

        landing = asyncio.create_task(typing_lands())
        view = await docs.notebooks.view(fw.ref, frontier=frontier, wait_seconds=2.0)
        await landing
    assert view.covered is True
    assert _source(view.view, ORDERS).startswith("-- typed\n")


async def test_a_frontier_that_never_arrives_is_reported_uncovered_after_the_wait(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        row = await _row(real_session, fw)
        tab.type(ORDERS, 0, "-- never sent\n")
        view = await docs.notebooks.view(
            fw.ref, frontier=encode_token(row.epoch, tab.vv), wait_seconds=0.1
        )
    assert view.covered is False
    assert not _source(view.view, ORDERS).startswith("-- never sent")


async def test_an_update_out_of_normal_form_is_normalized_by_the_server_peer(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        applied = await _write(docs, real_session, fw, owner, tab, tab.place_again(WEEKLY), 1)
        assert applied.notes.get("normal") is False
        # The commit scheduled the server peer's pass; it has run once drained.
        await docs.drain()
        assert await docs.notebooks.normalize(fw.ref) is False
        view = await docs.notebooks.view(fw.ref)
    agents = (
        (
            await real_session.execute(
                select(CrdtUpdate.agent_id)
                .where(CrdtUpdate.doc_id == fw.ref.doc_id)
                .order_by(CrdtUpdate.log_seq)
            )
        )
        .scalars()
        .all()
    )
    assert agents == [None, "server:normalize"]
    assert [c["id"] for c in view.view["cells"]].count(WEEKLY) == 1


async def test_a_file_that_is_not_a_notebook_is_not_a_notebook_document(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin, content=FULL.encode(), name="weekly.py")
    ref = DocRef(org_id=fw.ref.org_id, doc_type="notebook", doc_id=fw.ref.doc_id)
    async with crdt_docs() as docs:
        with pytest.raises(CrdtError) as hidden:
            await docs.access(
                real_session, ref, user=fw.world.owner.user, ent=fw.world.owner.ent, agent_id=None
            )
        with pytest.raises(CrdtError) as refused:
            await docs.notebooks.view(ref)
    assert hidden.value.code == "not_found"
    assert refused.value.code == "not_editable"


async def test_a_newer_major_format_written_outside_closes_the_session_read_only(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    async with crdt_docs() as docs:
        await docs.notebooks.view(fw.ref)
        await outside_write(
            real_session, fw, FULL.replace('# format = "1.0"', '# format = "2.0"').encode()
        )
        settled = await docs.sessions.merge_outside(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "closed"
        with pytest.raises(CrdtError) as refused:
            await docs.notebooks.apply(
                fw.ref,
                ops=[{"op": "replace", "cell_id": ORDERS, "source": "SELECT 3"}],
                base_token=None,
                submit_id=None,
                agent_id="a",
                author=None,
            )
    assert refused.value.code == "not_editable"
    assert "newer_format" in str(refused.value.reason)


async def test_the_graph_is_analysed_off_the_hot_path_at_the_latest_state(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Off the hot path: a result is answered before the analysis, which
    runs once the edits are still and holds the latest state's errors."""
    fw = await notebook_world(real_session, org_admin)
    async with crdt_docs() as docs:
        docs.notebooks.graph_debounce = 0.3
        first = await docs.notebooks.apply(
            fw.ref,
            ops=[{"op": "replace", "cell_id": ORDERS, "source": "SELECT 1"}],
            base_token=None,
            submit_id=None,
            agent_id="a",
            author=None,
        )
        second = await docs.notebooks.apply(
            fw.ref,
            ops=[{"op": "insert", "source": "weekly = 2", "after": WEEKLY}],
            base_token=None,
            submit_id=None,
            agent_id="a",
            author=None,
        )
        assert docs.notebooks.graph_at(fw.ref, second.token) is None
        await docs.notebooks.drain_graphs()
        analysed = docs.notebooks.graph_at(fw.ref, second.token)
        assert docs.notebooks.graph_at(fw.ref, first.token) is None
    assert analysed is not None
    summary = GraphSummary.of_analysis(analysed)
    # Two cells now define ``weekly``: the analysis names both, as structured
    # errors that say which name and which cells.
    named = {cell for cell, info in summary.cells.items() if info.errors}
    assert WEEKLY in named and len(named) == 2
    (error,) = summary.cells[WEEKLY].errors
    assert (error.code, error.name, sorted(error.cells or [])) == (
        "multiple_definitions",
        "weekly",
        sorted(named),
    )


async def test_a_writer_keeps_one_peer_and_its_old_token_still_merges_on_its_own_edits(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two batches of one agent session at the same old token: both are its
    peer's, and the second is made on top of the first (it knows what it
    wrote), never writing an operation id twice."""
    fw = await notebook_world(real_session, org_admin)
    async with crdt_docs() as docs:
        read = await docs.notebooks.view(fw.ref)
        first = await docs.notebooks.apply(
            fw.ref,
            ops=[{"op": "edit", "cell_id": WEEKLY, "edits": [{"old": ".sum()", "new": ".mean()"}]}],
            base_token=read.token,
            submit_id=None,
            agent_id="agent:analyst",
            author=None,
            actor_key="agent:session-1",
        )
        second = await docs.notebooks.apply(
            fw.ref,
            ops=[{"op": "edit", "cell_id": WEEKLY, "edits": [{"old": ".mean()", "new": ".max()"}]}],
            base_token=read.token,
            submit_id=None,
            agent_id="agent:analyst",
            author=None,
            actor_key="agent:session-1",
        )
        other = await docs.notebooks.apply(
            fw.ref,
            ops=[{"op": "replace", "cell_id": ORDERS, "source": "SELECT 1"}],
            base_token=None,
            submit_id=None,
            agent_id="agent:other",
            author=None,
            actor_key="agent:session-2",
        )
        view = await docs.notebooks.view(fw.ref)
    assert first.peer == second.peer != other.peer
    assert ".max()" in _source(view.view, WEEKLY) and ".mean()" not in _source(view.view, WEEKLY)
    peers = (
        (
            await real_session.execute(
                select(CrdtUpdate.loro_peer)
                .where(CrdtUpdate.doc_id == fw.ref.doc_id)
                .order_by(CrdtUpdate.log_seq)
            )
        )
        .scalars()
        .all()
    )
    assert peers == [first.peer, first.peer, other.peer]


async def test_a_notebook_has_no_plain_text_document_beside_its_notebook_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two live documents would each write the one file back: a notebook's
    file document is refused, and a plain file's notebook one is too."""
    fw = await notebook_world(real_session, org_admin)
    as_text = DocRef(org_id=fw.ref.org_id, doc_type="file", doc_id=fw.ref.doc_id)
    async with crdt_docs() as docs:
        with pytest.raises(CrdtError) as refused:
            await docs.sync(real_session, as_text, since=None, epoch_seen=None)
    await real_session.rollback()
    assert (refused.value.code, refused.value.reason) == ("not_editable", "notebook")


async def test_unsaved_notebook_edits_are_written_back_before_a_trash(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    async with crdt_docs() as docs:
        await docs.notebooks.apply(
            fw.ref,
            ops=[{"op": "replace", "cell_id": ORDERS, "source": "SELECT 42"}],
            base_token=None,
            submit_id=None,
            agent_id="a",
            author=fw.world.owner.user,
            actor_key="agent:s",
        )
        moved = await docs.flush_unsaved(fw.ref.org_id, fw.node_id)
    assert set(moved) == {fw.ref.doc_id}
    assert "SELECT 42" in await drive_text(real_session, fw)


async def test_a_dormant_notebook_session_with_nothing_unsaved_is_deleted(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from datetime import UTC, datetime, timedelta

    from backend.services.crdt import sweeper
    from sqlalchemy import update

    fw = await notebook_world(real_session, org_admin)
    async with crdt_docs() as docs:
        await docs.notebooks.view(fw.ref)
    now = datetime.now(UTC)
    await real_session.execute(
        update(CrdtDoc)
        .where(CrdtDoc.doc_id == fw.ref.doc_id)
        .values(updated_at=now - timedelta(days=sweeper.DORMANT_DAYS + 1))
    )
    await real_session.commit()
    await sweeper.expire_dormant(real_session, now=now)
    await real_session.commit()
    left = (
        (await real_session.execute(select(CrdtDoc).where(CrdtDoc.doc_id == fw.ref.doc_id)))
        .scalars()
        .all()
    )
    assert left == []


async def test_a_box_saving_the_whole_file_merges_cell_by_cell_with_live_typing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box holding the folder joins a notebook as a text peer, as it does a
    file: its agent's save on disk is merged into the cells it changed, and a
    person's typing since the box read the document stays."""
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        read = await docs.peers.read(fw.ref)
        assert read is not None
        await _write(docs, real_session, fw, owner, tab, tab.type(WEEKLY, 0, "# live\n"), 1)
        saved = read.text.replace("SELECT order_id", "SELECT DISTINCT order_id")
        answer = await docs.peers.submit(
            fw.ref, text=saved, submit_id="box-save-01", agent_id="m-1", base_token=read.token
        )
        view = await docs.notebooks.view(fw.ref)
    assert answer is not None
    assert _source(view.view, ORDERS).startswith("SELECT DISTINCT order_id")
    assert _source(view.view, WEEKLY).startswith("# live\n")


async def _restart(docs: CrdtDocs, fw: FileWorld) -> int:
    async with docs.admitted(), docs.session_factory() as db:
        epoch = await docs.restart(db, fw.ref, reason="rotation", quarantine=False)
        await db.commit()
    return epoch


async def test_a_history_restart_reseeds_the_same_cell_ids_and_keeps_the_old_state(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from alkera_core.notebooks.models import NotebookEpochTail

    fw = await notebook_world(real_session, org_admin)
    async with crdt_docs() as docs:
        before = await docs.notebooks.view(fw.ref)
        epoch = await _restart(docs, fw)
        after = await docs.notebooks.view(fw.ref)
    assert epoch == 2
    assert [c["id"] for c in after.view["cells"]] == [c["id"] for c in before.view["cells"]]
    assert [c["source"] for c in after.view["cells"]] == [c["source"] for c in before.view["cells"]]
    tails = (
        (
            await real_session.execute(
                select(NotebookEpochTail).where(NotebookEpochTail.item_id == fw.node_id)
            )
        )
        .scalars()
        .all()
    )
    assert [(t.epoch, t.next_epoch) for t in tails] == [(1, 2)]


async def test_a_batch_made_before_a_restart_is_carried_into_the_new_epoch_cell_by_cell(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The agent read in epoch 1. People typed in epoch 1 after that, the
    history restarted, and people typed in epoch 2. The agent's edit, named
    against what it read, lands in its cells and nobody's typing is lost."""
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        read = await docs.notebooks.view(fw.ref)
        await _write(docs, real_session, fw, owner, tab, tab.type(WEEKLY, 0, "# one\n"), 1)
        await _restart(docs, fw)
        fresh = Tab(5001)
        await _open(docs, real_session, fw, owner, fresh)
        await _write(docs, real_session, fw, owner, fresh, fresh.type(WEEKLY, 0, "# two\n"), 2)
        applied = await docs.notebooks.apply(
            fw.ref,
            ops=[
                {"op": "edit", "cell_id": WEEKLY, "edits": [{"old": ".sum()", "new": ".mean()"}]},
                {"op": "replace", "cell_id": ORDERS, "source": "SELECT 7"},
            ],
            base_token=read.token,
            submit_id=None,
            agent_id="agent:analyst",
            author=owner.user,
            actor_key="agent:session-9",
        )
        view = await docs.notebooks.view(fw.ref)
    weekly = _source(view.view, WEEKLY)
    assert weekly.startswith("# two\n# one\n") or weekly.startswith("# one\n# two\n")
    assert ".mean()" in weekly and ".sum()" not in weekly
    assert _source(view.view, ORDERS) == "SELECT 7"
    assert applied.touched == [WEEKLY, ORDERS]


async def test_a_batch_before_a_restart_is_matched_against_what_it_read(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        read = await docs.notebooks.view(fw.ref)
        await _write(docs, real_session, fw, owner, tab, tab.type(WEEKLY, 0, "# later\n"), 1)
        await _restart(docs, fw)
        with pytest.raises(NotebookOpError) as refused:
            await docs.notebooks.apply(
                fw.ref,
                ops=[
                    {
                        "op": "edit",
                        "cell_id": WEEKLY,
                        "edits": [{"old": "# later", "new": "# mine"}],
                    }
                ],
                base_token=read.token,
                submit_id=None,
                agent_id="a",
                author=owner.user,
            )
    assert refused.value.op_code == "edit_not_found"


async def test_an_editors_unsent_typing_survives_a_history_restart_cell_by_cell(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A tab typed in epoch 1 and had not sent it when the history restarted;
    another tab typed in epoch 2. The first tab hands its unsent update over
    and both typings stand, each in its cell."""
    from loro import ExportMode

    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        slow = Tab(5000)
        await _open(docs, real_session, fw, owner, slow)
        sent = slow.doc.oplog_vv
        slow.type(WEEKLY, 0, "# unsent\n")
        unsent = bytes(slow.doc.export(ExportMode.Updates(sent)))
        await _restart(docs, fw)
        fresh = Tab(5001)
        await _open(docs, real_session, fw, owner, fresh)
        await _write(docs, real_session, fw, owner, fresh, fresh.type(ORDERS, 0, "-- new\n"), 1)
        token = await docs.notebooks.rebase_update(
            fw.ref, epoch=1, update=unsent, author=owner.user
        )
        view = await docs.notebooks.view(fw.ref)
    assert token == view.token
    assert _source(view.view, WEEKLY).startswith("# unsent\n")
    assert _source(view.view, ORDERS).startswith("-- new\n")


async def test_an_unsent_update_the_rules_refuse_is_refused_after_a_restart_too(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from loro import ExportMode

    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        slow = Tab(5000)
        await _open(docs, real_session, fw, owner, slow)
        sent = slow.doc.oplog_vv
        nb.child(slow.doc.get_map("cells"), WEEKLY).insert("kind", "rust")
        slow.doc.commit()
        bad = bytes(slow.doc.export(ExportMode.Updates(sent)))
        await _restart(docs, fw)
        with pytest.raises(CrdtError) as refused:
            await docs.notebooks.rebase_update(fw.ref, epoch=1, update=bad, author=owner.user)
        with pytest.raises(CrdtError) as gone:
            await docs.notebooks.rebase_update(fw.ref, epoch=0, update=bad, author=owner.user)
    assert (refused.value.code, refused.value.reason) == ("crdt_rejected", "kind")
    assert gone.value.code == "crdt_rebase_unavailable"


async def test_an_edited_notebook_reads_as_unsaved_until_its_write_back_settles_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A small notebook's keystroke projection carries its rendered file's
    real hash: an edit reads as unsaved (it is not the file's hash), typing
    it back out reads as saved again with no write back, and a write back
    of a real edit settles it."""
    fw = await notebook_world(real_session, org_admin)
    owner = fw.world.owner
    async with crdt_docs() as docs:
        tab = Tab(5000)
        await _open(docs, real_session, fw, owner, tab)
        saved = (await _row(real_session, fw)).source_sha256
        assert (await _row(real_session, fw)).projection["sha256"] == saved
        await _write(docs, real_session, fw, owner, tab, tab.type(WEEKLY, 0, "# x\n"), 1)
        row = await _row(real_session, fw)
        edited = row.projection["sha256"]
        assert edited not in (nb.UNRENDERED, saved)
        assert edited == hashlib.sha256(nb.NOTEBOOK.render(tab.doc).encode()).hexdigest()
        await _write(docs, real_session, fw, owner, tab, tab.erase(WEEKLY, 0, 4), 2)
        assert (await _row(real_session, fw)).projection["sha256"] == saved
        await _write(docs, real_session, fw, owner, tab, tab.type(WEEKLY, 0, "# y\n"), 3)
        settled = await docs.sessions.write_back(real_session, fw.ref)
        await real_session.commit()
        row = await _row(real_session, fw)
    assert settled.outcome == "written"
    assert row.projection["sha256"] == row.source_sha256 != saved
