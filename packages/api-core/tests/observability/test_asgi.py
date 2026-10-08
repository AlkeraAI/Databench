"""End-to-end FastAPI glue: canonical envelope, trace-id propagation + header,
generic 500 (no leak), and validation errors that never echo the input value."""

from __future__ import annotations

import httpx
import pytest
from alkera_core.config import AppEnv
from alkera_core.observability import asgi as asgi_module
from alkera_core.observability.asgi import (
    RequestContextMiddleware,
    install_exception_handlers,
    setup_observability,
)
from alkera_core.observability.errors import (
    ConflictError,
    IntegrationNotConfiguredError,
    UpstreamServiceError,
)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from structlog.testing import capture_logs


class _Body(BaseModel):
    secret_code: int


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)

    @app.get("/boom-alkera")
    async def boom_alkera() -> None:
        raise ConflictError("a user with that email already exists")

    @app.get("/boom-http")
    async def boom_http() -> None:
        raise HTTPException(status_code=404, detail="missing thing")

    @app.get("/boom-dict")
    async def boom_dict() -> None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "personal_email_blocked",
                "message": "use your work email",
                "domain": "gmail.com",
            },
        )

    @app.get("/boom-500")
    async def boom_500() -> None:
        raise RuntimeError("internal detail xyz that must not leak")

    @app.post("/validate")
    async def validate(body: _Body) -> dict[str, int]:
        return {"ok": body.secret_code}

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_alkera_error_renders_envelope() -> None:
    async with _client(_make_app()) as client:
        resp = await client.get("/boom-alkera")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "conflict"
    assert body["error"]["message"] == "a user with that email already exists"
    assert body["error"]["trace_id"] == resp.headers["x-trace-id"]


async def test_incoming_request_id_becomes_trace_id() -> None:
    async with _client(_make_app()) as client:
        resp = await client.get("/boom-alkera", headers={"X-Request-Id": "trace-from-caller"})
    assert resp.headers["x-trace-id"] == "trace-from-caller"
    assert resp.json()["error"]["trace_id"] == "trace-from-caller"


async def test_http_exception_maps_to_envelope() -> None:
    async with _client(_make_app()) as client:
        resp = await client.get("/boom-http")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert resp.json()["error"]["message"] == "missing thing"


async def test_dict_detail_becomes_code_and_details() -> None:
    async with _client(_make_app()) as client:
        resp = await client.get("/boom-dict")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "personal_email_blocked"
    assert body["error"]["message"] == "use your work email"
    assert body["error"]["details"] == {"domain": "gmail.com"}


async def test_unhandled_exception_is_generic_500_without_leak() -> None:
    async with _client(_make_app()) as client:
        resp = await client.get("/boom-500")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal_error"
    assert "internal detail xyz" not in resp.text
    assert "x-trace-id" in resp.headers
    assert body["error"]["trace_id"] == resp.headers["x-trace-id"]


def _five_hundreds_app() -> FastAPI:
    """Two 5xx that differ only in whether anyone can act on them."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)

    @app.get("/not-configured")
    async def not_configured() -> None:
        raise IntegrationNotConfiguredError(
            "GitHub integration is not configured on this deployment.",
            details={"integration": "github"},
        )

    @app.get("/upstream-down")
    async def upstream_down() -> None:
        raise UpstreamServiceError("provider returned 500 for arn:secret")

    return app


async def test_a_deliberate_5xx_keeps_its_own_answer_and_pages_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability nobody configured is a setting to fix, not an outage.

    It answers with its own code and message — "an unexpected error occurred"
    sends the one person who can fix it looking for a crash — and it does not
    raise a Sentry event, because an alert nobody can act on is what trains an
    on-call to ignore the channel.
    """
    captured: list[BaseException] = []
    monkeypatch.setattr(
        asgi_module, "capture_exception", lambda exc, **_kw: captured.append(exc) or "evt"
    )
    logged: list[dict[str, object]] = []
    with capture_logs() as events:
        async with _client(_five_hundreds_app()) as client:
            resp = await client.get("/not-configured")
        logged.extend(events)

    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["code"] == "integration_not_configured"
    assert body["message"] == "GitHub integration is not configured on this deployment."
    assert body["details"] == {"integration": "github"}
    assert captured == []
    levels = {e["log_level"] for e in logged if e.get("event") == "request.alkera_error"}
    assert levels == {"warning"}


