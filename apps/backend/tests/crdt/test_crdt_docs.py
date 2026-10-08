"""The CRDT document store against real Postgres and real sandbox workers.

What a write stores (the canonical delta, never the client's bytes), what it
announces, what it refuses and that a refusal stores nothing; who may write;
that a document survives the workers that held it, is folded and restarted
without losing a character, and is re-seeded when its history no longer
rebuilds.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import uuid
from typing import Any

import pytest
from alkera_core.doc_type_names import respell_channel, respell_payload
from alkera_core.models import CrdtDoc, CrdtUpdate, EventOutbox, RealtimeDoc
from alkera_core.schemas.realtime import DocEnvelope, decode_b64
from backend.services.crdt import docs as docs_module
from backend.services.crdt.docs import Applied, CrdtDocs, CrdtError
from backend.services.crdt.registry import (
    ChatWorkspaceType,
    CrdtRegistry,
    FileDocType,
)
from backend.services.crdt.sandbox.pool import SandboxCrashedError, SandboxTimeoutError
from loro import ExportMode
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Peer, Person, World, crdt_docs, make_world, same_vv

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _open(docs: CrdtDocs, db: AsyncSession, world: World, who: Person, peer: Peer) -> Any:
    await docs.access(db, world.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, world.ref, since=None, epoch_seen=None)
    await db.commit()
    peer.receive(sync.data)
    return sync


async def _write(
    docs: CrdtDocs,
    db: AsyncSession,
    world: World,
    who: Person,
    peer: Peer,
    update_bytes: bytes,
    *,
    epoch: int = 1,
    update_id: str = "u-1",
    agent_id: str | None = None,
) -> Applied:
    try:
        applied = await docs.apply(
            db,
            world.ref,
            user=who.user,
            ent=who.ent,
            agent_id=agent_id,
            epoch=epoch,
            peer=peer.peer,
            update_id=update_id,
            update=update_bytes,
            socket_peer_id=f"p:{peer.peer}",
        )
    except BaseException:
        await db.rollback()
        raise
    await db.commit()
    await docs.broadcast(world.ref, applied)
    await docs.after_commit(world.ref, applied)
    return applied


async def _log_rows(db: AsyncSession, world: World) -> int:
    """How many update-log rows the document still keeps."""
    value = await db.scalar(
        select(func.count())
        .select_from(CrdtUpdate)
        .where(
            CrdtUpdate.org_id == world.ref.org_id,
            CrdtUpdate.doc_type == world.ref.stored_type,
            CrdtUpdate.doc_id == world.ref.doc_id,
        )
    )
    return int(value or 0)


async def _row(db: AsyncSession, world: World) -> CrdtDoc:
    db.expire_all()
    doc = (
        await db.execute(
            select(CrdtDoc).where(
                CrdtDoc.org_id == world.org_id,
                CrdtDoc.doc_type == "chat_workspace",
                CrdtDoc.doc_id == world.ref.doc_id,
            )
        )
    ).scalar_one()
    return doc


async def _frames(db: AsyncSession, world: World) -> list[dict[str, Any]]:
    """The doc events written for the world's document, read back the way a
    replica reads them: rows carry the spelling replicas exchange, and the hub
    respells them to the current one."""
    rows = (
        await db.execute(
            select(EventOutbox.payload)
            .where(EventOutbox.entity_id == respell_channel(world.ref.channel, "to_replicas"))
            .order_by(EventOutbox.id)
        )
    ).scalars()
    return [respell_payload(row, "from_replicas") for row in rows]


class Heard:
    """What every replica's listener hears on the ephemeral lane for one
    document: a real ``LISTEN`` connection, as a replica holds."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.events: list[Any] = []
        self._conn: Any = None

    async def __aenter__(self) -> Heard:
        import asyncpg
        from alkera_core.config import settings
        from alkera_core.events.listener import EPHEMERAL_CHANNEL, asyncpg_dsn, parse_ephemeral

        def heard(_conn: object, _pid: int, _channel: str, payload: str) -> None:
            event = parse_ephemeral(payload)
            if event.channel == self.channel:
                self.events.append(event)

        self._conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
        await self._conn.add_listener(EPHEMERAL_CHANNEL, heard)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._conn.close()

    async def updates(self, count: int) -> list[DocEnvelope]:
        """The first ``count`` announced updates, waiting briefly for them."""
        import asyncio

        for _ in range(100):
            found = [
                DocEnvelope.model_validate(e.payload["envelope"])
                for e in self.events
                if e.type == docs_module.CRDT_UPDATE_EVENT_TYPE
            ]
            if len(found) >= count:
                return found
            await asyncio.sleep(0.02)
        return found


def _fingerprint(doc: CrdtDoc) -> tuple[Any, ...]:
    return (
        doc.epoch,
        doc.log_seq,
        bytes(doc.vv),
        bytes(doc.snapshot),
        doc.log_bytes,
        dict(doc.projection),
    )


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


async def test_a_first_open_seeds_an_empty_document_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        sync = await _open(docs, real_session, world, world.owner, tab)
        assert (sync.mode, sync.epoch, sync.doc_schema) == ("snapshot", 1, 1)
        assert tab.text == ""
        row = await _row(real_session, world)
        assert (row.seeded_from, row.log_seq) == ("empty", 0)
        assert same_vv(bytes(row.vv), sync.vv)
        again = await _open(docs, real_session, world, world.reader, Peer(5001))
        assert same_vv(again.vv, sync.vv)


