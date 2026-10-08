"""End-to-end tests for the daemon's JSON-RPC server.

We drive the server in-process via an `asyncio` pipe pair, send framed
JSON-RPC envelopes, and assert the framed responses. No subprocesses,
no real stdio — fast and deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.daemon import (
    JsonRpcServer,
    PipeWriter,
    connect_pipe_reader,
    connect_pipe_writer,
    read_frame_async,
    write_frame_async,
)
from alkera_cli.daemon import methods as _register_methods  # noqa: F401 — populates METHODS

# A ceiling on how long a reply may take, not how long one does: a daemon on an
# idle box answers in milliseconds, and a loaded Windows runner has taken most of
# a minute to schedule one worker's process.
REPLY_SECONDS = 30.0


async def _connected_streams() -> tuple[
    asyncio.StreamReader,
    PipeWriter,
    asyncio.StreamReader,
    PipeWriter,
]:
    """Build two pipes wired so:
    - the server reads from `server_reader`, writes to `server_writer`
    - the test sends to `client_writer`, reads from `client_reader`

    Wired through ``daemon.pipes`` (NOT raw ``connect_read_pipe``) so each
    platform's CI drives the exact transport production uses there — the
    Windows ProactorEventLoop has no transport for ``os.pipe()`` handles.
    """
    # client → server
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))

    # server → client
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))

    return server_reader, server_writer, client_reader, client_writer


async def _send(writer: PipeWriter, envelope: dict[str, Any]) -> None:
    await write_frame_async(writer, json.dumps(envelope).encode("utf-8"))


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    return json.loads((await read_frame_async(reader)).decode("utf-8"))


@pytest.mark.asyncio
async def test_write_wraps_non_finite_floats_strict_json() -> None:
    """The daemon's single wire egress must never emit the invalid JSON tokens
    NaN/Infinity (the vscode-jsonrpc TS client's JSON.parse rejects them); a
    non-finite float anywhere in a response is wrapped into $nonfinite."""
    sr, sw, cr, _cw = await _connected_streams()
    server = JsonRpcServer(reader=sr, writer=sw)

    await server._write(
        {"jsonrpc": "2.0", "id": 1, "result": {"v": float("inf"), "n": float("nan")}}
    )
    body = (await read_frame_async(cr)).decode("utf-8")

    parsed = json.loads(body)  # strict-parseable
    assert "Infinity" not in body and "NaN" not in body  # no invalid tokens on the wire
    assert parsed["result"] == {"v": {"$nonfinite": "inf"}, "n": {"$nonfinite": "nan"}}


@pytest.fixture
async def daemon():
    """Spin up a JsonRpcServer wired to two pipes; yield ``(server, send, recv, stop)``."""
    sr, sw, cr, cw = await _connected_streams()
    server = JsonRpcServer(reader=sr, writer=sw)
    task = asyncio.create_task(server.serve())

    async def stop() -> None:
        server.request_shutdown()
        cw.close()
        await asyncio.wait_for(task, timeout=REPLY_SECONDS)

    try:
        yield server, cw, cr, stop
    finally:
        if not task.done():
            await stop()


# ---------------- happy path ----------------


@pytest.mark.asyncio
async def test_ping_round_trip(daemon):
    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    resp = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["pong"] == "pong"
    assert isinstance(resp["result"]["daemon_pid"], int)
    assert resp["result"]["uptime_seconds"] >= 0
    await stop()


@pytest.mark.asyncio
async def test_info_returns_platform_and_log_file(daemon, tmp_path: Path, monkeypatch):
    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 2, "method": "info", "params": {}})
    resp = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp["id"] == 2
    body = resp["result"]
    assert body["daemon_pid"] == os.getpid()
    assert "-" in body["platform"]  # "darwin-arm64", etc.
    assert body["log_file"].endswith("daemon.log")
    await stop()


@pytest.mark.asyncio
async def test_health_includes_uptime(daemon):
    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 3, "method": "health", "params": {}})
    resp = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp["result"]["status"] == "ok"
    assert resp["result"]["inflight_requests"] >= 0
    await stop()


# ---------------- error paths ----------------


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found(daemon):
    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 4, "method": "nope.nada", "params": {}})
    resp = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp["id"] == 4
    assert resp["error"]["code"] == -32601
    assert "nope.nada" in resp["error"]["message"]
    await stop()


@pytest.mark.asyncio
async def test_malformed_json_returns_parse_error_and_server_survives(daemon):
    _server, cw, cr, stop = daemon
    # Send a frame whose body is NOT valid JSON.
    await write_frame_async(cw, b"this is not json")
    resp = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp["error"]["code"] == -32700
    # Now send a valid ping — server should still be alive.
    await _send(cw, {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}})
    resp2 = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp2["result"]["pong"] == "pong"
    await stop()


@pytest.mark.asyncio
async def test_invalid_params_returns_invalid_params(daemon):
    """`ping`'s params model has `extra='forbid'`; a typo'd field should
    trip the validator with code -32602."""
    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": {"typo": True}})
    resp = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp["error"]["code"] == -32602
    await stop()


# ---------------- shutdown ----------------


@pytest.mark.asyncio
async def test_shutdown_method_stops_loop(daemon):
    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 7, "method": "shutdown", "params": {}})
    resp = await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)
    assert resp["result"]["message"] == "shutting down"
    # Close the client side and confirm the server stops cleanly.
    cw.close()
    # The serve() task should already have exited from the shutdown flag;
    # the fixture's stop() will await it.
    await stop()


# ---------------- D2: outbound requests fail fast on disconnect ----------------


@pytest.mark.asyncio
async def test_pending_outbound_request_fails_on_client_disconnect(daemon):
    """D2: when the JSON-RPC peer disconnects mid-prompt, every in-flight
    server→client request must wake up with `ConnectionError` instead of
    hanging until the (intentionally None) timeout. The harness's
    permission/question brokers catch the error and auto-reject — without
    this, a closed editor leaves the harness blocked forever, holding
    the chat lock."""
    server, cw, cr, stop = daemon

    # Schedule a server→client request the client will NEVER answer.
    request_task = asyncio.create_task(
        server.request(
            "editor.something",
            None,
            timeout_seconds=None,
        )
    )

    # Confirm the request reached the client wire so the daemon's
    # _outbound bookkeeping has actually populated.
    await asyncio.wait_for(_recv(cr), timeout=REPLY_SECONDS)

    # Client disconnects without replying.
    cw.close()

    # The pending request must surface as ConnectionError rather than sit on the
    # deliberately-absent timeout: the claim is that the drop WAKES the waiter,
    # and the wake is an in-process asyncio callback. A sub-second budget on top
    # of it measured the host -- a thirty-two-worker shard could spend half a
    # second in the scheduler with nothing wrong -- and proved nothing the raise
    # does not. The ceiling below is a stop for a waiter that never wakes.
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(request_task, timeout=REPLY_SECONDS)

    await stop()


def test_no_public_dto_carries_scopes() -> None:
    """No request DTO declares a scopes field: the daemon resolves the reader."""
    from alkera_cli.daemon.protocol import METHODS

    assert METHODS and all("scopes" not in s.request_type.model_fields for s in METHODS.values())
