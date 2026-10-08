"""Shared observability toolkit: errors, redaction, request context, the error
envelope, Sentry (gated), analytics events, and FastAPI glue.

Only the logging-independent leaves are re-exported here. The submodules that
depend on `alkera_core.logging` (`sentry`, `events`) or on a web framework
(`asgi`) are imported explicitly by consumers, e.g.::

    from alkera_core.observability.sentry import init_sentry, capture_exception
    from alkera_core.observability.events import emit_event, EventName
    from alkera_core.observability.asgi import setup_observability

Keeping them out of this package `__init__` avoids an import cycle
(`logging` -> `observability.redaction` -> package `__init__`) and keeps the CLI
binary's import closure from dragging in a web framework it never serves.
"""

from __future__ import annotations

from alkera_core.observability.context import (
    bind_log_context,
    bind_trace_id,
    clear_log_context,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
)
from alkera_core.observability.envelope import build_error_body, error_body_for
from alkera_core.observability.errors import (
    AlkeraError,
    AuthRequiredError,
    BadRequestError,
    ConflictError,
    ErrorCode,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnavailableError,
    UpstreamServiceError,
    ValidationFailedError,
)
from alkera_core.observability.redaction import (
    REDACTED,
    scrub_mapping,
    scrub_path,
    scrub_text,
    scrub_url_credentials,
    scrub_value,
)

__all__ = [
    "REDACTED",
    "AlkeraError",
    "AuthRequiredError",
    "BadRequestError",
    "ConflictError",
    "ErrorCode",
    "ForbiddenError",
    "NotFoundError",
    "RateLimitedError",
    "UnavailableError",
    "UpstreamServiceError",
    "ValidationFailedError",
    "bind_log_context",
    "bind_trace_id",
    "build_error_body",
    "clear_log_context",
    "error_body_for",
    "get_trace_id",
    "new_trace_id",
    "reset_trace_id",
    "scrub_mapping",
    "scrub_path",
    "scrub_text",
    "scrub_url_credentials",
    "scrub_value",
]
