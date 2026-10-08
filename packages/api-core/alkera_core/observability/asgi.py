"""FastAPI/Starlette glue: request-context middleware + the shared exception
handlers that render the canonical error envelope.

This module is the ONLY part of `alkera_core.observability` that imports a web
framework. It is imported explicitly by the FastAPI apps (backend + gateway) —
never by the package `__init__`, the CLI, or the worker — so non-web consumers
don't drag Starlette into their import closure.

`RequestContextMiddleware` is a pure-ASGI middleware (NOT `BaseHTTPMiddleware`),
because the gateway streams SSE and `BaseHTTPMiddleware` is known to interfere
with streaming responses. It only wraps `send` to stamp `X-Trace-Id` and capture
the status code; it never buffers the body.
"""

from __future__ import annotations

import hmac
import time
import zlib
from collections.abc import Callable, Sequence
from typing import Any, Final, TypeVar, cast

from asyncpg import PostgresError
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from alkera_core.config import settings
from alkera_core.db.errors import classify as classify_database_error
from alkera_core.logging import get_logger
from alkera_core.observability import metrics
from alkera_core.observability.context import (
    bind_trace_id,
    clear_log_context,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
)
from alkera_core.observability.envelope import ErrorEnvelope, build_error_body, error_body_for
from alkera_core.observability.errors import GENERIC_SERVER_MESSAGE, AlkeraError, ErrorCode
from alkera_core.observability.redaction import scrub_text, scrub_url_credentials
from alkera_core.observability.security_headers import SecurityHeadersMiddleware
from alkera_core.observability.sentry import capture_exception, init_sentry
from alkera_core.validation.storable_text import (
    SelfValidated,
    Unstorable,
    reason_for,
    scan_headers,
    scan_json_body,
    scan_path,
    scan_query_string,
)

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])

log = get_logger("alkera.http")

_TRACE_HEADER = "x-trace-id"
_REQUEST_ID_HEADER = "x-request-id"
_BEARER_PREFIX = "bearer "


def setup_observability(
    app: Starlette,
    *,
    component: str,
    dsn: str | None = None,
    full_error_envelope: bool = True,
    security_headers: bool = True,
    self_validated: Sequence[SelfValidated] = (),
) -> None:
    """One-call wiring for a FastAPI app: Sentry (gated) + request context +
    security response headers + exception handling.

    `full_error_envelope=True` (the backend) installs the canonical
    `{error:{...}}` handlers for every error. The model gateway passes False: it
    emulates provider wire protocols, so its 4xx error *bodies* must stay
    provider-shaped for the LLM SDK clients — only the catch-all 500 handler is
    installed (to log + capture unexpected failures).

    `security_headers` stamps HSTS / CSP / nosniff / frame + cross-origin
    policies onto every response. Only the SPA gets these from its CDN; the API
    hosts sit behind a bare ALB, so without this they ship none. Added LAST so it
    is the outermost user middleware and covers responses the inner ones
    short-circuit (a CORS preflight, a body-limit rejection).

    `self_validated` declares, per mounted surface, the parts of a request
    that surface refuses in its own vocabulary: it already validates the text
    there, and its refusal carries a code its clients act on. The scan defers
    on exactly those parts and checks the rest of the request as it does
    everywhere else. Anything a surface misses still meets the driver-level
    refusal, so a declaration can cost a better message, never a 500."""
    init_sentry(component, dsn=dsn)
    # Refuse text Postgres cannot store before any route sees it. Appended
    # rather than added, which puts it INNERMOST of the user middleware: a
    # caller wires its CORS layer and its body limit before calling this, and
    # `add_middleware` would put the scan outside both — so a browser would get
    # an opaque network failure instead of the coded refusal, and an
    # unauthenticated body would be buffered here before the limiter that
    # exists to turn it away unread ever saw it.
    render = _unstorable_envelope if full_error_envelope else _unstorable_plain
    app.user_middleware.append(
        Middleware(
            StorableTextMiddleware,
            render=render,
            max_scan_bytes=settings.request_scan_max_body_bytes,
            self_validated=self_validated,
        )
    )
    # The catch-all reads the same renderer back off the app: a value the
    # boundary could not see still reaches the driver, and the refusal it earns
    # there has to arrive in the shape this app's clients parse.
    app.state.unstorable_render = render
    # The component is bound to THIS app's middleware (not a process global), so
    # the metrics label is correct even when several apps share one process.
    app.add_middleware(RequestContextMiddleware, component=component)
    if security_headers:
        app.add_middleware(
            SecurityHeadersMiddleware,
            # Never in local dev: the dev server is plain HTTP, and a browser
            # that has seen HSTS for `localhost` refuses http on every localhost
            # port from then on — across every project on the machine.
            hsts=not settings.is_local,
            cross_origin_resource_policy=settings.security_cross_origin_resource_policy,
        )
    if full_error_envelope:
        install_exception_handlers(app)
        if isinstance(app, FastAPI):
            document_error_envelope(app)
    else:
        app.add_exception_handler(Exception, _handle_unexpected_error)
    if settings.metrics_enabled:
        # A plain route (not an API route) so it stays out of the OpenAPI schema.
        app.add_route("/metrics", metrics_endpoint, methods=["GET"])
        if not settings.metrics_auth_token and not settings.is_local:
            log.warning(
                "metrics.scrape_disabled",
                component=component,
                app_env=settings.app_env,
                reason="no_metrics_auth_token",
            )


