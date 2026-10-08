"""Whether a live file is saving reaches every tab, however late it arrives,
against real Postgres, real Files and real sandbox workers.

The notice that saving paused is sent once, when a write back is first
refused, over a lossy ``pg_notify``. A tab that opens the file afterwards, or
reconnects after a deploy, was not there to hear it. These pin that the
answer to every open (the sync) and every ``reload`` carries the state the
document's row records, so such a tab shows "not saving" from its first
frame, and shows nothing once saving resumed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import pytest
from alkera_core.doc_type_names import respell_channel, respell_payload
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import CrdtDoc, EventOutbox, User
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.schemas.realtime import CrdtSaving
from backend.services.crdt.docs import CrdtDocs, CrdtError, Sync
from backend.services.crdt.registry import DocRef
from backend.services.crdt.sessions import QUARANTINED_LOST
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Peer, Person, make_world
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import SOURCE, FileWorld, drive_text, file_world
from tests.crdt.test_crdt_file_docs import _holder, _idem
from tests.crdt.test_crdt_gateway import Tab
from tests.test_ws_gateway import connect, logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PAUSED_NO_WRITER = CrdtSaving(state="paused", reason="no_writer")
OK = CrdtSaving(state="ok")


@asynccontextmanager
async def _docs() -> AsyncIterator[CrdtDocs]:
    """A store whose session passes run only when a test calls them."""
    async with lane_docs() as docs:
        docs.run_sessions = False
        yield docs


async def _row(db: AsyncSession, ref: DocRef) -> CrdtDoc:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == ref.stored_type, CrdtDoc.doc_id == ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    await db.commit()
    return row


async def _open(
    docs: CrdtDocs,
    db: AsyncSession,
    ref: DocRef,
    who: Person,
    tab: Peer,
    *,
    since: bytes | None = None,
    epoch_seen: int | None = None,
) -> Sync:
    await docs.access(db, ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, ref, since=since, epoch_seen=epoch_seen)
    await db.commit()
    tab.receive(sync.data)
    return sync


async def _type(
    docs: CrdtDocs, db: AsyncSession, fw: FileWorld, who: Person, tab: Peer, typed: str
) -> None:
    row = await _row(db, fw.ref)
    applied = await docs.apply(
        db,
        fw.ref,
        user=who.user,
        ent=who.ent,
        agent_id=None,
        epoch=row.epoch,
        peer=tab.peer,
        update_id=uuid.uuid4().hex,
        update=tab.type(0, typed),
        socket_peer_id=f"p:{tab.peer}",
    )
    await db.commit()
    await docs.after_commit(fw.ref, applied)


async def _share(db: AsyncSession, fw: FileWorld, who: Person, role: str) -> None:
    chat = await db.get(WorkspaceObject, uuid.UUID(fw.world.ref.doc_id))
    owner = await db.get(User, fw.world.owner.user.id)
    assert chat is not None and owner is not None
    await share_chat_with(
        db, chat=chat, owner=owner, principal=Principal(kind="user", id=who.user.id), role=role
    )
    await db.commit()


async def _written_back(docs: CrdtDocs, db: AsyncSession, ref: DocRef) -> str:
    settled = await docs.sessions.write_back(db, ref)
    await db.commit()
    return settled.outcome


async def _paused(docs: CrdtDocs, db: AsyncSession, fw: FileWorld, tab: Peer) -> None:
    """Typed by the writer, who then lost the right to write: the write back
    is refused and the session parks as ``no_writer``."""
    await _open(docs, db, fw.ref, fw.world.writer, tab)
    await _type(docs, db, fw, fw.world.writer, tab, "# typed\n")
    await _share(db, fw, fw.world.writer, ROLE_READER)
    assert await _written_back(docs, db, fw.ref) == "refused"
    assert (await _row(db, fw.ref)).save_paused_reason == "no_writer"


async def _last_reload(db: AsyncSession, ref: DocRef) -> dict[str, Any]:
    """The latest ``reload`` stored for ``ref``, read the way a replica reads it."""
    rows = (
        await db.execute(
            select(EventOutbox.payload)
            .where(EventOutbox.entity_id == respell_channel(ref.channel, "to_replicas"))
            .order_by(EventOutbox.id)
        )
    ).scalars()
    await db.commit()
    reloads = [
        envelope
        for envelope in (respell_payload(row, "from_replicas")["envelope"] for row in rows)
        if envelope["kind"] == "reload"
    ]
    assert reloads, "no reload was stored"
    return dict(reloads[-1])


@pytest.mark.parametrize(
    "reconnect",
    [
        pytest.param(False, id="a-new-tab"),
        pytest.param(True, id="a-tab-reconnecting-with-its-vector"),
    ],
)
async def test_a_tab_that_opens_after_saving_paused_is_told_by_its_sync(
    real_session: AsyncSession, org_admin: OrgWithAdmin, reconnect: bool
) -> None:
    """Saving paused before this tab opened (or while it was away), so it
    never heard the notice: the sync that opens it says paused and why. Once
    a write back lands again, the next open says ok."""
    fw = await file_world(real_session, org_admin)
    typist = Peer(6200, container="content")
    async with _docs() as docs:
        await _paused(docs, real_session, fw, typist)
        late = Peer(6201, container="content")
        since, epoch_seen = None, None
        if reconnect:
            first = await _open(docs, real_session, fw.ref, fw.world.owner, late)
            since, epoch_seen = late.vv, first.epoch
        sync = await _open(
            docs, real_session, fw.ref, fw.world.owner, late, since=since, epoch_seen=epoch_seen
        )
        assert sync.saving == PAUSED_NO_WRITER

        await _share(real_session, fw, fw.world.writer, ROLE_WRITER)
        assert await _written_back(docs, real_session, fw.ref) == "written"
        again = await _open(
            docs, real_session, fw.ref, fw.world.owner, Peer(6202, container="content")
        )
        assert again.saving == OK
    assert await drive_text(real_session, fw) == "# typed\n" + SOURCE


async def test_a_sync_served_by_another_replica_reads_the_pause_from_the_row(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """One replica parked the session; a tab opens on another replica, which
    never paused anything itself. It still answers paused: the state is the
    row's, not any replica's memory."""
    fw = await file_world(real_session, org_admin)
    async with _docs() as first:
        await _paused(first, real_session, fw, Peer(6210, container="content"))
    async with _docs() as second:
        sync = await _open(
            second, real_session, fw.ref, fw.world.owner, Peer(6211, container="content")
        )
    assert sync.saving == PAUSED_NO_WRITER


async def test_a_new_epoch_while_saving_is_paused_tells_every_tab_in_its_reload(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The document moves to a new epoch while saving is paused (its history
    rotated). Every tab is sent a ``reload``, and that reload says saving is
    still paused, so a tab that missed the notice learns it there too."""
    fw = await file_world(real_session, org_admin)
    async with _docs() as docs:
        await _paused(docs, real_session, fw, Peer(6220, container="content"))
        epoch = await docs.restart(real_session, fw.ref, reason="rotation", quarantine=False)
        await real_session.commit()
    reload = await _last_reload(real_session, fw.ref)
    assert (reload["epoch"], reload["payload"]["reason"]) == (epoch, "compacted")
    assert reload["payload"]["saving"] == PAUSED_NO_WRITER.model_dump(mode="json")


