"""The machine channel on the box's socket.

A scripted gateway stands in for the server: it answers the handshake, says
``subscribed`` to whatever it is asked for, lets a test push frames down the
connection and drop it, and remembers every frame each connection sent. The
ticket mint is a real :class:`CloudRestClient` over a mock transport, so which
agent a connection speaks as is the agent the ticket was minted for — the one
fact the gateway admits the machine channel on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.folder import HeldFolder
from alkera_cli.cloud.machine_requests import MachineRequests
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

PLACEHOLDER = "box-demo"
MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
CHANNEL = f"machine:{MACHINE}"

_CLOSED = object()


class _Protocol:
    def __init__(self) -> None:
        self.close_exc: ConnectionClosed = ConnectionClosed(Close(1000, ""), None)


class ScriptedConnection:
    """One socket as the gateway holds it."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.protocol = _Protocol()
        self.inbound.put_nowait(
            json.dumps(
                {
                    "t": "welcome",
                    "peer_id": "p:1",
                    "server_time": datetime.now(UTC).isoformat(),
                    "instance": "gw-1",
                    "schema_version": "1.0.0",
                }
            )
        )

    async def __aenter__(self) -> ScriptedConnection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def recv(self) -> Any:
        item = await self.inbound.get()
        if item is _CLOSED:
            raise self.protocol.close_exc
        return item

    def __aiter__(self) -> ScriptedConnection:
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

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.drop(code, reason)

    def push(self, frame: dict[str, Any]) -> None:
        self.inbound.put_nowait(json.dumps({"schema_version": "1.0.0", **frame}))

    def drop(self, code: int = 1012, reason: str = "") -> None:
        self.protocol.close_exc = ConnectionClosed(Close(code, reason), None)
        self.inbound.put_nowait(_CLOSED)

    def subscribed(self) -> list[str]:
        return [frame["channel"] for frame in self.sent if frame.get("t") == "subscribe"]

    def unsubscribed(self) -> list[str]:
        return [frame["channel"] for frame in self.sent if frame.get("t") == "unsubscribe"]

    def acks(self) -> list[dict[str, Any]]:
        return [frame for frame in self.sent if frame.get("t") == "machine.ack"]


class ScriptedGateway:
    """Every connection the box opened, in order."""

    def __init__(self) -> None:
        self.connections: list[ScriptedConnection] = []

    def connect(self, _url: str, **_kwargs: Any) -> ScriptedConnection:
        connection = ScriptedConnection()
        self.connections.append(connection)
        return connection

    @property
    def latest(self) -> ScriptedConnection:
        return self.connections[-1]


def _rest(agent: str) -> CloudRestClient:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/ws/tickets"
        return httpx.Response(200, json={"ticket": "t-1", "expires_in": 30})

    return CloudRestClient(
        api_url="http://api.test",
        token="device-jwt",
        agent_id=agent,
        transport=httpx.MockTransport(answer),
    )


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _until(predicate: Callable[[], bool], what: str) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


class NoFolders:
    def held_by_lease_node(self, _lease_node_id: str) -> HeldFolder | None:
        return None


def _request(request_id: str = "req-1", **extra: Any) -> dict[str, Any]:
    return {
        "t": "machine.request",
        "request_id": request_id,
        "kind": "promote",
        "lease_node_id": "lease-node",
        "node_id": "file-node",
        "path": "scratch/report.md",
        "expected": {"size": 2, "mtime_ns": 1},
        "deadline_ms": 8000,
        **extra,
    }


@pytest.fixture
def gateway() -> ScriptedGateway:
    return ScriptedGateway()


async def _registered(gateway: ScriptedGateway) -> CloudSocket:
    """A socket that came up as the placeholder and then registered, the way
    the service does it: bind first, then rebind."""
    socket = CloudSocket(_rest(PLACEHOLDER), connect=gateway.connect, sleep=_no_sleep)
    await socket.start()
    await _until(lambda: socket.state == "connected", "the first connection")
    socket.bind_machine(MACHINE, MachineRequests(NoFolders()).handle)
    await socket.rebind(socket.rest.for_agent(MACHINE))
    await _until(
        lambda: len(gateway.connections) == 2 and CHANNEL in gateway.latest.subscribed(),
        "the machine channel on the machine's own connection",
    )
    return socket


