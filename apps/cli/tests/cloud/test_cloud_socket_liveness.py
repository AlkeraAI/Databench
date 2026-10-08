"""A box never takes an open socket on its word.

On staging a box's socket was closed and reopened during a backend deploy, and
the connection it came back on stayed open while delivering nothing. A chat
bound to the box minutes later was never picked up: nothing on that socket
said so, and nothing made the box look again until it was restarted.

So the socket asks the gateway's application loop for a ``pong`` on a cadence
and gives up on a connection that answers nothing within the window, the
service re-reads the chat list the moment the socket comes back, and the slow
poll of the list keeps running while the socket is up.

A scripted gateway stands in for the server — it welcomes, acknowledges
subscribes, and answers a ``ping`` only when told to — and the service runs
every loop ``start()`` launches, on a clock the test moves.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from _mirror_service import FakeMirror
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.cloud.transport import CLOSE_NO_PONG, CloudSocket
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
POLL_SECONDS = 15.0
#: The liveness window the socket tests run on: short, on the real clock.
PING_SECONDS = 0.05
PONG_SECONDS = 0.1
#: Long enough that no liveness close happens inside a test.
NEVER = 3600.0

_CLOSED = object()


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


class _Protocol:
    def __init__(self) -> None:
        self.close_exc: ConnectionClosed = ConnectionClosed(Close(1000, ""), None)


class Connection:
    """One socket as the gateway holds it."""

    def __init__(self, *, answers_pings: bool) -> None:
        self.answers_pings = answers_pings
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed_with: int | None = None
        self.protocol = _Protocol()
        self.push(
            {
                "t": "welcome",
                "peer_id": "p:1",
                "server_time": datetime.now(UTC).isoformat(),
                "instance": "gw-1",
            }
        )

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def recv(self) -> Any:
        item = await self.inbound.get()
        if item is _CLOSED:
            raise self.protocol.close_exc
        return item

    def __aiter__(self) -> Connection:
        return self

    async def __anext__(self) -> Any:
        item = await self.inbound.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    async def send(self, text: str) -> None:
        frame = json.loads(text)
        self.sent.append(frame)
        if frame.get("t") == "subscribe":
            self.push({"t": "subscribed", "channel": frame["channel"], "can_write": False})
        elif frame.get("t") == "ping" and self.answers_pings:
            self.push({"t": "pong"})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = code
        self.protocol.close_exc = ConnectionClosed(Close(code, reason), None)
        self.inbound.put_nowait(_CLOSED)

    def push(self, frame: dict[str, Any]) -> None:
        self.inbound.put_nowait(json.dumps({"schema_version": "1.0.0", **frame}))

    def pings(self) -> int:
        return sum(1 for frame in self.sent if frame.get("t") == "ping")


class Gateway:
    """Every connection the box opened, in order."""

    def __init__(self, *, answers_pings: bool) -> None:
        self.answers_pings = answers_pings
        self.connections: list[Connection] = []

    def connect(self, _url: str, **_kwargs: Any) -> Connection:
        connection = Connection(answers_pings=self.answers_pings)
        self.connections.append(connection)
        return connection


class Api(CloudRestClient):
    """The box's routes: a chat list the test edits, everything else quiet."""

    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: Counter[str] = Counter()

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def mint_ticket(self) -> str:
        return "t-1"

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
        # The event stream says nothing either: the incident's box heard of the
        # new chat from neither surface.
        await asyncio.Event().wait()
        yield {}

    def bind(self, chat_id: str) -> None:
        self.rows[chat_id] = {"id": chat_id, "last_seq": 0, "machine_id": MACHINE}


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


