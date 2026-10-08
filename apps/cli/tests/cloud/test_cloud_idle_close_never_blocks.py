"""Putting one chat to sleep never stops the box from finding the next.

On staging a box closed an idle chat at 22:02. Closing it pushes the chat's
folder, the push waited on a drive that was slow to commit, and the poll of
the chat list was awaiting that close: the box read its chat list once more in
the next thirty-seven minutes, and a chat bound to it at 22:34 was never
taken. Its event stream, which might have named the chat, had no read bound
either, so a stream nothing fed was listened to for ever.

The idle sweep now runs beside the poll, and the stream gives up on a
connection that says nothing for longer than four of the server's keepalives.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from _mirror_service import FakeMirror, NoSocket
from alkera_cli.cloud.rest import STREAM_OPENED, CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
POLL_SECONDS = 15.0
IDLE_MINUTES = 1.0


@pytest.fixture(autouse=True)
def _box_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    from alkera_cli.harness import prewarm
    from alkera_cli.host import paths

    home = tmp_path_factory.mktemp("alkera-home")
    monkeypatch.setenv("ALKERA_HOME", str(home))
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "0")
    prewarm._reset_for_tests()
    yield
    prewarm._reset_for_tests()


class Ticker:
    """The service's clock and sleep, moved only by the test."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self._waiters: list[asyncio.Event] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        deadline = self.now + max(0.0, seconds)
        while self.now < deadline:
            wake = asyncio.Event()
            self._waiters.append(wake)
            await wake.wait()

    def advance(self, seconds: float) -> None:
        self.now += seconds
        waiters, self._waiters = self._waiters, []
        for wake in waiters:
            wake.set()


