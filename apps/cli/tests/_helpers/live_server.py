"""Start a uvicorn server inside a test and wait until it serves.

The poll-count loop these helpers replaced (500 x ``sleep(0.01)``) gave a
lifespan about five seconds to bind. The gateway's own startup warms the token
encodings for up to ``gateway_tokenizer_warm_timeout_seconds`` (10 s by
default, 120 s at most) before it listens, so a runner with a cold tiktoken
cache lost the e2e gate to "gateway did not start" twice on the same day. The
wait is against the wall clock now, wider than any lifespan budget a test
server carries, and a server that exits before it binds is reported as that,
with its lifespan's failure attached, never as a slow start.
"""

from __future__ import annotations

import asyncio
import time

import uvicorn

#: Wider than any lifespan budget a test server carries: the gateway's
#: tokenizer warm-up is bounded at 10 s by default and capped at 120 s, and the
#: reservation sweep behind it is one query.
DEFAULT_START_TIMEOUT = 90.0


async def wait_until_serving(
    server: uvicorn.Server,
    serve_task: asyncio.Task[None],
    *,
    budget_seconds: float = DEFAULT_START_TIMEOUT,
    what: str = "server",
) -> None:
    """Return once ``server`` listens; raise when it exits first or the clock runs out.

    ``serve_task`` is the task running ``server.serve()``. uvicorn logs a failed
    lifespan startup and returns from ``serve()`` without raising, so an early
    return with no exception is reported as the lifespan failing, pointing at
    the captured log; an exception is chained.
    """
    deadline = time.monotonic() + budget_seconds
    while not server.started:
        if serve_task.done():
            if serve_task.cancelled():
                raise RuntimeError(f"{what} was cancelled before it started")
            error = serve_task.exception()
            if error is None:
                raise RuntimeError(
                    f"{what} exited before it started: its lifespan startup failed (see the log)"
                )
            raise RuntimeError(f"{what} exited before it started") from error
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{what} did not start within {budget_seconds:g}s")
        await asyncio.sleep(0.02)


def bound_port(server: uvicorn.Server) -> int:
    """The ephemeral port a ``port=0`` server was given."""
    return int(server.servers[0].sockets[0].getsockname()[1])
