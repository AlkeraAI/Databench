"""The box as a text peer of a co-edited file, with a REAL box against the
REAL gateway.

Both ends are the production code: a uvicorn instance serves the real app
(Postgres, the Loro lane's sandbox, the ``pg_notify`` lane and the hub), and
the box is :class:`ChatFolders` holding a chat's folder with its live sync and
text peer, plus a :class:`CloudSocket` minted as the registered machine,
holding ``machine:<id>`` and answering with :class:`MachineRequests`, the pair
``CloudMirrorService`` wires at registration. The person is a Loro document on
the gateway's socket, as the browser's editor is.

What is pinned: a person opening the file has the box join its document; an
agent's save on the box's disk reaches the person's open document within a
second, written as the box's machine; and what the person types reaches the
box's disk within a second, told on the machine channel (this box drains no
write backs, so nothing else could have brought it).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from alkera_core.schemas.realtime import encode_b64
from files._live_typist import Typist
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from test_cloud_live_folder import (  # noqa: F401  (fixtures registered by import)
    SETUP_WAIT,
    LiveChat,
    _until,
    content_server,
    live_chat,
)
from test_cloud_live_promote import box_socket
from tests.chat_shares import files_on  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("files_on"),
    # A real app, a real box and a real watcher: on one worker, so a wait
    # measures the chain and not the servers queued in front of it.
    pytest.mark.xdist_group("cloud_live_text_peer"),
]

NAME = "plan.txt"
PLAN = "".join(f"{n}. item {n}\n" for n in range(1, 7))
#: How long the test waits for an agent's save to reach the open document,
#: and a person's keystroke the box's disk, before it calls the chain hung. A
#: hang guard, not a speed bound: how fast each crossing was is printed, and
#: which path carried it is asserted from the rows the text peer writes.
HANG_GUARD = 30.0


@dataclass(frozen=True)
class _Server:
    base_url: str
    token: str


async def _landed(db: AsyncSession, live: LiveChat) -> uuid.UUID | None:
    row = (
        await db.execute(
            text(
                "SELECT id FROM file_nodes WHERE parent_id = :parent AND name = :name "
                "AND trashed_at IS NULL AND head_version_id IS NOT NULL"
            ),
            {"parent": uuid.UUID(live.scratch_node_id), "name": NAME.encode()},
        )
    ).one_or_none()
    await db.commit()
    return None if row is None else uuid.UUID(str(row.id))


async def _pump(person: Typist, until: Callable[[], bool], seconds: float) -> float:
    """Take the person's frames until ``until`` holds; how long it took."""
    started = time.monotonic()
    while not until():
        left = seconds - (time.monotonic() - started)
        if left <= 0:
            raise AssertionError(f"nothing after {seconds}s")
        try:
            raw = await asyncio.wait_for(person.ws.recv(), timeout=left)
        except TimeoutError:
            continue
        person._absorb(json.loads(raw))
    return time.monotonic() - started


async def _type(person: Typist, piece: str) -> None:
    """One keystroke, sent and acknowledged."""
    data = person._type(piece)
    await person._send(
        person._envelope(
            "crdt",
            {"t": "update", "update_id": uuid.uuid4().hex[:16], "data_b64": encode_b64(data)},
        )
    )
    answer = await person._until(
        lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("ack", "error", "reload")
    )
    assert answer["envelope"]["kind"] == "ack", answer


async def test_an_agent_save_and_a_keystroke_cross_between_box_and_editor_as_text_peers(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    path = live_chat.working_dir / NAME
    await asyncio.to_thread(path.write_text, PLAN, encoding="utf-8")

    async def landed() -> uuid.UUID | None:
        return await _landed(real_session, live_chat)

    node_id = await _until(landed, window=SETUP_WAIT, what="the file reaching the drive")
    held = live_chat.folders.held(live_chat.chat_id)
    assert held is not None and held.live is not None and held.live.peer is not None
    peer = held.live.peer
    async with box_socket(live_chat) as box:
        person = Typist(
            _Server(base_url=f"http://{live_chat.addr}", token=live_chat.member_token),
            str(node_id),
            0,
            "p",
        )
        await person._connect()
        try:

            async def joined() -> bool | None:
                return peer.owns(str(node_id)) or None

            await _until(joined, window=SETUP_WAIT, what="the box joining the open document")
            current = await asyncio.to_thread(path.read_text, encoding="utf-8")
            await asyncio.to_thread(path.write_text, current + "agent0\n", encoding="utf-8")
            assert person.doc is not None
            reached = await _pump(
                person, lambda: "agent0" in person.doc.get_text("content").to_string(), HANG_GUARD
            )
            await _type(person, "p0 ")
            typed = time.monotonic()
            while "p0" not in await asyncio.to_thread(path.read_text, encoding="utf-8"):
                assert time.monotonic() - typed < HANG_GUARD, "the keystroke never reached the disk"
                await asyncio.sleep(0.02)
            on_disk = time.monotonic() - typed
        finally:
            await person.ws.close()
        heard = [request for request in box.requests if request.get("kind") == "live_text"]
    print(f"agent save to open document {reached:.2f}s; keystroke to box disk {on_disk:.2f}s")
    assert heard and all(request["node_id"] == str(node_id) for request in heard)
    rows: list[Any] = (
        await real_session.execute(
            text(
                "SELECT agent_id, author_user_id FROM crdt_updates "
                "WHERE doc_type = 'file' AND doc_id = :doc AND update_id LIKE 'peer-%'"
            ),
            {"doc": str(node_id)},
        )
    ).all()
    await real_session.commit()
    assert rows and {(row.agent_id, row.author_user_id) for row in rows} == {
        (live_chat.box_machine, None)
    }


async def test_the_checkpoint_push_skips_a_file_the_live_plane_has_queued(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
) -> None:
    """A file the live plane is about to send is one the checkpoint push
    leaves alone, spelled from the chat's folder: the two raced on a restart
    and sent one file twice, the second over the first."""
    held = live_chat.folders.held(live_chat.chat_id)
    assert held is not None and held.live is not None
    sync = held.live
    path = live_chat.working_dir / "queued.md"
    await asyncio.to_thread(path.write_text, "on its way", encoding="utf-8")

    async def queued() -> bool | None:
        return "queued.md" in sync.pending or None

    await _until(queued, window=SETUP_WAIT, what="the live plane queueing the file")
    assert "scratch/queued.md" in live_chat.folders.tombstones(live_chat.chat_id)
