"""Per-request correlation context.

A single `trace_id` ties together every log line, error-envelope body, Sentry
event, and crash report produced while handling one request. The id lives in a
`contextvar` (so any code deep in the call stack can read it without threading a
request object) AND is mirrored into structlog's contextvars (so every
`get_logger().info(...)` inherits it automatically).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Any

import structlog

_trace_id_var: ContextVar[str | None] = ContextVar("alkera_trace_id", default=None)


def new_trace_id() -> str:
    """A fresh, opaque correlation id (uuid4 hex — 32 chars, no dashes)."""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    """The current request's trace id, or None outside a request."""
    return _trace_id_var.get()


def bind_trace_id(trace_id: str) -> Token[str | None]:
    """Bind `trace_id` to the context + structlog. Returns a token for reset."""
    token = _trace_id_var.set(trace_id)
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    return token


def reset_trace_id(token: Token[str | None] | None = None) -> None:
    """Undo a `bind_trace_id` (best-effort)."""
    structlog.contextvars.unbind_contextvars("trace_id")
    if token is not None:
        _trace_id_var.reset(token)
    else:
        _trace_id_var.set(None)


def bind_log_context(**values: Any) -> None:
    """Bind extra key/values onto every subsequent log line in this context
    (e.g. `user_id`, `org_id` once auth resolves)."""
    structlog.contextvars.bind_contextvars(**values)


def clear_log_context() -> None:
    """Drop all structlog context vars (call at request start for isolation)."""
    structlog.contextvars.clear_contextvars()
