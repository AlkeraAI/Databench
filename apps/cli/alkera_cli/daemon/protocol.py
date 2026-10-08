"""Typed protocol contract for the Alkera daemon.

Every method the daemon exposes is declared here as a triple of
Pydantic models: ``Request``, ``Response``, and the handler function.
Server-initiated requests (daemon → extension) and notifications are
declared the same way; the protocol module is the single source of
truth that the TypeScript codegen pipeline reads.

Adding a method:

    class FooRequest(BaseModel):
        bar: str

    class FooResponse(BaseModel):
        baz: int

    @method("foo")
    async def handle_foo(server: "JsonRpcServer", params: FooRequest) -> FooResponse:
        return FooResponse(baz=len(params.bar))

Re-export the request/response from this module (it's how the schema
exporter discovers them) and then run ``make gen-daemon-protocol``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    TypeVar,
)

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


# ---------------------------------------------------------------------------
# Read authorization, resolved inside the trusted boundary
# ---------------------------------------------------------------------------
#
# A request carries no scopes of its own. A handler asks authorize_read() who
# is reading and threads the answer to every shared KB read, so nothing a
# client sends can widen one.

#: The one principal a local daemon serves: the person whose machine,
#: sign-in session, and on-disk stores these are.
MACHINE_OWNER = "owner"


@dataclass(frozen=True, slots=True)
class ReadAuthorization:
    """One RPC's resolved reader and the shared visibility scopes its KB
    reads may surface."""

    principal: str
    allowed_scopes: frozenset[str] | None
    """Shared scopes the principal may read. ``None`` bounds nothing, and only
    :func:`authorize_read` may choose it, for the machine owner reading their
    own store. It never comes from a request."""

    @property
    def scope_filter(self) -> set[str] | None:
        """This reader's entitlements as the ``allowed_scopes`` argument the
        KB read surfaces accept."""
        return set(self.allowed_scopes) if self.allowed_scopes is not None else None


def authorize_read() -> ReadAuthorization:
    """Resolve the reader for one RPC from daemon-owned state alone.

    The daemon serves exactly one principal, the machine owner, and their
    entitlement to the local store is the whole store: the backend ACL filters
    every synced row before it lands on disk, and a revocation lands as
    removal on the next pull. A daemon that ever serves a second principal
    must narrow entitlements here, the one place they are resolved.
    """
    return ReadAuthorization(principal=MACHINE_OWNER, allowed_scopes=None)


# ---------------------------------------------------------------------------
# Method registry
# ---------------------------------------------------------------------------


class _DaemonModel(BaseModel):
    """Base for every wire-message model.

    `extra='forbid'` catches typos in client requests with a clear error
    rather than silently dropping fields. Production clients are typed via
    codegen, so unknown fields are always a bug.
    """

    model_config = ConfigDict(extra="forbid")


TRequest = TypeVar("TRequest", bound=_DaemonModel)
TResponse = TypeVar("TResponse", bound=_DaemonModel)
THandler = Callable[["JsonRpcServer", TRequest], Awaitable[TResponse]]


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """One row of the dispatch table."""

    name: str
    request_type: type[_DaemonModel]
    response_type: type[_DaemonModel]
    handler: Callable[[JsonRpcServer, Any], Awaitable[Any]]


METHODS: dict[str, MethodSpec] = {}


def _resolve(annotation: Any, globalns: dict[str, Any]) -> Any:
    """Eval a stringified annotation against the function's module globals.

    We only call this for the request and return annotations on a handler
    — never the `server` parameter, which is TYPE_CHECKING-only.
    """
    if isinstance(annotation, str):
        return eval(annotation, globalns)  # noqa: S307 — controlled by us, not user input
    return annotation


def method(name: str) -> Callable[[Any], Any]:
    """Register an async handler under ``name``.

    Type info is pulled from the function's annotations: the second
    positional parameter is the request model; the return type is the
    response model. Both must be Pydantic ``BaseModel`` subclasses so
    the server can validate the wire payloads.

    The signature is intentionally ``Callable[[Any], Any]`` instead of a
    generic constrained on ``THandler`` because handlers vary in their
    concrete request/response types and mypy can't infer the generic from
    `__annotations__` introspection. The runtime checks below enforce the
    real constraint.
    """

    def register(fn: Any) -> Any:
        # PEP 563 (`from __future__ import annotations`) makes type hints
        # strings. We can't use typing.get_type_hints on the whole function
        # because the `server: "JsonRpcServer"` annotation references a
        # TYPE_CHECKING-only import. Instead, eval just the param/return
        # annotations we care about, in the function's own globals.
        raw = fn.__annotations__
        if "return" not in raw:
            raise TypeError(f"@method({name!r}): handler missing return annotation")
        param_keys = [k for k in raw if k not in ("return", "server")]
        if not param_keys:
            raise TypeError(f"@method({name!r}): handler must take (server, params: SomeRequest)")

        globalns = getattr(fn, "__globals__", {})
        request_type = _resolve(raw[param_keys[-1]], globalns)
        response_type = _resolve(raw["return"], globalns)

        if not (isinstance(request_type, type) and issubclass(request_type, _DaemonModel)):
            raise TypeError(
                f"@method({name!r}): request param must be a _DaemonModel subclass, "
                f"got {request_type!r}"
            )
        if not (isinstance(response_type, type) and issubclass(response_type, _DaemonModel)):
            raise TypeError(
                f"@method({name!r}): return type must be a _DaemonModel subclass, "
                f"got {response_type!r}"
            )

        if name in METHODS:
            raise ValueError(f"method {name!r} already registered")
        METHODS[name] = MethodSpec(
            name=name,
            request_type=request_type,
            response_type=response_type,
            handler=fn,
        )
        return fn

    return register


# ---------------------------------------------------------------------------
# Server → client request types (daemon initiates; extension answers)
# ---------------------------------------------------------------------------


CLIENT_REQUESTS: dict[str, tuple[type[_DaemonModel], type[_DaemonModel]]] = {}


def client_request(
    name: str,
) -> Callable[[tuple[type[TRequest], type[TResponse]]], tuple[type[TRequest], type[TResponse]]]:
    """Document a server→client request pair. Doesn't dispatch — the daemon
    sends it via ``server.request(name, params)``; this exists so the schema
    exporter sees the types.
    """

    def register(
        pair: tuple[type[TRequest], type[TResponse]],
    ) -> tuple[type[TRequest], type[TResponse]]:
        if name in CLIENT_REQUESTS:
            raise ValueError(f"client request {name!r} already registered")
        CLIENT_REQUESTS[name] = pair
        return pair

    return register


# ---------------------------------------------------------------------------
# Notification types (server → client, fire-and-forget)
# ---------------------------------------------------------------------------


NOTIFICATIONS: dict[str, type[_DaemonModel]] = {}


def notification(name: str) -> Callable[[type[TRequest]], type[TRequest]]:
    """Document a server→client notification. Same purpose as client_request
    but no response shape — the daemon just emits.
    """

    def register(cls: type[TRequest]) -> type[TRequest]:
        if name in NOTIFICATIONS:
            raise ValueError(f"notification {name!r} already registered")
        NOTIFICATIONS[name] = cls
        return cls

    return register


__all__ = [
    "CLIENT_REQUESTS",
    "MACHINE_OWNER",
    "METHODS",
    "NOTIFICATIONS",
    "MethodSpec",
    "ReadAuthorization",
    "authorize_read",
    "client_request",
    "method",
    "notification",
]
