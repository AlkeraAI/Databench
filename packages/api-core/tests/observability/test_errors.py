"""AlkeraError taxonomy: codes/statuses, client-safe messages, details, and
that 5xx never leak their internal message."""

from __future__ import annotations

from alkera_core.observability.errors import (
    GENERIC_SERVER_MESSAGE,
    AlkeraError,
    ConflictError,
    ErrorCode,
    NotFoundError,
    UnavailableError,
    UpstreamServiceError,
)


def test_base_defaults_to_opaque_500() -> None:
    err = AlkeraError()
    assert err.code is ErrorCode.internal_error
    assert err.status_code == 500
    assert err.client_message == GENERIC_SERVER_MESSAGE


def test_4xx_subclass_exposes_message() -> None:
    err = ConflictError("A user with that email already exists.")
    assert err.status_code == 409
    assert err.code is ErrorCode.conflict
    assert err.client_message == "A user with that email already exists."
    assert str(err) == "A user with that email already exists."


def test_5xx_subclass_hides_internal_message_but_keeps_a_safe_default() -> None:
    err = UpstreamServiceError("bedrock ThrottlingException: rate exceeded for arn:xyz")
    assert err.status_code == 502
    # Real message retained for logs...
    assert "bedrock" in err.message
    # ...but the client gets the class's SAFE default, never the instance detail.
    assert "bedrock" not in err.client_message
    assert "arn:xyz" not in err.client_message
    assert err.client_message == UpstreamServiceError.default_message


def test_503_exposes_its_safe_message_not_a_leak() -> None:
    err = UnavailableError("postgres unreachable at 10.0.0.5:5432")
    assert err.status_code == 503
    # The authored 503 message is safe + user-facing; the instance detail isn't leaked.
    assert err.client_message == "The service is temporarily unavailable."
    assert "10.0.0.5" not in err.client_message


def test_details_are_carried() -> None:
    err = NotFoundError("nope", details={"resource": "team", "id": "t-1"})
    assert err.details == {"resource": "team", "id": "t-1"}


def test_subclass_can_override_code() -> None:
    class UserConflictError(ConflictError):
        code = ErrorCode.user_conflict

    err = UserConflictError("dupe")
    assert err.status_code == 409
    assert err.code is ErrorCode.user_conflict


def test_default_message_used_when_none_given() -> None:
    assert NotFoundError().client_message == NotFoundError.default_message
