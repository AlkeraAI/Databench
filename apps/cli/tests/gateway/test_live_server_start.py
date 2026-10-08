"""The test-side uvicorn starter waits on the wall clock and names an early exit.

Pins the mechanism that replaced the 500-poll loop: a lifespan slower than the
old five-second budget still comes up, a lifespan that fails is reported as the
exit (at once, not after the budget), and a server that never binds is reported
against the clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from _helpers.live_server import bound_port, wait_until_serving
from fastapi import FastAPI

#: The budget the replaced loop gave a lifespan (500 x 0.01 s), which the
#: gateway's tokenizer warm-up alone can exceed.
OLD_POLL_BUDGET_SECONDS = 5.0


def _app(*, startup_seconds: float = 0.0, fail_with: Exception | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.sleep(startup_seconds)
        if fail_with is not None:
            raise fail_with
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


@asynccontextmanager
async def _started(
    app: FastAPI, *, budget_seconds: float
) -> AsyncIterator[tuple[uvicorn.Server, float]]:
    """Start ``app`` the way the gateway helpers do and hand back the server and
    how long the wait took; always stops the server."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    serve_task = asyncio.create_task(server.serve())
    began = time.monotonic()
    try:
        await wait_until_serving(
            server, serve_task, budget_seconds=budget_seconds, what="probe server"
        )
        yield server, time.monotonic() - began
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(serve_task, timeout=5.0)
        if not serve_task.done():
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await serve_task


async def test_a_lifespan_slower_than_the_old_poll_budget_still_comes_up() -> None:
    """The gateway's startup may legitimately take longer than five seconds (a
    cold tokenizer cache); the starter waits for it and the server answers."""
    startup = OLD_POLL_BUDGET_SECONDS + 1.0
    async with _started(_app(startup_seconds=startup), budget_seconds=60.0) as (server, waited):
        assert waited >= startup
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{bound_port(server)}/ping")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


async def test_a_lifespan_that_fails_is_reported_as_the_exit_at_once() -> None:
    """uvicorn returns from serve() when the lifespan fails; the starter says so
    immediately instead of spending the whole budget and blaming a slow start."""
    app = _app(fail_with=RuntimeError("the ranks could not be fetched"))
    began = time.monotonic()
    with pytest.raises(RuntimeError, match=r"probe server exited before it started"):
        async with _started(app, budget_seconds=60.0):
            pass  # pragma: no cover
    assert time.monotonic() - began < OLD_POLL_BUDGET_SECONDS / 2


async def test_a_server_that_never_binds_is_reported_against_the_clock() -> None:
    """Only the wait is timed: stopping a server stuck in its lifespan is the
    caller's cleanup and takes uvicorn's own grace period."""
    server = uvicorn.Server(
        uvicorn.Config(_app(startup_seconds=30.0), host="127.0.0.1", port=0, log_level="warning")
    )
    serve_task = asyncio.create_task(server.serve())
    budget = 0.5
    began = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match=r"probe server did not start within 0\.5s"):
            await wait_until_serving(server, serve_task, budget_seconds=budget, what="probe server")
        elapsed = time.monotonic() - began
        assert budget <= elapsed < OLD_POLL_BUDGET_SECONDS / 2, elapsed
    finally:
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve_task


async def test_a_cancelled_serve_task_is_named_too() -> None:
    server = uvicorn.Server(
        uvicorn.Config(_app(startup_seconds=30.0), host="127.0.0.1", port=0, log_level="warning")
    )
    serve_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0)
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    with pytest.raises(RuntimeError, match=r"probe server was cancelled before it started"):
        await wait_until_serving(server, serve_task, budget_seconds=5.0, what="probe server")
