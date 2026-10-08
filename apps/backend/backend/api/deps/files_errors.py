"""The single place a Files failure becomes an HTTP response.

A route never spells a status. It lets the library raise, and one exception
handler maps the class to the status the class itself declares and the ``code``
to the body's machine-readable field. Keeping the mapping here — rather than a
try/except per route — is what makes the "not yours" contract hold everywhere
at once: a nonexistent node, a node in another org and a node the caller may
not read all reach this module as the same :class:`NotFound` and leave it as
byte-identical responses.

Bodies carry ids and enums, never a name, a path or a symlink target: a refusal
that named the sibling it collided with would be an oracle for something the
caller may not read.

The response is the platform's error envelope, ``{"error": {type, code,
status, message, trace_id, details}}``, with the same ``code`` values Files has
always answered. Beside it the body still carries the flat ``code``,
``message`` and ``detail`` keys Files answered with before it joined the
envelope: a box runs the CLI it was provisioned with, so a box that predates
the envelope keeps reading its refusals until every box in service reads
``error``. New readers read ``error``.
"""

from __future__ import annotations

from typing import Any, Final

from alkera_core.files.authz.authorize import Denied
from alkera_core.files.errors import Conflict, FilesError, NotFound
from alkera_core.observability.context import get_trace_id, new_trace_id
from alkera_core.observability.envelope import build_error_body
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

#: The body every "not yours" answer carries, whatever the caller actually
#: asked about. Spelled once so no route can drift from it.
NOT_FOUND_BODY: Final[dict[str, str]] = {"code": "not_found"}

#: The sentence every "not yours" answer carries: the same for a node that is
#: not there, one in another org and one the caller may not read.
NOT_FOUND_MESSAGE: Final = "Not found."

#: The keys a `detail` may carry onto the wire. A refusal explains itself with
#: ids and enums; anything else is dropped rather than trusted, so a service
#: that puts a name in a detail cannot leak it by accident.
_SAFE_DETAIL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "holder_principal_id",
        # A lease refusal names its holder: who has the folder, on which
        # machine, since when. The holder is a member of the caller's own org
        # and the caller has already been shown they may read the leased node,
        # so "Ana on MacBook Pro" is what makes the 409 actionable rather than
        # a dead end — and it is the same triple the lease facet already
        # carries on every listing of that folder.
        "holder",
        "machine",
        "since",
        "ancestor_id",
        "node_id",
        "drive_id",
        "operation_id",
        "session_id",
        "lease_epoch",
        "expected_etag",
        "limit",
        "retry_after",
        # A refused tree report names the entries it refused -- by position
        # and by path -- so the holder drops exactly those and resends the
        # rest. Both are the holder's own words back to it.
        "indexes",
        "paths",
        # A read of a file whose bytes are still on the machine holding its
        # folder says what asking that machine came to and how far its upload
        # has got: an enum and two byte counts.
        "outcome",
        "landing",
    }
)


def files_error_for(refused: HTTPException) -> FilesError:
    """The platform authorization engine's refusal, said in Files' vocabulary.

    ``backend.authz.enforce`` answers a denial by raising its own
    ``HTTPException``, which the platform handler renders as
    ``{"error": {code, message, trace_id}}``. Left alone that body reaches the
    caller for a *same-org node they may not read*, while a nonexistent id
    leaves through the Files handler as ``{"code": "not_found"}`` — two shapes,
    and so an existence oracle where there must be one answer for "not yours".

    Translating here — after ``enforce`` has already written its decision row —
    is what makes all three "not yours" classes leave through the one Files
    handler with the same bytes. A refusal the policy made *visible* (403: the
    caller can already read the node) keeps its code and message, so they still
    learn which action they lack.
    """
    if refused.status_code == 404:
        return NotFound()
    detail = refused.detail
    if isinstance(detail, dict):
        return Denied(
            str(detail.get("code") or Denied.code),
            str(detail.get("message") or "Not allowed"),
        )
    return Denied(Denied.code, str(detail) if detail else "Not allowed")


def safe_detail(detail: object) -> dict[str, Any] | None:
    """``detail`` reduced to the keys that are safe to return."""
    if not isinstance(detail, dict):
        return None
    kept = {k: v for k, v in detail.items() if k in _SAFE_DETAIL_KEYS}
    return kept or None


