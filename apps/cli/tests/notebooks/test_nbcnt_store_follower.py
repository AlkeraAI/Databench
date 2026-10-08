"""The engine's follower of a notebook's live document survives its channel
failing (a refused subscribe, a socket that drops, a replaced credential):
it reads the document again, reports what moved meanwhile, and joins again.
It ends only once the notebook is gone.

The backend's view route is a ``MockTransport`` serving a document the test
edits; the channel is a scripted :class:`DocSignals`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from alkera_cli.notebooks.store_loro import (
    DocSignal,
    Located,
    LoroDocumentStore,
    StoreError,
)
from alkera_notebook.document.store import DocumentChange

PATH = "analysis/weekly.alknb.py"
WHERE = Located(drive_id="d1", item_id="i1")


class Backend:
    """The notebook view route over a document the test edits."""

    def __init__(self) -> None:
        self.cells: dict[str, str] = {"c1": "x = 1"}
        self.version = 1
        self.gone = False

    def edit(self, cell: str, source: str) -> None:
        self.cells[cell] = source
        self.version += 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.gone:
            return httpx.Response(404, json={"code": "not_found", "message": "gone"})
        return httpx.Response(
            200,
            json={
                "token": f"v{self.version}",
                "cells": [
                    {"id": cid, "kind": "python", "source": src} for cid, src in self.cells.items()
                ],
                "settings": {},
            },
        )


class Channel:
    """A document channel whose each join does what the script says next:
    ``"refuse"`` (the subscribe is refused), ``"end"`` (the socket closes),
    or ``"listen"`` (it signals ready, then whatever the test sends)."""

    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.joins = 0
        self.joined = asyncio.Event()
        #: ``None`` closes the socket.
        self.queue: asyncio.Queue[DocSignal | None] = asyncio.Queue()
        self.listening = asyncio.Event()

    async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
        self.joins += 1
        self.joined.set()
        step = self.script.pop(0) if self.script else "listen"
        if step == "refuse":
            raise StoreError("not_found", "no such channel", 403)
        if step == "end":
            return
        yield DocSignal(kind="ready")
        self.listening.set()
        while (signal := await self.queue.get()) is not None:
            yield signal


def _store(backend: Backend, channel: Channel, slept: list[float]) -> LoroDocumentStore:
    async def resolve(path: str) -> Located:
        return WHERE

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    return LoroDocumentStore(
        http=httpx.AsyncClient(
            base_url="http://backend", transport=httpx.MockTransport(backend.handler)
        ),
        resolve=resolve,
        signals=channel,
        follow_retry_initial=1.0,
        follow_retry_max=4.0,
        sleep=sleep,
    )


async def _next(changes: AsyncIterator[DocumentChange]) -> DocumentChange:
    return await asyncio.wait_for(changes.__anext__(), timeout=5)


@pytest.mark.parametrize("failure", ["refuse", "end"])
async def test_a_failed_channel_is_joined_again_and_what_moved_meanwhile_is_reported(
    failure: str,
) -> None:
    backend, channel, slept = Backend(), Channel(failure, failure, failure), []
    changes = _store(backend, channel, slept).changes(PATH)
    pending = asyncio.ensure_future(_next(changes))
    await asyncio.wait_for(channel.joined.wait(), timeout=5)
    backend.edit("c1", "x = 2")  # written while the channel is down
    change = await pending
    assert change.origin == "ops" and change.cell_ids == ["c1"]
    assert change.token == "v2"
    # It keeps going: once the channel listens, a later edit arrives on it.
    pending = asyncio.ensure_future(_next(changes))
    await asyncio.wait_for(channel.listening.wait(), timeout=5)
    backend.edit("c2", "y = 1")
    channel.queue.put_nowait(DocSignal(kind="update", actor_id="ada"))
    change = await pending
    assert (change.cell_ids, change.actor_id) == (["c2"], "ada")
    assert channel.joins == 4
    # The pause grows while the channel keeps failing, up to its most.
    assert slept == [1.0, 2.0, 4.0]
    await changes.aclose()  # type: ignore[attr-defined]


async def test_the_pause_starts_over_once_the_channel_works_again() -> None:
    backend, channel, slept = Backend(), Channel("refuse", "listen", "refuse"), []
    changes = _store(backend, channel, slept).changes(PATH)
    pending = asyncio.ensure_future(_next(changes))
    await asyncio.wait_for(channel.listening.wait(), timeout=5)
    channel.listening.clear()
    channel.queue.put_nowait(None)  # the working channel closes
    await asyncio.wait_for(channel.listening.wait(), timeout=5)
    backend.edit("c1", "x = 3")
    channel.queue.put_nowait(DocSignal(kind="update"))
    assert (await pending).cell_ids == ["c1"]
    # Refused, then worked (the pause starts over), then closed and refused.
    assert slept == [1.0, 1.0, 2.0]
    await changes.aclose()  # type: ignore[attr-defined]


async def test_the_follower_ends_only_when_the_notebook_is_gone() -> None:
    backend, channel, slept = Backend(), Channel("refuse"), []
    changes = _store(backend, channel, slept).changes(PATH)
    pending = asyncio.ensure_future(_next(changes))
    await asyncio.wait_for(channel.joined.wait(), timeout=5)
    backend.gone = True
    change = await pending
    assert change.origin == "deleted"
    with pytest.raises(StopAsyncIteration):
        await _next(changes)


async def test_a_notebook_deleted_while_followed_ends_the_follower() -> None:
    backend, channel, slept = Backend(), Channel("listen"), []
    changes = _store(backend, channel, slept).changes(PATH)
    pending = asyncio.ensure_future(_next(changes))
    await asyncio.wait_for(channel.listening.wait(), timeout=5)
    backend.gone = True
    channel.queue.put_nowait(DocSignal(kind="update"))
    assert (await pending).origin == "deleted"
    assert slept == []
