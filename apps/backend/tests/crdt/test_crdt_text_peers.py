"""The box as a text peer of a co-edited file, against real Postgres, real
Files and real sandbox workers.

The machine holding a file's folder reads the live document as text with a
version token and submits the agent's whole text made on that token. These
pin what a person typing meanwhile keeps (the same spot, CRLF line breaks,
characters past the basic plane), that a retried submit lands once, what a
token from an earlier epoch falls back to, who the update is written as, that
the holder is told when the document moves, and that only the holder of the
folder's lease is answered by the route.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
from alkera_core.events import HubEvent
from alkera_core.models import CrdtDoc, CrdtUpdate
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.realtime import LiveTextRequest
from backend.authz import decide_on_record
from backend.services.crdt.docs import CrdtDocs
from backend.services.crdt.errors import CrdtError
from backend.services.crdt.sandbox.pool import SandboxTimeoutError
from backend.services.crdt.sandbox.protocol import Frame
from backend.services.crdt.text_peers import TextPeers, decode_token, encode_token
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.crdt_world import Peer
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import SOURCE, FileWorld, file_world, node_of
from tests.files._live_holder import MockHolder

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c333"


def _tab(peer: int) -> Peer:
    return Peer(peer, container="content")


async def _open(docs: CrdtDocs, db: AsyncSession, fw: FileWorld, tab: Peer) -> None:
    who = fw.world.owner
    await docs.access(db, fw.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, fw.ref, since=None, epoch_seen=None)
    await db.commit()
    tab.receive(sync.data)


async def _type(
    docs: CrdtDocs, db: AsyncSession, fw: FileWorld, tab: Peer, at: int, text: str
) -> None:
    who = fw.world.owner
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
        update=tab.type(at, text),
        socket_peer_id=f"p:{tab.peer}",
    )
    await db.commit()
    await docs.after_commit(fw.ref, applied)


async def _row(db: AsyncSession, fw: FileWorld) -> CrdtDoc:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    return row


async def _peer_rows(db: AsyncSession, fw: FileWorld) -> list[CrdtUpdate]:
    rows = (
        await db.execute(
            select(CrdtUpdate).where(
                CrdtUpdate.doc_type == "file",
                CrdtUpdate.doc_id == fw.ref.doc_id,
                CrdtUpdate.update_id.like("peer-%"),
            )
        )
    ).scalars()
    found = list(rows)
    await db.commit()
    return found


class _Docs:
    """A store whose session passes run only when a test calls them."""

    def __init__(self) -> None:
        self._context = lane_docs()

    async def __aenter__(self) -> CrdtDocs:
        docs = await self._context.__aenter__()
        docs.run_sessions = False
        return docs

    async def __aexit__(self, *exc: object) -> None:
        await self._context.__aexit__(*exc)


async def test_a_text_peer_reads_nothing_until_a_session_is_open(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """No document, no peer: the box writes the file the ordinary way. Once
    a person opens it, the box reads its content and a token of its state."""
    fw = await file_world(real_session, org_admin)
    async with _Docs() as docs:
        assert await docs.peers.read(fw.ref) is None
        await _open(docs, real_session, fw, _tab(7001))
        found = await docs.peers.read(fw.ref)
    assert found is not None and found.text == SOURCE
    named = decode_token(found.token)
    assert named is not None and named[0] == (await _row(real_session, fw)).epoch


@pytest.mark.parametrize(
    ("original", "person", "agent_from", "agent_to", "expected"),
    [
        pytest.param(
            "one\ntwo\nthree\n",
            (4, "P"),
            "one\ntwo\nthree\n",
            "one\nAtwo\nthree\n",
            None,
            id="typing-at-the-same-spot",
        ),
        pytest.param(
            "alpha\r\nbeta\r\ngamma\r\n",
            (0, "# person\r\n"),
            "alpha\r\nbeta\r\ngamma\r\n",
            "alpha\r\nBETA\r\ngamma\r\n",
            "# person\r\nalpha\r\nBETA\r\ngamma\r\n",
            id="crlf",
        ),
        pytest.param(
            "名前 😀 αβγ\nline two\n",
            (4, "!"),
            "名前 😀 αβγ\nline two\n",
            "名前 😀 αβγ\nline 2\n",
            "名前 😀! αβγ\nline 2\n",
            id="multibyte",
        ),
    ],
)
async def test_an_agent_edit_on_its_token_merges_around_what_a_person_typed_since(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    original: str,
    person: tuple[int, str],
    agent_from: str,
    agent_to: str,
    expected: str | None,
) -> None:
    """The box read the document, a person typed, then the agent's edit made
    on what the box read arrives: both edits are in the document once, line
    breaks the agent left alone are as they were, and offsets count
    characters, not bytes. The state ``at_token`` names holds exactly what
    the box sent, and the update is the machine's, with no person as its
    author."""
    fw = await file_world(real_session, org_admin, content=original.encode(), name="notes.txt")
    tab = _tab(7002)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, tab)
        read = await docs.peers.read(fw.ref)
        assert read is not None and read.text == agent_from
        await _type(docs, real_session, fw, tab, *person)
        answer = await docs.peers.submit(
            fw.ref,
            text=agent_to,
            submit_id=uuid.uuid4().hex,
            agent_id=MACHINE,
            base_token=read.token,
        )
        assert answer is not None and answer.at_token is not None
        now = await docs.peers.read(fw.ref)
        assert now is not None
        at = decode_token(answer.at_token)
        assert at is not None
        row = await _row(real_session, fw)
        assert await docs.content(real_session, fw.ref, row, at=at[1]) == agent_to
        await real_session.commit()
    assert answer.text == now.text and answer.token == now.token
    if expected is None:
        # Two inserts at one spot are ordered by their peers' numbers: both
        # are there, once each, and nothing else moved.
        assert answer.text in ("one\nPAtwo\nthree\n", "one\nAPtwo\nthree\n")
    else:
        assert answer.text == expected
    if "\r\n" in original:
        assert answer.text.count("\n") == answer.text.count("\r\n")
    rows = await _peer_rows(real_session, fw)
    assert [(row.agent_id, row.author_user_id) for row in rows] == [(MACHINE, None)]


async def test_a_retried_submit_lands_once_on_this_replica_and_on_another(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The answer to a submit was lost and the box sends it again: it is the
    same answer, and a replica that never saw it (a restarted backend)
    recognises it and answers the document as it is, with the state the
    first one made, never applying it twice. Also once the log that held it
    has been folded into a snapshot: merged again from the same base, the
    agent's line landed twice (the third ten-minute soak)."""
    fw = await file_world(real_session, org_admin, content=b"a\nb\n", name="log.txt")
    async with _Docs() as docs:
        await _open(docs, real_session, fw, _tab(7003))
        read = await docs.peers.read(fw.ref)
        assert read is not None
        submit_id = uuid.uuid4().hex
        sent: dict[str, Any] = {
            "text": "a\nb\nagent\n",
            "submit_id": submit_id,
            "agent_id": MACHINE,
            "base_token": read.token,
        }
        first = await docs.peers.submit(fw.ref, **sent)
        again = await docs.peers.submit(fw.ref, **sent)
        elsewhere = await TextPeers(docs).submit(fw.ref, **sent)
        async with docs.admitted():
            assert await docs.compact(real_session, fw.ref)
        await real_session.commit()
        folded = await TextPeers(docs).submit(fw.ref, **sent)
        now = await docs.peers.read(fw.ref)
    assert first is not None and again == first
    for later in (elsewhere, folded):
        assert later is not None and later.repeat
        assert later.at_token == first.at_token
        assert later.text == "a\nb\nagent\n"
    assert now is not None and now.text == "a\nb\nagent\n"


