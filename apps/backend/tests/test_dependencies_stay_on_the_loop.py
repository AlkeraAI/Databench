"""Every dependency a request resolves runs on the event loop, not a worker thread.

FastAPI runs a plain-function dependency through ``run_in_threadpool``, which
borrows one of anyio's forty worker threads per hop. A dependency that only
reads settings or a header gains nothing from a thread and loses the request
whenever the pool is busy elsewhere: the Files routes stalled for a whole test
module because a marker dependency that returns ``None`` sat behind a queue of
file watchers. The rule is mechanical, so it is checked mechanically over the
whole app, and a route that needs a genuinely blocking dependency names it in
the allowlist below with the reason.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from tests._suite_app import app

#: Dependencies that block on purpose and so belong on a worker thread.
#: Empty today: every dependency the app registers is a coroutine.
BLOCKING_BY_DESIGN: frozenset[str] = frozenset()


def _dependants(root: Dependant) -> Iterator[Dependant]:
    for dep in root.dependencies:
        yield dep
        yield from _dependants(dep)


def _runs_on_the_loop(call: Any) -> bool:
    target = call
    if not inspect.isfunction(target) and not inspect.ismethod(target):
        # A callable instance: FastAPI inspects its ``__call__``.
        target = type(call).__call__
    return inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target)


def _route_id(route: APIRoute | APIWebSocketRoute) -> str:
    if isinstance(route, APIWebSocketRoute):
        return f"WS {route.path}"
    return f"{','.join(sorted(route.methods or ()))} {route.path}"


def _routes() -> list[APIRoute | APIWebSocketRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute | APIWebSocketRoute)]


def test_the_app_registers_routes() -> None:
    assert len(_routes()) > 50


@pytest.mark.parametrize(
    "route",
    _routes(),
    ids=lambda r: _route_id(r),
)
def test_every_dependency_on_the_route_is_a_coroutine(route: APIRoute | APIWebSocketRoute) -> None:
    threaded = sorted(
        {
            getattr(dep.call, "__qualname__", repr(dep.call))
            for dep in _dependants(route.dependant)
            if dep.call is not None
            and not _runs_on_the_loop(dep.call)
            and getattr(dep.call, "__qualname__", "") not in BLOCKING_BY_DESIGN
        }
    )
    assert threaded == [], (
        f"{route.path} resolves these dependencies on a worker thread: {threaded}; "
        "make them `async def`, or name them in BLOCKING_BY_DESIGN with the reason"
    )
