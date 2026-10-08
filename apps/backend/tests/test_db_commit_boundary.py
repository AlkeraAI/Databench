"""The read-after-write commit boundary.

A mutation's commit must be durable BEFORE its response is sent: a caller that
immediately acts on the response (the device-code create-then-approve flow did,
and hit a 404 over real TCP) must find the row. FastAPI only guarantees that
when the `get_db` dependency is injected with scope="function" — the default
request scope runs the teardown (and thus the commit) after delivery. The
dependency cache keys on the computed scope, so the two spellings never share:
a bare injection opens a SECOND session beside the scoped one, committed
independently and only after the response is out. The request is then split
across two transactions, and whichever writes went through the bare session
are not durable when the caller reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal, get_db, get_health_db
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app


def _walk_get_db_scopes(dependant, path: str, found: list[tuple[str, object]]) -> None:  # type: ignore[no-untyped-def]
    for sub in dependant.dependencies:
        # Readiness has a unit-of-work dependency of its own (its own pool); the
        # same ordering invariant holds for it.
        if sub.call is get_db or sub.call is get_health_db:
            found.append((path, sub.scope))
        _walk_get_db_scopes(sub, path, found)


def test_every_get_db_injection_is_function_scoped_because_one_bare_one_splits_the_request() -> (
    None
):
    """Walk the whole app's resolved dependency graph and pin the scope on every
    `get_db` injection. A bare `Depends(get_db)` does not share the scoped
    session, because the dependency cache keys on the scope: it opens a second
    one beside it, whose commit lands after the response. Any write that took
    that path is invisible to a caller acting on the response, so the invariant
    has to hold at every site rather than only on the shared `DbSession`."""
    found: list[tuple[str, object]] = []
    for route in fastapi_app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            _walk_get_db_scopes(dependant, getattr(route, "path", "?"), found)
    assert found, "no get_db injections resolved — the walk is broken, not the app"
    bare = sorted({path for path, scope in found if scope != "function"})
    assert not bare, f"routes with a bare (request-scoped) get_db injection: {bare}"


async def _ordering_events(app, events: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Drive GET /health/ready through `app` with the get_db call swapped for a
    probe that records its teardown, and an ASGI recorder noting when the
    response starts. The Depends declaration (and its scope) stays the app's own —
    an override replaces only the callable."""

    async def probe_db() -> AsyncIterator[AsyncSession]:
        async with AsyncSessionLocal() as session:
            try:
                yield session
            finally:
                events.append("teardown:get_db")

    async def recorder(scope, receive, send):  # type: ignore[no-untyped-def]
        async def send_probe(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                events.append("sent:response_start")
            await send(message)

        await app(scope, receive, send_probe)

    # Readiness resolves its session through `get_health_db` — the same
    # yield-and-commit shape as `get_db`, on the probe's own pool.
    app.dependency_overrides[get_health_db] = probe_db
    try:
        transport = ASGITransport(app=recorder)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health/ready")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(get_health_db, None)


@pytest.mark.asyncio
async def test_session_teardown_completes_before_the_response_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behavioral half of the invariant above, observed at the ASGI boundary
    on the production app: the session (and its commit) is torn down before the
    first response byte leaves. Under a bare request-scoped injection the order
    flips — see the twin below, which characterizes the broken ordering.

    Files is pinned off because the carrier route is readiness, which also probes
    the object store: with the ambient dotenv deciding, this ordering test turns
    red wherever no store is running, over a dependency it says nothing about."""
    monkeypatch.setattr(settings, "files_enabled", False)
    events: list[str] = []
    await _ordering_events(fastapi_app, events)
    assert events == ["teardown:get_db", "sent:response_start"]


@pytest.mark.asyncio
async def test_a_request_scoped_injection_would_send_the_response_first() -> None:
    """The discriminating twin: the same probe on a minimal app whose route takes
    a bare `Depends(gen)` shows teardown AFTER the response start — the exact
    window the scope="function" fix closes. If FastAPI's semantics ever change
    under us, this is the test that says the probe stopped discriminating."""
    from fastapi import Depends, FastAPI

    events: list[str] = []
    app = FastAPI()

    async def gen() -> AsyncIterator[str]:
        try:
            yield "session"
        finally:
            events.append("teardown:get_db")

    @app.get("/probe")
    async def probe(dep: str = Depends(gen)) -> dict[str, bool]:
        return {"ok": True}

    async def recorder(scope, receive, send):  # type: ignore[no-untyped-def]
        async def send_probe(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                events.append("sent:response_start")
            await send(message)

        await app(scope, receive, send_probe)

    async with AsyncClient(transport=ASGITransport(app=recorder), base_url="http://test") as client:
        resp = await client.get("/probe")
    assert resp.status_code == 200
    assert events == ["sent:response_start", "teardown:get_db"]
