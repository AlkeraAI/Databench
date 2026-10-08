"""A batch that trashes co-edited files writes their live edits back first.

A single trash does it in its ``If-Match`` dependency; a batch trashes many
nodes in one Files transaction, so the edits under every node it trashes are
written back before that transaction opens (the write back takes the file's
rows in transactions of its own), and each trash is fenced on the etag that
write produced, so the caller's etags hold.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from backend.authz import decide_on_record
from backend.services.crdt.docs import CrdtDocs
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.crdt_world import Peer
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import SOURCE, FileWorld, drive_text, file_world, node_of

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _typed(docs: CrdtDocs, db: AsyncSession, fw: FileWorld, peer: int, piece: str) -> None:
    """A person opened the file and typed ``piece``, acknowledged and not yet
    written back."""
    who = fw.world.owner
    tab = Peer(peer, container="content")
    await docs.access(db, fw.ref, user=who.user, ent=who.ent, agent_id=None)
    sync = await docs.sync(db, fw.ref, since=None, epoch_seen=None)
    await db.commit()
    tab.receive(sync.data)
    epoch = sync.epoch
    applied = await docs.apply(
        db,
        fw.ref,
        user=who.user,
        ent=who.ent,
        agent_id=None,
        epoch=epoch,
        peer=tab.peer,
        update_id=uuid.uuid4().hex,
        update=tab.type(0, piece),
        socket_peer_id=f"p:{tab.peer}",
    )
    await db.commit()
    await docs.after_commit(fw.ref, applied)


@asynccontextmanager
async def _replica(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The app as a replica runs it, with its live lane (no listener)."""
    from alkera_core.config import settings
    from backend.app_factory import process_app

    app = process_app()
    from backend.services.realtime import runtime

    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    started = await runtime.start(app, decide=decide_on_record)
    try:
        yield
    finally:
        await runtime.stop(app, started)


async def _bulk(client: AsyncClient, drive_id: Any, items: list[dict[str, Any]]) -> Any:
    return await client.post(
        f"/api/v1/files/drives/{drive_id}/bulk",
        json={"items": items},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


async def test_a_batch_trash_writes_back_the_acked_edit_first_and_keeps_the_callers_etag(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person's edit was acknowledged and the file is trashed in a batch
    before the session wrote it back: the trash lands on the caller's etag,
    and the version it trashed holds the edit (a restore brings it back)."""
    fw = await file_world(real_session, org_admin)
    node = await node_of(real_session, fw.node_id)
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    async with lane_docs() as docs:
        docs.run_sessions = False
        await _typed(docs, real_session, fw, 8001, "# acked\n")
    async with _replica(monkeypatch):
        answer = await _bulk(
            client,
            node.drive_id,
            [{"id": "t", "op": "trash", "itemId": str(fw.node_id), "ifMatch": node.etag}],
        )
    assert answer.status_code == 200, answer.text
    assert [row["status"] for row in answer.json()["responses"]] == [204]
    assert await drive_text(real_session, fw) == "# acked\n" + SOURCE


async def test_a_batch_trashing_two_live_files_writes_both_back_on_their_etags(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each file's write back moves its etag before its item is reached:
    every trash is fenced on what its write produced, never refused for an
    etag the server itself moved."""
    fw = await file_world(real_session, org_admin)
    other = await file_world(real_session, org_admin, name="other.py")
    node = await node_of(real_session, fw.node_id)
    beside = await node_of(real_session, other.node_id)
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    async with lane_docs() as docs:
        docs.run_sessions = False
        await _typed(docs, real_session, fw, 8002, "# one\n")
        await _typed(docs, real_session, other, 8003, "# two\n")
    async with _replica(monkeypatch):
        answer = await _bulk(
            client,
            node.drive_id,
            [
                {"id": "a", "op": "trash", "itemId": str(fw.node_id), "ifMatch": node.etag},
                {"id": "b", "op": "trash", "itemId": str(other.node_id), "ifMatch": beside.etag},
            ],
        )
    assert answer.status_code == 200, answer.text
    assert [row["status"] for row in answer.json()["responses"]] == [204, 204]
    assert await drive_text(real_session, fw) == "# one\n" + SOURCE
    assert await drive_text(real_session, other) == "# two\n" + SOURCE
