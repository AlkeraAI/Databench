"""A tab drops its socket while the server is still answering its hello.

The sequence the chaos suite once caught hanging: the history restarts, the
tab is told ``reload`` and says ``hello`` again, and before the answer arrives
the socket goes and the tab comes back on the OTHER replica and says
``hello`` there. The second hello must be answered — every time, whatever
point the first one had reached when its socket went.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import AsyncExitStack

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from backend.services.realtime.runtime import runtime_of
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import create_app
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, serve_controlled
from tests.crdt.crdt_client import LaneClient
from tests.crdt.crdt_world import make_world
from tests.test_ws_gateway import logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

ROUNDS = 40


async def test_a_hello_cut_off_mid_answer_never_strands_the_next(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Forty reconnects in a few seconds is exactly what the handshake limiter
    # refuses; it is not what this test is about.
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    rng = random.Random(11)
    world = await make_world(real_session, org_admin)
    async with AsyncExitStack() as stack:
        apps = [create_app(), create_app()]
        servers = [await stack.enter_async_context(serve_controlled(app)) for app in apps]
        owner = await logged_in(world.owner.user.email, world.owner.password)
        stack.push_async_callback(owner.aclose)
        tab = LaneClient(owner, world.ref.channel, "tab")
        where = 0
        await tab.connect(servers[where].addr)
        stack.push_async_callback(tab.disconnect)
        await tab.wait_synced()
        for round_ in range(ROUNDS):
            await tab.type_token(f"<{round_}>")
            runtime = runtime_of(apps[rng.randrange(2)])
            assert runtime is not None and runtime.crdt is not None
            async with AsyncSessionLocal() as db:
                await runtime.crdt.restart(db, world.ref, reason="race", quarantine=False)
                await db.commit()
            # Cut the socket somewhere inside the reload → hello → snapshot
            # exchange the restart set off.
            await asyncio.sleep(rng.choice([0, 0.001, 0.003, 0.005, 0.01, 0.02]))
            await tab.disconnect()
            where = 1 - where
            await tab.connect(servers[where].addr)
            await tab.wait_synced(15)
        await tab.settle(30)


async def test_an_edit_committed_while_a_hello_is_answered_reaches_the_tab_after_its_snapshot(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hello reads what the tab is missing, then answers. An edit another
    tab commits in between is not in that answer, and its broadcast reaches
    this socket while the answer is still being made: sent first, it lands on
    a tab that has no document to apply it to yet and is lost to it. It must
    arrive after the snapshot."""
    world = await make_world(real_session, org_admin)
    app = create_app()
    async with AsyncExitStack() as stack:
        served = await stack.enter_async_context(serve_controlled(app))
        writer_client = await logged_in(world.owner.user.email, world.owner.password)
        stack.push_async_callback(writer_client.aclose)
        writer = LaneClient(writer_client, world.ref.channel, "writer")
        stack.push_async_callback(writer.disconnect)
        await writer.connect(served.addr)
        await writer.wait_synced()
        await writer.type_token("<before>")
        await writer.settle()

        runtime = runtime_of(app)
        assert runtime is not None and runtime.crdt is not None
        docs = runtime.crdt
        read = asyncio.Event()
        answer = asyncio.Event()
        real_sync = docs.sync

        async def sync_then_wait(*args: object, **kwargs: object) -> object:
            found = await real_sync(*args, **kwargs)  # type: ignore[arg-type]
            read.set()
            await answer.wait()
            return found

        monkeypatch.setattr(docs, "sync", sync_then_wait)
        reader_client = await logged_in(world.writer.user.email, world.writer.password)
        stack.push_async_callback(reader_client.aclose)
        reader = LaneClient(reader_client, world.ref.channel, "reader")
        stack.push_async_callback(reader.disconnect)
        await reader.connect(served.addr)
        await asyncio.wait_for(read.wait(), 20)
        # Committed after the reader's hello read the document, before it answered.
        await writer.type_token("<during>")
        await writer.settle()
        broadcast = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < broadcast:
            if any(
                frame.get("t") == "doc" and frame["envelope"]["kind"] == "crdt"
                for frame in reader.received
            ):
                break
            await asyncio.sleep(0.02)
        monkeypatch.setattr(docs, "sync", real_sync)
        answer.set()
        await reader.wait_synced()
        await reader.settle()
        await asyncio.sleep(0.5)

        assert reader.text == "<before><during>", reader.trace[-10:]