async def test_a_write_from_a_left_epoch_is_answered_with_the_saving_state(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A tab still on the epoch before a rotation sends an edit. The refusal
    names the current epoch for the ``reload`` the gateway sends, and carries
    the saving state that reload repeats."""
    fw = await file_world(real_session, org_admin)
    stale = Peer(6230, container="content")
    async with _docs() as docs:
        await _paused(docs, real_session, fw, Peer(6231, container="content"))
        old = await _open(docs, real_session, fw.ref, fw.world.owner, stale)
        await docs.restart(real_session, fw.ref, reason="rotation", quarantine=False)
        await real_session.commit()
        with pytest.raises(CrdtError) as refused:
            await docs.apply(
                real_session,
                fw.ref,
                user=fw.world.owner.user,
                ent=fw.world.owner.ent,
                agent_id=None,
                epoch=old.epoch,
                peer=stale.peer,
                update_id="late",
                update=stale.type(0, "x"),
                socket_peer_id=f"p:{stale.peer}",
            )
        await real_session.rollback()
    assert (refused.value.code, refused.value.epoch) == ("stale_epoch", old.epoch + 1)
    assert refused.value.saving == PAUSED_NO_WRITER


async def test_a_document_that_rests_nowhere_says_nothing_about_saving(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat's draft is not written anywhere: its sync and its reloads carry
    no saving state at all, so a tab never shows "not saving" for it."""
    world = await make_world(real_session, org_admin)
    async with _docs() as docs:
        sync = await _open(docs, real_session, world.ref, world.owner, Peer(6240))
        await docs.restart(real_session, world.ref, reason="rotation", quarantine=False)
        await real_session.commit()
    assert sync.saving is None
    assert (await _last_reload(real_session, world.ref))["payload"]["saving"] is None


async def test_a_tab_opened_while_a_machine_holds_the_folder_is_told_at_its_first_frame(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over real sockets: a tab typed, a machine took the folder, and that tab
    was told saving paused. A second person opens the file afterwards: the
    snapshot that opens it says paused and why, with no notice needed. Once
    the machine lets go and the edit lands, a third tab's snapshot says ok."""
    from alkera_core.config import settings
    from backend.services.realtime.runtime import runtime_of
    from tests.conftest import fastapi_app as app

    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 1)
    runtime = runtime_of(app)
    assert runtime is not None
    monkeypatch.setattr(runtime.crdt, "write_back_delay", 5.0)
    fw = await file_world(real_session, org_admin)
    async with AsyncExitStack() as stack:

        async def tab(person: Person, *, writes: bool) -> Tab:
            http = await logged_in(person.user.email, person.password)
            stack.push_async_callback(http.aclose)
            sock = await stack.enter_async_context(connect(uvicorn_server, http))
            opened = Tab(sock, fw.ref.channel, container="content")
            # Nobody may type while the machine holds the folder.
            assert await sock.subscribe(fw.ref.channel) is writes
            return opened

        typist = await tab(fw.world.owner, writes=True)
        await typist.hello()
        assert (await typist.type(0, "# typed\n"))["kind"] == "ack"
        holder = await _holder(real_session, client, fw, purpose="mount", inbound=False)
        await typist.sock.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["payload"].get("t") == "saving",
            seconds=20,
        )

        late = await tab(fw.world.writer, writes=False)
        saving = (await late.hello())["payload"]["saving"]
        assert (saving["state"], saving["reason"]) == ("paused", "leased")

        released = await holder.release(real_session, _idem)
        assert released.status_code in (200, 204), released.text
        for _ in range(80):
            if await drive_text(real_session, fw) == "# typed\n" + SOURCE:
                break
            await asyncio.sleep(0.25)
        assert (await _row(real_session, fw.ref)).save_paused_reason is None
        fresh = await tab(fw.world.owner, writes=True)
        saving = (await fresh.hello())["payload"]["saving"]
        assert (saving["state"], saving["reason"]) == ("ok", "")


