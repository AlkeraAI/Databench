"""Smoke tests for ``alkera_sdk.AlkeraClient``.

We exercise the wrapper against ``httpx.MockTransport`` rather than the
real FastAPI app — the backend's own test suite covers the routes
end-to-end. What this test guarantees is that the hand-written wrapper:

1. Sends correctly-shaped JSON bodies to the right URLs.
2. Persists cookies set by the server across calls (so login-then-me
   works without manual cookie wiring).
3. Decodes responses into the generated attrs models the wrapper exposes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from alkera_sdk import AlkeraClient


def _make_handler(
    *,
    on_request: list[httpx.Request] | None = None,
) -> tuple[Any, list[httpx.Request]]:
    captured: list[httpx.Request] = on_request if on_request is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/health/live":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and path == "/health/ready":
            return httpx.Response(200, json={"status": "ok", "checks": {"db": "ok"}})
        if request.method == "POST" and path == "/api/v1/auth/login":
            body = json.loads(request.content)
            assert body == {"email": "user@example.com", "password": "pw"}
            return httpx.Response(
                200,
                json={
                    "user": {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "email": "user@example.com",
                        "first_name": "Test",
                        "last_name": "User",
                        "display_name": "Test User",
                        "org_team_id": "22222222-2222-2222-2222-222222222222",
                        "platform_role": None,
                        "email_verified_at": "2026-05-01T00:00:00Z",
                        "created_at": "2026-05-01T00:00:00Z",
                    },
                    "expires_at": "2026-05-08T00:00:00Z",
                },
                headers={"set-cookie": "alkera_session=abc; Path=/; HttpOnly"},
            )
        if request.method == "GET" and path == "/api/v1/auth/me":
            assert request.headers.get("cookie") == "alkera_session=abc"
            return httpx.Response(
                200,
                json={
                    "id": "11111111-1111-1111-1111-111111111111",
                    "email": "user@example.com",
                    "first_name": "Test",
                    "last_name": "User",
                    "display_name": "Test User",
                    "org_team_id": "22222222-2222-2222-2222-222222222222",
                    "platform_role": None,
                    "email_verified_at": "2026-05-01T00:00:00Z",
                    "created_at": "2026-05-01T00:00:00Z",
                },
            )
        if request.method == "POST" and path == "/api/v1/auth/logout":
            return httpx.Response(200, json={"message": "ok"})
        return httpx.Response(404, json={"detail": f"unmatched {request.method} {path}"})

    return handler, captured


@pytest.fixture
def api() -> Iterator[tuple[AlkeraClient, list[httpx.Request]]]:
    handler, captured = _make_handler()
    transport = httpx.MockTransport(handler)
    with AlkeraClient(base_url="http://test", httpx_args={"transport": transport}) as c:
        yield c, captured


def test_health_live_parses(api: tuple[AlkeraClient, list[httpx.Request]]) -> None:
    client, _ = api
    result = client.health.live()
    assert int(result.status_code) == 200
    assert result.parsed is not None
    # The generated model exposes the field via the `status` attribute.
    assert getattr(result.parsed, "status", None) == "ok"


@pytest.mark.parametrize(
    ("strict", "sent"),
    [pytest.param(False, "false", id="default"), pytest.param(True, "true", id="strict")],
)
def test_health_ready_names_whether_it_asks_strictly(
    api: tuple[AlkeraClient, list[httpx.Request]], strict: bool, sent: str
) -> None:
    client, captured = api
    result = client.health.ready(strict=strict)
    assert int(result.status_code) == 200
    assert [r.url.params.get("strict") for r in captured] == [sent]


def test_login_then_me_carries_cookie(
    api: tuple[AlkeraClient, list[httpx.Request]],
) -> None:
    client, captured = api

    login_resp = client.auth.login(email="user@example.com", password="pw")
    assert login_resp.user.email == "user@example.com"

    me = client.auth.me()
    assert me.email == "user@example.com"
    assert str(me.id) == "11111111-1111-1111-1111-111111111111"

    me_request = next(r for r in captured if r.method == "GET" and r.url.path == "/api/v1/auth/me")
    assert me_request.headers.get("cookie") == "alkera_session=abc"


def test_logout_returns_message(api: tuple[AlkeraClient, list[httpx.Request]]) -> None:
    client, _ = api
    msg = client.auth.logout()
    assert msg.message == "ok"


@pytest.mark.parametrize(
    ("org_id", "expected"),
    [
        pytest.param(
            "22222222-2222-2222-2222-222222222222",
            "22222222-2222-2222-2222-222222222222",
            id="asserted",
        ),
        pytest.param(None, None, id="omitted"),
        pytest.param("", None, id="empty-is-omitted"),
    ],
)
def test_the_org_assertion_header_rides_every_request_only_when_named(
    org_id: str | None, expected: str | None
) -> None:
    handler, captured = _make_handler()
    with AlkeraClient(
        base_url="http://api.test",
        token="tok",
        org_id=org_id,
        httpx_args={"transport": httpx.MockTransport(handler)},
    ) as api:
        api.health.live()
    assert captured[0].headers.get("authorization") == "Bearer tok"
    assert captured[0].headers.get("x-alkera-org") == expected