async def test_an_ordinary_5xx_is_still_generic_and_still_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asymmetric half: nothing else gets the deliberate treatment — an
    upstream failure keeps its generic message and still pages."""
    captured: list[BaseException] = []
    monkeypatch.setattr(
        asgi_module, "capture_exception", lambda exc, **_kw: captured.append(exc) or "evt"
    )
    logged: list[dict[str, object]] = []
    with capture_logs() as events:
        async with _client(_five_hundreds_app()) as client:
            resp = await client.get("/upstream-down")
        logged.extend(events)

    assert resp.status_code == 502
    assert "arn:secret" not in resp.text
    assert len(captured) == 1
    levels = {e["log_level"] for e in logged if e.get("event") == "request.alkera_error"}
    assert levels == {"error"}


async def test_validation_error_does_not_echo_input_value() -> None:
    async with _client(_make_app()) as client:
        resp = await client.post("/validate", json={"secret_code": "leak-me-please"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["errors"]  # has at least one entry with type/loc/msg
    # The offending input value must NOT be reflected back.
    assert "leak-me-please" not in resp.text


async def test_success_response_carries_trace_header() -> None:
    async with _client(_make_app()) as client:
        resp = await client.get("/ok")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert resp.headers["x-trace-id"]


@pytest.mark.parametrize("path", ["/boom-alkera", "/boom-http", "/boom-500", "/ok"])
async def test_every_response_has_a_trace_header(path: str) -> None:
    async with _client(_make_app()) as client:
        resp = await client.get(path)
    assert resp.headers.get("x-trace-id")


def _capture_only_app() -> FastAPI:
    """A gateway-style app: only the catch-all 500 handler, 4xx left untouched."""
    app = FastAPI()
    setup_observability(app, component="gateway-test", full_error_envelope=False)

    @app.get("/not-there")
    async def not_there() -> None:
        raise HTTPException(status_code=404, detail="nope")

    return app


async def test_capture_only_leaves_4xx_unshaped() -> None:
    async with _client(_capture_only_app()) as client:
        resp = await client.get("/not-there")
    # 4xx keeps FastAPI's default {detail} shape (provider-compatible), NOT the
    # envelope — the gateway needs this so LLM SDK clients parse its errors.
    assert resp.status_code == 404
    assert resp.json() == {"detail": "nope"}
    assert resp.headers["x-trace-id"]
    # NB: the catch-all 500 in capture-only mode reuses the same
    # `_handle_unexpected_error` proven by test_unhandled_exception_* above.


# --- /metrics scrape credential -------------------------------------------------
#
# The deployed apps sit behind a load balancer that routes on Host alone, so an
# anonymous /metrics is internet-reachable: it discloses request/error counts,
# latency, restart times and the exact CPython version. Anonymous scrape is
# allowed only in local dev; a configured token is required everywhere.

_SCRAPE_TOKEN = "scrape-token-9f4b2c7e1a"


@pytest.fixture
def metrics_config(monkeypatch: pytest.MonkeyPatch):
    """Configure the metrics surface, then build an app that wires it up.

    The route is registered at `setup_observability` time, so the settings have
    to be in place before the app is built.
    """

    def _build(
        *, app_env: AppEnv = "production", token: str | None = None, enabled: bool = True
    ) -> FastAPI:
        monkeypatch.setattr(asgi_module.settings, "app_env", app_env)
        monkeypatch.setattr(asgi_module.settings, "metrics_auth_token", token)
        monkeypatch.setattr(asgi_module.settings, "metrics_enabled", enabled)
        app = FastAPI()
        setup_observability(app, component="metrics-test", full_error_envelope=False)
        return app

    return _build


@pytest.mark.parametrize(
    # Empty == unset, the same reading `_validate_production` takes: the
    # alkera/<env> Secrets Manager envelope seeds every key as "", so that is the
    # shape "no scrape credential" actually arrives in on a deployed task.
    "token",
    [pytest.param(None, id="token-unset"), pytest.param("", id="token-empty")],
)
@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        pytest.param("local", 200, id="local-dev-scrape-stays-anonymous"),
        pytest.param("staging", 404, id="staging-refuses-anonymous"),
        pytest.param("production", 404, id="production-refuses-anonymous"),
    ],
)
async def test_anonymous_scrape_is_local_only(metrics_config, app_env, expected, token) -> None:
    app = metrics_config(app_env=app_env, token=token)
    async with _client(app) as client:
        resp = await client.get("/metrics")
    assert resp.status_code == expected
    if expected == 404:
        assert "alkera_http_requests_total" not in resp.text
        assert "python_info" not in resp.text


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(None, id="no-authorization-header"),
        pytest.param("Bearer ", id="empty-bearer-value"),
        pytest.param("Bearer anything", id="any-bearer-value"),
    ],
)
async def test_an_empty_token_is_never_a_usable_credential(metrics_config, header) -> None:
    """Fails CLOSED, so `""` can't be brute-forced into an open scrape: outside
    local an empty credential serves nothing, no matter what the caller presents —
    in particular `Authorization: Bearer ` must not compare equal to it."""
    app = metrics_config(app_env="production", token="")
    headers = {} if header is None else {"Authorization": header}
    async with _client(app) as client:
        resp = await client.get("/metrics", headers=headers)
    assert resp.status_code == 404
    assert "alkera_http_requests_total" not in resp.text


@pytest.mark.parametrize(
    "app_env",
    [pytest.param("local", id="local"), pytest.param("production", id="production")],
)
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param(None, 404, id="no-authorization-header"),
        pytest.param("", 404, id="empty-authorization-header"),
        pytest.param(f"Bearer {_SCRAPE_TOKEN}", 200, id="correct-bearer-token"),
        pytest.param(f"bearer {_SCRAPE_TOKEN}", 200, id="scheme-is-case-insensitive"),
        pytest.param(f"Bearer  {_SCRAPE_TOKEN} ", 200, id="surrounding-whitespace-tolerated"),
        pytest.param(f"Bearer {_SCRAPE_TOKEN}x", 404, id="token-with-a-suffix"),
        pytest.param(f"Bearer {_SCRAPE_TOKEN[:-1]}", 404, id="prefix-of-the-token"),
        pytest.param(_SCRAPE_TOKEN, 404, id="raw-token-without-the-scheme"),
        pytest.param(f"Basic {_SCRAPE_TOKEN}", 404, id="wrong-scheme"),
        pytest.param("Bearer ", 404, id="empty-bearer-value"),
        # Raw bytes: a header value is latin-1 on the wire, so the handler sees a
        # non-ASCII str — `hmac.compare_digest` rejects those as str, and a 500
        # here would be an oracle of its own.
        pytest.param(b"Bearer \xc3\xbc\xc3\xb1", 404, id="non-ascii-credential-does-not-blow-up"),
    ],
)
async def test_configured_token_is_enforced_everywhere(
    metrics_config, app_env, header, expected
) -> None:
    """A configured token is required in EVERY environment — local included, so a
    dev never proves the guard against a surface that is open anyway."""
    app = metrics_config(app_env=app_env, token=_SCRAPE_TOKEN)
    headers = {} if header is None else {"Authorization": header}
    async with _client(app) as client:
        resp = await client.get("/metrics", headers=headers)
    assert resp.status_code == expected


async def test_authorized_scrape_serves_the_prometheus_exposition(metrics_config) -> None:
    app = metrics_config(app_env="production", token=_SCRAPE_TOKEN)

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    async with _client(app) as client:
        await client.get("/ok")  # record at least one request
        resp = await client.get("/metrics", headers={"Authorization": f"Bearer {_SCRAPE_TOKEN}"})
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "alkera_http_requests_total" in resp.text
    assert 'component="metrics-test"' in resp.text


@pytest.mark.parametrize(
    "app_env",
    [pytest.param("local", id="local"), pytest.param("production", id="production")],
)
async def test_metrics_disabled_registers_no_route(metrics_config, app_env) -> None:
    """METRICS_ENABLED=false wins over a configured token: no endpoint at all."""
    app = metrics_config(app_env=app_env, token=_SCRAPE_TOKEN, enabled=False)
    async with _client(app) as client:
        resp = await client.get("/metrics", headers={"Authorization": f"Bearer {_SCRAPE_TOKEN}"})
    assert resp.status_code == 404
    assert "alkera_http_requests_total" not in resp.text


def _provider_refusals_app() -> FastAPI:
    """5xx HTTPExceptions shaped as the machine console raises them."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)

    @app.get("/provider-refused")
    async def provider_refused() -> None:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "provider_error",
                "message": "The provider refused: EC2 create_secret failed: ExpiredTokenException",
                "internal": "kept off the wire",
            },
        )

    @app.get("/provider-refused-with-key")
    async def provider_refused_with_key() -> None:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "provider_error",
                "message": (
                    "The provider refused: Bearer abcDEF123456789xyz and AKIAABCDEFGHIJKLMNOP"
                ),
            },
        )

    @app.get("/other-code")
    async def other_code() -> None:
        raise HTTPException(
            status_code=502, detail={"code": "not_provider", "message": "internal words"}
        )

    @app.get("/plain-string")
    async def plain_string() -> None:
        raise HTTPException(status_code=502, detail="provider_error")

    return app