def _scrape_authorized(request: Request) -> bool:
    """Whether this caller may read `/metrics`.

    A configured `METRICS_AUTH_TOKEN` is always required when present, local dev
    included. With none configured, anonymous scrape is allowed ONLY in
    `APP_ENV=local`: everywhere else the app is fronted by a load balancer that
    routes on Host alone (no path filter, no reverse proxy), so an anonymous
    /metrics is internet-reachable.
    """
    token = settings.metrics_auth_token
    if not token:
        return settings.is_local
    header = request.headers.get("authorization", "")
    if header[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return False
    # Compare as bytes: `compare_digest` rejects non-ASCII str operands with a
    # TypeError, and neither side is guaranteed ASCII.
    presented = header[len(_BEARER_PREFIX) :].strip().encode("utf-8")
    return hmac.compare_digest(presented, token.encode("utf-8"))


async def metrics_endpoint(request: Request) -> Response:
    """`/metrics`, behind the scrape credential.

    An unauthorized caller gets a bare 404, not a 401: the endpoint should look
    nonexistent rather than locked (same posture as the org-usage surface), and
    the counters themselves are a business-volume oracle worth hiding.
    """
    if not _scrape_authorized(request):
        log.info("metrics.scrape_denied", path=request.url.path)
        return PlainTextResponse("Not Found", status_code=404)
    return await metrics.metrics_endpoint(request)


class RequestContextMiddleware:
    """Assigns/propagates a `trace_id`, binds it to logs, stamps the response
    header, and emits one structured access log per request."""

    def __init__(self, app: ASGIApp, component: str = "unknown") -> None:
        self.app = app
        self.component = component

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        trace_id = headers.get(_REQUEST_ID_HEADER) or headers.get(_TRACE_HEADER) or new_trace_id()
        # Persist on scope state so exception handlers can read it even after the
        # contextvar is reset (the 500 handler runs outside this middleware).
        state = cast(dict[str, Any], scope.setdefault("state", {}))
        state["trace_id"] = trace_id

        clear_log_context()
        token = bind_trace_id(trace_id)
        start = time.perf_counter()
        status_holder = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                MutableHeaders(scope=message)["X-Trace-Id"] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            duration_ms = round(elapsed * 1000, 2)
            # Reuse this single timing for the Prometheus RED metrics (no-op when
            # metrics are disabled) — no second middleware pass.
            metrics.record_request(
                component=self.component,
                method=scope.get("method", "?"),
                status=status_holder["code"],
                duration_seconds=elapsed,
            )
            client = scope.get("client")
            raw_path = scope.get("path")
            # The password-reset and email-verification links carry a live
            # single-use credential as the last path segment, so the raw path is
            # a takeover token for anyone who can read the log. Scrubbed here as
            # well as in the logging processor: this line must stay safe even in
            # a process that never called `configure_logging`.
            path = scrub_url_credentials(raw_path) if isinstance(raw_path, str) else raw_path
            log.info(
                "http.access",
                method=scope.get("method"),
                path=path,
                status=status_holder["code"],
                duration_ms=duration_ms,
                client=client[0] if client else None,
                user_id=state.get("user_id"),
                org_id=state.get("org_id"),
            )
            reset_trace_id(token)
            clear_log_context()


def _trace_id_of(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id() or new_trace_id()


#: Renders a refusal. The backend answers in the house envelope; the gateway
#: emulates provider wire protocols, so its 4xx bodies stay provider-shaped.
UnstorableRenderer = Callable[[Unstorable, str], Response]


def _unstorable_envelope(problem: Unstorable, trace_id: str) -> Response:
    body = build_error_body(
        code=problem.code,
        message=problem.message,
        trace_id=trace_id,
        status=422,
        details=problem.details(),
    )
    return _json(422, body, trace_id)


def _unstorable_plain(problem: Unstorable, trace_id: str) -> Response:
    return JSONResponse(
        {"error": problem.message, "field": problem.location},
        status_code=400,
        headers={"X-Trace-Id": trace_id},
    )


_JSON_CONTENT_TYPES: Final = ("application/json", "+json", "text/json")

#: The content codings the scan reads through. gzip is the one a route
#: accepts (the Files tree report); a body in any other coding is handed on
#: unread, and no route decodes it.
_SCANNED_CODINGS: Final = frozenset({"identity", "gzip"})


def _decoded_for_scan(body: bytes | None, coding: str, limit: int) -> bytes | None:
    """The JSON bytes the scan reads, or ``None`` when it stands aside.

    A gzip body is decoded once and never past ``limit`` bytes, so a small
    body that inflates to gigabytes costs the scan the limit and no more. A
    body that decodes past the limit is treated like a plain body past it:
    handed on unscanned, to the route's own ceiling. A stream that is not one
    whole gzip member is the route's to call malformed.
    """
    if body is None or coding == "identity":
        return body
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(body, limit + 1)
    except zlib.error:
        return None
    if len(decoded) > limit or not decoder.eof or decoder.unused_data:
        return None
    return decoded


#: A request under no declared surface, and one the scan has no part in.
_NOTHING_ITS_OWN: Final = SelfValidated(prefix="")
_ALL_ITS_OWN: Final = SelfValidated.whole(prefix="")


class StorableTextMiddleware:
    """Refuses a request carrying text a Postgres `text` column cannot hold.

    A NUL (and any unpaired surrogate) in a string that reaches a query raises
    `CharacterNotInRepertoireError` from deep inside a service, which lands on
    the catch-all as an opaque 500 — one bug with as many surfaces as there are
    routes. Checking once at the boundary fixes the class: the path, the query
    string, every header value and a JSON body are all scanned before routing,
    so a route added tomorrow inherits the refusal without knowing about it.

    Pure ASGI, not `BaseHTTPMiddleware`: the gateway streams SSE and the content
    plane streams uploads, and neither may be buffered on the way out. On the
    way IN only a declared, bounded JSON body is buffered, and it is replayed
    verbatim to the app.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        render: UnstorableRenderer,
        max_scan_bytes: int,
        self_validated: Sequence[SelfValidated] = (),
    ) -> None:
        self.app = app
        self.render = render
        self.max_scan_bytes = max_scan_bytes
        self.self_validated = tuple(self_validated)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        own = self._own_refusals(scope) if scope["type"] == "http" else _ALL_ITS_OWN
        if own.everything:
            await self.app(scope, receive, send)
            return
        problem = self._safely(lambda: self._scan_metadata(scope, own), scope)
        coding = self._scannable_coding(scope) if problem is None and not own.body else None
        if coding is not None:
            # Buffered outside the guard below: reading the body is transport,
            # and a transport failure is the caller's connection ending, not a
            # scan that could not answer.
            body, receive = await self._buffer(receive)
            text = _decoded_for_scan(body, coding, self.max_scan_bytes)
            if text is not None:
                problem = self._safely(
                    lambda: scan_json_body(text, deferred=own.body_fields), scope
                )
        if problem is not None:
            await self._refuse(problem, scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _own_refusals(self, scope: Scope) -> SelfValidated:
        """What the surface this path belongs to refuses in its own words:
        the first declaration whose prefix covers the path, or nothing."""
        path = scope.get("path")
        if isinstance(path, str):
            for surface in self.self_validated:
                if surface.covers(path):
                    return surface
        return _NOTHING_ITS_OWN

    def _safely(self, scan: Callable[[], Unstorable | None], scope: Scope) -> Unstorable | None:
        """Run one scan, and stand aside if it cannot answer.

        The scan is a guard, not the authority: the driver-level net in
        `_handle_unexpected_error` still refuses text Postgres cannot store. A
        guard that breaks must not turn every request in the deployment into
        the 500 it exists to remove — but it says so loudly enough to be fixed.
        """
        try:
            return scan()
        except Exception:
            log.exception("request.scan_failed", path=scope.get("path"))
            return None

    def _scan_metadata(self, scope: Scope, own: SelfValidated) -> Unstorable | None:
        """The first refusal the path, query string or headers earn, leaving
        out the parts the surface answers for itself."""
        raw_path = scope.get("path")
        problem = None
        if not own.path and isinstance(raw_path, str):
            problem = scan_path(raw_path)
        if problem is None and not own.query:
            problem = scan_query_string(scope.get("query_string") or b"")
        if problem is None and not own.headers:
            problem = scan_headers(scope.get("headers") or [])
        return problem

    def _scannable_coding(self, scope: Scope) -> str | None:
        """The content coding of a JSON body small enough to buffer, or
        ``None`` when the body is not the scan's to read.

        Content-Length gated: a chunked or oversized body streams through
        untouched rather than being held in memory. A coding the scan cannot
        decode is handed on unread: its bytes are not the JSON the type names.
        """
        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "").lower()
        if not any(marker in content_type for marker in _JSON_CONTENT_TYPES):
            return None
        coding = headers.get("content-encoding", "").strip().lower() or "identity"
        if coding not in _SCANNED_CODINGS:
            return None
        declared = headers.get("content-length")
        if declared is None:
            return None
        try:
            length = int(declared)
        except ValueError:
            return None
        return coding if 0 < length <= self.max_scan_bytes else None

    async def _buffer(self, receive: Receive) -> tuple[bytes | None, Receive]:
        """Read the body, and hand back a `receive` that replays it exactly.

        `None` means the body outran the ceiling while it was being read and
        was not scanned: the declared Content-Length is the caller's claim, and
        an in-process ASGI caller writes it by hand, so the bound has to hold
        on the bytes that actually arrive.

        A disconnect mid-body ends the read; the replay reproduces the chunks
        that arrived and then the disconnect, so the app sees what it would
        have seen.
        """
        messages: list[Message] = []
        chunks: list[bytes] = []
        read = 0
        oversized = False
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            chunk = bytes(message.get("body", b""))
            chunks.append(chunk)
            read += len(chunk)
            if read > self.max_scan_bytes:
                oversized = True
                break
            if not message.get("more_body", False):
                break
        replayed = iter(messages)

        async def replay() -> Message:
            try:
                return next(replayed)
            except StopIteration:
                return await receive()

        return (None if oversized else b"".join(chunks)), replay

    async def _refuse(
        self, problem: Unstorable, scope: Scope, receive: Receive, send: Send
    ) -> None:
        state = cast(dict[str, Any], scope.setdefault("state", {}))
        trace_id = state.get("trace_id") or get_trace_id() or new_trace_id()
        log.info(
            "request.unstorable_text",
            path=scope.get("path"),
            method=scope.get("method"),
            field=problem.location,
            reason=problem.reason,
        )
        await self.render(problem, trace_id)(scope, receive, send)


#: Where the OpenAPI document keeps its schemas, and the two FastAPI adds on its
#: own for a validation failure, which this app never answers with.
_SCHEMAS_REF: Final = "#/components/schemas/"
_FASTAPI_VALIDATION_SCHEMAS: Final = ("HTTPValidationError", "ValidationError")


def document_error_envelope(app: FastAPI) -> None:
    """Make the app's OpenAPI document say what its errors look like.

    FastAPI documents every route's ``422`` as its own ``{"detail": [...]}``
    and leaves an error response declared without a model with no body at all.
    Neither is what a client receives: the handlers above answer every error
    in :class:`~alkera_core.observability.envelope.ErrorEnvelope`. So the
    document says that, and a route that declares a different error model
    stands out (``test_error_envelope_shapes`` refuses one unless its surface
    is a registered exception)."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        document = generate()
        _envelope_in_document(document)
        return document

    app.openapi = openapi  # type: ignore[method-assign]


def _envelope_in_document(document: dict[str, Any]) -> None:
    """Rewrite ``document`` in place; running it twice changes nothing."""
    schemas: dict[str, Any] = document.setdefault("components", {}).setdefault("schemas", {})
    envelope = ErrorEnvelope.model_json_schema(ref_template=_SCHEMAS_REF + "{model}")
    for name, schema in envelope.pop("$defs", {}).items():
        schemas.setdefault(name, schema)
    schemas.setdefault(ErrorEnvelope.__name__, envelope)
    content = {"application/json": {"schema": {"$ref": _SCHEMAS_REF + ErrorEnvelope.__name__}}}
    validation = _SCHEMAS_REF + _FASTAPI_VALIDATION_SCHEMAS[0]
    for operations in document.get("paths", {}).values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for status_code, response in operation.get("responses", {}).items():
                if not str(status_code).startswith(("4", "5")):
                    continue
                if "content" not in response:
                    response["content"] = content
                    continue
                schema = response["content"].get("application/json", {}).get("schema") or {}
                if schema.get("$ref") == validation:
                    response["content"] = content
    for name in _FASTAPI_VALIDATION_SCHEMAS:
        schemas.pop(name, None)


def install_exception_handlers(app: Starlette) -> None:
    """Register the four handlers that render the canonical error envelope."""
    app.add_exception_handler(AlkeraError, _handle_alkera_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(DBAPIError, _handle_database_error)
    app.add_exception_handler(PoolTimeoutError, _handle_database_error)
    # The database away (restarting, failing over) surfaces as the driver's own
    # errors rather than SQLAlchemy's: a refused socket, or asyncpg's
    # "starting up" raised while connecting. Classified like the rest; anything
    # unclassified still renders as the opaque 500.
    app.add_exception_handler(OSError, _handle_database_error)
    app.add_exception_handler(PostgresError, _handle_database_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)


async def _handle_database_error(request: Request, exc: Exception) -> Response:
    """A database refusal answered as what it means to the caller.

    :func:`alkera_core.db.errors.classify` owns the mapping: busy or away is a
    retryable ``503`` with a code, a duplicate the ``409`` a create of
    something that exists earns (naming the field when its constraint is
    registered), a dangling reference a ``409``, a value the column cannot hold
    a ``422``. Text Postgres cannot store keeps its own refusal, in the shape
    this app's clients parse. Anything else the driver raised is the opaque
    ``500`` it always was.

    These are answered here rather than at each route because they reach every
    one of them: two requests both pass a create's existence check, and the
    second insert meets the index at flush or at commit. The constraint is
    named in the log, never to the caller.
    """
    if _untranslatable_text(exc):
        return _unstorable_answer(request)
    refusal = classify_database_error(exc)
    if refusal is None:
        return await _handle_unexpected_error(request, exc)
    trace_id = _trace_id_of(request)
    if refusal.code == ErrorCode.db_pool_exhausted:
        metrics.record_db_pool_exhausted()
    if refusal.status >= 500:
        # A warning, not an error with a stack: the request did nothing wrong
        # and the line is read for its rate, which is what says something is
        # holding rows or connections.
        log.warning(
            "request.database_busy",
            code=refusal.code,
            path=request.url.path,
            method=request.method,
        )
    else:
        log.info(
            "request.database_refused",
            code=refusal.code,
            status=refusal.status,
            path=request.url.path,
            method=request.method,
            constraint=refusal.constraint,
        )
    body = build_error_body(
        code=refusal.code,
        message=refusal.message,
        trace_id=trace_id,
        status=refusal.status,
        details=dict(refusal.details) or None,
    )
    headers = (
        {"Retry-After": str(refusal.retry_after_seconds)}
        if refusal.retry_after_seconds is not None
        else None
    )
    return _json(refusal.status, body, trace_id, headers=headers)


async def _handle_alkera_error(request: Request, exc: Exception) -> Response:
    err = cast(AlkeraError, exc)
    trace_id = _trace_id_of(request)
    if err.status_code >= 500 and err.deliberate:
        # An expected condition the operator has to act on (an integration
        # nobody configured), not a failure: it keeps its own code and message,
        # the precise reason goes to the log, and Sentry is not paged.
        log.warning(
            "request.alkera_error",
            code=str(err.code),
            status=err.status_code,
            error=err.message,
        )
    elif err.status_code >= 500:
        log.error(
            "request.alkera_error",
            code=str(err.code),
            status=err.status_code,
            error=err.message,
            exc_info=err,
        )
        capture_exception(err, code=str(err.code))
    else:
        log.info("request.alkera_error", code=str(err.code), status=err.status_code)
    return _json(err.status_code, error_body_for(err, trace_id=trace_id), trace_id)


_NAMED_BODY_ERRORS: dict[Any, tuple[str, str]] = {}


def names_body_errors(code: str, message: str) -> Callable[[_Endpoint], _Endpoint]:
    """Give one endpoint's body-validation refusal a name of its own.

    Every malformed body gets the same answer by default: ``validation_error``
    plus the raw pydantic error list. That is the least useful thing to hand a
    client whose body has ONE meaningful way to be wrong — a permission mode
    that is not one of three, a membership that names the wrong team. It cannot
    branch on it, and the person reading it has to decode ``loc``/``msg`` pairs
    to find which word was rejected.

    A route opts in by decorating its endpoint with a code and a message that
    says what IS accepted. The registry is keyed by the endpoint function, the
    thing Starlette puts on the scope when it matches the route, so naming the
    next route is one decorator and never a change here. The pydantic detail is
    still carried in ``details.errors`` — the name replaces the headline, not
    the diagnosis.
    """

    def register(endpoint: _Endpoint) -> _Endpoint:
        _NAMED_BODY_ERRORS[endpoint] = (code, message)
        return endpoint

    return register


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    err = cast(RequestValidationError, exc)
    trace_id = _trace_id_of(request)
    # Echo type/loc/msg only — NEVER the offending input value (it may be a
    # password or other secret the user submitted).
    details = {
        "errors": [
            {"type": e.get("type"), "loc": jsonable_encoder(e.get("loc")), "msg": e.get("msg")}
            for e in err.errors()
        ]
    }
    code: ErrorCode | str = ErrorCode.validation_error
    message = "The request failed validation."
    named = _NAMED_BODY_ERRORS.get(request.scope.get("endpoint"))
    if named is not None:
        code, message = named
    log.info("request.validation_error", count=len(details["errors"]), code=str(code))
    body = build_error_body(
        code=code, message=message, trace_id=trace_id, status=422, details=details
    )
    return _json(422, body, trace_id)


#: The codes whose 5xx message is written for the person who asked and so is
#: shown, not replaced by the generic sentence. ``provider_error``: a compute
#: provider refused an admin's request (an expired key, no capacity, a quota),
#: and that refusal is the only thing the admin can act on. The message still
#: passes the credential scrubber and never carries ``details``; every other
#: 5xx stays opaque.
CLIENT_SAFE_SERVER_CODES: Final = frozenset({"provider_error"})


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    err = cast(StarletteHTTPException, exc)
    trace_id = _trace_id_of(request)
    code, message, details = _decompose_http_detail(err.status_code, err.detail)
    if err.status_code >= 500:
        log.error("request.http_exception", status=err.status_code, error=str(err.detail))
        capture_exception(err, status=err.status_code)
        if str(code) in CLIENT_SAFE_SERVER_CODES and isinstance(err.detail, dict):
            message, details = scrub_text(message), None
        else:
            code, message, details = ErrorCode.internal_error, GENERIC_SERVER_MESSAGE, None
    else:
        log.info("request.http_exception", status=err.status_code, code=str(code))
    body = build_error_body(
        code=code, message=message, trace_id=trace_id, status=err.status_code, details=details
    )
    return _json(err.status_code, body, trace_id, headers=getattr(err, "headers", None))


def _decompose_http_detail(
    status_code: int, detail: Any
) -> tuple[ErrorCode | str, str, dict[str, Any] | None]:
    """Map an `HTTPException.detail` into (code, message, details).

    A dict detail (`{"code": ..., "message": ..., **extra}`) carries a precise
    error code + structured context straight into the envelope — that's how
    routes opt into a machine-readable error (e.g. the signup work-email gate)
    without a custom exception class. A string detail becomes the message; a
    missing/other detail falls back to the status-derived default.
    """
    default_code = _STATUS_TO_CODE.get(status_code, ErrorCode.bad_request)
    if isinstance(detail, dict):
        code = detail.get("code") or default_code
        message = detail.get("message")
        if not isinstance(message, str) or not message:
            message = _default_message(default_code)
        extra = {k: v for k, v in detail.items() if k not in ("code", "message")}
        return code, message, (extra or None)
    if isinstance(detail, str) and detail:
        return default_code, detail, None
    return default_code, _default_message(default_code), None


#: The SQLSTATEs Postgres raises for a NUL it cannot store: `22021`,
#: character_not_in_repertoire, when one reaches a text column or comparison,
#: and `22P05`, untranslatable_character, when one rides a `jsonb` document as
#: `\u0000`. A surrogate never gets that far — the driver fails earlier,
#: encoding the parameter — which is why the chain walk below looks for that
#: encode failure rather than for another SQLSTATE.
_UNTRANSLATABLE_SQLSTATES: Final = frozenset({"22021", "22P05"})


def _untranslatable_text(exc: BaseException) -> bool:
    """Whether this failure is text the database cannot represent, anywhere in
    the cause chain.

    The safety net behind `StorableTextMiddleware`: a value the boundary could
    not see — assembled by a service, read back from a queue, carried in a body
    too large to scan — must still be the caller's 4xx and never a 500.

    Two shapes reach here, and only these two: Postgres refusing a NUL
    (SQLSTATE 22021 or 22P05, wrapped by asyncpg and again by SQLAlchemy), and the
    driver's own UTF-8 encode of a parameter refusing a surrogate — a generic
    `22000` whose real cause is a `UnicodeEncodeError`. The encode failure is
    matched on the slice that actually failed, so an encode error about
    anything else stays the server-side bug it is.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if str(getattr(current, "sqlstate", "") or "") in _UNTRANSLATABLE_SQLSTATES:
            return True
        if isinstance(current, UnicodeEncodeError) and reason_for(
            current.object[current.start : current.end]
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _unstorable_render_for(request: Request) -> UnstorableRenderer:
    """How THIS app renders a refusal — whatever `setup_observability` wired.

    The gateway's 4xx bodies are parsed by LLM SDK clients, so a refusal the
    driver earns cannot arrive in the house envelope just because the boundary
    is not the one answering. An app that installed the handlers directly gets
    the envelope.
    """
    render = getattr(getattr(request.app, "state", None), "unstorable_render", None)
    return cast(UnstorableRenderer, render) if callable(render) else _unstorable_envelope


def _unstorable_answer(request: Request) -> Response:
    """The refusal for text the database could not store, in this app's shape."""
    log.info("request.unstorable_text", path=request.url.path, method=request.method)
    return _unstorable_render_for(request)(
        Unstorable("request", "untranslatable_character"), _trace_id_of(request)
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    if _untranslatable_text(exc):
        return _unstorable_answer(request)
    trace_id = _trace_id_of(request)
    log.error(
        "request.unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=exc,
    )
    capture_exception(exc)
    body = build_error_body(
        code=ErrorCode.internal_error,
        message=GENERIC_SERVER_MESSAGE,
        trace_id=trace_id,
        status=500,
    )
    return _json(500, body, trace_id)


def _json(
    status_code: int,
    body: dict[str, Any],
    trace_id: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    merged = {"X-Trace-Id": trace_id, **(headers or {})}
    return JSONResponse(body, status_code=status_code, headers=merged)


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.bad_request,
    401: ErrorCode.auth_required,
    403: ErrorCode.forbidden,
    404: ErrorCode.not_found,
    409: ErrorCode.conflict,
    422: ErrorCode.validation_error,
    429: ErrorCode.rate_limited,
    500: ErrorCode.internal_error,
    502: ErrorCode.upstream_error,
    503: ErrorCode.unavailable,
}


def _default_message(code: ErrorCode) -> str:
    return {
        ErrorCode.bad_request: "The request was invalid.",
        ErrorCode.auth_required: "Authentication is required.",
        ErrorCode.forbidden: "You do not have permission to perform this action.",
        ErrorCode.not_found: "The requested resource was not found.",
        ErrorCode.conflict: "The request conflicts with the current state.",
        ErrorCode.rate_limited: "Too many requests. Please retry shortly.",
        ErrorCode.unavailable: "The service is temporarily unavailable.",
    }.get(code, "The request could not be completed.")
