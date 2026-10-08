"""One account must not be able to take a whole gateway task's stream capacity.

A gate slot is held for the entire life of a stream, and nothing else in the
request path caps concurrency, so without a per-principal bound a single
authenticated account can open enough streams to shed every other tenant with a
429. The shed is invisible to request-rate autoscaling: a wedged stream issues no
new requests, so the fleet looks idle while it is full.

The second half is the wedge itself. The relay's wall-clock deadline can only fire
while the generator is running, and a client that simply stops reading parks it
inside a ``yield`` under ASGI back-pressure — no code of ours runs again until the
connection dies, which for this deployment's long-SSE idle timeout is over an hour.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from alkera_core.config import settings
from httpx import AsyncClient
from model_gateway.pipeline import _GATE, _gated, _StreamGate


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(model_id: str) -> dict:
    return {"model": model_id, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


# --------------------------------------------------------------------------- #
# The gate's accounting — what decides who gets a 429
# --------------------------------------------------------------------------- #


def test_one_principal_cannot_take_every_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 8)
    gate = _StreamGate()
    assert gate.per_principal_limit() == 2

    assert gate.try_acquire("alice") is True
    assert gate.try_acquire("alice") is True
    assert gate.try_acquire("alice") is False  # her share is spent...
    assert gate.try_acquire("bob") is True  # ...but the task is not
    assert gate.active == 3


def test_the_process_wide_cap_still_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-principal share is an extra bound, never a replacement: enough
    distinct principals still saturate the task and get shed."""
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 3)
    gate = _StreamGate()
    assert [gate.try_acquire(f"u{i}") for i in range(4)] == [True, True, True, False]


def test_releasing_returns_the_principal_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 4)
    gate = _StreamGate()
    assert gate.try_acquire("alice") is True
    assert gate.try_acquire("alice") is False
    gate.release("alice")
    assert gate.active_for("alice") == 0
    assert gate.try_acquire("alice") is True


def test_a_share_of_at_least_one_slot_survives_a_tiny_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asymmetric case: a cap so small that a quarter of it rounds to zero must
    still let a principal through, or the gateway refuses everyone."""
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 1)
    gate = _StreamGate()
    assert gate.per_principal_limit() == 1
    assert gate.try_acquire("alice") is True


def test_a_shared_principal_is_bounded_only_by_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-hosted gateway relays its whole deployment through one machine
    account, so applying a single user's share to it would throttle an entire
    customer to a quarter of one task."""
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 8)
    gate = _StreamGate()
    for _ in range(8):
        assert gate.try_acquire("machine", shared=True) is True
    assert gate.try_acquire("machine", shared=True) is False  # the task cap holds


# --------------------------------------------------------------------------- #
# A client that stops reading cannot pin a slot indefinitely
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_client_that_stops_reading_stops_holding_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gateway_max_stream_seconds", 1)
    gate = _StreamGate()
    assert gate.try_acquire("alice") is True

    async def upstream() -> AsyncIterator[bytes]:
        yield b"first"
        await asyncio.Event().wait()  # the provider is still streaming

    stream = _gated(upstream(), gate, "alice")
    assert await anext(stream) == b"first"
    assert gate.active == 1

    # The consumer never asks for another chunk — the generator is parked in its
    # yield exactly as it is when a client stops reading the socket.
    for _ in range(60):
        await asyncio.sleep(0.05)
        if gate.active == 0:
            break
    assert gate.active == 0, "a non-reading client pinned the slot past the max stream duration"
    assert gate.active_for("alice") == 0

    await stream.aclose()
    assert gate.active == 0  # the eventual close must not double-release


@pytest.mark.asyncio
async def test_the_watchdog_closes_the_upstream_before_it_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Freeing the slot is not enough on its own. A parked relay still holds an
    open provider stream, so a replacement admitted into the freed slot runs
    against capacity this stream never gave back — the counters stop bounding
    live upstream streams, which is the only thing they exist to bound.
    """
    monkeypatch.setattr(settings, "gateway_max_stream_seconds", 1)
    gate = _StreamGate()
    assert gate.try_acquire("alice") is True
    closed = asyncio.Event()
    active_at_close: list[int] = []

    async def upstream() -> AsyncIterator[bytes]:
        try:
            yield b"first"
            await asyncio.Event().wait()  # the provider is still streaming
        finally:
            # Sampled from inside the teardown, so it records what the accounting
            # said at the moment the provider stream actually ended.
            active_at_close.append(gate.active)
            closed.set()

    stream = _gated(upstream(), gate, "alice")
    assert await anext(stream) == b"first"
    assert gate.active == 1

    for _ in range(80):
        await asyncio.sleep(0.05)
        if closed.is_set() and gate.active == 0:
            break

    assert closed.is_set(), "the watchdog freed the slot and left the provider stream open"
    assert active_at_close == [1], "the slot was freed before the upstream was torn down"
    assert gate.active == 0
    assert gate.active_for("alice") == 0

    await stream.aclose()
    assert gate.active == 0  # the eventual close must not double-release


@pytest.mark.asyncio
async def test_a_normally_finished_stream_releases_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog is a backstop, not the release path: an ordinary stream frees
    its slot when it ends, and the cancelled timer cannot free a second one."""
    monkeypatch.setattr(settings, "gateway_max_stream_seconds", 1)
    gate = _StreamGate()
    assert gate.try_acquire("alice") is True
    assert gate.try_acquire("bob") is True

    async def upstream() -> AsyncIterator[bytes]:
        yield b"only"

    assert [chunk async for chunk in _gated(upstream(), gate, "alice")] == [b"only"]
    assert gate.active == 1  # bob's slot untouched
    await asyncio.sleep(1.2)  # past the watchdog the finished stream armed
    assert gate.active == 1
    assert gate.active_for("bob") == 1


# --------------------------------------------------------------------------- #
# End to end: the 429 a saturated principal actually receives
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_saturated_principal_is_shed_while_another_is_served(
    gateway_client: AsyncClient, upstream, seed, make_response, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 4)  # share of 1
    first = await seed(granted_nanos=10**12)
    second = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))

    # Stand in for a stream the first user already has open.
    assert _GATE.try_acquire(first.user_id.hex) is True
    try:
        shed = await gateway_client.post(
            "/anthropic/v1/messages",
            headers=_auth(first.token),
            json=_body(first.model_id),
        )
        served = await gateway_client.post(
            "/anthropic/v1/messages",
            headers=_auth(second.token),
            json=_body(second.model_id),
        )
    finally:
        _GATE.release(first.user_id.hex)

    assert shed.status_code == 429
    assert shed.headers["retry-after"] == "1"
    assert served.status_code == 200  # a different tenant is unaffected

    # And the shed principal is servable again once its stream ends.
    again = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(first.token), json=_body(first.model_id)
    )
    assert again.status_code == 200