class QuietSchema:
    pending: tuple[str, ...] = ()

    async def sync_once(self) -> bool:
        return False

    async def stop(self) -> None:
        return None


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _until(predicate: Callable[[], bool], what: str, *, seconds: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def _socket(api: Api, gateway: Gateway, *, ping: float, pong: float) -> CloudSocket:
    return CloudSocket(
        api, connect=gateway.connect, sleep=_no_sleep, ping_interval=ping, pong_timeout=pong
    )


def _service(
    tmp_path: Path, api: Api, socket: CloudSocket, ticker: Ticker
) -> tuple[CloudMirrorService, dict[str, FakeMirror]]:
    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="liveness box",
        provider_pod_id="pod-liveness",
        machine_type_code="cpu3c",
        poll_interval=POLL_SECONDS,
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    built: dict[str, FakeMirror] = {}

    def factory(chat_id: str, _chat: dict[str, Any]) -> Any:
        built[chat_id] = FakeMirror(chat_id, tmp_path / ".alkera" / "chats" / chat_id / "scratch")
        return built[chat_id]

    service = CloudMirrorService(
        settings,
        runtime,
        rest=api,
        socket=socket,
        mirror_factory=factory,
        schema_loader=QuietSchema(),  # type: ignore[arg-type]
        clock=ticker,
        sleep=ticker.sleep,
    )
    return service, built


# ---------------------------------------------------------------------------
# The socket
# ---------------------------------------------------------------------------


async def test_a_connection_that_stays_open_and_answers_nothing_is_closed_and_replaced() -> None:
    gateway = Gateway(answers_pings=False)
    socket = _socket(Api(), gateway, ping=PING_SECONDS, pong=PONG_SECONDS)
    await socket.start()
    try:
        await _until(lambda: len(gateway.connections) >= 2, "a reconnect after the silence")
        first = gateway.connections[0]
        assert first.pings() >= 1, "the box asked before it gave up"
        assert first.closed_with == CLOSE_NO_PONG
        assert socket.silent_closes >= 1
        await _until(lambda: socket.state == "connected", "the replacement connection")
        assert socket.state != "fatal"
    finally:
        await socket.stop()


async def test_a_connection_that_answers_its_pings_is_kept() -> None:
    gateway = Gateway(answers_pings=True)
    socket = _socket(Api(), gateway, ping=PING_SECONDS, pong=PONG_SECONDS)
    await socket.start()
    try:
        await _until(lambda: bool(gateway.connections), "the first connection")
        only = gateway.connections[0]
        # Many liveness windows, every one of them answered.
        await _until(lambda: only.pings() >= 5, "five answered pings")
        assert len(gateway.connections) == 1
        assert only.closed_with is None
        assert socket.silent_closes == 0
        assert socket.state == "connected"
    finally:
        await socket.stop()


async def test_any_frame_inside_the_window_counts_as_the_gateway_being_there() -> None:
    """A gateway busy delivering is not a silent one: traffic that lands
    after a ping answers it as well as a pong would."""
    gateway = Gateway(answers_pings=False)
    socket = _socket(Api(), gateway, ping=PING_SECONDS, pong=PONG_SECONDS * 4)
    await socket.start()
    try:
        await _until(lambda: bool(gateway.connections), "the first connection")
        only = gateway.connections[0]
        for _ in range(6):
            seen = only.pings()
            await _until(lambda seen=seen: only.pings() > seen, "the next ping")
            only.push({"t": "presence", "channel": "doc:chat:x", "event": "heartbeat"})
        assert len(gateway.connections) == 1
        assert socket.silent_closes == 0
    finally:
        await socket.stop()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


async def test_a_chat_bound_while_the_socket_was_silent_is_taken_on_the_reconnect(
    tmp_path: Path,
) -> None:
    """The incident: the socket sits open delivering nothing, a chat is bound
    to the box, and the poll's next tick is still far off. The silence is
    noticed, the socket reconnects, and the reconnect reads the list."""
    api = Api()
    gateway = Gateway(answers_pings=True)
    socket = _socket(api, gateway, ping=PING_SECONDS, pong=PONG_SECONDS)
    ticker = Ticker()
    service, built = _service(tmp_path, api, socket, ticker)
    await service.start()
    try:
        # Registered before the socket opened: its first connection is the
        # machine's, and the poll's first read has happened.
        await _until(lambda: len(gateway.connections) == 1, "the machine's connection")
        await _until(lambda: socket.state == "connected", "the machine's connection up")
        await _until(lambda: api.calls["GET /chats"] >= 1, "the first read")
        await service.settle_background()
        reads = api.calls["GET /chats"]
        # From now on the gateway answers nothing, and the poll never ticks.
        gateway.answers_pings = False
        gateway.connections[-1].answers_pings = False
        api.bind("chat-slack")
        await _until(lambda: "chat-slack" in built, "the new chat to be taken")
        assert len(gateway.connections) >= 2
        assert socket.silent_closes >= 1
        assert api.calls["GET /chats"] > reads
        assert service.mirrors["chat-slack"].state == "running"
    finally:
        await service.stop()


async def test_a_reconnect_reads_the_list_even_with_the_poll_far_off(tmp_path: Path) -> None:
    """Whatever drops the socket — a deploy's close, a rebind — the list is read
    the moment it is back, not up to a whole poll interval later."""
    api = Api()
    gateway = Gateway(answers_pings=True)
    socket = _socket(api, gateway, ping=NEVER, pong=NEVER)
    ticker = Ticker()
    service, built = _service(tmp_path, api, socket, ticker)
    await service.start()
    try:
        await _until(lambda: len(gateway.connections) == 1, "the machine's connection")
        await _until(lambda: socket.state == "connected", "the machine's connection up")
        await _until(lambda: api.calls["GET /chats"] >= 1, "the first read")
        await service.settle_background()
        api.bind("chat-after-deploy")
        # The server restarts: it closes the socket with 1012.
        await gateway.connections[-1].close(1012, "service restart")
        await _until(lambda: "chat-after-deploy" in built, "the chat bound across the restart")
        assert len(gateway.connections) == 2
    finally:
        await service.stop()


async def test_the_poll_keeps_reading_the_list_while_the_socket_is_up(tmp_path: Path) -> None:
    """The socket being up is not a reason to stop looking: a chat the socket
    never mentions is taken within one poll interval."""
    api = Api()
    gateway = Gateway(answers_pings=True)
    socket = _socket(api, gateway, ping=NEVER, pong=NEVER)
    ticker = Ticker()
    service, built = _service(tmp_path, api, socket, ticker)
    await service.start()
    try:
        await _until(lambda: len(gateway.connections) == 1, "the machine's connection")
        await _until(lambda: socket.state == "connected", "the machine's connection up")
        await _until(lambda: api.calls["GET /chats"] >= 1, "the first read")
        await service.settle_background()
        api.bind("chat-quiet")
        await asyncio.sleep(0.05)
        assert "chat-quiet" not in built, "nothing but the poll can find it"
        ticker.advance(POLL_SECONDS)
        await _until(lambda: "chat-quiet" in built, "the poll to take the chat")
        assert socket.state == "connected" and len(gateway.connections) == 1
    finally:
        await service.stop()
