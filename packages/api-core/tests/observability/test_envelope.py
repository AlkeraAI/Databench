"""Error-envelope builders."""

from __future__ import annotations

from alkera_core.observability.envelope import build_error_body, error_body_for
from alkera_core.observability.errors import ConflictError, ErrorCode, UpstreamServiceError


def test_build_error_body_minimal() -> None:
    body = build_error_body(code=ErrorCode.not_found, message="gone", trace_id="t-1", status=404)
    assert body == {
        "error": {
            "type": "urn:alkera:error:not_found",
            "code": "not_found",
            "status": 404,
            "message": "gone",
            "trace_id": "t-1",
        }
    }


def test_each_code_has_its_own_problem_type() -> None:
    """The type names the kind of problem, so two codes never share one and
    one code keeps the same type whatever its status or message."""
    first = build_error_body(code="files.leased", message="a", trace_id="t", status=409)
    second = build_error_body(code="files.leased", message="b", trace_id="u", status=423)
    other = build_error_body(code="files.exists", message="a", trace_id="t", status=409)
    assert first["error"]["type"] == second["error"]["type"] == "urn:alkera:error:files.leased"
    assert other["error"]["type"] != first["error"]["type"]


def test_build_error_body_includes_details_when_present() -> None:
    body = build_error_body(
        code="validation_error", message="bad", trace_id="t-1", status=422, details={"errors": [1]}
    )
    assert body["error"]["details"] == {"errors": [1]}


def test_build_error_body_omits_empty_details() -> None:
    body = build_error_body(code="bad_request", message="x", trace_id="t", status=400, details={})
    assert "details" not in body["error"]


def test_error_body_for_4xx_uses_real_message() -> None:
    body = error_body_for(ConflictError("dupe email"), trace_id="t-9")
    assert body["error"] == {
        "type": "urn:alkera:error:conflict",
        "code": "conflict",
        "status": 409,
        "message": "dupe email",
        "trace_id": "t-9",
    }


def test_error_body_for_5xx_hides_internal_message() -> None:
    body = error_body_for(UpstreamServiceError("arn:secret detail"), trace_id="t-9")
    assert "arn:secret" not in body["error"]["message"]
    assert body["error"]["code"] == "upstream_error"
