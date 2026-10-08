"""The box sizes its realtime traffic by what the server says, not by copies.

Two sizes on the box mirror server settings. A streamed chunk rides
``pg_notify`` and the server refuses a notification over
``realtime_ephemeral_max_bytes``; a hello is answered with the whole document,
which may grow to ``realtime_doc_max_bytes``. The box held both as constants,
so a deployment that raised either setting broke long chats on the box: its
receive cap refused the larger snapshot and the chat could never be opened.

The server now names both in the welcome's ``limits``. The box honours them,
and falls back to the old constants when a server that predates the fields
sends nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import ChatMirror, chunk_size
from alkera_cli.cloud.transport import RealtimeSizes, realtime_sizes
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import AgentMessageChunk
from alkera_core.schemas.realtime.frames import SocketLimits
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

MIB = 1024 * 1024
#: A bound on a hang, never a wait an assertion depends on.
HANG = 20.0
#: What a server that predates the size fields sends: the inbound budget only.
BUDGET_ONLY: dict[str, Any] = {
    "frames_per_window": 200,
    "bytes_per_window": 4 * MIB,
    "window_seconds": 10.0,
    "max_frame_bytes": 2 * MIB + 64 * 1024,
}


def _limits(**sizes: int) -> SocketLimits:
    return SocketLimits.model_validate({**BUDGET_ONLY, **sizes})


# ---------------------------------------------------------------------------
# The sizes the welcome implies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        pytest.param(
            None,
            # 4096 notify bytes less 1024 of envelope headroom; twice 4 MiB.
            RealtimeSizes(chunk_max_bytes=3072, receive_max_bytes=8 * MIB),
            id="no-limits-at-all-falls-back",
        ),
        pytest.param(
            _limits(),
            RealtimeSizes(chunk_max_bytes=3072, receive_max_bytes=8 * MIB),
            id="budget-only-server-falls-back",
        ),
        pytest.param(
            _limits(ephemeral_max_bytes=8000, doc_max_bytes=16 * MIB),
            RealtimeSizes(chunk_max_bytes=6976, receive_max_bytes=32 * MIB),
            id="larger-limits-are-honoured",
        ),
        pytest.param(
            _limits(ephemeral_max_bytes=2048),
            RealtimeSizes(chunk_max_bytes=1024, receive_max_bytes=8 * MIB),
            id="a-smaller-notify-cap-is-honoured-too",
        ),
        pytest.param(
            _limits(doc_max_bytes=16 * MIB),
            RealtimeSizes(chunk_max_bytes=3072, receive_max_bytes=32 * MIB),
            id="only-the-doc-ceiling-sent",
        ),
        pytest.param(
            _limits(doc_max_bytes=1024),
            # Accepting more than a document can reach is harmless; refusing
            # an ordinary frame because the ceiling dropped is not.
            RealtimeSizes(chunk_max_bytes=3072, receive_max_bytes=8 * MIB),
            id="a-lower-doc-ceiling-never-shrinks-the-receive-cap",
        ),
    ],
)
def test_the_welcome_sets_the_box_sizes(
    limits: SocketLimits | None, expected: RealtimeSizes
) -> None:
    assert realtime_sizes(limits) == expected


# ---------------------------------------------------------------------------
# The socket, over a real websocket
# ---------------------------------------------------------------------------


class _Api(CloudRestClient):
    def __init__(self, ws_url: str) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self._ws_url = ws_url

    @property
    def ws_url(self) -> str:
        return self._ws_url

    async def mint_ticket(self) -> str:
        return "t-1"


def _welcome(limits: dict[str, Any] | None) -> str:
    frame: dict[str, Any] = {
        "schema_version": "1.0.0",
        "t": "welcome",
        "peer_id": "p:1",
        "server_time": datetime.now(UTC).isoformat(),
        "instance": "gw-1",
    }
    if limits is not None:
        frame["limits"] = limits
    return json.dumps(frame)


async def _serve_one_large_frame(limits: dict[str, Any] | None, size: int) -> str:
    """Welcome the box, send it one ``size``-byte frame once it has spoken, and
    report whether it kept the connection: ``"kept"`` when its next ping
    arrives, else the close code it sent."""
    outcome: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws: ServerConnection) -> None:
        await ws.send(_welcome(limits))
        large = json.dumps({"schema_version": "1.0.0", "t": "pong", "pad": "x" * size})
        try:
            # A snapshot only ever answers a frame the box sent after reading
            # the welcome, so the large frame waits for the box to speak.
            await ws.recv()
            await ws.send(large)
            while True:
                frame = json.loads(await ws.recv())
                if frame.get("t") == "ping":
                    outcome.set_result("kept")
                    return
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            outcome.set_result(f"closed {code}")

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        socket = CloudSocket(_Api(f"ws://127.0.0.1:{port}/ws"), ping_interval=0.05)
        await socket.start()
        try:
            return await asyncio.wait_for(outcome, timeout=HANG)
        finally:
            await socket.stop()


async def test_a_snapshot_above_the_old_cap_is_accepted_when_the_server_raised_its_ceiling() -> (
    None
):
    """A deployment that raised ``realtime_doc_max_bytes`` to 8 MiB sends
    snapshots up to that size plus their JSON. The box used to refuse anything
    over 8 MiB (``1009``), so the chat could never be opened."""
    limits = {**BUDGET_ONLY, "doc_max_bytes": 8 * MIB}
    assert await _serve_one_large_frame(limits, 9 * MIB) == "kept"


async def test_a_server_that_names_no_ceiling_keeps_the_old_receive_cap() -> None:
    """The fallback is today's cap: a frame over it is still refused as too
    big, which proves the raise above came from the welcome."""
    assert await _serve_one_large_frame(BUDGET_ONLY, 9 * MIB) == "closed 1009"


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_a_reconnect_opens_with_the_cap_the_last_welcome_named() -> None:
    """The second connection takes a large frame straight after its welcome,
    before the box has said anything, which only fits if the socket opened
    with the receive cap the previous welcome named. Every welcome resets the
    sizes, so a server that lowered its notify cap is followed down."""
    outcome: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    connections = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        # The second server lowered its notify cap: the box follows it down.
        notify = {} if connections == 1 else {"ephemeral_max_bytes": 2048}
        await ws.send(_welcome({**BUDGET_ONLY, "doc_max_bytes": 8 * MIB, **notify}))
        if connections == 1:
            await ws.recv()  # the box is up and has read the welcome
            await ws.close(code=1012, reason="restart")
            return
        try:
            await ws.send(
                json.dumps({"schema_version": "1.0.0", "t": "pong", "pad": "x" * 9 * MIB})
            )
            while json.loads(await ws.recv()).get("t") != "ping":
                pass
            outcome.set_result("kept")
        except ConnectionClosed as exc:
            outcome.set_result(f"closed {exc.rcvd.code if exc.rcvd is not None else None}")

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        socket = CloudSocket(_Api(f"ws://127.0.0.1:{port}/ws"), ping_interval=0.05, sleep=_no_sleep)
        await socket.start()
        try:
            assert await asyncio.wait_for(outcome, timeout=HANG) == "kept"
        finally:
            await socket.stop()
    # The notify budget follows the newest welcome; the receive cap is the
    # newest welcome's too (twice its 8 MiB ceiling).
    assert socket.sizes == RealtimeSizes(chunk_max_bytes=1024, receive_max_bytes=16 * MIB)


# ---------------------------------------------------------------------------
# The mirror splits a streamed chunk by the server's notify cap
# ---------------------------------------------------------------------------


class _Doc:
    def __init__(self) -> None:
        self.chunks: list[dict[str, Any]] = []
        self.sent = asyncio.Event()

    async def send_chunk(self, events: Sequence[dict[str, Any]]) -> bool:
        self.chunks.extend(events)
        self.sent.set()
        return True


async def _streamed_pieces(tmp_path: Path, sizes: RealtimeSizes, text: str) -> list[dict[str, Any]]:
    project = ProjectDirectory(tmp_path / ".alkera")
    runtime = HarnessRuntime(project, adapter_factory=FakeAdapterFactory(FakeAdapter))
    mirror = ChatMirror(
        chat_id="chat-sizes",
        runtime=runtime,
        socket=cast(CloudSocket, SimpleNamespace(sizes=sizes)),
        rest=CloudRestClient(
            api_url="http://sizes.test",
            token="t",
            agent_id="chat-sizes",
            transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        ),
        user_id="u-1",
        owner_user_id="u-1",
        chunk_interval=0.001,
    )
    doc = _Doc()
    mirror._doc = cast(Any, doc)
    mirror._coalescer.add(
        AgentMessageChunk(
            event_id="chunk-1",
            time=datetime.now(UTC),
            session_id="chat-sizes",
            message_id="m-1",
            part_id="p-1",
            sequence=1,
            text=text,
            is_final=True,
        )
    )
    flusher = asyncio.create_task(mirror._flusher())
    try:
        await asyncio.wait_for(doc.sent.wait(), timeout=HANG)
        # Every piece of one event goes out in the same flush, without a yield.
    finally:
        flusher.cancel()
    return doc.chunks


async def test_a_chunk_under_a_raised_notify_cap_goes_whole(tmp_path: Path) -> None:
    """5000 ASCII characters measure over the old 3072-byte piece budget but
    well under the 6976 a server with an 8000-byte notify cap allows."""
    text = "x" * 5000
    sizes = realtime_sizes(_limits(ephemeral_max_bytes=8000))
    pieces = await _streamed_pieces(tmp_path, sizes, text)
    assert [piece["text"] for piece in pieces] == [text]


async def test_a_chunk_is_split_by_the_fallback_cap_without_server_sizes(tmp_path: Path) -> None:
    text = "x" * 5000
    pieces = await _streamed_pieces(tmp_path, realtime_sizes(None), text)
    assert "".join(piece["text"] for piece in pieces) == text
    assert len(pieces) == 2
    assert all(chunk_size([piece]) <= 3072 for piece in pieces)