async def test_a_first_open_carries_the_draft_the_op_log_lane_held(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    real_session.add(
        RealtimeDoc(
            org_id=world.org_id,
            doc_type="chat",
            doc_id=world.ref.doc_id,
            epoch=1,
            seq=3,
            state={"meta": {"draft": {"text": "half a thought 😀", "at": 1.0}}, "events": []},
        )
    )
    await real_session.commit()
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        assert tab.text == "half a thought 😀"
        assert (await _row(real_session, world)).seeded_from == "legacy_draft"


async def test_the_op_log_draft_is_read_once_and_never_again(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The stored ``meta.draft`` is the live draft's first text and nothing
    more: once the document exists, a different ``meta.draft`` (a publisher's
    rebuild naming its own) never re-seeds it, and the live text stands."""
    from sqlalchemy import update

    world = await make_world(real_session, org_admin)
    real_session.add(
        RealtimeDoc(
            org_id=world.org_id,
            doc_type="chat",
            doc_id=world.ref.doc_id,
            epoch=1,
            seq=1,
            state={"meta": {"draft": {"text": "first", "at": 1.0}}, "events": []},
        )
    )
    await real_session.commit()
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        await _write(docs, real_session, world, world.owner, tab, tab.type(5, " words"))
        await real_session.execute(
            update(RealtimeDoc)
            .where(RealtimeDoc.org_id == world.org_id, RealtimeDoc.doc_id == world.ref.doc_id)
            .values(state={"meta": {"draft": {"text": "rewritten", "at": 2.0}}, "events": []})
        )
        await real_session.commit()
        later = Peer(5001)
        await _open(docs, real_session, world, world.writer, later)
        assert later.text == "first words"
        row = await _row(real_session, world)
        assert (row.epoch, row.seeded_from, row.projection["text"]) == (
            1,
            "legacy_draft",
            "first words",
        )


async def test_a_document_created_again_is_never_served_from_a_worker_holding_the_old_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Replica A's worker holds the document at epoch 1, position 0. The row is
    deleted and created again through replica B — also epoch 1, position 0,
    different text. A must serve the new one."""
    from sqlalchemy import delete

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as replica_a, crdt_docs() as replica_b:
        first = Peer(5000)
        await _open(replica_a, real_session, world, world.owner, first)
        assert first.text == ""
        await real_session.execute(
            delete(CrdtDoc).where(
                CrdtDoc.org_id == world.org_id, CrdtDoc.doc_id == world.ref.doc_id
            )
        )
        real_session.add(
            RealtimeDoc(
                org_id=world.org_id,
                doc_type="chat",
                doc_id=world.ref.doc_id,
                epoch=1,
                seq=1,
                state={"meta": {"draft": {"text": "the new life", "at": 1.0}}, "events": []},
            )
        )
        await real_session.commit()
        await _open(replica_b, real_session, world, world.owner, Peer(5001))
        row = await _row(real_session, world)
        assert (row.epoch, row.log_seq) == (1, 0)
        again = Peer(5002)
        await _open(replica_a, real_session, world, world.owner, again)
        assert again.text == "the new life"


async def test_two_first_opens_racing_seed_one_document(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    import asyncio

    from alkera_core.db.session import AsyncSessionLocal

    world = await make_world(real_session, org_admin)
    async with crdt_docs(workers=2) as docs:

        async def first_open() -> bytes:
            async with AsyncSessionLocal() as db:
                sync = await docs.sync(db, world.ref, since=None, epoch_seen=None)
                await db.commit()
                return sync.vv

        a, b = await asyncio.gather(first_open(), first_open())
        assert a == b
        count = len(
            (
                await real_session.execute(
                    select(CrdtDoc).where(CrdtDoc.doc_id == world.ref.doc_id)
                )
            ).all()
        )
        assert count == 1


async def test_a_reopen_sends_only_what_the_peer_lacks(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        writer, behind = Peer(5000), Peer(5001)
        await _open(docs, real_session, world, world.owner, writer)
        await _open(docs, real_session, world, world.reader, behind)
        await _write(docs, real_session, world, world.owner, writer, writer.type(0, "hello"))
        sync = await docs.sync(real_session, world.ref, since=behind.vv, epoch_seen=1)
        await real_session.commit()
        assert sync.mode == "updates"
        behind.receive(sync.data)
        assert behind.text == "hello"
        # A vector from another epoch is no basis for a delta: the whole document.
        stale = await docs.sync(real_session, world.ref, since=behind.vv, epoch_seen=7)
        await real_session.commit()
        assert stale.mode == "snapshot"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def test_a_write_stores_the_canonical_delta_and_announces_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs, Heard(world.ref.channel) as heard:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        first = await _write(
            docs, real_session, world, world.owner, tab, tab.type(0, "hello"), update_id="u-6"
        )
        tab.type(5, " world")
        # The client re-sends everything it holds, the committed "hello" too:
        # only what the server lacks is stored and announced.
        everything = tab.since(Peer(9999).vv)
        applied = await _write(
            docs, real_session, world, world.owner, tab, everything, update_id="u-7"
        )
        assert applied.changed and applied.log_seq == 2
        assert same_vv(applied.vv, tab.vv)

        logged = (
            await real_session.execute(
                select(CrdtUpdate).where(
                    CrdtUpdate.doc_id == world.ref.doc_id, CrdtUpdate.log_seq == 2
                )
            )
        ).scalar_one()
        assert bytes(logged.data) == applied.delta
        assert len(applied.delta) < len(everything)
        assert bytes(logged.sha256) == hashlib.sha256(applied.delta).digest()
        assert (logged.update_id, logged.loro_peer, logged.author_user_id) == (
            "u-7",
            5000,
            world.owner.user.id,
        )
        row = await _row(real_session, world)
        assert row.projection["text"] == "hello world"
        assert row.projection["by_user_id"] == str(world.owner.user.id)
        assert (row.log_seq, row.log_bytes) == (2, len(first.delta) + len(applied.delta))

        envelope = (await heard.updates(2))[-1]
        assert (envelope.kind, envelope.seq, envelope.epoch, envelope.peer_id) == (
            "crdt",
            0,
            1,
            "p:5000",
        )
        assert decode_b64(envelope.payload["data_b64"]) == applied.delta
        assert same_vv(decode_b64(envelope.payload["vv_b64"]), applied.vv)
        assert envelope.payload["user_id"] == str(world.owner.user.id)
        # Announced on the ephemeral lane only: no outbox row, so no commit
        # of a keystroke waits on the database-wide NOTIFY lock.
        assert [
            f for f in await _frames(real_session, world) if f["envelope"]["kind"] == "crdt"
        ] == []

        other = Peer(5001)
        await _open(docs, real_session, world, world.reader, other)
        assert other.text == "hello world"


async def test_a_retried_update_is_acknowledged_without_storing_it_twice(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    import asyncio

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs, Heard(world.ref.channel) as heard:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        update_bytes = tab.type(0, "x")
        first = await _write(docs, real_session, world, world.owner, tab, update_bytes)
        again = await _write(docs, real_session, world, world.owner, tab, update_bytes)
        assert (again.changed, again.log_seq) == (False, 1)
        assert same_vv(again.vv, first.vv)
        assert len(await heard.updates(1)) == 1
        await asyncio.sleep(0.2)
        assert len(await heard.updates(1)) == 1


async def test_concurrent_writers_converge(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs, Heard(world.ref.channel) as heard:
        a, b = Peer(5000), Peer(5001)
        await _open(docs, real_session, world, world.owner, a)
        await _open(docs, real_session, world, world.writer, b)
        from_a = a.type(0, "alpha ")
        from_b = b.type(0, "beta ")
        await _write(docs, real_session, world, world.owner, a, from_a)
        await _write(docs, real_session, world, world.writer, b, from_b, update_id="u-b")
        for envelope in await heard.updates(2):
            delta = decode_b64(envelope.payload["data_b64"])
            a.receive(delta)
            b.receive(delta)
        assert a.text == b.text == (await _row(real_session, world)).projection["text"]
        assert sorted(a.text.split()) == ["alpha", "beta"]


@pytest.mark.parametrize(
    "case",
    ["forged-peer", "garbage", "map-container", "missing-history"],
)
async def test_a_refused_update_leaves_the_stored_document_byte_identical(
    real_session: AsyncSession, org_admin: OrgWithAdmin, case: str
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        await _write(docs, real_session, world, world.owner, tab, tab.type(0, "kept"))
        before = _fingerprint(await _row(real_session, world))
        frames_before = len(await _frames(real_session, world))
        expected = "crdt_rejected"
        if case == "forged-peer":
            impostor = Peer(5001)
            impostor.receive(tab.since(Peer(9999).vv))
            sent = impostor.type(0, "x")
        elif case == "garbage":
            sent = b"\x00garbage" * 10
        elif case == "map-container":
            vv = tab.doc.oplog_vv
            tab.doc.get_map("evil").insert("k", 1)
            tab.doc.commit()
            from loro import ExportMode

            sent = bytes(tab.doc.export(ExportMode.Updates(vv)))
        else:
            tab.type(0, "lost ")
            sent = tab.type(0, "next ")
            expected = "crdt_resync"
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, world.owner, tab, sent)
        assert excinfo.value.code == expected
        assert _fingerprint(await _row(real_session, world)) == before
        assert len(await _frames(real_session, world)) == frames_before


async def test_a_write_against_another_epoch_names_the_current_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, world.owner, tab, tab.type(0, "x"), epoch=2)
        assert (excinfo.value.code, excinfo.value.epoch) == ("stale_epoch", 1)


async def test_an_oversized_update_is_refused_before_the_sandbox_sees_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        cap = ChatWorkspaceType.limits.max_update_bytes
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, world.owner, tab, b"x" * (cap + 1))
        assert (excinfo.value.code, excinfo.value.reason) == ("crdt_rejected", "update_too_large")


# ---------------------------------------------------------------------------
# Who may write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "agent", "code"),
    [
        pytest.param("owner", None, None, id="owner"),
        pytest.param("writer", None, None, id="shared-can-edit"),
        pytest.param("commenter", None, "forbidden", id="shared-can-comment"),
        pytest.param("reader", None, "forbidden", id="shared-can-view"),
        pytest.param("owner", "machine-1", "forbidden", id="owner-socket-asserting-an-agent"),
        pytest.param("stranger", None, "not_found", id="not-shared"),
    ],
)
async def test_only_a_person_with_edit_rights_writes_the_draft(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    who: str,
    agent: str | None,
    code: str | None,
) -> None:
    world = await make_world(real_session, org_admin)
    person: Person = getattr(world, who)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        sent = tab.type(0, "x")
        if code is None:
            assert (
                await _write(docs, real_session, world, person, tab, sent, agent_id=agent)
            ).changed
            return
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, person, tab, sent, agent_id=agent)
        assert excinfo.value.code == code
        assert (await _row(real_session, world)).log_seq == 0


async def test_a_member_of_another_org_cannot_see_the_document(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    from alkera_core.models import User
    from backend.services.realtime.filters import load_entitlements

    world = await make_world(real_session, org_admin)
    foreign = await real_session.get(User, platform_admin.admin_id)
    assert foreign is not None
    async with crdt_docs() as docs:
        with pytest.raises(CrdtError) as excinfo:
            await docs.access(
                real_session,
                world.ref,
                user=foreign,
                ent=await load_entitlements(real_session, foreign, org_id=foreign.home_org_team_id),
                agent_id=None,
            )
        assert excinfo.value.code == "not_found"


async def test_an_org_with_no_settings_of_its_own_is_on_the_lane(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """No per-org switch decides whether the lane serves a chat: an org whose
    settings row was never written reads and writes its live draft."""
    from alkera_core.models import OrgSettings
    from sqlalchemy import delete

    world = await make_world(real_session, org_admin)
    await real_session.execute(delete(OrgSettings).where(OrgSettings.org_team_id == world.org_id))
    await real_session.commit()
    async with crdt_docs() as docs:
        access = await docs.access(
            real_session, world.ref, user=world.owner.user, ent=world.owner.ent, agent_id=None
        )
        assert (access.can_read, access.can_write) == (True, True)
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        await _write(docs, real_session, world, world.owner, tab, tab.type(0, "typed"))
        assert (await _row(real_session, world)).projection["text"] == "typed"


@pytest.mark.parametrize(
    ("who", "budget"),
    [
        # The chat's owner: decided from the chat row alone.
        pytest.param("owner", 4, id="owner"),
        # Someone it is shared with: the node's ACL as well, read under the
        # files role (set the role, the actor, read, reset the role).
        pytest.param("writer", 8, id="shared-writer"),
    ],
)
async def test_a_steady_write_stays_within_its_statement_budget(
    real_session: AsyncSession, org_admin: OrgWithAdmin, who: str, budget: int
) -> None:
    """Every statement of a keystroke's write is paid at typing rate by every
    person typing. The budget: the chat's ACL (five, with the role it is read
    under), the row lock, the stored update and the row's new position. No
    org settings read, no peer read (the socket holds them from its hello), no
    outbox row."""
    from alkera_core.db.session import AsyncSessionLocal
    from sqlalchemy import event

    world = await make_world(real_session, org_admin)
    person = getattr(world, who)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, person, tab)
        await _write(docs, real_session, world, person, tab, tab.type(0, "warm"))
        async with AsyncSessionLocal() as db:
            peers = await docs.peers_of(db, world.ref, user=person.user)
        statements: list[str] = []
        async with AsyncSessionLocal() as db:
            conn = await db.connection()

            def counted(*args: Any) -> None:
                statements.append(str(args[2])[:80])

            event.listen(conn.sync_connection, "before_cursor_execute", counted)
            try:
                await docs.apply(
                    db,
                    world.ref,
                    user=person.user,
                    ent=person.ent,
                    agent_id=None,
                    epoch=1,
                    peer=tab.peer,
                    update_id="u-budget",
                    update=tab.type(4, "!"),
                    socket_peer_id="p:5000",
                    peers=peers | {tab.peer},
                )
                await db.commit()
            finally:
                event.remove(conn.sync_connection, "before_cursor_execute", counted)
        assert len(statements) <= budget, statements


async def test_a_write_is_judged_against_the_share_as_it_stands_now(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A writer demoted mid-session is refused on the next write, not the next subscribe."""
    from alkera_core.files.authz.grants import Principal
    from alkera_core.files.authz.ladder import ROLE_READER
    from alkera_core.models import User, WorkspaceObject
    from tests.chat_shares import share_chat_with

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.writer, tab)
        assert (
            await _write(docs, real_session, world, world.writer, tab, tab.type(0, "a"))
        ).changed
        chat = await real_session.get(WorkspaceObject, uuid.UUID(world.ref.doc_id))
        owner = await real_session.get(User, world.owner.user.id)
        assert chat is not None and owner is not None
        await share_chat_with(
            real_session,
            chat=chat,
            owner=owner,
            principal=Principal(kind="user", id=world.writer.user.id),
            role=ROLE_READER,
        )
        with pytest.raises(CrdtError) as excinfo:
            await _write(
                docs, real_session, world, world.writer, tab, tab.type(1, "b"), update_id="u-2"
            )
        assert excinfo.value.code == "forbidden"


async def test_a_document_too_large_for_one_transfer_is_restarted_rather_than_sent(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync is one transfer: one that would not fit is never attempted.
    The tab is told to come back shortly and the document starts a new epoch
    from its content, which is far smaller than its history."""
    world = await make_world(real_session, org_admin)
    tab = Peer(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, world, world.owner, tab)
        await _write(docs, real_session, world, world.owner, tab, tab.type(0, "kept"))
        monkeypatch.setattr(docs_module, "CRDT_MAX_TRANSFER_BYTES", 8)
        with pytest.raises(CrdtError) as excinfo:
            await docs.sync(real_session, world.ref, since=None, epoch_seen=None)
        await real_session.rollback()
        assert excinfo.value.code == "crdt_busy"
        await docs.drain()
        monkeypatch.undo()
        row = await _row(real_session, world)
        assert (row.epoch, row.seeded_from) == (2, "rotation")
        fresh = Peer(5001)
        await _open(docs, real_session, world, world.reader, fresh)
        assert fresh.text == "kept"


# ---------------------------------------------------------------------------
# Surviving the sandbox
# ---------------------------------------------------------------------------


async def test_a_fresh_worker_rebuilds_the_document_from_the_store(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    tab = Peer(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, world, world.owner, tab)
        for n, word in enumerate(("one ", "two ", "three ")):
            await _write(
                docs, real_session, world, world.owner, tab, tab.type(0, word), update_id=f"u-{n}"
            )
    # A new process: nothing cached anywhere.
    async with crdt_docs() as docs:
        fresh = Peer(5001)
        await _open(docs, real_session, world, world.reader, fresh)
        assert fresh.text == tab.text == "three two one "
        # ...and a write lands on the rebuilt document.
        await _write(
            docs, real_session, world, world.owner, tab, tab.type(0, "zero "), update_id="u-9"
        )
        assert (await _row(real_session, world)).projection["text"] == "zero three two one "


async def test_a_second_replica_catches_up_and_keeps_writing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two backend processes, one document: each worker holds the document at
    whatever the other last committed, and every write is still judged against
    the stored position."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as replica_a, crdt_docs() as replica_b:
        a, b = Peer(5000), Peer(5001)
        await _open(replica_a, real_session, world, world.owner, a)
        await _open(replica_b, real_session, world, world.writer, b)
        for n in range(4):
            sent = a.type(len(a.text), f"a{n} ")
            applied = await _write(
                replica_a, real_session, world, world.owner, a, sent, update_id=f"a{n}"
            )
            b.receive(applied.delta)
            sent = b.type(len(b.text), f"b{n} ")
            applied = await _write(
                replica_b, real_session, world, world.writer, b, sent, update_id=f"b{n}"
            )
            a.receive(applied.delta)
        assert a.text == b.text == (await _row(real_session, world)).projection["text"]
        assert a.text == "a0 b0 a1 b1 a2 b2 a3 b3 "


async def test_a_worker_already_at_a_commit_keeps_the_document_when_told_of_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two writes on one process, the second judged before the first one's
    commit reached the worker: judging it caught the worker up past the first
    commit. Being told of a commit it already holds must not cost the worker
    the document."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        a, b = Peer(5000), Peer(5001)
        await _open(docs, real_session, world, world.owner, a)
        await _open(docs, real_session, world, world.writer, b)
        written: list[Applied] = []
        for who, tab, word in ((world.owner, a, "a"), (world.writer, b, "b")):
            written.append(
                await docs.apply(
                    real_session,
                    world.ref,
                    user=who.user,
                    ent=who.ent,
                    agent_id=None,
                    epoch=1,
                    peer=tab.peer,
                    update_id=f"u-{word}",
                    update=tab.type(0, word),
                    socket_peer_id=f"p:{tab.peer}",
                )
            )
            await real_session.commit()
        for applied in written:
            await docs.after_commit(world.ref, applied)
        held = await docs.pool.request(
            world.ref.key,
            {"op": "export", "key": written[-1].cache_key, "epoch": 1, "log_seq": 2},
            budget_seconds=10.0,
        )
        assert held.header.get("need") is not True, "the worker dropped the document"
        assert held.header["mode"] == "snapshot"


class _CatchupCrashingPool:
    """The real pool, except that catching a cached document up from the log
    kills the worker."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def request(
        self, key: str, header: dict[str, Any], blobs: Any = (), **kwargs: Any
    ) -> Any:
        if header.get("op") == "catchup":
            raise SandboxCrashedError("the sandbox worker exited")
        return await self.inner.request(key, header, blobs, **kwargs)

    async def close(self) -> None:
        await self.inner.close()


async def test_a_worker_dying_while_catching_up_is_never_blamed_on_the_update(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Replica B's worker holds the document one commit behind, so judging B's
    write first catches it up from the log. A worker that dies there died on
    the stored history, not on the update: the update is judged on a fresh load,
    and is never refused as one that crashes the validator."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as replica_a, crdt_docs() as replica_b:
        a, b = Peer(5000), Peer(5001)
        await _open(replica_a, real_session, world, world.owner, a)
        await _open(replica_b, real_session, world, world.writer, b)
        b.receive(
            (await _write(replica_a, real_session, world, world.owner, a, a.type(0, "a "))).delta
        )
        replica_b.pool = _CatchupCrashingPool(replica_b.pool)  # type: ignore[assignment]
        sent = b.type(len(b.text), "b")
        applied = await _write(replica_b, real_session, world, world.writer, b, sent, update_id="b")
        assert applied.changed
        assert (await _row(real_session, world)).projection["text"] == "a b"
        # The same bytes, sent again, are a retry the server already holds.
        again = await _write(replica_b, real_session, world, world.writer, b, sent, update_id="b2")
        assert not again.changed


class _CrashingPool:
    """The real pool, except that validating chosen bytes kills the worker."""

    def __init__(self, inner: Any, poison: bytes) -> None:
        self.inner = inner
        self.poison = poison
        self.validations = 0

    async def request(
        self, key: str, header: dict[str, Any], blobs: Any = (), **kwargs: Any
    ) -> Any:
        if header.get("op") == "validate" and list(blobs) == [self.poison]:
            self.validations += 1
            raise SandboxCrashedError("the sandbox worker exited")
        return await self.inner.request(key, header, blobs, **kwargs)

    async def close(self) -> None:
        await self.inner.close()


async def test_an_update_that_kills_the_worker_twice_is_refused_from_then_on(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        deadly = tab.type(0, "boom")
        crashing = _CrashingPool(docs.pool, deadly)
        docs.pool = crashing  # type: ignore[assignment]
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, world.owner, tab, deadly)
        assert (excinfo.value.code, excinfo.value.reason) == ("crdt_rejected", "validator_crash")
        assert crashing.validations == 2, "retried once on a fresh worker"
        with pytest.raises(CrdtError) as again:
            await _write(docs, real_session, world, world.owner, tab, deadly)
        assert again.value.reason == "poisoned"
        assert crashing.validations == 2, "a poisoned update never reaches a worker"
        healthy = Peer(5001)
        await _open(docs, real_session, world, world.writer, healthy)
        assert (
            await _write(docs, real_session, world, world.writer, healthy, healthy.type(0, "ok"))
        ).changed


# ---------------------------------------------------------------------------
class _SlowPool:
    """The real pool, except that the chosen requests outlive their budget,
    ``times`` times each (for ever when ``None``): what a cold worker on a
    loaded host does to an honest request."""

    def __init__(
        self,
        inner: Any,
        *,
        update: bytes | None = None,
        ops: set[str] = set(),  # noqa: B006
        times: int | None = None,
    ) -> None:
        self.inner = inner
        self.update = update
        self.ops = ops
        self.times = times
        self.timeouts = 0

    async def request(
        self, key: str, header: dict[str, Any], blobs: Any = (), **kwargs: Any
    ) -> Any:
        slow_update = header.get("op") == "validate" and list(blobs) == [self.update]
        slow = slow_update or header.get("op") in self.ops
        if slow and (self.times is None or self.timeouts < self.times):
            self.timeouts += 1
            raise SandboxTimeoutError("the sandbox worker timed out")
        return await self.inner.request(key, header, blobs, **kwargs)

    async def close(self) -> None:
        await self.inner.close()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_an_update_too_slow_to_judge_is_busy_every_time_and_lands_when_resent(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A SQL cell typed into a notebook on a cold worker outlived the
    validate budget three times, was poisoned, and the tab lost it. A timeout
    is never a verdict: each one answers busy with a longer wait, the bytes
    are never poisoned, and once the sandbox keeps up the same bytes land."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        clock = _Clock()
        docs.clock = clock
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        slow = tab.type(0, "SELECT 1")
        pool = _SlowPool(docs.pool, update=slow, times=4)
        docs.pool = pool  # type: ignore[assignment]
        waits = []
        for attempt in range(4):
            with pytest.raises(CrdtError) as busy:
                await _write(docs, real_session, world, world.owner, tab, slow)
            assert (busy.value.code, busy.value.reason) == ("crdt_busy", "validator_timeout")
            waits.append(busy.value.retry_after_ms)
            # Sent again before the wait is up, it is not sent to a worker.
            with pytest.raises(CrdtError) as early:
                await _write(docs, real_session, world, world.owner, tab, slow)
            assert early.value.code == "crdt_busy" and pool.timeouts == attempt + 1
            clock.now += busy.value.retry_after_ms / 1000
        assert waits == [1000, 2000, 4000, 8000]
        applied = await _write(docs, real_session, world, world.owner, tab, slow)
        assert applied.changed
        assert (await _row(real_session, world)).projection["text"] == "SELECT 1"


async def test_an_update_whose_caller_went_away_mid_judgment_is_not_held_against_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A socket that closes while its update is being judged cancels the
    write. The cancellation is nobody's verdict: the same bytes, sent again
    on the next socket, land."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        update_bytes = tab.type(0, "kept")
        judging = asyncio.Event()

        class _Hanging:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            async def request(
                self, key: str, header: dict[str, Any], blobs: Any = (), **kwargs: Any
            ) -> Any:
                if header.get("op") == "validate":
                    judging.set()
                    await asyncio.Event().wait()
                return await self.inner.request(key, header, blobs, **kwargs)

        real = docs.pool
        docs.pool = _Hanging(real)  # type: ignore[assignment]
        write = asyncio.create_task(
            _write(docs, real_session, world, world.owner, tab, update_bytes)
        )
        await judging.wait()
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        docs.pool = real
        applied = await _write(docs, real_session, world, world.owner, tab, update_bytes)
        assert applied.changed
        assert (await _row(real_session, world)).projection["text"] == "kept"


async def test_a_document_too_slow_to_load_is_never_quarantined(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Loading that times out says the host is busy, not that the history is
    broken: however often it happens, the document keeps its epoch and history."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as first:
        tab = Peer(5000)
        await _open(first, real_session, world, world.owner, tab)
        await _write(first, real_session, world, world.owner, tab, tab.type(0, "kept"))
    async with crdt_docs() as cold:
        clock = _Clock()
        cold.clock = clock
        cold.pool = _SlowPool(cold.pool, ops={"load"})  # type: ignore[assignment]
        waits = []
        for _ in range(4):
            with pytest.raises(CrdtError) as busy:
                await _open(cold, real_session, world, world.owner, Peer(5001))
            await real_session.rollback()
            assert busy.value.code == "crdt_busy"
            waits.append(busy.value.retry_after_ms)
            clock.now += busy.value.retry_after_ms / 1000
        # Each timed-out load killed a worker: the tabs are told to wait longer.
        assert waits == sorted(waits) and waits[-1] > waits[0]
        await cold.drain()
        row = await _row(real_session, world)
        assert (row.epoch, row.quarantined_at) == (1, None)
        cold.pool = cold.pool.inner  # type: ignore[attr-defined]
        again = Peer(5002)
        await _open(cold, real_session, world, world.owner, again)
        assert again.text == "kept"


# Compaction, restarting the history, quarantine
# ---------------------------------------------------------------------------


def _registry(**limits: int) -> CrdtRegistry:
    """The real types, with the chat workspace's limits shrunk so a test can
    reach them in a few writes."""
    import dataclasses

    workspace = ChatWorkspaceType()
    workspace.limits = dataclasses.replace(ChatWorkspaceType.limits, **limits)  # type: ignore[misc]
    return CrdtRegistry({"chat_draft": workspace, "file": FileDocType()})


async def test_a_grown_log_is_folded_into_a_snapshot_without_losing_anything(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs(registry=_registry(compact_log_rows=3)) as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        for n in range(3):
            await _write(
                docs, real_session, world, world.owner, tab, tab.type(0, f"{n}"), update_id=f"u{n}"
            )
        await docs.drain()
        row = await _row(real_session, world)
        assert (row.log_seq, row.snapshot_log_seq, row.log_bytes, row.epoch) == (3, 3, 0, 1)
        assert await _log_rows(real_session, world) == 0
        # The folded document still takes the peer's next write and opens whole elsewhere.
        await _write(docs, real_session, world, world.owner, tab, tab.type(0, "3"), update_id="u3")
    async with crdt_docs() as docs:
        fresh = Peer(5001)
        await _open(docs, real_session, world, world.reader, fresh)
        assert fresh.text == tab.text == "3210"


async def test_a_full_document_restarts_its_history_and_every_peer_rebases(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with crdt_docs(registry=_registry(max_doc_bytes=600, compact_log_rows=10_000)) as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        n = 0
        with pytest.raises(CrdtError) as excinfo:
            while True:
                await _write(
                    docs,
                    real_session,
                    world,
                    world.owner,
                    tab,
                    tab.type(0, "words "),
                    update_id=f"u{n}",
                )
                n += 1
        assert excinfo.value.code == "doc_full"
        await docs.drain()
        row = await _row(real_session, world)
        assert (row.epoch, row.log_seq, row.seeded_from) == (2, 0, "rotation")
        assert row.projection["text"] == "words " * n
        assert await _log_rows(real_session, world) == 0
        reload = (await _frames(real_session, world))[-1]["envelope"]
        assert (reload["kind"], reload["epoch"], reload["payload"]["reason"]) == (
            "reload",
            2,
            "compacted",
        )
        # The tab is on the old epoch until it rebases.
        with pytest.raises(CrdtError) as stale:
            await _write(
                docs, real_session, world, world.owner, tab, tab.type(0, "x"), update_id="late"
            )
        assert (stale.value.code, stale.value.epoch) == ("stale_epoch", 2)
        rebased = Peer(5002)
        await _open(docs, real_session, world, world.owner, rebased)
        assert rebased.text == "words " * n
        assert (
            await _write(
                docs, real_session, world, world.owner, rebased, rebased.type(0, "!"), epoch=2
            )
        ).changed


@pytest.mark.parametrize("quarantine", [False, True], ids=["rotation", "quarantine"])
async def test_no_two_epochs_share_an_operation_id(
    real_session: AsyncSession, org_admin: OrgWithAdmin, quarantine: bool
) -> None:
    """An update from a later epoch landing in an earlier epoch's copy is
    missing history, never text merged at the wrong place. The shape that
    collided while every epoch was seeded by one fixed peer: a draft cut down
    to less than its seed, then restarted, then edited."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        await _write(docs, real_session, world, world.owner, tab, tab.type(0, "x" * 40))
        await _write(docs, real_session, world, world.owner, tab, tab.erase(2, 38), update_id="u-2")
        copies: list[Peer] = [tab]
        for epoch in (2, 3):
            await docs.restart(real_session, world.ref, reason="test", quarantine=quarantine)
            await real_session.commit()
            fresh = Peer(5000 + epoch)
            await _open(docs, real_session, world, world.owner, fresh)
            assert (await _row(real_session, world)).epoch == epoch
            copies.append(fresh)
        for earlier, later in itertools.pairwise(copies):
            before = earlier.text
            edit = later.type(1, "NEW")
            status = earlier.doc.import_(edit)
            assert status.pending is not None and not status.pending.is_empty
            assert earlier.text == before


@pytest.mark.parametrize("damage", ["gap", "bad-bytes", "vector"])
async def test_a_history_that_does_not_rebuild_is_quarantined_and_reseeded_from_its_text(
    real_session: AsyncSession, org_admin: OrgWithAdmin, damage: str
) -> None:
    world = await make_world(real_session, org_admin)
    tab = Peer(5000)
    async with crdt_docs() as docs:
        await _open(docs, real_session, world, world.owner, tab)
        for n in range(3):
            await _write(
                docs,
                real_session,
                world,
                world.owner,
                tab,
                tab.type(len(tab.text), f"w{n} "),
                update_id=f"u{n}",
            )
    where = (
        CrdtUpdate.org_id == world.org_id,
        CrdtUpdate.doc_id == world.ref.doc_id,
        CrdtUpdate.log_seq == 2,
    )
    if damage == "gap":
        from sqlalchemy import delete

        await real_session.execute(delete(CrdtUpdate).where(*where))
    elif damage == "bad-bytes":
        await real_session.execute(update(CrdtUpdate).where(*where).values(data=b"\x00not loro"))
    else:
        await real_session.execute(
            update(CrdtDoc).where(CrdtDoc.doc_id == world.ref.doc_id).values(vv=b"\x01\x02")
        )
    await real_session.commit()
    async with crdt_docs() as docs:
        with pytest.raises(CrdtError) as excinfo:
            await docs.sync(real_session, world.ref, since=None, epoch_seen=None)
        await real_session.rollback()
        assert excinfo.value.code == "crdt_busy"
        await docs.drain()
        row = await _row(real_session, world)
        assert (row.epoch, row.seeded_from) == (2, "quarantine")
        assert row.quarantined_at is not None
        assert await _log_rows(real_session, world) == 0
        reload = (await _frames(real_session, world))[-1]["envelope"]
        assert (reload["kind"], reload["payload"]["reason"]) == ("reload", "quarantine")
        fresh = Peer(5001)
        await _open(docs, real_session, world, world.reader, fresh)
        assert fresh.text == "w0 w1 w2 "


async def test_a_person_with_more_peers_than_a_write_may_name_is_moved_to_a_new_epoch(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Every page load mints a Loro peer, and a write names every peer its
    writer was ever handed for the document. Past the cap a write could not
    name them all and was refused forever; now the document starts a new
    epoch, which only peers held by a live socket outlive, and writing goes
    on. A lapsed hold is not a live socket."""
    from datetime import UTC, datetime, timedelta

    from alkera_core.models import CrdtPeer
    from backend.services.crdt.sandbox.protocol import MAX_PEERS
    from sqlalchemy import func, insert

    world = await make_world(real_session, org_admin)
    where = (
        CrdtPeer.org_id == world.org_id,
        CrdtPeer.doc_type == "chat_workspace",
        CrdtPeer.doc_id == world.ref.doc_id,
    )
    now = datetime.now(UTC)
    row = {
        "org_id": world.org_id,
        "doc_type": "chat_workspace",
        "doc_id": world.ref.doc_id,
        "user_id": world.owner.user.id,
        "last_seen_at": now,
    }
    await real_session.execute(insert(CrdtPeer), [row] * MAX_PEERS)
    live = (
        await real_session.execute(
            insert(CrdtPeer)
            .values(**row, held_by="elsewhere:p:live", held_until=now + timedelta(minutes=5))
            .returning(CrdtPeer.loro_peer)
        )
    ).scalar_one()
    await real_session.execute(
        insert(CrdtPeer).values(
            **row, held_by="elsewhere:p:gone", held_until=now - timedelta(minutes=5)
        )
    )
    await real_session.commit()
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, world.owner, tab, tab.type(0, "lost?"))
        assert excinfo.value.code == "doc_full"
        await docs.drain()
        assert (await _row(real_session, world)).epoch == 2
        left = (await real_session.execute(select(CrdtPeer.loro_peer).where(*where))).scalars()
        assert list(left) == [live]
        rebased = Peer(5001)
        await _open(docs, real_session, world, world.owner, rebased)
        assert rebased.text == ""
        assert (
            await _write(
                docs, real_session, world, world.owner, rebased, rebased.type(0, "on"), epoch=2
            )
        ).changed
        count = await real_session.scalar(select(func.count()).select_from(CrdtPeer).where(*where))
        assert count == 1


async def test_a_large_delta_is_announced_by_reference(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_core.config import settings

    # Room for an announcement that names the update, not one that carries it.
    monkeypatch.setattr(settings, "realtime_ephemeral_max_bytes", 1200)
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs, Heard(world.ref.channel) as heard:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        applied = await _write(docs, real_session, world, world.owner, tab, tab.type(0, "x" * 2000))
        payload = (await heard.updates(1))[-1].payload
        assert payload["log_ref"] == applied.log_seq
        assert "data_b64" not in payload or payload["data_b64"] == ""
        assert (
            await docs.logged_delta(real_session, world.ref, epoch=1, log_seq=applied.log_seq)
            == applied.delta
        )


@pytest.mark.parametrize(
    "room", ["for_a_reference", "for_nothing"], ids=["vector_left_off", "nothing_fits"]
)
async def test_a_vector_too_large_to_announce_never_fails_the_write(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch, room: str
) -> None:
    """A document many tabs have written carries a long version vector. When
    even an announcement by reference cannot carry it, the announcement goes
    without it (tabs import the delta; none reads the vector off it) — and
    when nothing fits at all, the write still stands and nothing raises."""
    from alkera_core.config import settings
    from alkera_core.schemas.realtime import encode_b64

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs, Heard(world.ref.channel) as heard:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        writers = list(range(6000, 6600))
        before = tab.doc.oplog_vv
        for writer in writers:
            tab.doc.peer_id = writer
            tab.doc.get_text("draft").insert(0, "x")
            tab.doc.commit()
        tab.doc.peer_id = 7000
        update_bytes = bytes(tab.doc.export(ExportMode.Updates(before)))
        peers = frozenset([*writers, 7000])

        async def write(data: bytes, update_id: str) -> Applied:
            applied = await docs.apply(
                real_session,
                world.ref,
                user=world.owner.user,
                ent=world.owner.ent,
                agent_id=None,
                epoch=1,
                peer=7000,
                update_id=update_id,
                update=data,
                socket_peer_id="p:7000",
                peers=peers,
            )
            await real_session.commit()
            return applied

        first = await write(update_bytes, "u-many")
        await docs.broadcast(world.ref, first)
        assert len(await heard.updates(1)) == 1
        vv_b64 = encode_b64(first.vv)
        cap = len(vv_b64) if room == "for_a_reference" else 200
        monkeypatch.setattr(settings, "realtime_ephemeral_max_bytes", cap)
        second = await write(tab.type(0, "y"), "u-last")
        assert second.changed
        await docs.broadcast(world.ref, second)
        heard_now = await heard.updates(2)
        assert (await _row(real_session, world)).log_seq == 2
        if room == "for_a_reference":
            assert len(heard_now) == 2
            payload = heard_now[-1].payload
            assert payload["log_ref"] == second.log_seq
            assert "vv_b64" not in payload
            assert (
                await docs.logged_delta(real_session, world.ref, epoch=1, log_seq=2) == second.delta
            )
        else:
            assert len(heard_now) == 1


# ---------------------------------------------------------------------------
# Loro peers and carets
# ---------------------------------------------------------------------------


async def test_a_peer_is_minted_held_reused_and_never_shared(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from datetime import UTC, datetime, timedelta

    from alkera_core.models import CrdtPeer

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        mine = await docs.claim_peer(
            real_session, world.ref, user=world.owner.user, offered=None, holder="s1"
        )
        assert mine > 1023
        # The same socket offering it back keeps it.
        assert (
            await docs.claim_peer(
                real_session, world.ref, user=world.owner.user, offered=mine, holder="s1"
            )
            == mine
        )
        # Another socket (a duplicated tab) offering it while it is held gets its own.
        other = await docs.claim_peer(
            real_session, world.ref, user=world.owner.user, offered=mine, holder="s2"
        )
        assert other not in (mine,)
        # Someone else offering it never gets it.
        theirs = await docs.claim_peer(
            real_session, world.ref, user=world.writer.user, offered=mine, holder="s3"
        )
        assert theirs not in (mine, other)
        # Released, it goes back to its own person's next socket...
        await docs.release_peer(real_session, peer=mine, holder="s1")
        assert (
            await docs.claim_peer(
                real_session, world.ref, user=world.owner.user, offered=mine, holder="s4"
            )
            == mine
        )
        # ...and a hold left to expire is claimable too.
        assert not await docs.renew_peer(real_session, peer=mine, holder="s1")
        await real_session.execute(
            update(CrdtPeer)
            .where(CrdtPeer.loro_peer == mine)
            .values(held_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        assert (
            await docs.claim_peer(
                real_session, world.ref, user=world.owner.user, offered=mine, holder="s5"
            )
            == mine
        )
        assert await docs.renew_peer(real_session, peer=mine, holder="s5")
        await real_session.commit()


async def test_a_caret_is_relayed_only_as_its_own_peers(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from loro import EphemeralStore, Side

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        tab.type(0, "hello")
        cursor = tab.doc.get_text("draft").get_cursor(2, Side.Middle)
        assert cursor is not None
        store = EphemeralStore(60_000)
        store.set("5000", {"anchor": cursor.encode(), "focus": cursor.encode()})
        relayed = await docs.ephemeral(world.ref, peer=5000, data=bytes(store.encode_all()))
        echoed = EphemeralStore(60_000)
        echoed.apply(relayed)
        assert set(echoed.get_all_states()) == {"5000"}
        with pytest.raises(CrdtError) as excinfo:
            await docs.ephemeral(world.ref, peer=5001, data=bytes(store.encode_all()))
        assert (excinfo.value.code, excinfo.value.reason) == ("crdt_rejected", "ephemeral")


async def test_a_tab_clears_its_own_caret_and_no_one_elses(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A tab that loses focus deletes its caret; the deletion is relayed and
    removes the caret where it is shown. Deleting another tab's caret is not."""
    from loro import EphemeralStore, Side

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)
        tab.type(0, "hello")
        cursor = tab.doc.get_text("draft").get_cursor(2, Side.Middle)
        assert cursor is not None
        mine = EphemeralStore(60_000)
        mine.set("5000", {"anchor": cursor.encode(), "focus": cursor.encode()})
        shown = EphemeralStore(60_000)
        shown.apply(await docs.ephemeral(world.ref, peer=5000, data=bytes(mine.encode_all())))
        assert set(shown.get_all_states()) == {"5000"}

        # The store keeps whichever write is stamped later, and a delete in the
        # same millisecond as the set loses; a person blurs a field later than that.
        await asyncio.sleep(0.002)
        mine.delete("5000")
        shown.apply(await docs.ephemeral(world.ref, peer=5000, data=bytes(mine.encode_all())))
        assert shown.get_all_states() == {}

        with pytest.raises(CrdtError) as excinfo:
            await docs.ephemeral(world.ref, peer=5001, data=bytes(mine.encode_all()))
        assert (excinfo.value.code, excinfo.value.reason) == ("crdt_rejected", "ephemeral")
        with pytest.raises(CrdtError):
            await docs.ephemeral(
                world.ref, peer=5000, data=bytes(EphemeralStore(60_000).encode_all())
            )


async def test_edits_from_a_persons_earlier_peer_land_after_a_reconnect(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The tab typed under one Loro peer, lost its socket before the ack, and
    came back under a fresh one (the old hold had not lapsed). Its update carries
    both peers' edits — all of them its own — and lands. An update carrying
    another person's earlier peer does not."""
    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        first = await docs.claim_peer(
            real_session, world.ref, user=world.owner.user, offered=None, holder="s1"
        )
        await real_session.commit()
        tab = Peer(first)
        await _open(docs, real_session, world, world.owner, tab)
        tab.type(0, "before ")
        second = await docs.claim_peer(
            real_session, world.ref, user=world.owner.user, offered=first, holder="s2"
        )
        await real_session.commit()
        assert second != first
        tab.doc.peer_id = second
        tab.peer = second
        tab.type(7, "after")
        update = tab.since(Peer(9999).vv)
        applied = await _write(docs, real_session, world, world.owner, tab, update)
        assert applied.changed
        assert (await _row(real_session, world)).projection["text"] == "before after"

        theirs = await docs.claim_peer(
            real_session, world.ref, user=world.writer.user, offered=None, holder="s3"
        )
        await real_session.commit()
        stranger = Peer(theirs)
        stranger.receive(tab.since(Peer(9999).vv))
        stranger.type(0, "x")
        forged = stranger.since(Peer(9999).vv)
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, world.owner, tab, forged, update_id="u-f")
        assert (excinfo.value.code, excinfo.value.reason) == ("crdt_rejected", "peer")


async def test_a_person_keeps_their_newest_unheld_peers_and_every_held_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Every hello from a fresh page mints a peer. Past the per-person cap the
    oldest peers no socket holds are forgotten at the next mint, so reloading
    in a loop cannot grow the table or the set a write names. A held peer is
    never forgotten: a live tab may still be writing as it."""
    from alkera_core.models import CrdtPeer
    from backend.services.crdt.docs import PEERS_PER_PERSON

    world = await make_world(real_session, org_admin)
    extra = 6
    async with crdt_docs() as docs:
        minted = [
            await docs.claim_peer(
                real_session, world.ref, user=world.owner.user, offered=None, holder=f"s{i}"
            )
            for i in range(PEERS_PER_PERSON + extra)
        ]
        await real_session.commit()

        def mine() -> Any:
            return select(CrdtPeer.loro_peer).where(
                CrdtPeer.org_id == world.org_id,
                CrdtPeer.doc_type == "chat_workspace",
                CrdtPeer.doc_id == world.ref.doc_id,
                CrdtPeer.user_id == world.owner.user.id,
            )

        # All held: none may go, however many there are.
        assert set((await real_session.execute(mine())).scalars()) == set(minted)

        # The oldest stays held; every other socket leaves.
        for i, peer in enumerate(minted[1:], start=1):
            await docs.release_peer(real_session, peer=peer, holder=f"s{i}")
        theirs = await docs.claim_peer(
            real_session, world.ref, user=world.writer.user, offered=None, holder="w"
        )
        newest = await docs.claim_peer(
            real_session, world.ref, user=world.owner.user, offered=None, holder="fresh"
        )
        await real_session.commit()
        kept = set((await real_session.execute(mine())).scalars())
        assert len(kept) == PEERS_PER_PERSON
        assert {minted[0], newest} <= kept
        assert kept == {minted[0], newest, *minted[-(PEERS_PER_PERSON - 2) :]}
        # Another person's peers are theirs to keep.
        assert await real_session.scalar(
            select(CrdtPeer.loro_peer).where(CrdtPeer.loro_peer == theirs)
        )


async def test_a_paste_typed_and_cut_in_one_update_is_refused_past_the_update_cap(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The text cap judges what is left, so one update may type and delete
    far more than a draft holds; every such update spends the history's room
    and a few force a restart (a reload for every tab). An update that carries
    more than a few drafts' worth of operations is refused whole; one with
    room to spare goes through."""
    from loro import ExportMode

    world = await make_world(real_session, org_admin)
    async with crdt_docs() as docs:
        tab = Peer(5000)
        await _open(docs, real_session, world, world.owner, tab)

        def typed_and_cut(char: str, chars: int) -> bytes:
            before = tab.doc.oplog_vv
            text = tab.doc.get_text("draft")
            text.insert(0, char * chars)
            text.delete(0, chars)
            tab.doc.commit()
            return bytes(tab.doc.export(ExportMode.Updates(before)))

        modest = typed_and_cut("y", 40_000)
        assert (await _write(docs, real_session, world, world.owner, tab, modest)).changed
        # Three bytes a character keeps it inside the operation cap.
        huge = typed_and_cut("語", 48_000)
        assert 128 * 1024 < len(huge) < 512 * 1024
        with pytest.raises(CrdtError) as excinfo:
            await _write(docs, real_session, world, world.owner, tab, huge, update_id="u-2")
        assert (excinfo.value.code, excinfo.value.reason) == ("crdt_rejected", "update_too_large")


async def test_a_document_is_restarted_at_most_once_a_minute(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Every restart reloads every tab. A document that fills again right
    after one is answered busy, not restarted, until the interval has passed;
    then it restarts as before."""
    from backend.services.crdt.docs import ROTATION_INTERVAL_SECONDS

    world = await make_world(real_session, org_admin)
    now = [1000.0]
    async with crdt_docs(registry=_registry(max_doc_bytes=600, compact_log_rows=10_000)) as docs:
        docs.clock = lambda: now[0]

        async def fill(tab: Peer, epoch: int) -> CrdtError:
            for n in range(200):
                try:
                    await _write(
                        docs,
                        real_session,
                        world,
                        world.owner,
                        tab,
                        tab.type(0, "words "),
                        update_id=f"e{epoch}-{n}",
                        epoch=epoch,
                    )
                except CrdtError as exc:
                    return exc
            raise AssertionError("the document never filled")

        first = Peer(5000)
        await _open(docs, real_session, world, world.owner, first)
        assert (await fill(first, 1)).code == "doc_full"
        await docs.drain()
        assert (await _row(real_session, world)).epoch == 2

        now[0] += ROTATION_INTERVAL_SECONDS / 2
        second = Peer(5001)
        await _open(docs, real_session, world, world.owner, second)
        busy = await fill(second, 2)
        assert busy.code == "crdt_busy"
        assert busy.retry_after_ms is not None
        assert 0 < busy.retry_after_ms <= ROTATION_INTERVAL_SECONDS * 1000 / 2
        await docs.drain()
        assert (await _row(real_session, world)).epoch == 2

        now[0] += ROTATION_INTERVAL_SECONDS / 2
        # The tab sends again what it was told to wait with.
        unsent = second.since(bytes((await _row(real_session, world)).vv))
        with pytest.raises(CrdtError) as full:
            await _write(
                docs,
                real_session,
                world,
                world.owner,
                second,
                unsent,
                update_id="late",
                epoch=2,
            )
        assert full.value.code == "doc_full"
        await docs.drain()
        assert (await _row(real_session, world)).epoch == 3
