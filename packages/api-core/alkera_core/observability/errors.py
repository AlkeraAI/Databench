"""Canonical Alkera error taxonomy + base exception.

`AlkeraError` is the single base every domain error subclasses. It carries a
machine-readable `code` (the `ErrorCode` enum), an HTTP `status_code`, a
client-safe `message`, and optional structured `details`. The shared FastAPI
exception handlers (`alkera_core.observability.asgi`) turn any `AlkeraError` into
the canonical error envelope; domain code raises a subclass and never builds an
`HTTPException` by hand.

5xx errors never leak their `message` to the client — `client_message` swaps in a
generic string while the real `message` still goes to logs + Sentry.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

GENERIC_SERVER_MESSAGE = "An unexpected error occurred. Please try again."


class ErrorCode(StrEnum):
    """Stable, machine-readable error codes — the single source of truth.

    Values are dot/snake-cased and append-only: clients and the analytics
    pipeline may branch on them, so never repurpose an existing value.
    """

    # --- Generic transport-level ---
    internal_error = "internal_error"
    bad_request = "bad_request"
    validation_error = "validation_error"
    unstorable_text = "unstorable_text"
    not_found = "not_found"
    conflict = "conflict"
    conflict_reference = "conflict.reference"
    invalid_input = "invalid_input"
    forbidden = "forbidden"
    rate_limited = "rate_limited"
    unavailable = "unavailable"
    integration_not_configured = "integration_not_configured"
    email_send_failed = "email_send_failed"

    # --- Database contention (all 503, all retryable) ---
    db_lock_timeout = "db_lock_timeout"
    db_statement_timeout = "db_statement_timeout"
    db_pool_exhausted = "db_pool_exhausted"
    db_unavailable = "db_unavailable"

    # --- Auth ---
    auth_required = "auth_required"
    invalid_token = "invalid_token"  # noqa: S105 — error-code name, not a credential
    session_revoked = "session_revoked"
    refresh_token_reused = "refresh_token_reused"  # noqa: S105 — error-code name, not a credential
    csrf_origin_mismatch = "csrf_origin_mismatch"
    email_verification_required = "email_verification_required"
    personal_email_blocked = "personal_email_blocked"

    # --- Billing ---
    insufficient_credit = "insufficient_credit"

    # --- Upstream / model gateway ---
    upstream_error = "upstream_error"
    no_route = "no_route"
    at_capacity = "at_capacity"

    # --- Domain (backend services) ---
    user_conflict = "user_conflict"
    team_conflict = "team_conflict"
    invitation_error = "invitation_error"
    membership_error = "membership_error"
    oauth_error = "oauth_error"
    catalog_conflict = "catalog_conflict"
    catalog_invalid = "catalog_invalid"
    tier_conflict = "tier_conflict"

    # --- Project store ---
    lock_held = "lock_held"
    chat_not_found = "chat_not_found"


class AlkeraError(Exception):
    """Base for every Alkera domain error.

    Subclasses override the `ClassVar`s; instances may override the message and
    attach `details`. Defaults to an opaque 500 so a bare `AlkeraError()` is
    always safe to surface.
    """

    code: ClassVar[ErrorCode] = ErrorCode.internal_error
    status_code: ClassVar[int] = 500
    default_message: ClassVar[str] = GENERIC_SERVER_MESSAGE
    # Whether the *instance* message is safe to show the client. 4xx set this
    # True (their messages are authored to be user-facing). 5xx leave it False
    # so an instance message carrying upstream/internal detail never leaks — the
    # client gets the class's safe `default_message` instead.
    expose_message: ClassVar[bool] = False
    # Whether a 5xx is an expected, operator-facing condition rather than a
    # failure. A deliberate one keeps its own code and message and is logged as
    # a warning; everything else is reduced to `internal_error` and captured.
    deliberate: ClassVar[bool] = False

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.message = message if message is not None else self.default_message
        self.details: dict[str, Any] = dict(details) if details else {}
        super().__init__(self.message)

    @property
    def client_message(self) -> str:
        """Message safe to return to a client — the instance message for
        `expose_message` errors (4xx), else the class's safe `default_message`."""
        return self.message if self.expose_message else self.default_message


# --------------------------------------------------------------------------- #
# Generic building blocks — domain code subclasses these and overrides `code`.
# --------------------------------------------------------------------------- #


class BadRequestError(AlkeraError):
    code = ErrorCode.bad_request
    status_code = 400
    default_message = "The request was invalid."
    expose_message = True


class AuthRequiredError(AlkeraError):
    code = ErrorCode.auth_required
    status_code = 401
    default_message = "Authentication is required."
    expose_message = True


class ForbiddenError(AlkeraError):
    code = ErrorCode.forbidden
    status_code = 403
    default_message = "You do not have permission to perform this action."
    expose_message = True


class NotFoundError(AlkeraError):
    code = ErrorCode.not_found
    status_code = 404
    default_message = "The requested resource was not found."
    expose_message = True


class ConflictError(AlkeraError):
    code = ErrorCode.conflict
    status_code = 409
    default_message = "The request conflicts with the current state."
    expose_message = True


class ValidationFailedError(AlkeraError):
    code = ErrorCode.validation_error
    status_code = 422
    default_message = "The request failed validation."
    expose_message = True


class RateLimitedError(AlkeraError):
    code = ErrorCode.rate_limited
    status_code = 429
    default_message = "Too many requests. Please retry shortly."
    expose_message = True


# 5xx: expose_message stays False (inherited) — the client gets the safe
# default_message, never the instance message (which may carry upstream detail).
class UnavailableError(AlkeraError):
    code = ErrorCode.unavailable
    status_code = 503
    default_message = "The service is temporarily unavailable."


class IntegrationNotConfiguredError(AlkeraError):
    """A third-party integration this deployment never configured.

    Still a 503 — the capability genuinely is not available — but a DELIBERATE
    one, and the operator who has to act on it deserves to be told which
    integration rather than "an unexpected error occurred". `deliberate` is what
    keeps the shared handler from reducing it to `internal_error` and paging
    Sentry over a setting nobody set. The message names the integration and
    nothing else: which credential is missing is a fact about the deployment's
    configuration, so it goes to the log, not to the caller.
    """

    code = ErrorCode.integration_not_configured
    status_code = 503
    default_message = "This integration is not configured on this deployment."
    expose_message = True
    deliberate = True


class EmailSendFailedError(AlkeraError):
    """The mail relay refused a message the user is waiting on (a verification
    link). A deliberate 503 with its own message: the SPA must say the mail did
    NOT go out, rather than "an unexpected error" or, worse, "sent". The relay
    failure itself is already logged by the send path."""

    code = ErrorCode.email_send_failed
    status_code = 503
    default_message = "We couldn't send the email. Try again in a few minutes."
    expose_message = True
    deliberate = True


class UpstreamServiceError(AlkeraError):
    code = ErrorCode.upstream_error
    status_code = 502
    default_message = "An upstream service returned an error."
