"""The path the ASGI guards judge a request by is the path the router routes on.

The CSRF guard's exemption table and the body-limit guard's guarded paths are
spelled as routes. Judged on any other spelling of the request path, a guard
can exempt or bound a request the router serves as a different route. The
expected paths come from Starlette's own ``get_route_path``, which its router
calls, so a Starlette upgrade that changes the rule fails here.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.config import settings
from backend import content_app
from backend.api import body_limit, csrf
from backend.api.route_path import routed_path
from starlette._utils import get_route_path
from starlette.types import Message, Receive, Scope, Send

pytestmark = [pytest.mark.spread]

_SCOPES = [
    pytest.param("/api", "/api", id="path-is-the-root-path"),
    pytest.param("/api/", "/api", id="root-path-and-a-slash"),
    pytest.param("/api/v1/auth/logout-all", "/api", id="under-the-root-path"),
    pytest.param("/apis/v1/x", "/api", id="shares-a-spelling-not-a-segment"),
    pytest.param("/api/v1/x", "", id="no-root-path"),
    pytest.param("/other/v1/x", "/api", id="outside-the-root-path"),
    pytest.param("/", "", id="bare-root"),
]


def _scope(path: str, root_path: str, **extra: Any) -> Scope:
    return {"type": "http", "path": path, "root_path": root_path, **extra}


@pytest.mark.parametrize(("path", "root_path"), _SCOPES)
def test_route_path_is_the_path_starlettes_router_routes_on(path: str, root_path: str) -> None:
    assert routed_path(_scope(path, root_path)) == get_route_path(_scope(path, root_path))


def test_a_request_for_the_root_path_itself_routes_on_the_empty_path() -> None:
    assert routed_path(_scope("/prefix", "/prefix")) == ""


@pytest.mark.parametrize("module", [csrf, body_limit, content_app], ids=lambda m: m.__name__)
def test_every_guard_takes_the_path_from_the_one_owner(module: object) -> None:
    """No guard keeps a copy of the rule that could drift from the router."""
    assert module.routed_path is routed_path


class _Inner:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _run(app: Any, scope: Scope) -> int:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return int(next(m for m in sent if m["type"] == "http.response.start")["status"])


@pytest.mark.parametrize(("path", "root_path"), _SCOPES)
async def test_the_csrf_guard_exempts_by_the_routers_path(
    monkeypatch: pytest.MonkeyPatch, path: str, root_path: str
) -> None:
    """A cookie-authenticated POST with no ``Origin`` is refused unless its path is
    exempt. With ``/`` in the table, only a guard that judged ``/api`` under root
    ``/api`` as ``/`` (a path the router never routes on) would let it through."""
    monkeypatch.setattr(csrf, "EXEMPT_PREFIXES", ("/",))
    scope = _scope(
        path,
        root_path,
        method="POST",
        headers=[(b"cookie", f"{settings.auth_cookie_name}=x".encode())],
    )
    inner = _Inner()

    status = await _run(csrf.CsrfOriginMiddleware(inner), scope)

    exempt = csrf.is_exempt(get_route_path(scope))
    assert inner.called is exempt
    assert status == (200 if exempt else 403)