async def test_a_token_from_an_earlier_epoch_falls_back_to_the_agreed_version_then_keeps_all(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The document restarted since the box's token. Naming the drive etag it
    last agreed the file at, the agent's removal is applied from exactly
    there; naming nothing it could use, nothing is removed (a removal may
    only be the box not having seen what a person wrote) and its additions
    still land."""
    fw = await file_world(real_session, org_admin, content=b"keep\ndrop\n", name="plan.txt")
    agreed = (await node_of(real_session, fw.node_id)).etag
    async with _Docs() as docs:
        await _open(docs, real_session, fw, _tab(7004))
        read = await docs.peers.read(fw.ref)
        assert read is not None
        async with docs.admitted():
            await docs.restart(real_session, fw.ref, reason="test", quarantine=False)
        await real_session.commit()
        stale = read.token
        named = await docs.peers.submit(
            fw.ref,
            text="keep\n",
            submit_id=uuid.uuid4().hex,
            agent_id=MACHINE,
            base_token=stale,
            base_etag=agreed,
        )
        assert named is not None and named.text == "keep\n"
        assert named.at_text is None
        unknown_id = uuid.uuid4().hex
        unknown = await docs.peers.submit(
            fw.ref,
            text="new\n",
            submit_id=unknown_id,
            agent_id=MACHINE,
            base_token="garbage",
        )
        # The box is told the state its text went into holds more than it
        # sent (it never stands on that state as its file's), on the answer
        # and on a retry a replica that never saw it answers.
        again = await TextPeers(docs).submit(
            fw.ref, text="new\n", submit_id=unknown_id, agent_id=MACHINE, base_token="garbage"
        )
    # Lines it adds land whole beside the ones it would have replaced; none
    # is spliced into a word that stays.
    assert unknown is not None and unknown.text == "keep\nnew\n"
    assert unknown.at_token is not None and unknown.at_text == "keep\nnew\n"
    assert again is not None and again.repeat and again.at_text == "keep\nnew\n"


async def test_a_submit_with_no_live_session_is_refused_for_the_ordinary_path(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with _Docs() as docs:
        refused = await docs.peers.submit(
            fw.ref, text="x", submit_id=uuid.uuid4().hex, agent_id=MACHINE, base_token=None
        )
    assert refused is None
    assert await _peer_rows(real_session, fw) == []


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _holder(
    db: AsyncSession, client: AsyncClient, fw: FileWorld, *, machine: str = MACHINE
) -> MockHolder:
    """The chat's box, holding its folder and taking inbound writes."""
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    folder = (
        await db.execute(
            select(FileNode).where(FileNode.target_object_id == uuid.UUID(fw.world.ref.doc_id))
        )
    ).scalar_one()
    holder = MockHolder(client, folder.drive_id, folder.id, machine=machine)
    taken = await holder.take(db, _idem, purpose="chat", inbound=True, live=True)
    assert taken.status_code == 200, taken.text
    return holder


@pytest.mark.parametrize(
    ("machine", "joined", "told"),
    [
        pytest.param(MACHINE, True, True, id="a-registered-machine-that-joined"),
        pytest.param(MACHINE, False, False, id="a-box-that-never-joined"),
        pytest.param("laptop-of-someone", True, False, id="a-holder-with-no-machine-channel"),
    ],
)
async def test_the_holder_s_machine_is_told_the_document_moved(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    machine: str,
    joined: bool,
    told: bool,
) -> None:
    """After an edit the machine holding the folder is told, on its own
    channel, which node moved and the token it moved to, never its text. A
    box that never read the document as text (one from before text peers,
    which would log every notice) is told nothing, and neither is a holder
    that is no registered machine (it has no channel)."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw, machine=machine)
    published: list[HubEvent] = []

    async def publish(event: HubEvent) -> None:
        published.append(event)

    tab = _tab(7005)
    async with _Docs() as docs:
        peers = TextPeers(docs, publish=publish)
        assert await peers.notify(fw.ref) is False
        await _open(docs, real_session, fw, tab)
        if joined:
            assert await docs.peers.read(fw.ref) is not None
        await _type(docs, real_session, fw, tab, 0, "# person\n")
        assert await peers.notify(fw.ref) is told
        row = await _row(real_session, fw)
        token = encode_token(row.epoch, bytes(row.vv))
    if not told:
        assert published == []
        return
    (event,) = published
    assert event.channel == f"machine:{MACHINE}"
    notice = LiveTextRequest.model_validate(event.payload)
    assert notice.node_id == fw.node_id and notice.token == token
    assert notice.lease_node_id == holder.node_id and notice.epoch == holder.epoch
    assert "# person" not in str(event.payload)


async def test_only_the_folder_s_holder_reads_and_writes_the_live_text(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the route: the holder reads the document and its edit is
    merged and written as the lease's machine. The same person without the
    lease's fence (not the holder) is refused, before anything is read."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, _tab(7006))
    from alkera_core.config import settings
    from backend.app_factory import process_app

    app = process_app()
    from backend.services.realtime import runtime

    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    started = await runtime.start(app, decide=decide_on_record)
    try:
        unfenced = await holder.live_text(fw.node_id, headers={})
        stale = await holder.live_text(fw.node_id, headers=holder.stale_fence(epoch=0))
        read = await holder.live_text(fw.node_id)
        assert read.status_code == 200, read.text
        body = read.json()
        sent = await holder.submit_text(
            fw.node_id, body["text"] + "# agent\n", base_token=body["token"]
        )
        refused = await holder.submit_text(fw.node_id, "x", headers={})
    finally:
        await runtime.stop(app, started)
    assert unfenced.status_code == 409 and stale.status_code == 409
    assert refused.status_code == 409
    assert body["live"] is True and body["text"] == SOURCE
    assert sent.status_code == 200, sent.text
    merged = sent.json()
    assert merged["live"] is True and merged["text"] == SOURCE + "# agent\n"
    assert decode_token(merged["atToken"]) is not None
    rows = await _peer_rows(real_session, fw)
    assert [(row.agent_id, row.author_user_id) for row in rows] == [(MACHINE, None)]


async def test_a_token_names_its_epoch_and_version_and_garbage_names_nothing() -> None:
    token = encode_token(7, b"\x00\x01\xff")
    assert decode_token(token) == (7, b"\x00\x01\xff")
    assert decode_token(None) is None
    assert decode_token("x.AAAA") is None
    assert decode_token("7.") is None
    assert decode_token("7.!!!") is None


async def test_a_session_pass_refused_as_busy_runs_again_without_another_edit(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """People typing kept the sandbox busy, and a merge of the box's edit was
    refused: it runs again on its own shortly after, rather than waiting for
    the next announcement or keepalive tick (the soak's agent edits waited a
    median 16 s for one)."""

    from backend.services.crdt import docs as docs_module
    from backend.services.crdt.errors import CrdtError
    from backend.services.crdt.sessions import Settled

    fw = await file_world(real_session, org_admin)
    monkeypatch.setattr(docs_module, "BUSY_RETRY_SECONDS", 0.05)
    tries: list[str] = []
    again = asyncio.Event()

    async def busy_once(db: AsyncSession, ref: Any) -> Settled:
        tries.append(ref.key)
        if len(tries) == 1:
            raise CrdtError("crdt_busy", "the sandbox could not hold the document")
        again.set()
        return Settled("unchanged")

    async with lane_docs() as docs:
        monkeypatch.setattr(docs.sessions, "merge_outside", busy_once)
        docs.check_source(fw.ref)
        await asyncio.wait_for(again.wait(), timeout=5)
    assert tries == [fw.ref.key, fw.ref.key]


async def test_a_box_that_lost_its_token_is_merged_from_the_state_it_was_handed(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box wrote the document to disk between two write backs, then
    restarted and lost its token; the agent's edit arrives naming no state.
    It is merged from the state the box was handed, so what a person typed
    after the last write back is not added a second time."""
    fw = await file_world(real_session, org_admin, content=b"base\n", name="notes.txt")
    tab = _tab(7007)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, tab)
        await _type(docs, real_session, fw, tab, 0, "# first\n")
        assert (await docs.sessions.write_back(real_session, fw.ref)).outcome == "written"
        await real_session.commit()
        await _type(docs, real_session, fw, tab, len("# first\n"), "# second\n")
        handed = await docs.peers.read(fw.ref)
        assert handed is not None and handed.text == "# first\n# second\nbase\n"
        answer = await docs.peers.submit(
            fw.ref,
            text=handed.text + "agent\n",
            submit_id=uuid.uuid4().hex,
            agent_id=MACHINE,
            base_token=None,
        )
    assert answer is not None and answer.text == "# first\n# second\nbase\nagent\n"