async def test_the_machine_channel_waits_for_a_connection_that_speaks_as_the_machine(
    gateway: ScriptedGateway,
) -> None:
    """Registration names the machine while the socket is up as a placeholder.
    The gateway would refuse the channel on that connection, so it is asked
    for only on the one the rebind opens."""
    socket = await _registered(gateway)
    try:
        first, second = gateway.connections
        assert CHANNEL not in first.subscribed()
        assert second.subscribed().count(CHANNEL) == 1
        assert socket.machine_channel == CHANNEL
    finally:
        await socket.stop()


async def test_a_socket_that_has_not_registered_holds_no_machine_channel(
    gateway: ScriptedGateway,
) -> None:
    socket = CloudSocket(_rest(MACHINE), connect=gateway.connect, sleep=_no_sleep)
    await socket.start()
    try:
        await _until(lambda: socket.state == "connected", "the connection")
        assert not any(channel.startswith("machine:") for channel in gateway.latest.subscribed())
        assert socket.machine_channel is None
    finally:
        await socket.stop()


async def test_binding_on_a_connection_that_already_speaks_as_the_machine_subscribes_at_once(
    gateway: ScriptedGateway,
) -> None:
    """A box whose ticket already asserted the machine (a restart that kept its
    registration) needs no reconnect: the channel is asked for on the spot."""
    socket = CloudSocket(_rest(MACHINE), connect=gateway.connect, sleep=_no_sleep)
    await socket.start()
    try:
        await _until(lambda: socket.state == "connected", "the connection")
        socket.bind_machine(MACHINE, MachineRequests(NoFolders()).handle)
        await _until(lambda: CHANNEL in gateway.latest.subscribed(), "the machine channel")
        assert len(gateway.connections) == 1
    finally:
        await socket.stop()


async def test_the_machine_channel_is_subscribed_again_after_a_reconnect(
    gateway: ScriptedGateway,
) -> None:
    socket = await _registered(gateway)
    try:
        gateway.latest.drop(1012, "restart")
        await _until(
            lambda: len(gateway.connections) == 3 and CHANNEL in gateway.latest.subscribed(),
            "the machine channel on the reconnect",
        )
    finally:
        await socket.stop()


async def test_a_request_on_the_channel_is_answered_on_the_same_socket(
    gateway: ScriptedGateway,
) -> None:
    """The handler decides — here, that this box holds no folder under the
    lease — and its answer goes back naming the request."""
    socket = await _registered(gateway)
    try:
        gateway.latest.push(_request("req-7"))
        await _until(lambda: bool(gateway.latest.acks()), "the ack")
        assert gateway.latest.acks() == [
            {"t": "machine.ack", "request_id": "req-7", "outcome": "not_holder"}
        ]
    finally:
        await socket.stop()


async def test_a_request_of_a_kind_the_box_does_not_serve_is_left_unanswered(
    gateway: ScriptedGateway,
) -> None:
    """The unanswerable request goes first; the answerable one behind it
    proves the socket read past it and is still serving."""
    socket = await _registered(gateway)
    try:
        gateway.latest.push(_request("req-odd", kind="evict"))
        gateway.latest.push(_request("req-next"))
        await _until(lambda: bool(gateway.latest.acks()), "the second ack")
        assert [ack["request_id"] for ack in gateway.latest.acks()] == ["req-next"]
    finally:
        await socket.stop()


async def test_a_handler_that_fails_does_not_take_the_socket_down(
    gateway: ScriptedGateway,
) -> None:
    calls: list[str] = []

    def flaky(frame: Any) -> dict[str, Any] | None:
        calls.append(str(frame["request_id"]))
        if len(calls) == 1:
            raise RuntimeError("the disk went away")
        return MachineRequests(NoFolders()).handle(frame)

    socket = await _registered(gateway)
    try:
        socket.bind_machine(MACHINE, flaky)
        gateway.latest.push(_request("req-1"))
        gateway.latest.push(_request("req-2"))
        await _until(lambda: bool(gateway.latest.acks()), "the ack after the failure")
        assert [ack["request_id"] for ack in gateway.latest.acks()] == ["req-2"]
        assert len(gateway.connections) == 2
    finally:
        await socket.stop()


async def test_a_request_with_no_machine_bound_is_not_answered(
    gateway: ScriptedGateway,
) -> None:
    socket = CloudSocket(_rest(MACHINE), connect=gateway.connect, sleep=_no_sleep)
    await socket.start()
    try:
        await _until(lambda: socket.state == "connected", "the connection")
        gateway.latest.push(_request("req-1"))
        # A frame the socket must read and do nothing with; the one behind it
        # is how the test knows the first was read.
        gateway.latest.push({"t": "pong"})
        await asyncio.sleep(0.05)
        assert gateway.latest.acks() == []
    finally:
        await socket.stop()


