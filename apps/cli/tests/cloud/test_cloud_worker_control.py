"""An org worker takes a credential the moment the supervisor sends it, and
says so, even while a pass a route asked for is still running."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from alkera_cli.cloud.org_worker import serve_control
from alkera_cli.org_worker_protocol import Credential, Route, encode


class _BusyService:
    """A service whose sync pass hangs until released, as one does on a
    backend refusing an expiring bearer."""

    def __init__(self) -> None:
        self.org = self
        self.routed: asyncio.Queue[tuple[str, ...]] = asyncio.Queue()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.passes = 0
        self.drained: list[bool] = []

    async def sync_once(self) -> None:
        self.passes += 1
        self.started.set()
        await self.release.wait()

    def route(self, chat_ids: tuple[str, ...]) -> None:
        self.routed.put_nowait(chat_ids)

    def begin_drain(self, *, restart: bool) -> None:
        self.drained.append(restart)


def _control(
    service: _BusyService,
    reader: asyncio.StreamReader,
    stop: asyncio.Event,
    *,
    on_credential: Callable[[Credential], None] = lambda _frame: None,
    report: Callable[[], None] = lambda: None,
) -> asyncio.Task[None]:
    return asyncio.create_task(
        serve_control(
            cast(Any, service),
            reader,
            stop=stop,
            on_credential=on_credential,
            report=report,
        )
    )


async def test_a_credential_sent_behind_a_busy_route_pass_is_taken_and_reported() -> None:
    service = _BusyService()
    reader = asyncio.StreamReader()
    reader.feed_data(encode(Route(chat_ids=("c1",))))
    reader.feed_data(encode(Credential(credential="alkm_org.second", seq=3)))
    taken: list[Credential] = []
    reported = asyncio.Event()
    stop = asyncio.Event()
    control = _control(service, reader, stop, on_credential=taken.append, report=reported.set)
    try:
        await asyncio.wait_for(reported.wait(), timeout=5)
        assert [(f.credential, f.seq) for f in taken] == [("alkm_org.second", 3)]
        assert service.passes == 1 and not service.release.is_set()
    finally:
        service.release.set()
        reader.feed_eof()
        await asyncio.wait_for(control, timeout=5)
    assert stop.is_set() and service.drained == [True]


async def test_routes_that_land_during_a_pass_get_exactly_one_more_pass() -> None:
    service = _BusyService()
    reader = asyncio.StreamReader()
    stop = asyncio.Event()
    control = _control(service, reader, stop)
    reader.feed_data(encode(Route(chat_ids=("c1",))))
    await asyncio.wait_for(service.started.wait(), timeout=5)
    service.started.clear()
    reader.feed_data(encode(Route(chat_ids=("c1", "c2"))))
    reader.feed_data(encode(Route(chat_ids=("c1", "c2", "c3"))))
    for _ in range(3):
        await asyncio.wait_for(service.routed.get(), timeout=5)
    service.release.set()
    await asyncio.wait_for(service.started.wait(), timeout=5)
    for _ in range(5):
        await asyncio.sleep(0)
    reader.feed_eof()
    await asyncio.wait_for(control, timeout=5)
    assert service.passes == 2
