"""The canonical API error envelope.

Every error response of the public HTTP API serializes to::

    {"error": {"type": "urn:alkera:error:<code>", "code": "...", "status": 409,
               "message": "...", "trace_id": "...", "details": {...}}}

The members inside ``error`` follow RFC 9457 problem details: ``type`` is a
stable URI naming the kind of problem (one per ``code``, a URN so it does not
depend on where the deployment is served), ``status`` repeats the HTTP status
so a body read apart from its response still says what it was. ``code``,
``message``, ``trace_id`` and ``details`` are the keys clients have always read
and keep their meaning: ``code`` is what a client branches on.

``message`` is always client-safe (5xx are reduced to a generic string by
`AlkeraError.client_message`). ``details`` is omitted when empty. The builders
are pure dicts so they can be unit-tested without a web framework; the models
describe the same shape in the OpenAPI document.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from alkera_core.observability.errors import AlkeraError, ErrorCode

#: The URI scheme every problem ``type`` is spelled in.
ERROR_TYPE_PREFIX: Final = "urn:alkera:error:"


def error_type(code: ErrorCode | str) -> str:
    """The stable problem-type URI for ``code``."""
    return f"{ERROR_TYPE_PREFIX}{code}"


def build_error_body(
    *,
    code: ErrorCode | str,
    message: str,
    trace_id: str,
    status: int,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble an error-envelope body."""
    error: dict[str, Any] = {
        "type": error_type(code),
        "code": str(code),
        "status": status,
        "message": message,
        "trace_id": trace_id,
    }
    if details:
        error["details"] = details
    return {"error": error}


def error_body_for(err: AlkeraError, *, trace_id: str) -> dict[str, Any]:
    """Envelope body for an `AlkeraError` (uses its client-safe message)."""
    return build_error_body(
        code=err.code,
        message=err.client_message,
        trace_id=trace_id,
        status=err.status_code,
        details=err.details or None,
    )


class ErrorDetail(BaseModel):
    """One problem, as the ``error`` member of :class:`ErrorEnvelope` carries it."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(description="A stable URI naming the kind of problem, one per code.")
    code: str = Field(description="The machine-readable code a client branches on.")
    status: int = Field(description="The HTTP status the response carried.")
    message: str = Field(description="A sentence written for the person who asked.")
    trace_id: str = Field(description="The request's trace id, for support.")
    details: dict[str, Any] | None = Field(
        default=None, description="Ids and enums that explain this code; absent when empty."
    )


class ErrorEnvelope(BaseModel):
    """Every error the public HTTP API answers with."""

    error: ErrorDetail


__all__ = [
    "ERROR_TYPE_PREFIX",
    "ErrorDetail",
    "ErrorEnvelope",
    "build_error_body",
    "error_body_for",
    "error_type",
]
