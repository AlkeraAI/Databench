"""A server restart while somebody types into a file costs them nothing.

A tab types into a co-edited file, the server it is connected to stops, the
tab keeps typing with nowhere to send it, and a new server process starts on
the same database. The document the new process serves is the one the old one
held: the same epoch, the same version vector, answered as the difference and
not as a whole new document. What was typed while the server was away lands
once, and the drive receives it.

The restart is not what loses a browser's edits (a page that reloads with
edits pending is; the browser's tests cover that), and this pins it: a server
that came back on a new epoch, or forgot what it had acknowledged, would make
every restart a rebase for every open tab.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest
from alkera_core.schemas.realtime import decode_b64
from loro import VersionVector
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, fresh_app, serve_controlled
from tests.crdt.crdt_client import LaneClient
from tests.crdt.file_world import SOURCE, FileWorld, drive_text, file_world
from tests.test_ws_gateway import logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BEFORE = ["<up0>", "<up1>", "<up2>"]
WHILE_DOWN = ["<down0>", "<down1>", "<down2>", "<down3>"]


def _same_version(a: bytes, b: bytes) -> bool:
    """Whether two encoded version vectors name the same version (their
    bytes may differ in order)."""
    first, second = VersionVector.decode(a), VersionVector.decode(b)
    return bool(first.includes_vv(second) and second.includes_vv(first))


async def _until_drive_reads(
    db: AsyncSession, fw: FileWorld, expected: str, seconds: float = 30
) -> str:
    seen = ""
    for _ in range(int(seconds / 0.25)):
        seen = await drive_text(db, fw)
        if seen == expected:
            return seen
        await asyncio.sleep(0.25)
    return seen


async def test_a_restarted_server_serves_the_same_document_and_takes_what_was_typed_meanwhile_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    fw = await file_world(real_session, org_admin)
    async with AsyncExitStack() as stack:
        owner = await logged_in(fw.world.owner.user.email, fw.world.owner.password)
        stack.push_async_callback(owner.aclose)
        tab = LaneClient(owner, fw.ref.channel, "tab", text_name="content")
        stack.push_async_callback(tab.disconnect)

        async with serve_controlled(fresh_app()) as first:
            await tab.connect(first.addr)
            await tab.wait_synced()
            assert tab.text == SOURCE
            for token in BEFORE:
                await tab.type_token(token)
            await tab.settle()
            epoch, peer, acknowledged = tab.epoch, tab.peer, tab.server_vv
            assert acknowledged is not None
        # The first server is gone, its lifespan shut down. The tab's socket
        # went with it, and the tab types on.
        await tab.disconnect()
        for token in WHILE_DOWN:
            await tab.type_token(token)
        assert tab.unconfirmed() == WHILE_DOWN

        async with serve_controlled(fresh_app()) as second:
            seen = len(tab.received)
            await tab.connect(second.addr)
            await tab.wait_synced()
            answers = [
                frame["envelope"]
                for frame in tab.received[seen:]
                if frame.get("t") == "doc" and frame["envelope"]["kind"] == "snapshot"
            ]
            # The same document, answered as the difference from what the tab
            # holds: no new epoch, no whole document, nothing forgotten.
            assert answers[0]["epoch"] == epoch
            assert answers[0]["payload"]["mode"] == "updates"
            assert _same_version(decode_b64(answers[0]["payload"]["vv_b64"]), acknowledged)
            assert tab.epoch == epoch and tab.peer == peer

            await tab.settle()
            expected = SOURCE + "".join(BEFORE + WHILE_DOWN)
            assert tab.text == expected
            assert tab.errors == []
            assert await _until_drive_reads(real_session, fw, expected) == expected
