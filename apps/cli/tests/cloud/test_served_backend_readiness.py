"""What ``serve_controlled`` owes a test before it hands the address over.

Two promises the cloud suite leans on for every socket it opens, both of which
used to hold only on an idle machine:

* the port it yields is still BOUND — picked and held, never picked, released
  and re-bound, which loses the port to anything else that asks in the window;
* the realtime runtime the served lifespan started can already deliver, so the
  first socket of a test is not closed ``4503 realtime is not running here``
  because the outbox listener was still catching up.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest
import uvicorn
import websockets
from alkera_core.schemas.realtime.tickets import WS_PATH, WS_SUBPROTOCOL
from backend.services.realtime import close_codes
from tests.conftest import serve_controlled


async def test_the_served_port_is_still_bound_when_uvicorn_is_handed_it() -> None:
    """The port must never be free between the pick and uvicorn's bind: a probe
    that binds it at the moment ``serve()`` runs proves the window is open."""
    free_at_serve: list[bool] = []
    original = uvicorn.Server.serve

    async def watched(self: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        port = self.config.port if sockets is None else sockets[0].getsockname()[1]
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            free_at_serve.append(True)
        except OSError:
            free_at_serve.append(False)
        finally:
            probe.close()
        await original(self, sockets=sockets)

    uvicorn.Server.serve = watched  # type: ignore[method-assign]
    try:
        async with serve_controlled() as served:
            assert served.addr.startswith("127.0.0.1:")
    finally:
        uvicorn.Server.serve = original  # type: ignore[method-assign]

    assert free_at_serve == [False], "the port was released before uvicorn bound it"


async def test_a_slow_listener_does_not_hand_back_a_replica_that_refuses_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listener whose first catch-up read is slower than the socket route's
    two-second grace must delay the fixture, not the test: a socket opened the
    instant the address arrives is answered by the auth refusal, never by
    ``UNAVAILABLE``."""
    from alkera_core.events import listener as listener_module

    original_latest_id = listener_module.latest_id

    async def slow_latest_id(session: Any) -> int:
        await asyncio.sleep(3.0)
        return await original_latest_id(session)

    monkeypatch.setattr(listener_module, "latest_id", slow_latest_id)

    async with serve_controlled() as served:
        with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
            async with websockets.connect(
                f"ws://{served.addr}{WS_PATH}", subprotocols=[WS_SUBPROTOCOL]
            ) as ws:
                await ws.recv()
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == close_codes.UNAUTHORIZED, closed.value.rcvd