async def test_two_unnamed_submits_of_one_text_searched_at_once_add_it_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box restarted while its first send of the agent's edit was still
    being served, and the new process sent the same disk again, naming no
    state either. Both searched for the closest state before either landed,
    and found the same one: each merged from it, the agent's line landed
    twice. The second is merged from the state the first made instead."""
    fw = await file_world(real_session, org_admin, content=b"base\n", name="notes.txt")
    tab = _tab(7008)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, tab)
        await _type(docs, real_session, fw, tab, 0, "# typed\n")
        handed = await docs.peers.read(fw.ref)
        assert handed is not None
        searched = docs.sessions.closest_base
        both = asyncio.Event()
        calls: list[int] = []

        async def side_by_side(*args: Any, **kwargs: Any) -> bytes:
            found = await searched(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=10)
            return found

        monkeypatch.setattr(docs.sessions, "closest_base", side_by_side)
        disk = handed.text + "agent\n"
        answers = await asyncio.gather(
            *(
                docs.peers.submit(
                    fw.ref,
                    text=disk,
                    submit_id=uuid.uuid4().hex,
                    agent_id=MACHINE,
                    base_token=None,
                )
                for _ in range(2)
            )
        )
        assert len(calls) == 2
        current = await docs.peers.read(fw.ref)
    assert [answer is not None for answer in answers] == [True, True]
    assert current is not None and current.text == "# typed\nbase\nagent\n"


async def test_an_unnamed_submit_is_merged_from_the_drive_version_the_box_still_holds(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A person typed into a line of the file and the session wrote it back;
    the box, holding the version from before (its agent added a line), reads
    the document and sends its file naming no state. Being handed a state is
    no sign the box wrote it: the box writes a state only when its disk has
    not moved, and a box sending its own text did not. Searched only among
    the drive versions still current when the box was first handed a state,
    the file was merged from the typed version and its line came back
    untyped beside the typed one. It is merged from the version it holds."""
    plan = "1. one\n2. two\n3. three\n"
    fw = await file_world(real_session, org_admin, content=plan.encode(), name="plan.txt")
    tab = _tab(7011)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, tab)
        await _type(docs, real_session, fw, tab, len("1. one"), " typed")
        assert (await docs.sessions.write_back(real_session, fw.ref)).outcome == "written"
        await real_session.commit()
        handed = await docs.peers.read(fw.ref)
        assert handed is not None and handed.text.startswith("1. one typed\n")
        answer = await docs.peers.submit(
            fw.ref,
            text="1. one\n2. two\nagent0\n3. three\n",
            submit_id=uuid.uuid4().hex,
            agent_id=MACHINE,
            base_token=None,
        )
    assert answer is not None and answer.text == "1. one typed\n2. two\nagent0\n3. three\n"


