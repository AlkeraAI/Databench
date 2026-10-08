"""The boundary scan answers for itself — it never becomes the failure it removes.

`StorableTextMiddleware` fronts every request of both apps, which is what makes
it worth having and also what makes any 5xx of its own worse than the bug it
was written for: it fires before routing, before authentication and before the
rate limiter, so anything it raises is an unauthenticated crash on a path that
need not even exist.

These cases drive the middleware as the apps wire it — the app's own CORS layer
outside it, a route that reads the raw body inside it — and pin what it must do
with input it cannot make sense of: refuse with a code, or stand aside and let
the route answer. Never raise.
"""

from __future__ import annotations

import gzip
import json
import tracemalloc
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from alkera_core.observability import asgi as asgi_module
from alkera_core.observability.asgi import setup_observability
from alkera_core.validation.storable_text import (
    CHARACTER_REASONS,
    MAX_SCAN_DEPTH,
    Reason,
    SelfValidated,
)
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

ORIGIN = "http://localhost:5173"


def _make_app(*, full_error_envelope: bool = True, own: Sequence[SelfValidated] = ()) -> FastAPI:
    """An app wired the way the real ones are.

    The backend adds CORS and its body limit BEFORE `setup_observability`, so a
    middleware the wiring adds afterwards sits outside them unless it asks not
    to — which is exactly what a browser-visible refusal depends on.
    """
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    setup_observability(
        app,
        component="scan-test",
        full_error_envelope=full_error_envelope,
        self_validated=own,
    )

    @app.post("/own/words/{name}")
    async def own_words(name: str, request: Request) -> JSONResponse:
        """A surface that refuses hostile text in its own vocabulary."""
        body = await request.json()
        if "\x00" in name or "\x00" in str(body.get("name", "")):
            return JSONResponse({"code": "surface.invalid_name.nul"}, status_code=422)
        return JSONResponse({"read": name})

    @app.post("/own/wordsmith")
    async def wordsmith(request: Request) -> JSONResponse:
        """A sibling that only LOOKS like it is under the exempt prefix."""
        await request.json()
        return JSONResponse({"read": "sibling"})

    @app.post("/echo")
    async def echo(request: Request) -> JSONResponse:
        """Reads the body the way the gateway does: its own parser answers a
        body it cannot read, in its own vocabulary."""
        raw = await request.body()
        try:
            await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be valid JSON"}, status_code=400)
        return JSONResponse({"read": len(raw)})

    @app.get("/past-the-boundary")
    async def past_the_boundary() -> None:
        """A value the scan never saw, refused where it always was — by the
        driver. The cause chain is built by hand because this package has no
        database; that the REAL chain looks like this is pinned against real
        Postgres in `apps/backend/tests/test_unstorable_text_surface.py`.
        """
        try:
            raise _DriverRefusalError("character not in repertoire")
        except _DriverRefusalError as cause:
            raise RuntimeError("statement failed") from cause

    return app


class _DriverRefusalError(Exception):
    """What asyncpg raises for a NUL, as far as the chain walk can see."""

    sqlstate = "22021"


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _nested(depth: int, leaf: str) -> bytes:
    """A JSON body `depth` levels deep with `leaf` at the bottom."""
    return ('{"a":' * depth + f'"{leaf}"' + "}" * depth).encode()


_JSON = {"content-type": "application/json"}


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(MAX_SCAN_DEPTH + 1, id="one-past-the-bound"),
        pytest.param(1000, id="past-the-parsers-own-budget"),
        pytest.param(20_000, id="far-past-everything"),
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/echo", id="a-real-route"),
        pytest.param("/no-such-route", id="a-missing-route"),
    ],
)
async def test_a_body_nested_past_the_bound_is_refused_with_a_code(depth: int, path: str) -> None:
    """A body too deep to parse is the one input the scan cannot check.

    It has to say so: refusing with a code is an answer a client can act on,
    while raising is an unauthenticated 500 — on ANY path in the app, a missing
    one included, because this runs before routing. That is the shape of
    failure the middleware exists to remove, on a 2 KB request with no
    credentials.
    """
    async with _client(_make_app()) as client:
        resp = await client.post(path, content=_nested(depth, "a\\u0000b"), headers=_JSON)
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == {"field": "body", "reason": "excessive_nesting"}