class Api(CloudRestClient):
    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: Counter[str] = Counter()

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        self.calls["GET /chats"] += 1
        return {"items": [dict(row) for row in self.rows.values()]}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        return dict(self.rows[chat_id])

    async def register_machine(self, **kwargs: Any) -> dict[str, Any]:
        return {"id": MACHINE}

    async def heartbeat_machine(self, machine_id: str, **kwargs: Any) -> dict[str, Any]:
        return {}

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def events(self, *, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        await asyncio.Event().wait()
        yield {}

    def bind(self, chat_id: str) -> None:
        self.rows[chat_id] = {"id": chat_id, "last_seq": 0, "machine_id": MACHINE}


class StuckHandBack:
    """Chat folders whose hand-back never finishes: the push under it waits on
    a drive that does not commit. Released only by the test's teardown."""

    def fenced(self, key: str) -> bool:
        """No folder here is ever in doubt: the kernel fence has nothing to stop."""
        return False

    def __init__(self, *, take_takes: float = 0.0) -> None:
        self.enabled = True
        self.gate = threading.Event()
        self.handing_back: list[str] = []
        self._held: dict[str, Any] = {}
        #: How long a take runs on after the folder is already held: the gap in
        #: which the test can see the folder held while the pass that took it
        #: has not yet finished, let alone gone back to sleep.
        self.take_takes = take_takes

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        held = SimpleNamespace(
            record=SimpleNamespace(node_id=f"node-{chat_id}", heartbeat_every=15.0),
            live=SimpleNamespace(pull_inbound=lambda: []),
        )
        self._held[chat_id] = held
        time.sleep(self.take_takes)
        return held

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def held_by_lease_node(self, _lease_node_id: str) -> Any:
        return None

    def live(self, chat_id: str, working_dir: Path) -> Any:
        return None

    def stop_live(self, chat_id: str, deadline: float = 5.0, **_kwargs: Any) -> None:
        return None

    def owed(self) -> list[str]:
        return []

    def push(self, chat_id: str) -> Any:
        return None

    def beat(self, chat_id: str) -> bool:
        return chat_id in self._held

    def beat_all(self, chat_ids: list[str]) -> dict[str, bool] | None:
        return None

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self.handing_back.append(chat_id)
        self.gate.wait(30.0)
        self._held.pop(chat_id, None)
        return None


class QuietSchema:
    pending: tuple[str, ...] = ()

    async def sync_once(self) -> bool:
        return False

    async def stop(self) -> None:
        return None


async def _until(predicate: Callable[[], bool], what: str, *, seconds: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def _tick_until(
    ticker: Ticker, predicate: Callable[[], bool], what: str, *, seconds: float = 10.0
) -> None:
    """Move the service's clock one poll interval at a time until ``predicate``.

    A tick only wakes a loop already parked in its sleep; one still finishing
    its pass when the clock moves parks afterwards with its deadline measured
    from the moved clock, and would never wake if the clock moved only once.
    So the clock keeps moving until what the test is waiting for is SEEN —
    never on the assumption that the loop was asleep when it moved."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        ticker.advance(POLL_SECONDS)
        await _settle_briefly(predicate)


async def _settle_briefly(predicate: Callable[[], bool], *, seconds: float = 0.2) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() >= deadline:
            return
        await asyncio.sleep(0.005)


def _service(
    tmp_path: Path,
    api: CloudRestClient,
    folders: Any,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], Any],
) -> tuple[CloudMirrorService, dict[str, FakeMirror]]:
    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="idle-close box",
        provider_pod_id="pod-idle-close",
        machine_type_code="cpu3c",
        poll_interval=POLL_SECONDS,
        mirror_idle_minutes=IDLE_MINUTES,
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    built: dict[str, FakeMirror] = {}

    def factory(chat_id: str, _chat: dict[str, Any]) -> Any:
        built[chat_id] = FakeMirror(
            chat_id, tmp_path / ".alkera" / "chats" / chat_id / "scratch", clock=clock
        )
        return built[chat_id]

    service = CloudMirrorService(
        settings,
        runtime,
        rest=api,
        socket=NoSocket(api),
        mirror_factory=factory,
        schema_loader=QuietSchema(),  # type: ignore[arg-type]
        folders=folders,  # type: ignore[arg-type]
        clock=clock,
        sleep=sleep,
    )
    return service, built


#: A take that returns at once, and one that runs on past the moment its
#: folder is held — the shape of a slow runner, where the test sees the folder
#: held while the pass that took it is still going.
TAKES = [
    pytest.param(0.0, id="a-quick-take"),
    pytest.param(0.3, id="a-slow-take"),
]


@pytest.mark.parametrize("take_takes", TAKES)
async def test_a_chat_bound_while_another_is_being_put_to_sleep_is_still_taken(
    tmp_path: Path, take_takes: float
) -> None:
    api = Api()
    folders = StuckHandBack(take_takes=take_takes)
    ticker = Ticker()
    service, built = _service(tmp_path, api, folders, clock=ticker, sleep=ticker.sleep)
    api.bind("chat-idle")
    await service.start()
    try:
        await _until(lambda: "chat-idle" in built, "the first chat to be served")
        await _until(lambda: folders.held("chat-idle") is not None, "its folder to be held")
        # An hour of nothing puts the chat to sleep, and its hand-back never
        # comes back.
        await _tick_until(
            ticker, lambda: folders.handing_back == ["chat-idle"], "the sleep to start"
        )
        reads = api.calls["GET /chats"]
        # A chat is bound to the box while that sleep is stuck.
        api.bind("chat-slack")
        await _tick_until(ticker, lambda: "chat-slack" in built, "the new chat to be taken")
        assert api.calls["GET /chats"] > reads, "the poll kept reading the list"
        assert service.mirrors["chat-slack"].state == "running"
        assert "chat-idle" not in service.mirrors
        assert folders.handing_back == ["chat-idle"], "the sleep was still stuck throughout"
    finally:
        folders.gate.set()
        await service.stop()


@pytest.mark.parametrize("take_takes", TAKES)
async def test_a_chat_being_put_to_sleep_is_not_taken_again_underneath_its_sleep(
    tmp_path: Path, take_takes: float
) -> None:
    """The sleep runs beside the poll now, so the poll could find the same chat
    in the list while its folder is still being handed back. The take waits
    for the sleep: a chat is never served here while its folder is leaving."""
    api = Api()
    folders = StuckHandBack(take_takes=take_takes)
    ticker = Ticker()
    service, built = _service(tmp_path, api, folders, clock=ticker, sleep=ticker.sleep)
    api.bind("chat-idle")
    await service.start()
    try:
        await _until(lambda: "chat-idle" in built, "the first chat to be served")
        await _until(lambda: folders.held("chat-idle") is not None, "its folder to be held")
        first = built["chat-idle"]
        await _tick_until(
            ticker, lambda: folders.handing_back == ["chat-idle"], "the sleep to start"
        )
        # Somebody writes to it while it is being put to sleep: it owes a turn.
        api.rows["chat-idle"].update(pending_turn=True, last_seq=1)
        reads = api.calls["GET /chats"]
        # Several passes read the list — and see the chat owing a turn — while
        # the sleep is still stuck.
        await _tick_until(
            ticker, lambda: api.calls["GET /chats"] >= reads + 3, "three more reads of the list"
        )
        assert built["chat-idle"] is first, "a second mirror started under the sleep"
        assert "chat-idle" not in service.mirrors
        folders.gate.set()
        await _until(lambda: folders.held("chat-idle") is None, "the sleep to finish")
        await _until(lambda: not service._sleeping, "the sleep to be over")
        await _tick_until(
            ticker, lambda: built["chat-idle"] is not first, "the chat served again after"
        )
    finally:
        folders.gate.set()
        await service.stop()


# ---------------------------------------------------------------------------
# The event stream
# ---------------------------------------------------------------------------


class StreamServer:
    """An event stream that opens and then says only what it is told to."""

    def __init__(self, *, keepalive_every: float | None, lasts: float | None = None) -> None:
        self.keepalive_every = keepalive_every
        self.lasts = lasts
        self.connections = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                b"transfer-encoding: chunked\r\n\r\n"
            )
            self._chunk(writer, b": connected\n\n")
            await writer.drain()
            loop = asyncio.get_running_loop()
            ends = None if self.lasts is None else loop.time() + self.lasts
            while ends is None or loop.time() < ends:
                if self.keepalive_every is None:
                    await asyncio.sleep(3600)
                    continue
                await asyncio.sleep(self.keepalive_every)
                self._chunk(writer, b": keepalive\n\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _chunk(writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")


def _stream_client(url: str, *, read_timeout: float) -> CloudRestClient:
    return CloudRestClient(api_url=url, token="t", agent_id=None, sse_read_timeout=read_timeout)


async def test_an_event_stream_that_goes_silent_is_given_up_on() -> None:
    async with StreamServer(keepalive_every=None) as url:
        rest = _stream_client(url, read_timeout=0.3)
        frames: list[dict[str, Any]] = []
        with pytest.raises(httpx.ReadTimeout):
            async with asyncio.timeout(10):
                async for frame in rest.events():
                    frames.append(frame)
        assert [frame["type"] for frame in frames] == [STREAM_OPENED]


async def test_an_event_stream_fed_its_keepalives_is_kept() -> None:
    async with StreamServer(keepalive_every=0.05, lasts=1.0) as url:
        rest = _stream_client(url, read_timeout=0.3)
        frames = [frame async for frame in rest.events()]
        # The server ended it, after a second of keepalives every one of which
        # came inside the read bound: no timeout, no frame but the opening.
        assert [frame["type"] for frame in frames] == [STREAM_OPENED]


async def test_a_stop_that_meets_a_read_timeout_still_stops_the_event_stream(
    tmp_path: Path,
) -> None:
    """Seen as a 90-second hang in CI, and 3 times in 400 in a probe: a cancel
    that lands while the stream's read is timing out is turned by httpx into
    the ``ReadTimeout``. The loop took it for a dropped stream and reconnected
    for good, so a box's stop that cancels it never finished. The cancel is
    still recorded on the task, and the loop honours it."""
    opened = 0

    class SwallowsTheCancel(CloudRestClient):
        async def events(self, *, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
            nonlocal opened
            opened += 1
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise httpx.ReadTimeout("the read timed out as it was cancelled") from None
            yield {}

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    rest = SwallowsTheCancel(api_url="http://127.0.0.1:1", token="t", agent_id=None)
    service, _built = _service(tmp_path, rest, StuckHandBack(), clock=lambda: 0.0, sleep=no_wait)
    task = asyncio.create_task(service._stream_loop())
    await _until(lambda: opened == 1, "the stream to open")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5.0)
    assert opened == 1, "the stop reconnected the stream instead of stopping"


async def test_the_box_reconnects_an_event_stream_that_went_silent(tmp_path: Path) -> None:
    server = StreamServer(keepalive_every=None)

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    async with server as url:
        rest = _stream_client(url, read_timeout=0.2)
        service, _built = _service(
            tmp_path, rest, StuckHandBack(), clock=lambda: 0.0, sleep=no_wait
        )
        task = asyncio.create_task(service._stream_loop())
        try:
            await _until(lambda: server.connections >= 3, "the stream to be reopened")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