async def test_an_unnamed_submit_after_one_that_failed_is_merged_from_the_version_held(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box was handed the document twice (a join whose send failed, then
    another): neither state was written to its disk, which still holds the
    version from before a person's typing. Its text is merged from that."""
    plan = "1. one\n2. two\n3. three\n"
    fw = await file_world(real_session, org_admin, content=plan.encode(), name="plan.txt")
    tab = _tab(7012)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, tab)
        await _type(docs, real_session, fw, tab, len("1. one"), " typed")
        assert (await docs.sessions.write_back(real_session, fw.ref)).outcome == "written"
        await real_session.commit()
        assert await docs.peers.read(fw.ref) is not None
        await _type(docs, real_session, fw, tab, len(plan) + len(" typed"), "4. four\n")
        assert (await docs.sessions.write_back(real_session, fw.ref)).outcome == "written"
        await real_session.commit()
        assert await docs.peers.read(fw.ref) is not None
        answer = await docs.peers.submit(
            fw.ref,
            text="1. one\n2. two\nagent0\n3. three\n",
            submit_id=uuid.uuid4().hex,
            agent_id=MACHINE,
            base_token=None,
        )
    assert answer is not None
    assert answer.text == "1. one typed\n2. two\nagent0\n3. three\n4. four\n"