async def test_a_deep_body_the_scan_skips_still_reaches_the_route() -> None:
    """The same depth without a suspect escape is none of the scan's business,
    so it is handed on exactly as before — the refusal is about what the body
    carries, not how it is shaped."""
    body = _nested(1000, "ordinary")
    async with _client(_make_app()) as client:
        resp = await client.post("/echo", content=body, headers=_JSON)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"read": len(body)}


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        pytest.param(b"\x1f\x8b\x08\x00garbage", "gzip-without-a-header", id="binary-garbage"),
        pytest.param(b'{"model": "m"\x00', "truncated", id="truncated-with-a-nul"),
        pytest.param(b'{"a": "\xff\xfe\x00"}', "not-utf8", id="invalid-utf8"),
    ],
)
async def test_a_body_that_is_not_json_is_left_to_the_route(payload: bytes, label: str) -> None:
    """A body the scan cannot parse is not the scan's to refuse.

    Its own vocabulary ("body must be valid JSON") is what the client — an LLM
    SDK on the gateway, a browser on the API — is written against; answering
    "remove the null characters" for a body that was merely compressed replaces
    a true diagnosis with a misleading one.
    """
    async with _client(_make_app()) as client:
        resp = await client.post("/echo", content=payload, headers=_JSON)
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"error": "body must be valid JSON"}


@pytest.mark.parametrize(
    ("plain", "field", "reason"),
    [
        pytest.param(b'{"title": "a\\u0000b"}', "body.title", "null_character", id="nul"),
        pytest.param(
            b'{"title": "a\\ud800b"}', "body.title", "unpaired_surrogate", id="lone-surrogate"
        ),
        pytest.param(
            _nested(MAX_SCAN_DEPTH + 1, "a\\u0000b"), "body", "excessive_nesting", id="too-deep"
        ),
    ],
)
async def test_a_gzipped_body_earns_the_refusal_its_plain_body_earns(
    plain: bytes, field: str, reason: str
) -> None:
    """Compressing a body is no way past the scan: it reads the decoded JSON
    and answers exactly what the same body sent plain is answered."""
    async with _client(_make_app()) as client:
        refused_plain = await client.post("/echo", content=plain, headers=_JSON)
        refused_gzip = await client.post(
            "/echo",
            content=gzip.compress(plain, mtime=0),
            headers={**_JSON, "content-encoding": "gzip"},
        )
    assert refused_plain.status_code == 422, refused_plain.text
    assert refused_gzip.status_code == 422, refused_gzip.text
    assert refused_plain.json()["error"]["details"] == {"field": field, "reason": reason}
    assert refused_gzip.json()["error"]["details"] == {"field": field, "reason": reason}
    assert refused_gzip.json()["error"]["code"] == refused_plain.json()["error"]["code"]


async def test_brackets_in_the_compressed_bytes_are_not_read_as_structure() -> None:
    """A shallow body gzipped is judged on what it decodes to, not on the
    bytes the compressor emitted. Those bytes hold NULs and hundreds of
    brackets, so reading them as JSON would refuse the body as too deep."""
    plain = json.dumps({"entries": [{"path": f"f{i}.txt"} for i in range(2001)]}).encode()
    packed = gzip.compress(plain, mtime=0)
    assert b"\x00" in packed
    async with _client(_make_app()) as client:
        resp = await client.post(
            "/echo", content=packed, headers={**_JSON, "content-encoding": "gzip"}
        )
    # The route reads the bytes as they arrived, so its own parser answers.
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"error": "body must be valid JSON"}


