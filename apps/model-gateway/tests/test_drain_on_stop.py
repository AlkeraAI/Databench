"""A deploy costs one model step, never a chat turn.

uvicorn's graceful shutdown closes the listening socket and then lets the
responses it is already carrying run to `--timeout-graceful-shutdown`. What it
does NOT do is refuse a request that arrives on a connection a client already
holds open: that request would be served, and then torn mid-body when the window
closes. A torn transport is not a shape the caller can tell apart from a failed
step, so it ends the whole turn. These tests hold the guard that makes it a
retryable 503 instead, and hold that the guard leaves running streams alone.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator
from types import FrameType

import httpx
import pytest
from model_gateway.app_factory import install_drain_on_stop
from model_gateway.pipeline import begin_drain, draining, end_drain


def _body(model_id: str) -> dict:
    return {"model": model_id, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


class _GatedStream(httpx.AsyncByteStream):
    """An upstream that has begun answering and is not finished: it yields
    ``head``, then blocks until ``release`` is set, then yields ``tail``."""

    def __init__(self, head: bytes, tail: bytes) -> None:
        self._head = head
        self._tail = tail
        self.opened = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._head
        self.opened.set()
        await self.release.wait()
        yield self._tail


@pytest.mark.asyncio
async def test_a_new_stream_is_refused_with_a_retryable_503_while_draining(
    gateway_client, upstream, seed, make_response
) -> None:
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))

    begin_drain()
    resp = await gateway_client.post(
        "/anthropic/v1/messages",
        headers={"Authorization": f"Bearer {s.token}"},
        json=_body(s.model_id),
    )

    assert resp.status_code == 503
    # Retryable means the caller is TOLD to come back — to a task that is not
    # going away — rather than left to guess from a status code alone.
    assert resp.headers["retry-after"] == "1"
    # Refused before the provider was touched: no hold, no upstream call.
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_a_stream_already_open_runs_to_completion_through_the_drain(
    gateway_client, upstream, seed, make_sse
) -> None:
    """The point of draining rather than stopping: the answer already in flight
    finishes, while the answer that has not started yet is turned away.

    The ASGI transport hands the body back whole, so the stream is held open from
    the UPSTREAM side: the provider has sent its opening events and has not
    finished, which is exactly the state a deploy interrupts."""
    s = await seed(granted_nanos=10**12)
    full = make_sse(input_tokens=10, output_tokens=5, text="hello")
    split = full.index(b"event: message_delta")
    gated = _GatedStream(full[:split], full[split:])
    upstream.script(
        httpx.Response(200, stream=gated, headers={"content-type": "text/event-stream"})
    )
    headers = {"Authorization": f"Bearer {s.token}"}
    refused: list[httpx.Response] = []

    async def stop_the_process_mid_answer() -> None:
        await asyncio.wait_for(gated.opened.wait(), timeout=10)
        begin_drain()
        refused.append(
            await gateway_client.post(
                "/anthropic/v1/messages", headers=headers, json=_body(s.model_id)
            )
        )
        gated.release.set()

    stopper = asyncio.create_task(stop_the_process_mid_answer())
    resp = await asyncio.wait_for(
        gateway_client.post("/anthropic/v1/messages", headers=headers, json=_body(s.model_id)),
        timeout=30,
    )
    await stopper

    # The answer that was mid-flight when the process was told to stop is whole.
    assert resp.status_code == 200
    assert b"message_stop" in resp.content
    assert b"hello" in resp.content
    # The one that had not started is turned away, retryably.
    assert refused[0].status_code == 503
    assert refused[0].headers["retry-after"] == "1"


def test_the_stop_signal_flips_the_drain_and_still_reaches_the_server_handler() -> None:
    """Chaining, not replacing: the handler uvicorn installed still runs, so the
    process still shuts down. Replacing it would drain forever and never exit."""
    delivered: list[int] = []

    def server_handler(signum: int, frame: FrameType | None) -> None:
        delivered.append(signum)

    original = signal.signal(signal.SIGTERM, server_handler)
    try:
        restore = install_drain_on_stop()
        try:
            assert draining() is False
            signal.raise_signal(signal.SIGTERM)
            assert draining() is True
            assert delivered == [signal.SIGTERM]
        finally:
            restore()

        # Restored: the drain is off and the server's own handler is back in place,
        # so a second app in the same process starts from a clean slate.
        assert draining() is False
        assert signal.getsignal(signal.SIGTERM) is server_handler
        signal.raise_signal(signal.SIGTERM)
        assert draining() is False
        assert delivered == [signal.SIGTERM, signal.SIGTERM]
    finally:
        end_drain()
        signal.signal(signal.SIGTERM, original)