async def test_the_box_s_merges_reuse_one_peer_while_their_bases_hold_what_it_wrote(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Every peer a document has makes each later import into it dearer: a
    peer minted per agent save made a keystroke cost tens of milliseconds
    within minutes of two people typing. A save made on what the last one
    left is written as the same peer; one made on a state from before it
    (a box that missed the answer) as a new one, and both land."""
    fw = await file_world(real_session, org_admin, content=b"one\n", name="notes.txt")
    async with _Docs() as docs:
        await _open(docs, real_session, fw, _tab(7010))
        handed = await docs.peers.read(fw.ref)
        assert handed is not None
        sent: dict[str, Any] = {"agent_id": MACHINE}
        first = await docs.peers.submit(
            fw.ref, text="one\na\n", submit_id=uuid.uuid4().hex, base_token=handed.token, **sent
        )
        assert first is not None
        second = await docs.peers.submit(
            fw.ref, text="one\na\nb\n", submit_id=uuid.uuid4().hex, base_token=first.token, **sent
        )
        older = await docs.peers.submit(
            fw.ref, text="one\nc\n", submit_id=uuid.uuid4().hex, base_token=handed.token, **sent
        )
        now = await docs.peers.read(fw.ref)
    assert second is not None and older is not None and now is not None
    assert sorted(now.text.splitlines()) == ["a", "b", "c", "one"]
    peers = [
        row.loro_peer for row in sorted(await _peer_rows(real_session, fw), key=lambda r: r.log_seq)
    ]
    assert len(peers) == 3 and peers[0] == peers[1] != peers[2]


async def test_a_document_being_opened_tells_a_holder_that_has_not_joined_once_a_minute(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """Opening the file tells its box at once, joined or not, so a current
    box joins before anyone types; a box from before text peers hears it at
    most once a minute per document (and logs one line for it), never once
    per keystroke."""

    fw = await file_world(real_session, org_admin)
    await _holder(real_session, client, fw)
    published: list[HubEvent] = []

    async def publish(event: HubEvent) -> None:
        published.append(event)

    async with lane_docs() as docs:
        docs.peers.publish = publish
        docs.peers.notify_delay = 0.0
        await _open(docs, real_session, fw, _tab(7008))
        await _open(docs, real_session, fw, _tab(7009))
        for _ in range(100):
            if published:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    assert [event.channel for event in published] == [f"machine:{MACHINE}"]


async def test_the_open_notice_reads_the_document_its_open_committed(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """The first open creates the document's row in the opener's
    transaction. A notice that runs before that commits finds no document,
    tells nobody, and the once-a-minute limit then holds back the next
    open's: the box hears of the document a minute late. The notice waits
    for the open to commit."""
    fw = await file_world(real_session, org_admin)
    await _holder(real_session, client, fw)
    published: list[HubEvent] = []
    told = asyncio.Event()

    async def publish(event: HubEvent) -> None:
        published.append(event)

    async with lane_docs() as docs:
        docs.peers.publish = publish
        docs.peers.notify_delay = 0.0
        real_notify = docs.peers.notify

        async def notify(*args: Any, **kwargs: Any) -> bool:
            try:
                return await real_notify(*args, **kwargs)
            finally:
                told.set()

        docs.peers.notify = notify  # type: ignore[method-assign]
        who = fw.world.owner
        await docs.access(real_session, fw.ref, user=who.user, ent=who.ent, agent_id=None)
        sync = await docs.sync(real_session, fw.ref, since=None, epoch_seen=None)
        # The open has answered and not yet committed: whatever notice it
        # set off has had every chance to run.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(told.wait(), 2)
        await real_session.commit()
        _tab(7010).receive(sync.data)
        await asyncio.wait_for(told.wait(), 20)
        for _ in range(100):
            if published:
                break
            await asyncio.sleep(0.02)
    assert [event.channel for event in published] == [f"machine:{MACHINE}"]


async def test_an_open_that_rolls_back_tells_nobody_even_when_the_session_commits_later(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    fw = await file_world(real_session, org_admin)
    await _holder(real_session, client, fw)
    published: list[HubEvent] = []

    async def publish(event: HubEvent) -> None:
        published.append(event)

    async with lane_docs() as first:
        # The document exists: a notice sent now would find it.
        await _open(first, real_session, fw, _tab(7011))
    async with lane_docs() as docs:
        docs.peers.publish = publish
        docs.peers.notify_delay = 0.0
        who = fw.world.owner
        await docs.access(real_session, fw.ref, user=who.user, ent=who.ent, agent_id=None)
        await docs.sync(real_session, fw.ref, since=None, epoch_seen=None)
        await real_session.rollback()
        # Later, unrelated work on the same session commits.
        await real_session.execute(text("SELECT 1"))
        await real_session.commit()
        await asyncio.sleep(0.5)
    assert published == []


# ---------------------------------------------------------------------------
# A cold document after a box comes back
# ---------------------------------------------------------------------------


class _ColdWorker:
    """The sandbox as a box restart leaves it on a busy host: its worker no
    longer holds the document, so every request asks for the history first,
    and loading the history waits until the test lets it finish. A load that
    outlives its budget is killed, as the pool kills one."""

    def __init__(self, docs: CrdtDocs) -> None:
        self._real = docs.pool.request
        self.cold = False
        self.loading = asyncio.Event()
        self.release = asyncio.Event()

    async def request(
        self, key: str, header: dict[str, Any], blobs: Any, *, budget_seconds: float
    ) -> Frame:
        op = header.get("op")
        if self.cold and op == "load":
            self.loading.set()
            try:
                await asyncio.wait_for(self.release.wait(), budget_seconds)
            except TimeoutError as exc:
                raise SandboxTimeoutError("the load outlived its budget") from exc
            reply = await self._real(key, header, blobs, budget_seconds=budget_seconds)
            self.cold = False
            return reply
        if self.cold:
            return Frame(header={"need": True})
        return await self._real(key, header, blobs, budget_seconds=budget_seconds)


async def _apply(
    docs: CrdtDocs, fw: FileWorld, tab: Peer, update: bytes, update_id: str, *, wait: str
) -> Any:
    who = fw.world.owner
    async with docs.session_factory() as db:
        await db.execute(text(f"SET LOCAL lock_timeout = '{wait}'"))
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
                update_id=update_id,
                update=update,
                socket_peer_id=f"p:{tab.peer}",
            )
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
    await docs.after_commit(fw.ref, applied)
    return applied


async def test_a_box_coming_back_to_a_cold_document_never_holds_up_typing(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a box restart the worker holding the document is cold and the
    host is too slow to load its history. A load under the document's row
    gives up inside the short budget, so a person's edit and the box's save
    that queue behind it get the row within their own lock wait and are
    answered busy (the peer sends again), never a lock timeout that drops the
    edit. The load is finished with nothing locked, and then every one of
    them lands.

    The load used to run under the row for its whole ten-second budget, so
    everyone typing gave up with a lock timeout first, and a load that ran
    out of time left the worker as cold for the next holder."""
    fw = await file_world(real_session, org_admin, content=b"one\n", name="notes.txt")
    first, second = _tab(7101), _tab(7102)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, first)
        await _open(docs, real_session, fw, second)
        read = await docs.peers.read(fw.ref)
        assert read is not None
        worker = _ColdWorker(docs)
        monkeypatch.setattr(docs.pool, "request", worker.request)
        worker.cold = True
        typed_first = first.type(0, "A")
        typed_second = second.type(0, "B")

        outcomes: list[BaseException | Any] = []
        holder = asyncio.create_task(_apply(docs, fw, first, typed_first, "u-first", wait="5s"))
        await asyncio.wait_for(worker.loading.wait(), 10)
        # The box comes back and saves, and a second person types, while the
        # history is still loading.
        racing = await asyncio.gather(
            asyncio.wait_for(_apply(docs, fw, second, typed_second, "u-second", wait="5s"), 15),
            asyncio.wait_for(
                docs.peers.submit(
                    fw.ref,
                    text="one\nagent\n",
                    submit_id="box-after-restart",
                    agent_id=MACHINE,
                    base_token=read.token,
                ),
                15,
            ),
            return_exceptions=True,
        )
        outcomes.extend(racing)
        worker.release.set()
        outcomes.append(await asyncio.gather(holder, return_exceptions=True))
        for outcome in [*racing, *outcomes[-1]]:
            assert not isinstance(outcome, DBAPIError), f"waited out a lock: {outcome!r}"
            assert not isinstance(outcome, TimeoutError), "held up behind the load"
            if isinstance(outcome, BaseException):
                assert isinstance(outcome, CrdtError) and outcome.code == "crdt_busy", outcome

        # Loaded: each sends again and lands.
        await docs.drain()
        for tab, update, update_id in (
            (first, typed_first, "u-first"),
            (second, typed_second, "u-second"),
        ):
            await _apply(docs, fw, tab, update, update_id, wait="5s")
        answer = await docs.peers.submit(
            fw.ref,
            text="one\nagent\n",
            submit_id="box-after-restart",
            agent_id=MACHINE,
            base_token=read.token,
        )
        assert answer is not None
    assert sorted(answer.text) == sorted("ABone\nagent\n")
    assert "agent\n" in answer.text and answer.text.count("A") == 1 and answer.text.count("B") == 1