async def test_a_gzipped_body_that_decodes_past_the_ceiling_is_handed_on_unscanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decode is held to the scan ceiling, so a small body that inflates
    far past it costs the scan the ceiling and no more. Past it the body is
    treated as a plain body past it is: handed on to the route."""
    monkeypatch.setattr(asgi_module.settings, "request_scan_max_body_bytes", 4096)
    plain = f'{{"pad": "{"x" * (1 << 20)}", "title": "a\\u0000b"}}'.encode()
    packed = gzip.compress(plain, mtime=0)
    assert len(packed) < 4096
    async with _client(_make_app()) as client:
        resp = await client.post(
            "/echo", content=packed, headers={**_JSON, "content-encoding": "gzip"}
        )
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"error": "body must be valid JSON"}


async def test_a_gzip_bomb_costs_the_scan_the_ceiling_not_the_bomb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant behind the case above: the decode stops one byte past
    the ceiling. 64 MiB packed into a few kilobytes must not be inflated to
    find out it was too large."""
    monkeypatch.setattr(asgi_module.settings, "request_scan_max_body_bytes", 64 * 1024)
    packed = gzip.compress(b'{"pad": "' + b"x" * (64 << 20) + b'"}', mtime=0)
    assert len(packed) < 64 * 1024
    app = _make_app()
    tracemalloc.start()
    try:
        async with _client(app) as client:
            resp = await client.post(
                "/echo", content=packed, headers={**_JSON, "content-encoding": "gzip"}
            )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert resp.status_code == 400, resp.text
    assert peak < 16 << 20, f"the scan held {peak} bytes decoding a 64 MiB bomb"


@pytest.mark.parametrize(
    ("encoding", "payload", "status"),
    [
        pytest.param(
            "br", _nested(MAX_SCAN_DEPTH + 1, "a\\u0000b"), 200, id="a-coding-not-decoded"
        ),
        pytest.param("gzip", b"\x1f\x8b\x08\x00" + b"[" * 300 + b"\x00", 400, id="broken-gzip"),
    ],
)
async def test_a_body_the_scan_cannot_decode_is_left_to_the_route(
    encoding: str, payload: bytes, status: int
) -> None:
    """Bytes the scan cannot turn into the JSON the type names are not its to
    judge. Read as JSON, each of these would be refused as too deep; the
    status here is the test route's own answer to the raw bytes."""
    async with _client(_make_app()) as client:
        resp = await client.post(
            "/echo", content=payload, headers={**_JSON, "content-encoding": encoding}
        )
    assert resp.status_code == status, resp.text