async def test_letting_the_machine_go_leaves_its_channel(gateway: ScriptedGateway) -> None:
    socket = await _registered(gateway)
    try:
        socket.bind_machine(None)
        await _until(lambda: CHANNEL in gateway.latest.unsubscribed(), "the unsubscribe")
        assert socket.machine_channel is None
        gateway.latest.push(_request("req-late"))
        await asyncio.sleep(0.05)
        assert gateway.latest.acks() == []
    finally:
        await socket.stop()


async def test_a_refused_machine_channel_is_not_counted_as_held(
    gateway: ScriptedGateway,
) -> None:
    socket = await _registered(gateway)
    try:
        gateway.latest.push(
            {"t": "error", "code": "forbidden", "message": "not this machine", "channel": CHANNEL}
        )
        await _until(lambda: socket.machine_channel is None, "the refusal")
    finally:
        await socket.stop()


async def test_a_request_on_a_connection_that_holds_no_machine_channel_is_not_answered(
    gateway: ScriptedGateway,
) -> None:
    """Bound, but still on the placeholder's connection: whatever arrives is
    not for a channel this connection holds, and is not answered as if it
    were."""
    socket = CloudSocket(_rest(PLACEHOLDER), connect=gateway.connect, sleep=_no_sleep)
    await socket.start()
    try:
        await _until(lambda: socket.state == "connected", "the connection")
        socket.bind_machine(MACHINE, MachineRequests(NoFolders()).handle)
        gateway.latest.push(_request("req-early"))
        gateway.latest.push({"t": "pong"})
        await asyncio.sleep(0.05)
        assert socket.machine_channel is None
        assert gateway.latest.acks() == []
    finally:
        await socket.stop()


async def test_registration_binds_the_machine_the_box_now_publishes_as(tmp_path: Path) -> None:
    """The service binds the socket to the registered machine, so the
    reconnect registration causes is one that holds the machine channel."""
    service, _built = build_service(tmp_path, clock=Clock())
    socket = service._socket
    assert socket.bound_machine is None

    await service._adopt_machine(MACHINE)

    assert socket.bound_machine == MACHINE


async def test_the_gateways_admission_of_the_machine_channel_is_reported(
    gateway: ScriptedGateway, caplog: pytest.LogCaptureFixture
) -> None:
    """The gateway's ``subscribed`` for the machine channel is the one signal
    that a box is reachable by the drive: an operator reading the box's log,
    and a test driving a real gateway, both wait on it. It must not be taken
    for a document's answer and dropped because no document has that name."""
    caplog.set_level(logging.INFO, logger="alkera_cli.cloud.transport")
    socket = await _registered(gateway)
    try:
        await _until(
            lambda: any(
                "holding the machine channel" in record.getMessage()
                and CHANNEL in record.getMessage()
                for record in caplog.records
            ),
            "the admission reported",
        )
    finally:
        await socket.stop()


async def test_a_later_answer_is_sent_when_its_work_ends_and_the_socket_serves_meanwhile(
    gateway: ScriptedGateway,
) -> None:
    """A flush acknowledges at once and settles when its push ends; the push
    runs off the receive loop, so a request behind it is answered first."""
    from alkera_cli.cloud.transport import LaterAnswer

    pushed = asyncio.Event()

    async def push_then_settle(request_id: str) -> dict[str, Any]:
        await pushed.wait()
        return {"t": "machine.ack", "request_id": request_id, "outcome": "flushed"}

    def handler(frame: Any) -> Any:
        request_id = str(frame["request_id"])
        if frame["kind"] == "flush":
            return LaterAnswer(
                now={"t": "machine.ack", "request_id": request_id, "outcome": "accepted"},
                then=push_then_settle(request_id),
            )
        return MachineRequests(NoFolders()).handle(frame)

    socket = await _registered(gateway)
    try:
        socket.bind_machine(MACHINE, handler)
        gateway.latest.push(_request("flush-1", kind="flush"))
        gateway.latest.push(_request("req-2"))
        await _until(lambda: len(gateway.latest.acks()) == 2, "the first two acks")
        assert [(ack["request_id"], ack["outcome"]) for ack in gateway.latest.acks()] == [
            ("flush-1", "accepted"),
            ("req-2", "not_holder"),
        ]
        pushed.set()
        await _until(lambda: len(gateway.latest.acks()) == 3, "the flush settling")
        assert gateway.latest.acks()[-1] == {
            "t": "machine.ack",
            "request_id": "flush-1",
            "outcome": "flushed",
        }
    finally:
        await socket.stop()