def body_for(error: FilesError, *, may_read_named_node: bool = False) -> dict[str, Any]:
    """The response body for ``error``.

    A :class:`NotFound` is always the opaque body. A :class:`Conflict` carries
    its ``detail`` only when the caller may read the node the detail names —
    the flag is passed by the route that already resolved that fact, and
    defaults to withholding.
    """
    if isinstance(error, NotFound):
        return dict(NOT_FOUND_BODY)
    body: dict[str, Any] = {"code": error.code, "message": str(error)}
    if isinstance(error, Conflict) and not may_read_named_node:
        return body
    detail = safe_detail(error.detail)
    if detail is not None:
        body["detail"] = detail
    return body


def refusal_body(
    *,
    status: int,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    trace_id: str | None = None,
    flat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A refusal in the platform envelope, with the flat keys beside it.

    ``flat`` is the body as the route answered before the envelope; it
    defaults to ``{code, message, detail}``. The envelope's ``details`` is
    ``detail``. Shared by every surface that answered flat (Files, notebooks).
    """
    mirror = (
        flat
        if flat is not None
        else {"code": code, "message": message, **({"detail": detail} if detail else {})}
    )
    return {
        **build_error_body(
            code=code,
            message=message,
            trace_id=trace_id or get_trace_id() or new_trace_id(),
            status=status,
            details=detail,
        ),
        **mirror,
    }


def envelope_for(error: FilesError, *, trace_id: str) -> dict[str, Any]:
    """The full error body for ``error``: the platform envelope, with the flat
    keys of :func:`body_for` beside it for readers that predate the envelope.

    The envelope carries exactly what the flat body does, under the same
    rules: a :class:`NotFound` is opaque, and a :class:`Conflict` names its
    detail only to a caller who may read the node it names.
    """
    flat = body_for(error, may_read_named_node=bool(getattr(error, "may_read_named_node", False)))
    detail = flat.get("detail")
    return refusal_body(
        status=error.status,
        code=flat["code"],
        message=flat.get("message") or NOT_FOUND_MESSAGE,
        detail=detail if isinstance(detail, dict) else None,
        trace_id=trace_id,
        flat=flat,
    )


def response_for(error: FilesError, *, trace_id: str) -> JSONResponse:
    """The full response for ``error``, status and body together."""
    headers = getattr(error, "headers", None)
    return JSONResponse(
        status_code=error.status,
        content=envelope_for(error, trace_id=trace_id),
        headers=dict(headers) if isinstance(headers, dict) else None,
    )


async def files_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """The app-level handler. Registered for :class:`FilesError` so every
    subclass — present and future — is mapped without editing a table."""
    assert isinstance(exc, FilesError)
    trace_id = getattr(request.state, "trace_id", None) or get_trace_id() or new_trace_id()
    return response_for(exc, trace_id=trace_id)


async def content_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """The content plane's handler: the flat body, never the envelope.

    The content origin is a registered exception to the envelope. Every
    refusal there, a routing 404 and a Files one alike, is the one opaque
    ``{"code": "not_found"}`` body (``backend.content_app.NOT_FOUND_BODY``), so
    a prober cannot sort the refusals by their shape."""
    assert isinstance(exc, FilesError)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status,
        content=body_for(exc, may_read_named_node=bool(getattr(exc, "may_read_named_node", False))),
        headers=dict(headers) if isinstance(headers, dict) else None,
    )


def register_content_error_handlers(app: FastAPI) -> None:
    """Wire the content plane's flat handler onto ``app``."""
    app.add_exception_handler(FilesError, content_error_handler)


def register_files_error_handlers(app: FastAPI) -> None:
    """Wire the handler onto ``app``. Called once, from ``create_app``."""
    app.add_exception_handler(FilesError, files_error_handler)


__all__ = [
    "NOT_FOUND_BODY",
    "body_for",
    "content_error_handler",
    "envelope_for",
    "files_error_for",
    "files_error_handler",
    "refusal_body",
    "register_content_error_handlers",
    "register_files_error_handlers",
    "response_for",
    "safe_detail",
]