async def test_a_refusal_carries_the_headers_a_browser_needs_to_read_it() -> None:
    """A cross-origin caller has to be able to READ the refusal.

    A response rendered outside the app's CORS layer carries no
    `Access-Control-Allow-Origin`, so the browser hands the client an opaque
    network failure instead of the coded 422 — the client cannot tell a refused
    field from a dead server.
    """
    async with _client(_make_app()) as client:
        resp = await client.post(
            "/echo", content=b'{"title": "a\\u0000b"}', headers={**_JSON, "origin": ORIGIN}
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unstorable_text"
    assert resp.headers.get("access-control-allow-origin") == ORIGIN


async def test_a_preflight_is_still_answered_by_the_cors_layer() -> None:
    """The scan sits inside CORS, so a preflight never reaches it."""
    async with _client(_make_app()) as client:
        resp = await client.options(
            "/echo",
            headers={
                "origin": ORIGIN,
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("access-control-allow-origin") == ORIGIN


async def test_the_gateway_renders_the_refusal_provider_shaped() -> None:
    """The gateway's 4xx bodies are parsed by LLM SDK clients, so its refusal
    keeps their shape instead of the house envelope."""
    async with _client(_make_app(full_error_envelope=False)) as client:
        resp = await client.post("/echo", content=b'{"model": "m\\u0000x"}', headers=_JSON)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["field"] == "body.model"
    assert "error" in body and isinstance(body["error"], str)


@pytest.mark.parametrize(
    ("envelope", "status", "shape"),
    [
        pytest.param(True, 422, "error.code", id="the-house-envelope"),
        pytest.param(False, 400, "field", id="provider-shaped"),
    ],
)
async def test_a_value_refused_past_the_boundary_keeps_this_apps_shape(
    envelope: bool, status: int, shape: str
) -> None:
    """The driver-level refusal is the same answer, wherever it is noticed.

    It covers what the boundary cannot see — a value a service assembled, a
    body too large to scan — and it used to render the house envelope on both
    apps, which on the gateway is a 4xx body its clients cannot parse.
    """
    async with _client(_make_app(full_error_envelope=envelope)) as client:
        resp = await client.get("/past-the-boundary")
    assert resp.status_code == status, resp.text
    body = resp.json()
    if shape == "field":
        assert body["field"] == "request"
        assert isinstance(body["error"], str)
    else:
        assert body["error"]["code"] == "unstorable_text"


async def test_a_body_above_the_operator_ceiling_is_streamed_not_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling is a memory bound, read at wiring time from the setting.

    Past it the scan stands aside — the driver-level net behind it is what
    covers a body too big to hold — and the route still gets every byte.
    """
    monkeypatch.setattr(asgi_module.settings, "request_scan_max_body_bytes", 4096)
    app = _make_app()
    filler = "x" * 8192
    body = f'{{"pad": "{filler}", "title": "a\\u0000b"}}'.encode()
    async with _client(app) as client:
        resp = await client.post("/echo", content=body, headers=_JSON)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"read": len(body)}
    # …and the same body under the ceiling IS scanned, so the case above is the
    # ceiling doing its job rather than the scan being off.
    monkeypatch.setattr(asgi_module.settings, "request_scan_max_body_bytes", 1024 * 1024)
    async with _client(_make_app()) as client:
        refused = await client.post("/echo", content=body, headers=_JSON)
    assert refused.status_code == 422, refused.text


async def test_a_body_longer_than_it_declares_is_not_buffered_past_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling is enforced on the bytes that arrive, not on the number the
    caller wrote in a header — an in-process ASGI caller (the daemon bridge, a
    test) writes both by hand."""
    monkeypatch.setattr(asgi_module.settings, "request_scan_max_body_bytes", 4096)
    app = _make_app()
    oversized = b'{"title": "a\\u0000b", "pad": "' + b"x" * 200_000 + b'"}'
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": oversized, "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", b"12"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200, sent


async def test_a_surface_that_answers_for_itself_is_left_to_answer() -> None:
    """A precise refusal beats a generic one.

    Files names a null byte in a name `files.invalid_name.nul` in its own flat
    envelope, and the sentence its client shows is keyed off that code; the
    content plane answers every unresolvable path with one opaque 404 so it
    cannot be read as an oracle. Answering first would replace both with a
    refusal about characters in an envelope neither client parses.
    """
    async with _client(_make_app(own=[SelfValidated.whole("/own/words")])) as client:
        refused = await client.post(
            "/own/words/a%00b", content=b'{"name": "a\\u0000b"}', headers=_JSON
        )
    assert refused.status_code == 422, refused.text
    assert refused.json() == {"code": "surface.invalid_name.nul"}


async def test_an_exemption_covers_whole_segments_only() -> None:
    """A prefix must not capture a sibling that merely starts the same way, or
    exempting `/api/v1/files` would quietly exempt `/api/v1/filesystem` too."""
    async with _client(_make_app(own=[SelfValidated.whole("/own/words")])) as client:
        resp = await client.post("/own/wordsmith", content=b'{"a": "\\u0000"}', headers=_JSON)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unstorable_text"


async def test_nothing_is_exempt_unless_the_wiring_says_so() -> None:
    """The same request on the same route, with no exemption declared."""
    async with _client(_make_app()) as client:
        resp = await client.post(
            "/own/words/a%00b", content=b'{"name": "a\\u0000b"}', headers=_JSON
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unstorable_text"


#: A surface that validates the names it stores and the ids in its path, and
#: nothing else about its requests.
_NAMES_AND_PATH = SelfValidated(
    "/own/words",
    path=True,
    body_fields={
        "body.name": frozenset({Reason.NUL}),
        "body.items[].name": frozenset({Reason.NUL}),
        "body.report": CHARACTER_REASONS,
    },
)


@pytest.mark.parametrize(
    ("path", "body", "status", "answer"),
    [
        pytest.param(
            "/own/words/plain",
            b'{"name": "a\\u0000b"}',
            422,
            {"code": "surface.invalid_name.nul"},
            id="a-declared-field-is-the-surfaces-to-refuse",
        ),
        pytest.param(
            "/own/words/a%00b",
            b'{"name": "plain"}',
            422,
            {"code": "surface.invalid_name.nul"},
            id="a-declared-path-is-the-surfaces-to-refuse",
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "plain", "items": [{"name": "ok"}, {"name": "a\\u0000b"}]}',
            200,
            {"read": "plain"},
            id="a-declared-field-inside-a-list-reaches-the-route",
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "plain", "report": {"\\ud800": ["\\u0000"]}}',
            200,
            {"read": "plain"},
            id="everything-under-a-declared-container-reaches-the-route",
        ),
    ],
)
async def test_the_scan_defers_on_what_a_surface_declares(
    path: str, body: bytes, status: int, answer: dict[str, str]
) -> None:
    async with _client(_make_app(own=[_NAMES_AND_PATH])) as client:
        resp = await client.post(path, content=body, headers=_JSON)
    assert resp.status_code == status, resp.text
    assert resp.json() == answer


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        pytest.param(
            "/own/words/plain", b'{"name": "plain", "note": "a\\u0000b"}', "body.note", id="field"
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "a\\u0000b", "note": "a\\u0000b"}',
            "body.note",
            id="field-beside-a-declared-one",
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "plain", "meta": {"name": "a\\u0000b"}}',
            "body.meta.name",
            id="same-key-somewhere-else",
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "plain", "items": [{"name": "ok", "id": "a\\ud800b"}]}',
            "body.items[0].id",
            id="sibling-of-a-declared-list-field",
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "plain", "a\\u0000b": 1}',
            "body.<key>",
            id="key",
        ),
        pytest.param(
            "/own/words/plain?q=a%00b", b'{"name": "plain"}', "query.q", id="query-string"
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "a\\ud800b"}',
            "body.name",
            id="a-reason-the-declared-field-does-not-name",
        ),
        pytest.param(
            "/own/words/plain",
            b'{"name": "a\\u0000\\ud800b"}',
            "body.name",
            id="an-unnamed-reason-beside-a-named-one",
        ),
    ],
)
async def test_the_rest_of_a_declared_surfaces_request_is_still_scanned(
    path: str, body: bytes, field: str
) -> None:
    """Declaring some fields is not an exemption: the same surface that keeps
    its own answer for a name is refused at the boundary for everything it did
    not say it validates."""
    async with _client(_make_app(own=[_NAMES_AND_PATH])) as client:
        resp = await client.post(path, content=body, headers=_JSON)
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "unstorable_text"
    assert error["details"]["field"] == field


