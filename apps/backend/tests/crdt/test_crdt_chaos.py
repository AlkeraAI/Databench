"""Two replicas, three tabs and a hundred rounds of things going wrong.

Two real servers share one database (the two-replica deployment). Three tabs —
the owner and a writer, one of them with two tabs — type unique tokens into
one chat's live draft while the test, round after round, drops a tab's socket
and brings it back on the OTHER replica, SIGKILLs a replica's sandbox
workers, restarts the document's history (a new epoch), and lets typing race
all of it. At the end every tab, the stored projection and a document rebuilt
from the store in a fresh process agree, and every token anybody typed is
there exactly once: nothing acknowledged is lost, nothing is duplicated.

The first test is the plain two-replica case without the chaos: concurrent
typing on two servers converges through the fan-out between them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import random
from contextlib import AsyncExitStack

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import CrdtDoc
from backend.services.realtime.runtime import runtime_of
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import create_app
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, serve_controlled
from tests.crdt.crdt_client import LaneClient
from tests.crdt.crdt_world import Peer, World, crdt_docs, kill_hard, make_world
from tests.test_ws_gateway import logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

ROUNDS = 100
#: ``CRDT_CHAOS_SEED`` replays (or varies) the sequence of what goes wrong.
SEED = int(os.environ.get("CRDT_CHAOS_SEED", "7"))


async def _projection(world: World) -> str:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(CrdtDoc).where(CrdtDoc.doc_id == world.ref.doc_id))
        ).scalar_one()
        return str(row.projection["text"])


async def _rebuilt(world: World) -> str:
    """The draft as a fresh process rebuilds it from the store."""
    async with crdt_docs() as docs:
        peer = Peer(9001)
        async with AsyncSessionLocal() as db:
            sync = await docs.sync(db, world.ref, since=None, epoch_seen=None)
            await db.commit()
        peer.receive(sync.data)
        return peer.text


async def _converged(tabs: list[LaneClient], world: World, seconds: float = 30) -> str:
    for tab in tabs:
        await tab.settle(seconds)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        stored = await _projection(world)
        if all(tab.text == stored for tab in tabs):
            return stored
        if loop.time() > deadline:
            raise AssertionError(f"tabs never converged on {stored!r}: {[t.text for t in tabs]}")
        for tab in tabs:
            await tab.hello()
        await asyncio.sleep(0.2)


def _assert_every_token_once(text: str, tabs: list[LaneClient], typed: list[str]) -> None:
    for token in typed:
        assert text.count(token) == 1, f"{token!r} appears {text.count(token)} times in {text!r}"
    assert all(not tab.unconfirmed() for tab in tabs)


async def test_two_replicas_converge_under_concurrent_typing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with AsyncExitStack() as stack:
        a_server = await stack.enter_async_context(serve_controlled(create_app()))
        b_server = await stack.enter_async_context(serve_controlled(create_app()))
        owner = await logged_in(world.owner.user.email, world.owner.password)
        writer = await logged_in(world.writer.user.email, world.writer.password)
        stack.push_async_callback(owner.aclose)
        stack.push_async_callback(writer.aclose)
        a = LaneClient(owner, world.ref.channel, "a")
        b = LaneClient(writer, world.ref.channel, "b")
        for tab, server in ((a, a_server), (b, b_server)):
            await tab.connect(server.addr)
            stack.push_async_callback(tab.disconnect)
            await tab.wait_synced()
        typed: list[str] = []

        async def burst(tab: LaneClient, prefix: str) -> None:
            for n in range(25):
                token = f"[{prefix}{n}]"
                typed.append(token)
                await tab.type_token(token)
                await asyncio.sleep(0.01)

        await asyncio.gather(burst(a, "a"), burst(b, "b"))
        text = await _converged([a, b], world)
        _assert_every_token_once(text, [a, b], typed)
        assert await _rebuilt(world) == text


async def test_a_hundred_rounds_of_chaos_lose_and_duplicate_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    rng = random.Random(SEED)
    world = await make_world(real_session, org_admin)
    async with AsyncExitStack() as stack:
        apps = [create_app(), create_app()]
        servers = [await stack.enter_async_context(serve_controlled(app)) for app in apps]
        # This test kills workers from outside every few rounds, far faster
        # than the breaker's "a slot that keeps crashing" threshold allows; the
        # breaker exists for input that crashes a worker again and again
        # (covered in test_sandbox_pool), so it is held open here.
        for app in apps:
            runtime = runtime_of(app)
            assert runtime is not None and runtime.crdt is not None
            pool = runtime.crdt.pool
            pool.config = dataclasses.replace(pool.config, breaker_crashes=10_000)
        owner = await logged_in(world.owner.user.email, world.owner.password)
        writer = await logged_in(world.writer.user.email, world.writer.password)
        stack.push_async_callback(owner.aclose)
        stack.push_async_callback(writer.aclose)
        tabs = [
            LaneClient(owner, world.ref.channel, "owner1"),
            LaneClient(owner, world.ref.channel, "owner2"),
            LaneClient(writer, world.ref.channel, "writer"),
        ]
        where = {}
        for index, tab in enumerate(tabs):
            where[tab.name] = index % 2
            await tab.connect(servers[index % 2].addr)
            stack.push_async_callback(tab.disconnect)
            await tab.wait_synced()

        typed: list[str] = []
        happened: dict[str, int] = {}
        for round_ in range(ROUNDS):
            action = rng.choices(
                ["type", "reconnect", "kill_workers", "rotate"], weights=[70, 15, 8, 7]
            )[0]
            happened[action] = happened.get(action, 0) + 1
            if action == "type":
                tab = rng.choice(tabs)
                token = f"<{tab.name}:{round_}>"
                typed.append(token)
                await tab.type_token(token)
            elif action == "reconnect":
                tab = rng.choice(tabs)
                await tab.disconnect()
                where[tab.name] = 1 - where[tab.name]
                await tab.connect(servers[where[tab.name]].addr)
                await tab.wait_synced()
            elif action == "kill_workers":
                runtime = runtime_of(rng.choice(apps))
                assert runtime is not None and runtime.crdt is not None
                for pid in runtime.crdt.pool.pids():
                    if pid is not None:
                        # It may have been replaced on its own a moment ago.
                        kill_hard(pid)
            else:
                runtime = runtime_of(rng.choice(apps))
                assert runtime is not None and runtime.crdt is not None
                async with AsyncSessionLocal() as db:
                    await runtime.crdt.restart(db, world.ref, reason="chaos", quarantine=False)
                    await db.commit()
            # Let the round's traffic interleave with the next round's.
            await asyncio.sleep(rng.choice([0, 0, 0.01, 0.05]))

        assert happened.get("type", 0) > 50 and len(happened) == 4, happened
        text = await _converged(tabs, world, seconds=60)
        _assert_every_token_once(text, tabs, typed)
        assert await _rebuilt(world) == text
