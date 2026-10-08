"""The per-request access log must never carry a live credential.

The emailed password-reset and email-verification links put a single-use
account-takeover token in the request path itself, and the access log records
the path of every request. Anyone with read access to the log sink could
otherwise take over the account before the token expires.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from alkera_core.observability import asgi as asgi_module
from alkera_core.observability.asgi import RequestContextMiddleware
from alkera_core.observability.redaction import REDACTED
from fastapi import FastAPI

TOKEN = "kQ3v_9Zx1LmN-tRs7Yb2Wc4Ee6Gg8Ii0Kk2Mm4Oo6Qq8Ss0Uu2Ww4Yy6Aa8Cc0Ee2G"


class _RecordingLogger:
    """Stands in for the module logger so the emitted event is inspectable.

    Substituting at the logging boundary keeps the assertion on what actually
    ships to the sink, rather than on a structlog configuration that a given
    process may or may not have installed.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.post("/api/v1/auth/password-reset/{token}")
    async def reset(token: str) -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/auth/verify-email/{token}")
    async def verify(token: str) -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/auth/password-reset/request")
    async def request_reset() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/teams")
    async def teams() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    rec = _RecordingLogger()
    monkeypatch.setattr(asgi_module, "log", rec)
    return rec


def _access_event(recorder: _RecordingLogger) -> dict[str, Any]:
    events = [payload for name, payload in recorder.events if name == "http.access"]
    assert len(events) == 1, f"expected exactly one access log, got {recorder.events!r}"
    return events[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        pytest.param("/api/v1/auth/password-reset", id="password-reset"),
        pytest.param("/api/v1/auth/verify-email", id="verify-email"),
    ],
)
async def test_a_credential_path_segment_is_not_logged(
    recorder: _RecordingLogger, route: str
) -> None:
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"{route}/{TOKEN}")

    assert resp.status_code == 200
    event = _access_event(recorder)
    assert TOKEN not in str(event)
    # The route is still identifiable — only the credential goes.
    assert event["path"] == f"{route}/{REDACTED}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/v1/auth/password-reset/request", id="sibling-of-a-credential-route"),
        pytest.param("/api/v1/teams", id="ordinary-route"),
    ],
)
async def test_ordinary_paths_are_logged_verbatim(recorder: _RecordingLogger, path: str) -> None:
    """The redaction must not cost operations the paths it is not there to hide."""
    transport = httpx.ASGITransport(app=_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        method = client.post if path.endswith("request") else client.get
        resp = await method(path)

    assert resp.status_code == 200
    assert _access_event(recorder)["path"] == path