async def test_a_quarantine_that_lost_edits_says_so_in_its_reload_and_every_later_sync(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The stored snapshot went bad, so the edits since the last save could
    not be rebuilt and the document restarts from the drive. The reload that
    moves every tab to the new epoch says saving is paused with that reason,
    and so does the sync of a tab that opens afterwards, until a write back
    lands again."""
    fw = await file_world(real_session, org_admin)
    tab = Peer(6250, container="content")
    async with _docs() as docs:
        await _open(docs, real_session, fw.ref, fw.world.owner, tab)
        await _type(docs, real_session, fw, fw.world.owner, tab, "# lost\n")
        await real_session.execute(
            update(CrdtDoc)
            .where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
            .values(snapshot=b"\x00not a loro snapshot\xff")
        )
        await real_session.commit()
        await docs.restart(real_session, fw.ref, reason="test", quarantine=True)
        await real_session.commit()
        late = await _open(
            docs, real_session, fw.ref, fw.world.owner, Peer(6251, container="content")
        )
        resumed = Peer(6252, container="content")
        await _open(docs, real_session, fw.ref, fw.world.owner, resumed)
        await _type(docs, real_session, fw, fw.world.owner, resumed, "# again\n")
        assert await _written_back(docs, real_session, fw.ref) == "written"
        after = await _open(
            docs, real_session, fw.ref, fw.world.owner, Peer(6253, container="content")
        )
    lost = CrdtSaving(state="paused", reason=QUARANTINED_LOST)
    reload = await _last_reload(real_session, fw.ref)
    assert reload["payload"]["reason"] == "quarantine"
    assert reload["payload"]["saving"] == lost.model_dump(mode="json")
    assert late.saving == lost
    assert after.saving == OK
