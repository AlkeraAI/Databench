"""A pending device login's poll never outlives what stopped it.

The poll runs on a worker thread. Shutting the daemon down, or a new login
superseding it, must end that thread promptly and make no further token
request: a poll left sleeping on its interval kept polling into later tests
(and, in a real editor, past the daemon it belonged to). The real
``poll_for_token`` runs here against a local token endpoint that always
answers ``authorization_pending`` with a long interval, so a poll that only
noticed its stop after sleeping would still be alive when these assertions
run.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from alkera_cli.account import device_flow
from alkera_cli.daemon import (
    JsonRpcServer,
    PipeWriter,
    connect_pipe_reader,
    connect_pipe_writer,
    read_frame_async,
    write_frame_async,
)
from alkera_cli.daemon import methods as _register_methods  # noqa: F401

#: Long enough that a poll sleeping out one interval is unmistakably alive.
INTERVAL_S = 5
_BUDGET_S = 20.0


class _TokenEndpoint:
    """Always pending. Records which device code each poll asked for, and
    when each poll ended."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        #: One flag per poll, set the moment that poll returns or raises.
        self.ended: list[threading.Event] = []
        self._codes = iter(f"dc-{n}" for n in range(100))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(parse_qs(request.content.decode())["device_code"][0])
        return httpx.Response(400, json={"error": "authorization_pending"})

    def device(self, *_a: object, **_k: object) -> device_flow.DeviceCodeResponse:
        code = next(self._codes)
        return device_flow.DeviceCodeResponse(
            device_code=code,
            user_code="ABCD-1234",
            verification_uri="https://app.example.test/device",
            verification_uri_complete="https://app.example.test/device?user_code=ABCD-1234",
            expires_in=600,
            interval=INTERVAL_S,
        )

    def asked_for(self, code: str) -> int:
        return self.asked.count(code)


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> _TokenEndpoint:
    recorder = _TokenEndpoint()
    real_poll = device_flow.poll_for_token

    def _poll(*args: Any, **kwargs: Any) -> str:
        ended = threading.Event()
        recorder.ended.append(ended)
        try:
            return real_poll(*args, transport=httpx.MockTransport(recorder.handler), **kwargs)
        finally:
            ended.set()

    monkeypatch.setattr(device_flow, "request_device_code", recorder.device)
    monkeypatch.setattr(device_flow, "poll_for_token", _poll)
    return recorder


class _Daemon:
    def __init__(
        self, server: JsonRpcServer, task: asyncio.Task[None], reader: Any, writer: PipeWriter
    ) -> None:
        self.server = server
        self.task = task
        self._reader = reader
        self._writer = writer
        self._next = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        envelope = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        await write_frame_async(self._writer, json.dumps(envelope).encode())
        while True:
            frame = await asyncio.wait_for(read_frame_async(self._reader), timeout=_BUDGET_S)
            msg = json.loads(frame.decode())
            if msg.get("id") == rid:
                return msg

    async def shutdown(self) -> None:
        self.server.request_shutdown()
        self._writer.close()
        await asyncio.wait_for(self.task, timeout=_BUDGET_S)


@pytest.fixture
async def daemon() -> AsyncIterator[_Daemon]:
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))
    server = JsonRpcServer(reader=server_reader, writer=server_writer)
    task = asyncio.create_task(server.serve())
    handle = _Daemon(server, task, client_reader, client_writer)
    try:
        yield handle
    finally:
        if not task.done():
            await handle.shutdown()


async def _until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _BUDGET_S
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.02)


async def test_shutting_the_daemon_down_mid_poll_ends_the_poll_and_its_requests(
    daemon: _Daemon, endpoint: _TokenEndpoint
) -> None:
    await daemon.call("auth.startDeviceLogin")
    await _until(lambda: endpoint.asked_for("dc-0") >= 1)

    await daemon.shutdown()

    assert endpoint.ended[0].is_set(), "the poll outlived the daemon"
    assert daemon.server.pending_login is None
    asked = len(endpoint.asked)
    await asyncio.sleep(INTERVAL_S + 1)
    assert len(endpoint.asked) == asked, "a token request went out after shutdown"


async def test_a_new_login_ends_the_poll_it_supersedes(
    daemon: _Daemon, endpoint: _TokenEndpoint
) -> None:
    await daemon.call("auth.startDeviceLogin")
    await _until(lambda: endpoint.asked_for("dc-0") >= 1)

    await daemon.call("auth.startDeviceLogin")
    await _until(lambda: endpoint.asked_for("dc-1") >= 1)

    assert await asyncio.to_thread(endpoint.ended[0].wait, 2.0), "the superseded poll still runs"
    asked_first = endpoint.asked_for("dc-0")
    await asyncio.sleep(INTERVAL_S + 1)
    assert endpoint.asked_for("dc-0") == asked_first, "the superseded poll asked again"


async def test_cancelling_a_login_ends_its_poll(daemon: _Daemon, endpoint: _TokenEndpoint) -> None:
    await daemon.call("auth.startDeviceLogin")
    await _until(lambda: endpoint.asked_for("dc-0") >= 1)

    reply = await daemon.call("auth.cancelDeviceLogin")

    assert reply["result"]["cancelled"] is True
    assert await asyncio.to_thread(endpoint.ended[0].wait, 2.0), "the cancelled poll still runs"