async def test_a_declaration_reaches_no_further_than_its_surface() -> None:
    """The fields one surface declares mean nothing on another route."""
    async with _client(_make_app(own=[_NAMES_AND_PATH])) as client:
        resp = await client.post("/echo", content=b'{"name": "a\\u0000b"}', headers=_JSON)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["field"] == "body.name"


async def test_a_failure_inside_the_scan_does_not_fail_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan is a guard, not the authority.

    Behind it the driver-level net still refuses text Postgres cannot store, so
    a guard that cannot answer must stand aside rather than turn every request
    in the deployment into a 500 — the failure mode it was written to remove.
    """

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise MemoryError("the scanner fell over")

    monkeypatch.setattr(asgi_module, "scan_query_string", _explode)
    async with _client(_make_app()) as client:
        resp = await client.post("/echo?type=chat", content=b'{"ok": 1}', headers=_JSON)
    assert resp.status_code == 200, resp.text


async def test_a_websocket_scope_is_handed_straight_on() -> None:
    """Only HTTP carries the surfaces this scans; a websocket handshake must
    not be touched."""
    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware = asgi_module.StorableTextMiddleware(
        inner, render=asgi_module._unstorable_envelope, max_scan_bytes=1024
    )

    async def receive() -> Message:  # pragma: no cover - never called
        raise AssertionError("a websocket scope must not be read")

    async def send(message: Message) -> None:  # pragma: no cover - never called
        raise AssertionError("a websocket scope must not be answered")

    await middleware({"type": "websocket", "path": "/ws/\x00"}, receive, send)
    assert len(seen) == 1
