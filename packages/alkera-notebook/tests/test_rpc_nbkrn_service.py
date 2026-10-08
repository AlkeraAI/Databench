"""The RPC service and client bases over a real Unix socket: handshake,
single-use token, one connection, limits, cancellation and run scoping."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import struct
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.rpc import (
    Call,
    MethodRegistry,
    PeerClosedError,
    RpcService,
    UnixEndpoint,
    connect,
    frames,
    new_token,
)

# Protocol 1 speaks only Unix sockets (a TCP transport is the seam for Windows).
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets only")


class Harness:
    def __init__(self, service: RpcService, token: str, registry: MethodRegistry) -> None:
        self.service = service
        self.token = token
        self.registry = registry
        self.events: list[tuple[str, dict[str, Any]]] = []


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    endpoint = UnixEndpoint.create()
    registry = MethodRegistry()
    token = new_token()
    h: Harness

    async def on_event(method: str, params: dict[str, Any]) -> None:
        h.events.append((method, params))

    service = RpcService(
        endpoint,
        token=token,
        registry=registry,
        hello_result={"kernel_id": "k1"},
        on_notification=on_event,
        data_dir="/tmp/data",
    )
    h = Harness(service, token, registry)
    await service.start()
    try:
        yield h
    finally:
        await service.close()
        endpoint.remove()


async def test_rpc_hello_with_the_token_opens_a_session(harness: Harness) -> None:
    async def echo(call: Call) -> Any:
        return {"got": call.params}

    harness.registry.register("echo", echo)
    client = await connect(
        harness.service.endpoint.uri, harness.token, codecs=["json", "rows.json"]
    )
    session = await asyncio.wait_for(harness.service.accept(), 2)
    assert client.info["protocol"] == 1
    assert client.info["kernel_id"] == "k1"
    assert client.info["methods"] == ["echo"]
    assert client.info["limits"] == {
        "max_inflight": 32,
        "frame_in": frames.FRAME_LIMIT_CLIENT,
        "frame_out": frames.FRAME_LIMIT_SERVICE,
    }
    assert client.info["data_dir"] == "/tmp/data"
    assert session.codecs == {"json", "rows.json"}
    assert "token" in session.hello
    assert await client.peer.request("echo", {"x": b"\x00\x01"}) == {"got": {"x": b"\x00\x01"}}
    await client.close()


async def test_rpc_wrong_token_is_refused_and_disconnected(harness: Harness) -> None:
    with pytest.raises(frames.RpcError) as info:
        await connect(harness.service.endpoint.uri, new_token())
    assert info.value.code == frames.ErrorCode.UNAUTHORIZED
    with pytest.raises(PeerClosedError):
        await asyncio.wait_for(harness.service.accept(), 2)
    assert harness.service.refused == ["unauthorized"]


async def test_rpc_second_connection_is_refused(harness: Harness) -> None:
    client = await connect(harness.service.endpoint.uri, harness.token)
    await asyncio.wait_for(harness.service.accept(), 2)
    with pytest.raises((ConnectionRefusedError, FileNotFoundError)):
        await connect(harness.service.endpoint.uri, harness.token)
    await client.close()


async def test_rpc_token_is_single_use_on_the_same_connection(harness: Harness) -> None:
    client = await connect(harness.service.endpoint.uri, harness.token)
    with pytest.raises(frames.RpcError) as info:
        await client.peer.request("hello", {"token": harness.token})
    assert info.value.code == frames.ErrorCode.INVALID_REQUEST
    await client.close()


async def test_rpc_silent_connection_is_closed_after_the_hello_timeout(harness: Harness) -> None:
    reader, writer = await asyncio.open_unix_connection(str(harness.service.endpoint.path))
    started = time.monotonic()
    assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    elapsed = time.monotonic() - started
    assert frames.HELLO_TIMEOUT_S - 0.2 <= elapsed < frames.HELLO_TIMEOUT_S + 1.0
    assert harness.service.refused == ["hello_timeout"]
    writer.close()


async def test_rpc_request_before_hello_closes_the_connection(harness: Harness) -> None:
    async def secret(call: Call) -> Any:
        raise AssertionError("must not run before hello")

    harness.registry.register("secret", secret)
    reader, writer = await asyncio.open_unix_connection(str(harness.service.endpoint.path))
    writer.write(
        frames.encode_message(frames.request(1, "secret"), limit=frames.FRAME_LIMIT_CLIENT)
    )
    data = await asyncio.wait_for(reader.read(), timeout=5)
    (response,) = frames.FrameReader(limit=frames.FRAME_LIMIT_SERVICE).feed(data)
    assert response.header["error"]["code"] == frames.ErrorCode.UNAUTHORIZED
    writer.close()


async def test_rpc_oversized_inbound_frame_closes_the_connection(harness: Harness) -> None:
    client = await connect(harness.service.endpoint.uri, harness.token)
    session = await asyncio.wait_for(harness.service.accept(), 2)
    # A raw prefix announcing one byte more than the client-to-service limit.
    client.peer._writer.write(struct.pack(">II", frames.FRAME_LIMIT_CLIENT + 1, 2) + b"{}")
    await asyncio.wait_for(session.wait_closed(), timeout=5)
    assert session.peer.close_reason == "too_large"
    await client.close()


async def test_rpc_sender_refuses_a_frame_over_its_direction_limit(harness: Harness) -> None:
    client = await connect(harness.service.endpoint.uri, harness.token)
    with pytest.raises(frames.FrameTooLargeError):
        await client.peer.request(
            "anything", {"blob": frames.Segment(b"x" * frames.FRAME_LIMIT_CLIENT)}
        )
    await client.close()


async def test_rpc_client_queues_requests_above_max_inflight(harness: Harness) -> None:
    open_now = 0
    peak = 0
    release = asyncio.Event()

    async def slow(call: Call) -> Any:
        nonlocal open_now, peak
        open_now += 1
        peak = max(peak, open_now)
        await release.wait()
        open_now -= 1
        return call.params["i"]

    harness.registry.register("slow", slow)
    client = await connect(harness.service.endpoint.uri, harness.token)
    tasks = [asyncio.ensure_future(client.peer.request("slow", {"i": i})) for i in range(45)]
    for _ in range(50):
        await asyncio.sleep(0.01)
        if open_now == frames.MAX_INFLIGHT:
            break
    await asyncio.sleep(0.05)
    assert open_now == frames.MAX_INFLIGHT
    release.set()
    assert await asyncio.gather(*tasks) == list(range(45))
    assert peak == frames.MAX_INFLIGHT
    await client.close()


async def test_rpc_peer_exceeding_max_inflight_is_disconnected(harness: Harness) -> None:
    release = asyncio.Event()

    async def slow(call: Call) -> Any:
        await release.wait()
        return None

    harness.registry.register("slow", slow)
    client = await connect(harness.service.endpoint.uri, harness.token)
    session = await asyncio.wait_for(harness.service.accept(), 2)
    raw = b"".join(
        frames.encode_message(frames.request(100 + i, "slow"), limit=frames.FRAME_LIMIT_CLIENT)
        for i in range(frames.MAX_INFLIGHT + 1)
    )
    client.peer._writer.write(raw)
    await asyncio.wait_for(session.wait_closed(), timeout=5)
    assert session.peer.close_reason == "too_many_requests"
    release.set()
    await client.close()


async def test_rpc_cancel_request_cancels_the_handler(harness: Harness) -> None:
    started = asyncio.Event()
    outcome: list[str] = []

    async def sleepy(call: Call) -> Any:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        return "finished"

    harness.registry.register("sleepy", sleepy)
    client = await connect(harness.service.endpoint.uri, harness.token)
    task = asyncio.ensure_future(client.peer.request("sleepy"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if outcome:
            break
        await asyncio.sleep(0.01)
    assert outcome == ["cancelled"]
    await client.close()


async def test_rpc_cancelled_answer_reaches_a_raw_caller(harness: Harness) -> None:
    async def sleepy(call: Call) -> Any:
        await asyncio.sleep(30)

    harness.registry.register("sleepy", sleepy)
    client = await connect(harness.service.endpoint.uri, harness.token)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[frames.Response] = loop.create_future()
    client.peer._pending[900] = future
    await client.peer._send(frames.request(900, "sleepy"))
    await asyncio.sleep(0.05)
    await client.peer.notify(frames.CANCEL_METHOD, {"id": 900})
    response = await asyncio.wait_for(future, 5)
    assert response.error is not None and response.error.code == frames.ErrorCode.CANCELLED
    await client.close()


async def test_rpc_run_scoped_method_is_a_time_window(harness: Harness) -> None:
    seen: list[dict[str, Any]] = []

    async def sql(call: Call) -> Any:
        seen.append(call.extra)
        return {"rows": 1}

    harness.registry.register("sql.execute", sql, run_scoped=True)
    client = await connect(harness.service.endpoint.uri, harness.token)
    await asyncio.wait_for(harness.service.accept(), 2)
    ctx = {"run_id": "run-1", "cell_id": "c9"}
    with pytest.raises(frames.RpcError) as before:
        await client.peer.request("sql.execute", {}, ctx=ctx)
    assert before.value.data == {"name": "forbidden", "reason": "outside_run"}

    harness.service.scope.begin("run-1")
    assert await client.peer.request("sql.execute", {}, ctx=ctx) == {"rows": 1}
    assert seen == [{"run_id": "run-1", "cell_id": "c9"}]
    for forged in ({"run_id": "run-2"}, {}, None):
        with pytest.raises(frames.RpcError) as other:
            await client.peer.request("sql.execute", {}, ctx=forged)
        assert other.value.data["reason"] == "outside_run"

    await client.peer.notify("run.finished", {"run_id": "run-1", "status": "ok"})
    for _ in range(100):
        if not harness.service.scope.is_active("run-1"):
            break
        await asyncio.sleep(0.01)
    with pytest.raises(frames.RpcError) as after:
        await client.peer.request("sql.execute", {}, ctx=ctx)
    assert after.value.data["reason"] == "outside_run"
    assert ("run.finished", {"run_id": "run-1", "status": "ok"}) in harness.events
    with pytest.raises(ValueError, match="already ended"):
        harness.service.scope.begin("run-1")
    await client.close()


async def test_rpc_method_registered_after_connect_is_callable(harness: Harness) -> None:
    client = await connect(harness.service.endpoint.uri, harness.token)
    assert "late" not in client.info["methods"]
    with pytest.raises(frames.RpcError) as missing:
        await client.peer.request("late")
    assert missing.value.code == frames.ErrorCode.METHOD_NOT_FOUND

    async def late(call: Call) -> Any:
        return "here"

    harness.registry.register("late", late)
    assert await client.peer.request("late") == "here"
    await client.close()


async def test_rpc_handler_failure_answers_internal_error(harness: Harness) -> None:
    async def broken(call: Call) -> Any:
        raise KeyError("boom")

    harness.registry.register("broken", broken)
    client = await connect(harness.service.endpoint.uri, harness.token)
    with pytest.raises(frames.RpcError) as info:
        await client.peer.request("broken")
    assert info.value.code == frames.ErrorCode.INTERNAL_ERROR
    assert "KeyError" in info.value.message
    # The connection survives a failed handler.
    with pytest.raises(frames.RpcError):
        await client.peer.request("broken")
    await client.close()


async def test_rpc_service_side_request_reaches_client_methods(harness: Harness) -> None:
    from alkera_notebook.rpc import MethodRegistry as Registry

    client_methods = Registry()

    async def execute(call: Call) -> Any:
        return {"accepted": True, "steps": len(call.params["steps"])}

    client_methods.register("run.execute", execute)
    client = await connect(harness.service.endpoint.uri, harness.token, registry=client_methods)
    session = await asyncio.wait_for(harness.service.accept(), 2)
    result = await session.peer.request("run.execute", {"run_id": "r", "steps": [{}, {}]})
    assert result == {"accepted": True, "steps": 2}
    await client.close()


async def test_rpc_connection_drop_fails_pending_requests(harness: Harness) -> None:
    async def never(call: Call) -> Any:
        await asyncio.sleep(30)

    harness.registry.register("never", never)
    client = await connect(harness.service.endpoint.uri, harness.token)
    session = await asyncio.wait_for(harness.service.accept(), 2)
    pending = asyncio.ensure_future(client.peer.request("never"))
    await asyncio.sleep(0.05)
    await session.peer.close("service_gone")
    with pytest.raises(PeerClosedError):
        await asyncio.wait_for(pending, 5)
    await client.close()


def test_rpc_endpoint_is_private_and_short() -> None:
    endpoint = UnixEndpoint.create()
    try:
        mode = stat.S_IMODE(os.stat(endpoint.directory).st_mode)
        assert mode == 0o750
        assert len(str(endpoint.path)) < 100
        assert endpoint.uri == f"unix:{endpoint.path}"
        assert str(endpoint.directory).startswith("/tmp/alknb-")
    finally:
        endpoint.remove()


def test_rpc_endpoint_refuses_a_socket_path_too_long_for_macos(tmp_path: Path) -> None:
    deep = Path(tempfile.mkdtemp(prefix="alknb-long-", dir="/tmp"))
    try:
        base = deep / ("d" * 90)
        base.mkdir()
        with pytest.raises(ValueError, match="too long"):
            UnixEndpoint.create(base=str(base))
        assert list(base.iterdir()) == []
    finally:
        shutil.rmtree(deep)


@pytest.mark.parametrize(
    ("uri", "path"),
    [
        pytest.param("unix:/tmp/a/k.sock", "/tmp/a/k.sock", id="unix"),
        pytest.param("tcp://127.0.0.1:9", None, id="tcp-refused"),
        pytest.param("unix:", None, id="empty-path"),
        pytest.param("/tmp/a.sock", None, id="no-scheme"),
    ],
)
def test_rpc_parse_endpoint(uri: str, path: str | None) -> None:
    from alkera_notebook.rpc import parse_endpoint

    if path is None:
        with pytest.raises(ValueError):
            parse_endpoint(uri)
    else:
        assert parse_endpoint(uri) == path


def test_rpc_token_shape() -> None:
    import base64

    tokens = {new_token() for _ in range(20)}
    assert len(tokens) == 20
    for token in tokens:
        assert len(base64.urlsafe_b64decode(token + "=")) == 32
        assert "=" not in token and "+" not in token and "/" not in token


async def test_rpc_socket_is_not_world_reachable(harness: Harness) -> None:
    mode = stat.S_IMODE(os.stat(harness.service.endpoint.path).st_mode)
    assert mode & 0o007 == 0