async def test_a_provider_refusal_reaches_the_admin_in_its_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one 5xx whose message is the answer: a compute provider refused, and
    the admin can only act on the provider's reason. It still pages."""
    captured: list[BaseException] = []
    monkeypatch.setattr(
        asgi_module, "capture_exception", lambda exc, **_kw: captured.append(exc) or "evt"
    )
    async with _client(_provider_refusals_app()) as client:
        resp = await client.get("/provider-refused")
    assert resp.status_code == 502
    body = resp.json()["error"]
    assert body["code"] == "provider_error"
    assert body["message"] == (
        "The provider refused: EC2 create_secret failed: ExpiredTokenException"
    )
    assert "details" not in body or body["details"] is None
    assert "kept off the wire" not in resp.text
    assert len(captured) == 1


async def test_a_provider_refusal_never_carries_a_credential() -> None:
    async with _client(_provider_refusals_app()) as client:
        resp = await client.get("/provider-refused-with-key")
    body = resp.json()["error"]
    assert body["code"] == "provider_error"
    assert "abcDEF123456789xyz" not in resp.text
    assert "AKIAABCDEFGHIJKLMNOP" not in resp.text
    assert body["message"].startswith("The provider refused:")


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/other-code", id="another-code-stays-opaque"),
        pytest.param("/plain-string", id="a-string-detail-is-not-a-code"),
    ],
)
async def test_every_other_5xx_detail_stays_generic(path: str) -> None:
    async with _client(_provider_refusals_app()) as client:
        resp = await client.get(path)
    assert resp.status_code == 502
    body = resp.json()["error"]
    assert body["code"] == "internal_error"
    assert "internal words" not in resp.text
