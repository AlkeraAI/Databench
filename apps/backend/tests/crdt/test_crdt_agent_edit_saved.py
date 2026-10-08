"""An agent's edit merged through the text peer reaches the drive when no
person can be named for it.

The box holding a chat's folder joins a file's live document as a text peer
when somebody opens the file, and from then on the agent's saves go into the
document instead of being uploaded: the box treats the merge as landed. When
the only person in the session is reading (a viewer, or a writer who looked
and left), nobody can be named as the write back's author, so the drive must
take it as the box itself, under the box's own fence, exactly as the box's
upload would have landed. Everything here is the real route stack: the app's
own lane, a registered box on its credential, real Postgres and real Files.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from alkera_core.authz.headers import agent_headers
from alkera_core.config import settings
from alkera_core.models import CrdtDoc
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.tree import FileNode
from backend.authz import decide_on_record
from backend.services.crdt.docs import CrdtDocs
from backend.services.realtime import crdt_lane, runtime
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client
from tests.conftest import fastapi_app as app
from tests.crdt.crdt_world import Peer
from tests.crdt.file_world import SOURCE, FileWorld, drive_text, file_world, head_version
from tests.files._boxes import registered_box
from tests.files._live_holder import MockHolder

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


@dataclass
class Box:
    holder: MockHolder
    machine_id: str
    client: AsyncClient


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _box(db: AsyncSession, fw: FileWorld) -> Box:
    """The chat's box on its own registered credential, bound to the chat and
    holding its folder, taking inbound writes, as an awake chat's box does."""
    owner = fw.world.owner.user
    token, machine_id = await registered_box(
        db, user_id=owner.id, email=owner.email, org_id=fw.world.org_id
    )
    await db.execute(
        text(
            "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
            "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
        ),
        {"machine": machine_id, "id": uuid.UUID(fw.world.ref.doc_id)},
    )
    await db.commit()
    folder = (
        await db.execute(
            select(FileNode).where(FileNode.target_object_id == uuid.UUID(fw.world.ref.doc_id))
        )
    ).scalar_one()
    client = app_client(headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)})
    holder = MockHolder(client, folder.drive_id, folder.id, machine=machine_id)
    taken = await holder.take(db, _idem, purpose="chat", inbound=True, live=True)
    assert taken.status_code == 200, taken.text
    lease = await db.get(FileLease, folder.id)
    assert lease is not None and lease.holder_kind == "machine"
    await db.commit()
    return Box(holder=holder, machine_id=machine_id, client=client)


async def _lane(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CrdtDocs]:
    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    started = await runtime.start(app, decide=decide_on_record)
    try:
        lane = crdt_lane(app)
        assert lane is not None
        # Every pass is run by the test, so what it asserts is the pass's own.
        lane.run_sessions = False
        yield lane
    finally:
        await runtime.stop(app, started)


async def _reader_opens(lane: CrdtDocs, db: AsyncSession, fw: FileWorld) -> None:
    """The owner opens the file live and types nothing: the session exists,
    with nobody named as its last writer."""
    who = fw.world.owner
    await lane.access(db, fw.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await lane.sync(db, fw.ref, since=None, epoch_seen=None)
    await db.commit()
    Peer(9101, container="content").receive(sync.data)


async def _agent_saves(box: Box, fw: FileWorld, edit: str) -> str:
    """The agent's save, as the box sends it: read the document, submit the
    agent's whole text on that token. Answers the text sent."""
    read = await box.holder.live_text(fw.node_id)
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["live"] is True
    sent = body["text"] + edit
    answer = await box.holder.submit_text(fw.node_id, sent, base_token=body["token"])
    assert answer.status_code == 200, answer.text
    assert answer.json()["text"] == sent
    # The drive will get it: the box has nothing to upload.
    assert answer.json()["saved"] is True
    return sent


async def _row(db: AsyncSession, fw: FileWorld) -> CrdtDoc:
    row = (
        await db.execute(
            select(CrdtDoc).where(CrdtDoc.doc_type == "file", CrdtDoc.doc_id == fw.ref.doc_id)
        )
    ).scalar_one()
    await db.refresh(row)
    await db.commit()
    return row


async def test_an_agent_edit_nobody_typed_into_is_written_as_the_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader opens the file, the agent saves through the text peer, and
    the write back lands it on the drive as the box's own bytes (its machine
    stamped on the version, no person named), never parked for a person.
    After the reader leaves and the idle pass starts a new epoch, the agent's
    next save lands the same way."""
    fw = await file_world(real_session, org_admin)
    box = await _box(real_session, fw)
    lanes = _lane(monkeypatch)
    lane = await anext(lanes)
    try:
        await _reader_opens(lane, real_session, fw)
        first = await _agent_saves(box, fw, "# the agent's first save\n")
        settled = await lane.sessions.write_back(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "written"
        assert await drive_text(real_session, fw) == first
        version = await head_version(real_session, fw)
        assert (version.version_metadata or {}).get("machine_id") == box.machine_id
        assert version.source == "document_snapshot"
        row = await _row(real_session, fw)
        assert row.save_paused_reason is None
        epoch = row.epoch

        idled = await lane.sessions.idle(real_session, fw.ref)
        await real_session.commit()
        assert idled.outcome == "unchanged"
        assert (await _row(real_session, fw)).epoch == epoch + 1

        second = await _agent_saves(box, fw, "# and its second\n")
        assert second == first + "# and its second\n"
        settled = await lane.sessions.write_back(real_session, fw.ref)
        await real_session.commit()
        assert settled.outcome == "written"
        assert await drive_text(real_session, fw) == second
        assert (await _row(real_session, fw)).save_paused_reason is None
    finally:
        await lanes.aclose()
        await box.client.aclose()
    assert SOURCE in second


async def test_a_box_that_no_longer_holds_the_folder_is_not_written_as(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write back as the box is the box's own upload, so it is only ever
    made under a lease the box holds now: once its lease is gone the session
    parks as before (nobody may write it), and the drive is untouched."""
    fw = await file_world(real_session, org_admin)
    box = await _box(real_session, fw)
    lanes = _lane(monkeypatch)
    lane = await anext(lanes)
    try:
        await _reader_opens(lane, real_session, fw)
        await _agent_saves(box, fw, "# the agent\n")
        await real_session.execute(
            text("UPDATE file_leases SET released_at = now() WHERE node_id = :node"),
            {"node": box.holder.node_id},
        )
        await real_session.commit()
        settled = await lane.sessions.write_back(real_session, fw.ref)
        await real_session.commit()
    finally:
        await lanes.aclose()
        await box.client.aclose()
    assert settled.outcome == "refused"
    assert await drive_text(real_session, fw) == SOURCE
    assert (await _row(real_session, fw)).save_paused_reason == "no_writer"
