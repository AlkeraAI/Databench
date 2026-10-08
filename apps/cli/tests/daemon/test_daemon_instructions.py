"""Tests for the daemon's `instructions.get` / `instructions.set` methods.

Drives the daemon in-process over piped streams (same plumbing as
``test_daemon_preferences.py``) and isolates ``~/.alkera/`` to a tmp dir so the
real user's global-instructions file is never touched.
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
from alkera_cli.daemon import methods as _register_methods  # noqa: F401
from alkera_cli.host import paths

#: How long a test waits for one daemon reply or for shutdown. Well above the
#: preferences / instructions writers' own patience: a 2 s lock retry plus the
#: NTFS sharing-violation backoff behind every atomic write, which a loaded
#: Windows shard does spend. The suite-wide 90 s timeout still catches a hang.
_REPLY_BUDGET_S = 20.0


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", home / "instructions.md")
    monkeypatch.setattr(paths, "INSTRUCTIONS_LOCK_PATH", home / ".instructions.lock")
    return home


async def _connected_streams() -> tuple[
    asyncio.StreamReader,
    PipeWriter,
    asyncio.StreamReader,
    PipeWriter,
]:
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))
    return server_reader, server_writer, client_reader, client_writer


async def _send(writer: PipeWriter, envelope: dict[str, Any]) -> None:
    await write_frame_async(writer, json.dumps(envelope).encode("utf-8"))


async def _recv_until(
    reader: asyncio.StreamReader,
    predicate,
    *,
    timeout: float = _REPLY_BUDGET_S,  # noqa: ASYNC109
) -> dict[str, Any]:
    async def _recv() -> dict[str, Any]:
        return json.loads((await read_frame_async(reader)).decode("utf-8"))

    return await asyncio.wait_for(_drain_until(reader, predicate, _recv), timeout=timeout)


async def _drain_until(reader, predicate, recv):
    while True:
        msg = await recv()
        if predicate(msg):
            return msg


@pytest.fixture
async def daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _isolate_home(monkeypatch, tmp_path)
    sr, sw, cr, cw = await _connected_streams()
    server = JsonRpcServer(reader=sr, writer=sw)
    task = asyncio.create_task(server.serve())

    async def stop() -> None:
        server.request_shutdown()
        cw.close()
        await asyncio.wait_for(task, timeout=_REPLY_BUDGET_S)

    try:
        yield server, cw, cr, stop, tmp_path
    finally:
        if not task.done():
            await stop()


@pytest.mark.asyncio
async def test_instructions_get_defaults_to_empty(daemon):
    _server, cw, cr, stop, _home = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 1, "method": "instructions.get", "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 1)
    assert resp["result"]["content"] == ""
    await stop()


@pytest.mark.asyncio
async def test_instructions_set_then_get(daemon):
    _server, cw, cr, stop, _home = daemon
    body = "# Global\n\nAlways prefer DuckDB for local analytics.\n"
    await _send(
        cw,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "instructions.set",
            "params": {"content": body},
        },
    )
    set_resp = await _recv_until(cr, lambda m: m.get("id") == 2)
    assert set_resp["result"]["content"] == body

    await _send(cw, {"jsonrpc": "2.0", "id": 3, "method": "instructions.get", "params": {}})
    get_resp = await _recv_until(cr, lambda m: m.get("id") == 3)
    assert get_resp["result"]["content"] == body
    await stop()


@pytest.mark.asyncio
async def test_instructions_set_empty_clears(daemon):
    _server, cw, cr, stop, _home = daemon
    await _send(
        cw,
        {"jsonrpc": "2.0", "id": 4, "method": "instructions.set", "params": {"content": "stuff"}},
    )
    await _recv_until(cr, lambda m: m.get("id") == 4)

    await _send(
        cw,
        {"jsonrpc": "2.0", "id": 5, "method": "instructions.set", "params": {"content": ""}},
    )
    await _recv_until(cr, lambda m: m.get("id") == 5)

    await _send(cw, {"jsonrpc": "2.0", "id": 6, "method": "instructions.get", "params": {}})
    get_resp = await _recv_until(cr, lambda m: m.get("id") == 6)
    assert get_resp["result"]["content"] == ""
    await stop()
